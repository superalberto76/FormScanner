# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The submission review page: one submission, as an operator reviews it.

One read, :func:`get_review_detail`, and the rules it applies: which readings
are listed, why each was flagged, whether it is resolved, its current ruling
(outcome, substituted value, note, who and when, and whether it may be
confirmed or bulk-accepted, per ``review_rules``; whether its ruling sets the
candidate), and in what order — plus the first review save, the manual
candidate edit (:func:`edit_candidate`), and the scanned sheet the review
crops its focus panel from (:func:`get_page_image`).

The read is pure
----------------
It writes nothing, logs nothing, and leaves the soft lock alone — opening the
page and locking the submission are two calls, and the FE makes the second
itself. Allowed in EVERY status and with no ACTIVE gate on the project: a
closed submission is opened read-only, and which statuses the FE links here is
its decision, not this module's.

Flag reasons
------------
Each listed reading says WHY it needed a human, as stable codes the FE turns
into text. They are derived on read from what is persisted — the two axes
(``confidence_score``, ``validity_forces_review``), the ``result_type`` and the
batch's CURRENT effective threshold. Whether a reading needed a human is never
decided from the payload, which is each extractor's business; the payload is
read in one place only, to NAME the cause for RESPONDENT_IDENTIFIER (below):

  * :data:`LOW_CONFIDENCE` — ``confidence_score`` is known and strictly below
    the threshold (the rule of ``confidence_threshold_service``);
  * ``validity_forces_review``, which means a different thing per type, so it
    is named per type (:data:`_VALIDITY_REASONS`):

      - QUESTION / MATRIX → :data:`INVALID_ANSWER`: the combination read breaks
        the question's rule (NO_ANSWER, TOO_MANY_ANSWERS; for a MATRIX, in at
        least one row or column);
      - FREE_TEXT → :data:`ALWAYS_REVIEW`: set unconditionally, handwriting is
        never accepted unseen;
      - CODE → :data:`DECODE_FAILED`: the symbol did not decode;
      - RESPONDENT_IDENTIFIER → :data:`AMBIGUOUS_DIGITS`,
        :data:`MISSING_DIGITS` or :data:`UNREADABLE_GRID`. Its extractor sets
        the flag in exactly three cases, and the axis alone cannot tell them
        apart, so the payload picks the labels: ``ambiguous_columns`` not empty
        is an ambiguous digit; a "?" in ``raw_value`` is an empty column; the
        two may coincide, and then both are listed, in that order. Neither
        means the degenerate grid, whose payload the extractor writes with an
        EMPTY ``raw_value``, no digits and no ambiguous columns (a real grid
        always writes one character per column). Anything else carrying the
        flag — which no extractor writes — is labelled UNREADABLE_GRID too: the
        grid did not read as expected, and an unresolved reading must never
        come without a reason.

A reading taken on a page that could not be aligned to its template
(``sheet_not_aligned``) gets :data:`SHEET_NOT_ALIGNED`, FIRST, together with
whatever else the reading has: it forces review on its own, whatever the
confidence.

A PLACEHOLDER ("Review anyway" — a result with a NULL payload, which no
extractor ever writes) is checked FIRST and gets
:data:`EXTRACTION_FAILED` alone: it carries ``validity_forces_review`` like an
invalid reading, and the per-type mapping would otherwise call a QUESTION
nobody could read an "invalid answer".

With every type mapped, an UNRESOLVED reading always has at least one reason:
its ``review_required`` is either the extractor's or the recomputation's, both
the OR of the two axes under the current threshold, and each axis has a code.

"Resolved" is ``review_outcome IS NOT NULL``, read off the result itself —
never the existence of a ``submission_reviews`` row, which is an append-only
log that records UNDO too.

The list may be EMPTY for a RESOLVED reading. ``review_required`` is frozen
once a human has reviewed a result, while the threshold the reasons are derived
from is the batch's current one: lower it after the review and the low
confidence that sent the reading to review no longer shows. That is honest —
the reasons describe the reading under today's threshold — and the reading is
still listed, because it did need a human.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import PageObjectRole, PageObjectType, SubmissionResultType
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.repositories import (
    form_variant_page_repo,
    form_variant_repo,
    form_variant_role_binding_repo,
    submission_batch_repo,
    submission_object_result_repo,
    user_repo,
)
from app.schemas.submission import (
    SubmissionCandidateState,
    SubmissionReviewDetail,
    SubmissionReviewer,
    SubmissionReviewGeometry,
    SubmissionReviewObject,
    SubmissionReviewPage,
    SubmissionReviewTemplatePage,
)
from app.services import (
    audit_service,
    confidence_threshold_service,
    question_extraction_service,
    review_rules,
    storage_service,
    submission_batch_service,
    submission_lock_service,
    submission_score_service,
)
from app.utils import image_validation

# ---------------------------------------------------------------------------
# Flag reason codes — the contract with the FE (see the module docstring)
# ---------------------------------------------------------------------------

# The reading's confidence is below the batch's effective threshold.
LOW_CONFIDENCE = "LOW_CONFIDENCE"
# QUESTION / MATRIX: the selection read breaks the question's rule.
INVALID_ANSWER = "INVALID_ANSWER"
# FREE_TEXT: always reviewed by design, whatever was read.
ALWAYS_REVIEW = "ALWAYS_REVIEW"
# CODE: the QR / barcode did not decode.
DECODE_FAILED = "DECODE_FAILED"

# The page the reading was taken on could not be aligned to its template, so
# the reading may be shifted.
SHEET_NOT_ALIGNED = "SHEET_NOT_ALIGNED"

# A placeholder ("Review anyway"): no run could read the object, so
# its answer comes from the paper sheet.
EXTRACTION_FAILED = "EXTRACTION_FAILED"
# RESPONDENT_IDENTIFIER: a column carried more than one mark, and the digit
# written down is the one the system chose.
AMBIGUOUS_DIGITS = "AMBIGUOUS_DIGITS"
# RESPONDENT_IDENTIFIER: at least one column has no mark; the value has a "?".
MISSING_DIGITS = "MISSING_DIGITS"
# RESPONDENT_IDENTIFIER: the grid has no rows or no columns; nothing was read.
UNREADABLE_GRID = "UNREADABLE_GRID"

# What ``validity_forces_review`` means, per result type, where one code says
# it all. RESPONDENT_IDENTIFIER needs three, chosen by
# :func:`_respondent_identifier_reasons`.
_VALIDITY_REASONS: dict[SubmissionResultType, str] = {
    SubmissionResultType.QUESTION: INVALID_ANSWER,
    SubmissionResultType.MATRIX: INVALID_ANSWER,
    SubmissionResultType.FREE_TEXT: ALWAYS_REVIEW,
    SubmissionResultType.CODE: DECODE_FAILED,
}

def _respondent_identifier_reasons(result: SubmissionObjectResult) -> list[str]:
    # Reads the payload only to NAME the causes; that review is needed was
    # already decided by validity_forces_review. See the module docstring for
    # why "neither ambiguous nor incomplete" means the degenerate grid.
    payload = result.detected_payload_json or {}
    reasons: list[str] = []
    if payload.get("ambiguous_columns"):
        reasons.append(AMBIGUOUS_DIGITS)
    if review_rules.UNREAD_DIGIT in (payload.get("raw_value") or ""):
        reasons.append(MISSING_DIGITS)
    return reasons or [UNREADABLE_GRID]


def flag_reasons(
    result: SubmissionObjectResult, threshold: float
) -> list[str]:
    """Why *result* needs (or needed) a human, under *threshold*.

    Derived from the persisted axes (the payload only names a
    RESPONDENT_IDENTIFIER's cause); see the module docstring for the codes and
    for when the list may be empty.
    """
    if result.detected_payload_json is None:
        # A placeholder: nothing was read, so nothing else can be said about it.
        return [EXTRACTION_FAILED]
    reasons: list[str] = []
    if result.sheet_not_aligned:
        reasons.append(SHEET_NOT_ALIGNED)
    if result.confidence_score is not None and result.confidence_score < threshold:
        reasons.append(LOW_CONFIDENCE)
    if result.validity_forces_review:
        if result.result_type is SubmissionResultType.RESPONDENT_IDENTIFIER:
            reasons.extend(_respondent_identifier_reasons(result))
        else:
            reasons.append(_VALIDITY_REASONS[result.result_type])
    return reasons


def _reading_order(result: SubmissionObjectResult) -> tuple[float, float, str]:
    # Top to bottom, then left to right, as a person reads the sheet; the id
    # makes two objects authored at the same spot come back in a stable order.
    obj = result.page_object
    return (obj.y, obj.x, str(obj.id))


def _reviewer(
    result: SubmissionObjectResult, reviewers: dict[uuid.UUID, User]
) -> SubmissionReviewer | None:
    user = reviewers.get(result.reviewed_by) if result.reviewed_by else None
    if user is None:
        return None
    return SubmissionReviewer(
        user_id=user.id, display_name=submission_lock_service.display_name(user)
    )


def _review_object(
    result: SubmissionObjectResult,
    threshold: float,
    reviewers: dict[uuid.UUID, User],
    first_identifier_reading_id: uuid.UUID | None,
) -> SubmissionReviewObject:
    obj = result.page_object
    return SubmissionReviewObject(
        result_id=result.id,
        page_object_id=obj.id,
        object_type=obj.object_type,
        result_type=result.result_type,
        # Linked copies included: the FE reads it to tell an identity.
        role=obj.effective_role,
        label=obj.label,
        question_text=(
            obj.question_config.question_text
            if obj.object_type is PageObjectType.QUESTION
            and obj.question_config is not None
            else None
        ),
        geometry=SubmissionReviewGeometry(
            x=obj.x, y=obj.y, width=obj.width, height=obj.height
        ),
        detected_payload=result.detected_payload_json,
        confidence_score=result.confidence_score,
        flag_reasons=flag_reasons(result, threshold),
        resolved=result.review_outcome is not None,
        review_outcome=result.review_outcome,
        reviewed_payload=result.reviewed_payload_json,
        review_note=result.review_note,
        reviewed_by=_reviewer(result, reviewers),
        reviewed_at=(
            result.reviewed_at.astimezone(timezone.utc)
            if result.reviewed_at is not None
            else None
        ),
        confirmable=review_rules.is_confirmable(result, obj),
        bulk_acceptable=review_rules.is_bulk_acceptable(result, obj),
        sets_candidate=result.id == first_identifier_reading_id,
    )


def _review_page(
    page: SubmissionPage, objects: list[SubmissionReviewObject]
) -> SubmissionReviewPage:
    template = page.form_variant_page
    return SubmissionReviewPage(
        id=page.id,
        source_page_order=page.source_page_order,
        page_order_source=page.page_order_source,
        alignment_status=page.alignment_status,
        template_page=(
            SubmissionReviewTemplatePage(
                id=template.id,
                page_index=template.page_index,
                width_px=template.width_px,
                height_px=template.height_px,
            )
            if template is not None
            else None
        ),
        objects=objects,
    )


def get_review_detail(
    db: Session, submission_id: uuid.UUID, user: User
) -> SubmissionReviewDetail:
    """One submission for its review page: header, every sheet, and the
    readings that needed a human, each with why.

    404 for an unknown submission, 403 for a non-member — the usual submission
    authorisation. Any status, no ACTIVE gate, no write (module docstring).

    A CONSTANT number of statements however many sheets and results the
    submission holds: authorisation (submission, batch, project, membership),
    the variant, the sheets with their template page, the flagged results with
    their objects (the current ruling is on each result row), the readings that
    may hold the identifier with the select-in of their group bindings (for the
    first identity reading), the users who made those rulings (only when there
    is at least one), the lock holder (only when actively locked), and the
    variant's template pages and role — each one query. Missing pages are
    computed from the sheets already loaded, not from the batch-wide queue
    query. The score adds the variant's scorable objects with their eager loads
    and the submission's QUESTION and MATRIX results, only for a scored
    submission (SCANNED, REVIEWED or FINALIZED, with a variant:
    ``submission_score_service``).
    """
    submission, _project = submission_batch_service.load_submission_authorized(
        db, submission_id, user
    )
    batch = submission.submission_batch
    threshold = confidence_threshold_service.effective_threshold(batch)

    variant_id = submission.matched_form_variant_id
    variant = form_variant_repo.get(db, variant_id) if variant_id else None
    variant_ids = {variant_id} if variant_id else set()
    expected_pages = form_variant_page_repo.page_indices_for_variants(db, variant_ids)
    expects_candidate = bool(
        form_variant_role_binding_repo.variants_with_role(
            db, variant_ids, PageObjectRole.RESPONDENT_IDENTIFIER
        )
    )

    pages = submission_batch_repo.list_pages_with_template(db, submission.id)
    present_pages = {
        submission.id: {
            p.form_variant_page.page_index
            for p in pages
            if p.form_variant_page is not None
        }
    }

    results = submission_object_result_repo.list_review_required_for_submission(
        db, submission.id
    )
    reviewers = user_repo.list_by_ids(
        db, {r.reviewed_by for r in results if r.reviewed_by is not None}
    )
    # Among ALL the submission's readings, not only the listed ones: the first
    # may have needed no review.
    first_identifier = review_rules.first_identifier_reading(
        submission_object_result_repo.list_identifier_readings_for_submission(
            db, submission.id
        )
    )
    first_identifier_id = first_identifier.id if first_identifier else None
    objects_by_page: dict[uuid.UUID, list[SubmissionReviewObject]] = {}
    for result in sorted(results, key=_reading_order):
        objects_by_page.setdefault(result.submission_page_id, []).append(
            _review_object(result, threshold, reviewers, first_identifier_id)
        )
    objects = [o for page_objects in objects_by_page.values() for o in page_objects]

    lock = submission_lock_service.active_locks(db, [submission]).get(submission.id)

    return SubmissionReviewDetail(
        id=submission.id,
        submission_batch_id=batch.id,
        batch_name=batch.name,
        form_project_id=batch.form_project_id,
        status=submission.status,
        created_at=submission.created_at,
        matched_form_variant_id=variant_id,
        variant_name=variant.name if variant is not None else None,
        match_failure_reason=submission.match_failure_reason,
        match_failure_code_value=submission.match_failure_code_value,
        respondent_identifier=submission.respondent_identifier,
        respondent_identifier_expected=expects_candidate,
        candidate_reading_page_id=(
            first_identifier.submission_page_id if first_identifier else None
        ),
        missing_page_indices=submission_batch_service._missing_page_indices(
            submission, expected_pages, present_pages
        ),
        lock=lock,
        review_version=submission.review_version,
        effective_threshold=threshold,
        review_count=len(results),
        pending_review_count=sum(1 for o in objects if not o.resolved),
        bulk_acceptable_count=sum(
            1 for o in objects if not o.resolved and o.bulk_acceptable
        ),
        score=submission_score_service.compute(db, submission),
        pages=[_review_page(p, objects_by_page.get(p.id, [])) for p in pages],
    )


# ---------------------------------------------------------------------------
# The scanned sheet itself
# ---------------------------------------------------------------------------

# Served for bytes that do not sniff as a supported image: the browser is told
# nothing it cannot verify, and the FE treats the sheet as uncroppable.
_UNKNOWN_IMAGE_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class PageImage:
    """What :func:`get_page_image` hands the route.

    ``etag`` always; ``content`` and ``mime`` None when the client's copy,
    named by its ``If-None-Match``, is still the image served (304)."""

    etag: str
    content: bytes | None = None
    mime: str | None = None


def _page_image_etag(path: str, size: int, mtime_ns: int) -> str:
    """Weak validator of the image served for a sheet: from the path served,
    the size and the modification time, so it changes when the page goes from
    its original to its straightened image (first run) and when a straightened
    image is rewritten (realignment). Weak: equal bytes are not
    promised, only "the same stored image"."""
    digest = hashlib.sha256(f"{path}|{size}|{mtime_ns}".encode()).hexdigest()
    return f'W/"{digest[:32]}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """``If-None-Match`` against *etag* with the weak comparison RFC 9110
    prescribes for it: ``*`` or any listed tag equal once ``W/`` is dropped."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    bare = etag.removeprefix("W/")
    return any(
        tag.strip().removeprefix("W/") == bare for tag in if_none_match.split(",")
    )


def get_page_image(
    db: Session,
    page_id: uuid.UUID,
    user: User,
    *,
    if_none_match: str | None = None,
) -> PageImage:
    """The whole image of one physical sheet the extraction read on, with its
    real MIME type and a validator.

    The image is ``question_extraction_service.scan_image_path``'s — the SAME
    rule the extractors open a sheet with: the straightened image for a page
    ALIGNED (a page upside down included, already turned right way up), the
    original scan for a page NOT_ALIGNED or never aligned. The review focus
    panel crops it CLIENT-SIDE around the selected object, from the template
    page size and the raw geometry :func:`get_review_detail` already returns,
    in proportion: the straightened image has the template's proportions, so
    the crop falls on exactly the pixels that were read.

    Authorised through the sheet's submission (``load_submission_authorized``:
    404 unknown, 403 non-member). Like the review read: any status, no ACTIVE
    gate, no write, no audit, no lock interaction.

    404 "Scan image not found." when the file is missing, unreadable or its
    path fails storage validation — a "Review anyway" placeholder may point at
    exactly such a file, and the FE shows the reading without its crop. A page
    ALIGNED whose straightened image is gone is 404 too, NOT its original:
    that crop would not coincide with the reading; the next run realigns
    it. The MIME type is sniffed from the CONTENT, never the extension, which
    on a hand-edited row could say anything.

    The file served for one URL can change, so it carries a weak ETag
    (:func:`_page_image_etag`); with an ``If-None-Match`` naming it the file
    is not read and ``content`` is None.
    """
    page = submission_batch_repo.get_page(db, page_id)
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Submission page not found",
        )
    submission_batch_service.load_submission_authorized(
        db, page.submission_id, user
    )
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Scan image not found."
    )
    path = question_extraction_service.scan_image_path(page)
    if path is None:
        raise not_found
    try:
        resolved = storage_service.assert_within_submission_pages_dir(path)
        stat = resolved.stat()
        etag = _page_image_etag(str(resolved), stat.st_size, stat.st_mtime_ns)
        if _etag_matches(if_none_match, etag):
            return PageImage(etag=etag)
        content = resolved.read_bytes()
    except (ValueError, OSError) as exc:
        raise not_found from exc
    try:
        mime = image_validation.sniff_image(content).mime_type
    except image_validation.UnsupportedImageError:
        mime = _UNKNOWN_IMAGE_TYPE
    return PageImage(etag=etag, content=content, mime=mime)


# ---------------------------------------------------------------------------
# Review saves
# ---------------------------------------------------------------------------


def edit_candidate(
    db: Session,
    submission_id: uuid.UUID,
    user: User,
    respondent_identifier: str | None,
    declared_version: int,
) -> SubmissionCandidateState:
    """Set or correct a submission's candidate identifier by hand.

    A review save like any other, so it goes through
    ``submission_lock_service.guard_review_save`` FIRST: SCANNED only, not
    while a colleague holds the lock, not from a stale screen — the soft
    lock's structured 409s, unchanged. Admitted, the caller holds (or renews)
    the lock and the version is bumped. An edit to the same value is still a
    save and bumps the version too: simpler than a special case, and
    consistent with every other save the guard admits.

    *respondent_identifier* arrives normalised by the schema (trimmed, blank →
    None, at most 500 characters). Audited as ``submission.candidate_edited``
    with the old and the new value: the candidate attribution decides whose
    answers these are. Owns the transaction.
    """
    submission = submission_lock_service.guard_review_save(
        db, submission_id, user, declared_version
    )
    old_value = submission.respondent_identifier
    submission.respondent_identifier = respondent_identifier
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_CANDIDATE_EDITED,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={"old_value": old_value, "new_value": respondent_identifier},
    )
    db.commit()
    return SubmissionCandidateState(
        respondent_identifier=submission.respondent_identifier,
        review_version=submission.review_version,
        # The guard just made the lock the caller's.
        lock=submission_lock_service.lock_info(submission, user),
    )
