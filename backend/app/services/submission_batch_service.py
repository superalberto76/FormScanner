# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan-batch ingestion.

Two input shapes, one outcome — ``Submission`` rows made of ``SubmissionPage``
rows:

  * ``upload_pdf``    — one uploaded PDF = one submission, whatever its page
    count. Grouping and order are given: the file IS the submission and the
    physical page order IS the order.
  * ``upload_images`` — loose images, one sheet per file. Neither grouping nor
    order is given, so both are resolved per image from its codes and its
    filename (naming convention in ``app.utils.image_filename``). One call can
    open several submissions and extend submissions opened by earlier calls.

This service only establishes WHERE each physical page sits inside its
submission and HOW that position was determined:

  * ``source_page_order``  — the physical 0-based position in the PDF, ALWAYS set;
  * ``page_order_source``  — CODE when a page index was read off the page's
    QR/barcode, PDF_ORDER when the physical order had to be used instead;
  * ``identified_code_value`` — the RAW decoded payload, kept verbatim so the
    matching engine can re-parse it.

Which decoded code means what is decided by ``submission_identification_service``
reading the explicit role tag in each payload (``VID:`` / ``RID:``), NOT by
this service. A heuristic ("whichever code carries a page index must be the
variant identifier") could not tell a VARIANT_IDENTIFIER from a
RESPONDENT_IDENTIFIER without already knowing the FormVariant — precisely what
ingestion does not know.

Which FormVariant a submission belongs to is decided by the matching engine in
``submission_matching_service``, not here — but it is decided NOW, inside the
same transaction: both paths call it once the pages exist and before their
commit, so ingesting a submission and matching it is one atomic operation
and a fresh upload is never briefly visible in an unmatched state. Everything about
that decision (the two levels, the confidences, MATCH_FAILED) lives in that
module; this one only says WHEN it runs. The one identification this service
resolves on its own is ``Submission.respondent_identifier``, which needs no
variant: a tagged RID names the candidate by itself.

Robustness contract: a code that will not decode is a NORMAL outcome, not an
error. One unreadable page never blocks its siblings nor the upload — that page
simply falls back to PDF_ORDER.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import (
    CLOSED_SUBMISSION_STATUSES,
    FormVariantStatus,
    PageObjectRole,
    SubmissionStatus,
)
from app.models.form_project import FormProject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.repositories import (
    form_variant_page_repo,
    form_variant_repo,
    form_variant_role_binding_repo,
    submission_batch_repo,
    submission_object_result_repo,
)
from app.schemas.submission import (
    ExportReadiness,
    ExtractionRunSummary,
    RejectedImage,
    SubmissionBatchCreate,
    SubmissionBatchDetail,
    SubmissionBatchIngestResult,
    SubmissionBatchListItem,
    SubmissionBatchMatchingResult,
    SubmissionBatchRead,
    SubmissionSummary,
)
from app.services import (
    audit_service,
    batch_operation_lock,
    confidence_threshold_service,
    extraction_orchestrator_service,
    form_project_service,
    review_anyway_service,
    storage_service,
    submission_candidate_service,
    submission_identification_service,
    submission_lock_service,
    submission_matching_service,
)
from app.utils import code_rendering, image_validation


# ---------------------------------------------------------------------------
# Create a batch
# ---------------------------------------------------------------------------


def to_batch_read(batch: SubmissionBatch) -> SubmissionBatchRead:
    """Serialise a batch, resolving its effective threshold.

    Not ``SubmissionBatchRead.model_validate(batch)``: ``effective_threshold``
    is not a column but the result of applying the system default when the
    batch has no override of its own, so it has to be computed here. Kept in
    one place so every route reporting a batch reports the same pair.
    """
    return SubmissionBatchRead(
        id=batch.id,
        form_project_id=batch.form_project_id,
        created_by_user_id=batch.created_by_user_id,
        name=batch.name,
        description=batch.description,
        created_at=batch.created_at,
        confidence_threshold=batch.confidence_threshold,
        effective_threshold=confidence_threshold_service.effective_threshold(batch),
    )


def create_batch(
    db: Session,
    project_id: uuid.UUID,
    data: SubmissionBatchCreate,
    user: User,
) -> SubmissionBatch:
    """Open a new scan batch on *project_id* (any member; ACTIVE projects ready
    for scanning only).

    An explicit ``confidence_threshold`` is stored as the batch's override;
    omitting it leaves NULL, which means "follow the system default" rather than
    "no threshold". Nothing is recomputed either way — a batch created now has
    no results yet.
    """
    project = form_project_service.require_project_member(db, project_id, user)
    form_project_service.assert_project_active_for_scanning(project)
    _assert_scan_ready(db, project)

    batch = submission_batch_repo.add(
        db,
        SubmissionBatch(
            form_project_id=project.id,
            name=data.name,
            description=data.description,
            created_by_user_id=user.id,
            confidence_threshold=data.confidence_threshold,
        ),
    )
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_BATCH_CREATED,
        entity_type="SubmissionBatch",
        entity_id=batch.id,
        metadata={
            "project_id": str(project_id),
            "name": data.name,
            "confidence_threshold": data.confidence_threshold,
        },
    )
    db.commit()
    db.refresh(batch)
    return batch


# ---------------------------------------------------------------------------
# Move a batch's confidence threshold
# ---------------------------------------------------------------------------


def update_confidence_threshold(
    db: Session,
    batch_id: uuid.UUID,
    new_threshold: float | None,
    user: User,
) -> SubmissionBatch:
    """Set (or clear) the batch's threshold and re-derive what it implies.

    *new_threshold* is written verbatim, ``None`` included — that CLEARS the
    override and puts the batch back on ``settings.default_confidence_threshold``,
    which is a real decision and not an omission.

    The recomputation then runs in the SAME transaction, so the threshold and
    the ``review_required`` flags it decides can never be observed disagreeing.
    It spares every result a human has already reviewed: see
    ``confidence_threshold_service`` for why that exclusion is the whole point
    of the feature rather than an optimisation. In the same transaction, a
    respondent ID the recomputation lets out of review may set the candidate
    of its open submission (``submission_candidate_service``); no candidate is
    ever cleared.

    Authorisation is ``require_project_member``, like every other route here,
    and the ACTIVE project gate is deliberately NOT applied — for the same
    reason ``run_batch_matching`` skips it. That gate stops new sheets being
    COLLECTED outside the collection phase; moving a threshold collects nothing,
    it re-reads what is already stored. A concluded batch whose results deserve
    a second look is precisely the case this must keep serving.

    Refused with the 409 ``EXTRACTION_RUNNING`` while an extraction run holds
    the batch (``batch_operation_lock``): the run computes the results it
    is reading with the threshold it loaded at its start, so a change landing
    meanwhile would leave those results on the old one.

    Owns the transaction: commits and returns the refreshed batch.
    """
    batch, _project = _load_batch_authorized(db, batch_id, user)
    batch_operation_lock.acquire_shared(db, batch.id)

    old_threshold = batch.confidence_threshold
    batch.confidence_threshold = new_threshold
    db.flush()

    recomputed = confidence_threshold_service.recompute_review_required_for_batch(
        db, batch.id
    )
    # A respondent ID the new threshold lets out of review sets the candidate
    # of its open submission, as if extraction had read it that way. None is
    # ever cleared: one the threshold sends to review keeps the candidate it
    # gave. See submission_candidate_service.
    db.flush()
    candidates_set = submission_candidate_service.apply_candidates_for_batch(
        db, batch.id
    )

    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_BATCH_CONFIDENCE_THRESHOLD_UPDATED,
        entity_type="SubmissionBatch",
        entity_id=batch.id,
        metadata={
            "old_threshold": old_threshold,
            "new_threshold": new_threshold,
            # Results whose review_required actually CHANGED side, not the
            # number examined — see recompute_review_required_for_batch.
            "results_recomputed": recomputed,
            # Candidates set from a respondent-ID reading; never in the API.
            "candidates_set": candidates_set,
        },
    )
    db.commit()
    db.refresh(batch)
    return batch


# ---------------------------------------------------------------------------
# Resolve a batch (shared by upload + detail)
# ---------------------------------------------------------------------------


def _load_batch_authorized(
    db: Session, batch_id: uuid.UUID, user: User
) -> tuple[SubmissionBatch, FormProject]:
    """Fetch a batch the caller may access plus its project, or raise 404/403.

    404 for an unknown batch; the project membership check then supplies 404/403
    for the owning project exactly like every other entry point. The
    project comes back with it so callers needing a status gate do not re-query.
    """
    batch = submission_batch_repo.get(db, batch_id)
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Submission batch not found"
        )
    project = form_project_service.require_project_member(
        db, batch.form_project_id, user
    )
    return batch, project


def _assert_scan_ready(db: Session, project: FormProject) -> None:
    """Scan-readiness gate: refuse ingestion while the
    project's published variants cannot be told apart — the same rule as the
    FE's banner (``compute_scan_readiness``), as a structured 409
    ``NOT_SCAN_READY`` whose message is the banner's reason.

    Ingestion only: matching, extraction, manual assignment and review of the
    submissions already uploaded stay allowed.
    Runs after the ACTIVE gate, so a closed project keeps that message.

    The two ingestion suites (test_submission_ingestion_integration.py and
    test_submission_image_ingestion_integration.py) switch this function off by
    name: a further readiness check elsewhere would not be switched off there.
    """
    readiness = form_project_service.compute_scan_readiness(db, project.id)
    if readiness.ready:
        return
    extras: dict[str, object] = {
        "published_variant_count": readiness.published_variant_count
    }
    if readiness.colliding_variant_ids:
        extras["colliding_variant_ids"] = [
            str(vid) for vid in readiness.colliding_variant_ids
        ]
    raise submission_lock_service.structured_conflict(
        submission_lock_service.NOT_SCAN_READY, readiness.reason, **extras
    )


def _load_submission_authorized(
    db: Session, submission_id: uuid.UUID, user: User, *, for_update: bool = False
) -> tuple[Submission, FormProject]:
    """Fetch a submission the caller may access + its project, or raise 404/403.

    The single-Submission counterpart of :func:`_load_batch_authorized`, and
    deliberately the same shape: 404 for an unknown submission, then the project
    membership check on its batch's project supplies 404/403. Authorisation is
    ALWAYS reached through the owning project — a Submission has no access rules
    of its own — so the project comes back with it for the caller to reuse.

    *for_update* reads the row ``SELECT … FOR UPDATE``, for callers
    whose decision depends on values a concurrent request could be changing.
    The membership check then runs with the row already locked; a refusal
    simply rolls the transaction back, releasing it.
    """
    if for_update:
        submission = submission_batch_repo.get_submission_for_update(
            db, submission_id
        )
    else:
        submission = submission_batch_repo.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found"
        )
    project = form_project_service.require_project_member(
        db, submission.submission_batch.form_project_id, user
    )
    return submission, project


def load_submission_authorized(
    db: Session, submission_id: uuid.UUID, user: User, *, for_update: bool = False
) -> tuple[Submission, FormProject]:
    """Public entry to :func:`_load_submission_authorized` for the other
    services of this domain (the soft lock first), so the one authorisation
    rule for a submission is shared rather than restated."""
    return _load_submission_authorized(
        db, submission_id, user, for_update=for_update
    )


# ---------------------------------------------------------------------------
# Ingest one PDF = one submission
# ---------------------------------------------------------------------------


def _ingest_page(
    db: Session,
    submission: Submission,
    png: bytes,
    source_page_order: int,
    source_filename: str,
) -> tuple[SubmissionPage, list[str]]:
    """Persist one rasterised page and return it with everything decoded off it.

    The row id is generated up front because the page image is named after it
    (server-generated path, never the client filename). Decoding is best-effort:
    ``decode_codes_from_image`` never raises and returns EVERY readable symbol —
    a page routinely carries both a variant and a respondent identifier. What
    each one means is decided later, in one place, by the role tag; here the row
    is created with the neutral defaults (no identified value, PDF_ORDER).
    """
    page_id = uuid.uuid4()
    image_path = storage_service.save_submission_page_image(png, page_id)
    raw_codes = code_rendering.decode_codes_from_image(image_path)

    page = SubmissionPage(
        id=page_id,
        submission_id=submission.id,
        form_variant_page_id=None,  # resolved by matching
        source_file_path=str(image_path),
        source_filename=source_filename,
        source_page_order=source_page_order,
    )
    db.add(page)
    return page, raw_codes


def upload_pdf(
    db: Session,
    batch_id: uuid.UUID,
    filename: str,
    file_bytes: bytes,
    user: User,
) -> Submission:
    """Ingest one multipage PDF into *batch_id* as a single submission.

    Stores the original, rasterises every page, and creates one SubmissionPage
    per physical page in PDF order. Returns the Submission with its pages loaded.
    """
    batch, project = _load_batch_authorized(db, batch_id, user)
    # Re-check: the project may have left ACTIVE, or stopped being ready for
    # scanning, since the batch was created.
    form_project_service.assert_project_active_for_scanning(project)
    _assert_scan_ready(db, project)

    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty.",
        )
    if len(file_bytes) > settings.max_submission_pdf_bytes:
        limit_mb = settings.max_submission_pdf_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"PDF is too large (max {limit_mb:.0f} MB).",
        )

    submission = submission_batch_repo.add_submission(
        db,
        Submission(
            submission_batch_id=batch.id,
            matched_form_variant_id=None,  # resolved by matching
            status=SubmissionStatus.UPLOADED,
        ),
    )

    original_path = storage_service.save_submission_original(
        file_bytes, submission.id, Path(filename or "upload.pdf").suffix or ".pdf"
    )

    # The client filename is recorded on every page of this submission (they all
    # came from the same upload) but never used to build a path.
    source_filename = Path(filename or "upload.pdf").name

    # Files are written before the transaction commits, so an abort must unlink
    # what it wrote — discarding the rows alone would leave orphans on disk.
    written: list[Path] = [original_path]

    def _abort(detail: str) -> HTTPException:
        # No db.rollback(): nothing has been committed, so the pending Submission
        # / SubmissionPage rows are discarded when the request's session closes.
        # Rolling back here would also discard the CALLER's pending work.
        for path in written:
            path.unlink(missing_ok=True)
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
        )

    decoded: list[tuple[SubmissionPage, list[str]]] = []
    try:
        for index, png in enumerate(
            storage_service.rasterize_submission_pdf(original_path)
        ):
            page, raw_codes = _ingest_page(db, submission, png, index, source_filename)
            written.append(Path(page.source_file_path))
            decoded.append((page, raw_codes))
    except RuntimeError as exc:  # fitz.FileDataError & friends — unreadable input
        raise _abort("File could not be read as a PDF.") from exc

    page_count = len(decoded)
    if page_count == 0:
        raise _abort("PDF contains no pages.")

    # One pass over the whole submission, in physical PDF order: each page gets
    # its identified_code_value / page_order_source from its own VID tag, and the
    # first RID found on ANY page names the candidate.
    identifications = submission_identification_service.apply_submission_codes(
        submission, decoded
    )

    db.flush()
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_CREATED,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={
            "batch_id": str(batch_id),
            "source_filename": source_filename,
            "page_count": page_count,
            "respondent_identified": submission.respondent_identifier is not None,
            # Anomaly, not an error: several codes with the SAME role tag on one
            # page. Ingestion resolved it deterministically (first wins) but the
            # sheet is worth a look, so the audit trail records where.
            "pages_with_conflicting_codes": [
                page.source_page_order
                for page, _ in decoded
                if identifications[page.id].has_conflicting_codes
            ],
        },
    )

    # Matching runs INSIDE this transaction, on the submission just created: the
    # pages are flushed, so the engine reads them back and resolves the variant
    # and the page bindings before anything is committed. A submission is
    # therefore never visible in the "ingested but unmatched" intermediate state.
    # It logs its own audit event and, like everything above, does not commit.
    submission_matching_service.match_submission(
        db, submission, actor_user_id=user.id
    )

    db.commit()
    db.refresh(submission)
    return submission


# ---------------------------------------------------------------------------
# Ingest loose images = N submissions
#
# The other half of ingestion. A PDF answers "same submission?" and "which
# position?" by itself; a pile of images answers neither, so each image is
# grouped and positioned from two independent signals — its codes and its
# filename — by ``submission_identification_service.resolve_image_page``. This
# service does the parts that need the session and the filesystem: validating the
# bytes, resolving/creating the Submission behind a grouping key (including one
# opened by an EARLIER call), and picking a free slot when nothing declared a
# position.
#
# Robustness contract, one step stronger than the PDF path's: there, one
# unreadable page falls back to PDF_ORDER; here, one unusable FILE is dropped
# from the call and reported, while every other image in the same request is
# ingested normally. A batch of 200 scans must never be lost to one bad sheet.
# ---------------------------------------------------------------------------


# What a scanner produces and what the decode path expects. Narrower than the
# IMAGE PageObject whitelist, which also takes BMP and SVG: those are
# authoring assets, and a vector "scan" is a contradiction.
_SCANNED_IMAGE_MIME_TYPES = frozenset({image_validation.PNG, image_validation.JPEG})

# Column widths of SubmissionPage.source_filename / Submission.source_group_key.
# Both are enforced HERE rather than left to the DB, because a value that
# overflows would raise at flush and take the WHOLE upload call down with it —
# precisely the "one bad file must never block the batch" contract this service
# exists to keep. A filename is display-only, so it is truncated; a grouping key
# is not, so an overlong one rejects its file instead (see _resolve_group_key).
_MAX_SOURCE_FILENAME_LEN = 500
_MAX_GROUP_KEY_LEN = 500


def _display_filename(filename: str | None) -> str:
    """The client filename as recorded on the page: no path, length-capped.

    Never used to build an on-disk path — a page image is stored under its
    SubmissionPage id — so stripping directories is belt-and-braces rather than
    the security boundary. Mirrors image_object_service._display_filename.
    """
    name = Path(filename or "").name.strip()
    return name[:_MAX_SOURCE_FILENAME_LEN] or "upload"


def _validate_scanned_image(data: bytes) -> str:
    """Check one uploaded image and return the extension for its REAL format.

    Content-sniffed with Pillow (``image_validation.sniff_image``, the IMAGE
    PageObject path), never trusting the client's filename or Content-Type. Raises
    ``HTTPException`` with the same codes the single-image upload uses — 413 too
    large, 422 empty, 415 not a supported image — which the caller turns into a
    per-file rejection instead of a failed request.
    """
    if len(data) > settings.max_submission_image_bytes:
        limit_mb = settings.max_submission_image_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image is too large (max {limit_mb:.0f} MB).",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty.",
        )
    try:
        sniffed = image_validation.sniff_image(data)
    except image_validation.UnsupportedImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    if sniffed.mime_type not in _SCANNED_IMAGE_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported image type '{sniffed.mime_type}' for a scanned "
                "sheet. Allowed: PNG, JPEG."
            ),
        )
    return sniffed.ext


class _GroupResolver:
    """Maps grouping keys to submissions for the duration of one upload call.

    Holds the three pieces of state the per-image loop would otherwise thread by
    hand: which Submission each key resolved to, which ``source_page_order``
    values are already spoken for inside each of them, and which submissions
    this call created versus merely extended.

    Existing submissions are looked up ONCE per key and their taken page orders
    are seeded from the DB, so a second upload call appends after the first one's
    pages instead of restarting at 0 on top of them.
    """

    def __init__(self, db: Session, batch: SubmissionBatch) -> None:
        self._db = db
        self._batch = batch
        self._by_key: dict[str, Submission] = {}
        self._taken: dict[uuid.UUID, set[int]] = {}
        self.created: list[Submission] = []
        self.updated: list[Submission] = []

    def resolve(self, group_key: str | None) -> Submission:
        """The submission for *group_key*, creating or adopting one as needed.

        A None key is UNGROUPABLE, and gets a fresh single-page UNASSIGNED
        Submission every time — those must never merge with each other, which is
        exactly why the unique index on ``source_group_key`` is partial.
        """
        if group_key is None:
            return self._create(None, SubmissionStatus.UNASSIGNED)
        if group_key in self._by_key:
            return self._by_key[group_key]

        existing = submission_batch_repo.get_submission_by_group_key(
            self._db, self._batch.id, group_key
        )
        if existing is not None:
            self._by_key[group_key] = existing
            self._taken[existing.id] = submission_batch_repo.taken_page_orders(
                self._db, existing.id
            )
            self.updated.append(existing)
            return existing

        submission = self._create(group_key, SubmissionStatus.UPLOADED)
        self._by_key[group_key] = submission
        return submission

    def _create(
        self, group_key: str | None, submission_status: SubmissionStatus
    ) -> Submission:
        submission = submission_batch_repo.add_submission(
            self._db,
            Submission(
                submission_batch_id=self._batch.id,
                matched_form_variant_id=None,  # resolved by matching
                status=submission_status,
                source_group_key=group_key,
            ),
        )
        self._taken[submission.id] = set()
        self.created.append(submission)
        return submission

    def take_page_order(
        self, submission: Submission, declared_page_index: int | None
    ) -> tuple[int, bool]:
        """The ``source_page_order`` for this image, and whether it DUPLICATES one.

        A DECLARED position (from a VID page index or a filename's ``<n>``) is
        honoured verbatim, even when another sheet in the submission already
        claims it. Two sheets can collide legitimately-by-accident — an operator
        naming two files ``smith_page_1.jpg``, or a printing run that stamped
        the same page index on two masters — and NONE of the three alternatives
        is better than keeping both:

          * moving the second sheet would destroy the very signal read off it and
            invent a position nobody declared;
          * overwriting the first would silently discard a scanned page;
          * rejecting the second would lose a sheet the operator did upload, and
            breaks the "one bad file never costs you a page" contract.

        So both pages are created with the SAME source_page_order. The duplicate
        is real and is reported (second element of the return, surfaced in the
        audit trail as ``pages_with_duplicate_position``) rather than papered
        over — see the warning on ``SubmissionPage.source_page_order`` about
        what matching may and may not assume.

        With nothing declared, the fallback is the order of arrival within the
        group, implemented as the lowest free slot: for the ordinary case (no
        image in the group declares anything) that is plainly 0, 1, 2…, it
        appends cleanly after pages left by an earlier upload call, and it can
        never itself create a duplicate.
        """
        taken = self._taken.setdefault(submission.id, set())
        if declared_page_index is not None:
            is_duplicate = declared_page_index in taken
            taken.add(declared_page_index)
            return declared_page_index, is_duplicate
        order = 0
        while order in taken:
            order += 1
        taken.add(order)
        return order, False


def upload_images(
    db: Session,
    batch_id: uuid.UUID,
    files: Sequence[tuple[str, bytes]],
    user: User,
) -> SubmissionBatchIngestResult:
    """Ingest loose scanned images into *batch_id*, grouped into submissions.

    *files* is ``(client_filename, bytes)`` IN ARRIVAL ORDER — the order matters,
    it is the last-resort positional signal, so the caller must preserve what the
    client sent. Returns what the call did, per submission and per rejected file.
    """
    batch, project = _load_batch_authorized(db, batch_id, user)
    # Re-check: the project may have left ACTIVE, or stopped being ready for
    # scanning, since the batch was created.
    form_project_service.assert_project_active_for_scanning(project)
    _assert_scan_ready(db, project)

    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No files uploaded.",
        )

    resolver = _GroupResolver(db, batch)
    rejected: list[RejectedImage] = []
    pages_added = 0
    conflicting_codes: list[str] = []
    group_key_mismatches: list[str] = []
    duplicate_positions: list[dict[str, object]] = []

    # Files are written before the transaction commits, so an abort must unlink
    # what it wrote — discarding the rows alone would leave orphans on disk.
    written: list[Path] = []

    try:
        for filename, data in files:
            source_filename = _display_filename(filename)
            try:
                ext = _validate_scanned_image(data)
            except HTTPException as exc:
                # Per-FILE rejection: this image is dropped from the call and
                # reported, the rest of the request carries on untouched.
                rejected.append(
                    RejectedImage(filename=source_filename, reason=str(exc.detail))
                )
                continue

            # The row id is generated up front because the image is stored under
            # it (server-generated path, never the client filename) and the codes
            # have to be read back off the stored file.
            page_id = uuid.uuid4()
            image_path = storage_service.save_submission_page_upload(
                data, page_id, ext
            )
            written.append(image_path)

            # Decoding is best-effort and never raises; the filename is parsed
            # regardless of how it went, because the two signals answer different
            # questions and either may be the only one available.
            raw_codes = code_rendering.decode_codes_from_image(image_path)
            resolution = submission_identification_service.resolve_image_page(
                source_filename, raw_codes
            )

            if (
                resolution.group_key is not None
                and len(resolution.group_key) > _MAX_GROUP_KEY_LEN
            ):
                # A QR can hold thousands of characters, so an absurd RID is
                # reachable. Truncating it to fit would be worse than refusing:
                # two different keys could collapse onto one and silently merge
                # two candidates' submissions. The file goes back off the disk.
                image_path.unlink(missing_ok=True)
                written.pop()
                rejected.append(
                    RejectedImage(
                        filename=source_filename,
                        reason=(
                            "Grouping key is too long "
                            f"(max {_MAX_GROUP_KEY_LEN} characters)."
                        ),
                    )
                )
                continue

            submission = resolver.resolve(resolution.group_key)
            source_page_order, is_duplicate_position = resolver.take_page_order(
                submission, resolution.declared_page_index
            )
            page = SubmissionPage(
                id=page_id,
                submission_id=submission.id,
                form_variant_page_id=None,  # resolved by matching
                source_file_path=str(image_path),
                source_filename=source_filename,
                source_page_order=source_page_order,
            )
            db.add(page)
            submission_identification_service.apply_identification(
                page,
                resolution.identification,
                page_order_source=resolution.page_order_source,
            )
            # A RID names the candidate whatever else it did — including when the
            # filename, not the RID, decided the grouping. Never overwritten.
            submission_identification_service.lift_respondent_identifier(
                submission, resolution.identification
            )
            pages_added += 1

            if resolution.identification.has_conflicting_codes:
                conflicting_codes.append(source_filename)
            if resolution.has_group_key_mismatch:
                group_key_mismatches.append(source_filename)
            if is_duplicate_position:
                duplicate_positions.append(
                    {
                        "filename": source_filename,
                        "source_page_order": source_page_order,
                    }
                )

        db.flush()
    except Exception:
        # No db.rollback(): nothing has been committed, so the pending rows are
        # discarded when the request's session closes. Rolling back here would
        # also discard the CALLER's pending work.
        for path in written:
            path.unlink(missing_ok=True)
        raise

    for submission in resolver.created:
        audit_service.log_event(
            db,
            actor_user_id=user.id,
            action=audit_service.SUBMISSION_CREATED,
            entity_type="Submission",
            entity_id=submission.id,
            metadata={
                "batch_id": str(batch_id),
                "source": "image_upload",
                "source_group_key": submission.source_group_key,
                "status": submission.status.value,
            },
        )

    unassigned_count = sum(
        1 for s in resolver.created if s.status is SubmissionStatus.UNASSIGNED
    )

    # Matching runs once the WHOLE call is ingested, not per file, and over every
    # submission this call touched — created AND merely extended. An existing
    # submission matters just as much: the page that carries the readable
    # VARIANT_IDENTIFIER may be the one that just arrived, so a submission left
    # MATCH_FAILED by an earlier upload can resolve now. Idempotent, so the
    # submissions that were already settled are simply left alone.
    for submission in [*resolver.created, *resolver.updated]:
        submission_matching_service.match_submission(
            db, submission, actor_user_id=user.id
        )

    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_IMAGES_INGESTED,
        entity_type="SubmissionBatch",
        entity_id=batch.id,
        metadata={
            "file_count": len(files),
            "pages_added": pages_added,
            "submissions_created": len(resolver.created),
            "submissions_updated": len(resolver.updated),
            "unassigned_count": unassigned_count,
            "rejected": [
                {"filename": r.filename, "reason": r.reason} for r in rejected
            ],
            # Anomalies, not errors — both were resolved deterministically and
            # the upload was accepted, but the sheets are worth a look.
            # Several codes with the SAME role tag on one image:
            "pages_with_conflicting_codes": conflicting_codes,
            # A RID and a conforming filename that disagree on the submission
            # (the RID won). Very likely an operator naming slip.
            "pages_with_group_key_mismatch": group_key_mismatches,
            # Two sheets in ONE submission DECLARING the same position (two files
            # named _page_1, or two masters printed with the same VID page
            # index). Both pages exist and share a source_page_order — see
            # _GroupResolver.take_page_order for why keeping both beats moving,
            # overwriting or rejecting one. Recorded per sheet so the anomaly is
            # visible instead of silent; matching must not assume positions are
            # unique within a submission.
            "pages_with_duplicate_position": duplicate_positions,
        },
    )
    db.commit()

    return SubmissionBatchIngestResult(
        submissions_created=[s.id for s in resolver.created],
        submissions_updated=[s.id for s in resolver.updated],
        pages_added=pages_added,
        unassigned_count=unassigned_count,
        rejected=rejected,
    )


# ---------------------------------------------------------------------------
# Manual re-run of matching over a whole batch
# ---------------------------------------------------------------------------


def run_batch_matching(
    db: Session, batch_id: uuid.UUID, user: User
) -> SubmissionBatchMatchingResult:
    """Re-run the matching engine over every not-fully-resolved submission.

    The manual counterpart of the automatic pass both upload paths already do.
    It exists because matching depends on state that changes AFTER an upload: a
    variant gets published, a VARIANT_IDENTIFIER role is assigned, a misprinted
    code is corrected. Idempotent — nothing already resolved is recomputed or
    overwritten — so it is always safe to press again.

    Authorisation is ``require_project_member``, like every other route in this
    module, but the ACTIVE gate is deliberately NOT applied. That gate exists to
    stop new sheets being COLLECTED outside the collection phase; re-matching
    creates no data and adds no sheet, it only finishes interpreting what is
    already stored. Refusing it on a CLOSED project would strand exactly the
    batches that most need it — the ones discovered to be mismatched after
    collection ended — with no way to repair them.

    Owns the transaction: the engine only flushes, and this commits once for the
    whole batch.
    """
    batch, _project = _load_batch_authorized(db, batch_id, user)

    summary = submission_matching_service.match_batch(
        db, batch.id, actor_user_id=user.id
    )

    # One summary event for the RUN, next to the per-submission events the engine
    # logged itself — a manual re-run is an operator action worth recording even
    # when it changed nothing.
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_MATCHING_RUN,
        entity_type="SubmissionBatch",
        entity_id=batch.id,
        metadata=dict(summary),
    )
    db.commit()

    return SubmissionBatchMatchingResult(**summary)


# ---------------------------------------------------------------------------
# Manual extraction run over a batch
# ---------------------------------------------------------------------------


def run_batch_extraction(
    db: Session, batch_id: uuid.UUID, user: User
) -> ExtractionRunSummary:
    """Read the answers off every eligible sheet of the batch.

    The manual trigger for ``extraction_orchestrator_service``. MANUAL on
    purpose, and this is the one place the difference from ``run_batch_matching``
    is visible: both upload paths match what they ingest automatically, and
    neither extracts it. Matching compares strings already stored; extraction
    opens every page image and reads pixels out of it, which on a batch of
    hundreds of submissions is minutes of work. That is work an operator asks for
    when they are ready, not something an upload does to them silently.

    Authorisation is ``require_project_member``, and the ACTIVE gate is
    deliberately NOT applied — the same reasoning as run-matching and the
    confidence threshold. That gate stops new sheets being COLLECTED outside the
    collection phase; extraction collects nothing, it interprets sheets already
    stored. A batch whose project has since closed is precisely the one whose
    answers still need reading.

    Idempotent, so it is always safe to press again: an object already extracted
    is skipped, never re-read and never overwritten. The useful consequence:
    pressing this on an already-extracted batch picks up only the objects still
    unread and leaves every existing result exactly as it was, human review
    included.

    One run at a time per batch (``batch_operation_lock``): the run holds
    the batch exclusively until it commits, and is refused with the 409
    ``EXTRACTION_RUNNING`` while another run, a "Review anyway" or a threshold
    change holds it. Taken before the run reads anything; the batch is then
    re-read, so the threshold the extractors use is the one now committed.

    Owns the transaction: the orchestrator only stages rows, and this commits
    once for the whole run — so a run lands whole or not at all.
    """
    batch, _project = _load_batch_authorized(db, batch_id, user)
    batch_operation_lock.acquire_for_run(db, batch.id)
    db.refresh(batch)

    summary = extraction_orchestrator_service.run_extraction_for_batch(db, batch.id)

    # ONE event for the run. Not one per result: a batch produces thousands of
    # readings, and a per-object trail would bury every other event in the
    # project's history. The results themselves record what was read.
    # The whole summary goes in, error_details included: the orchestrator caps
    # that list (MAX_ERROR_DETAILS), so the row stays bounded however badly the
    # run went, and the coordinates of the first failures are exactly what an
    # operator reading this event later needs. ``errors`` carries the true total.
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_BATCH_EXTRACTION_RUN,
        entity_type="SubmissionBatch",
        entity_id=batch.id,
        metadata=asdict(summary),
    )
    db.commit()

    # ``candidates_set`` is for the audit trail only: the response keeps
    # its shape.
    return ExtractionRunSummary(
        **{
            name: value
            for name, value in asdict(summary).items()
            if name in ExtractionRunSummary.model_fields
        }
    )


# ---------------------------------------------------------------------------
# Manual variant assignment for one submission
# ---------------------------------------------------------------------------


def assign_variant_manually(
    db: Session,
    submission_id: uuid.UUID,
    form_variant_id: uuid.UUID,
    user: User,
) -> Submission:
    """Recover a MATCH_FAILED submission by naming its variant by hand.

    The human end of the matching engine. Level 1 answers "which variant is
    this submission?" from the printed codes; when no code answers it, the
    submission lands in MATCH_FAILED and only a person looking at the sheets
    can say. This writes that answer down and then lets the ENGINE finish the
    job: ``match_submission`` skips Level 1 (the variant is already resolved by
    the time it runs) and Level 2 binds the pages exactly as it would have on an
    automatic match — same rules, same confidences. The page-binding logic is
    therefore used, never duplicated.

    Validation, and deliberately no more:

      * the submission must be in MATCH_FAILED — this is a recovery action, not
        a re-assignment tool. Anything else is 409 naming the status found,
        which includes UNASSIGNED: an ungroupable sheet is a GROUPING problem,
        not a variant one, and it takes this same error path with no special
        case;
      * the variant must exist AND belong to the submission's project — 404 for
        both, like every other id that exists but hangs off the wrong parent;
      * the variant must be PUBLISHED — 409. Only published variants have printed
        sheets, so assigning a DRAFT or ARCHIVED one describes something that
        cannot be on the operator's desk.

    Nothing checks that the submission's page count or codes are CONSISTENT
    with the chosen variant. That is the point of the action: the operator is
    looking at the paper and the system is not. Every such anomaly is treated
    the same way — resolve, record, let review catch it — rather than blocking
    a correction a priori.

    Like ``run_batch_matching``, the ACTIVE project gate is NOT applied: a
    recovery action must stay available on a batch whose project has since left
    the collection phase, which is precisely when mismatches surface.

    Both refusals are structured 409s: ``INVALID_STATUS`` with
    the ``status`` found, ``VARIANT_NOT_PUBLISHED`` with the ``variant_status``.
    The row is read ``FOR UPDATE``, like mark / undo unresolvable: an
    assignment and a concurrent mark serialise, and the second gets the 409
    instead of overwriting the first.

    Owns the transaction: commits and returns the refreshed submission.
    """
    submission, project = _load_submission_authorized(
        db, submission_id, user, for_update=True
    )

    if submission.status is not SubmissionStatus.MATCH_FAILED:
        raise _invalid_status(
            submission,
            "Only a MATCH_FAILED submission can be assigned a variant "
            f"manually; this one is {submission.status.value}.",
        )

    variant = form_variant_repo.get(db, form_variant_id)
    if variant is None or variant.form_project_id != project.id:
        # One 404 for both: to this project, a variant of another project is
        # simply not there, and saying otherwise would leak its existence.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form variant not found"
        )
    if variant.status is not FormVariantStatus.PUBLISHED:
        raise submission_lock_service.structured_conflict(
            submission_lock_service.VARIANT_NOT_PUBLISHED,
            f"Cannot assign a {variant.status.value} variant: only PUBLISHED "
            "variants have printed sheets.",
            variant_status=variant.status.value,
        )

    submission.matched_form_variant_id = variant.id
    submission.status = SubmissionStatus.MATCHED
    # Resolved: the reason it had failed no longer describes it.
    submission.match_failure_reason = None
    submission.match_failure_code_value = None
    db.flush()

    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_MANUALLY_MATCHED,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={"variant_id": str(form_variant_id)},
    )

    # Level 2, through the ordinary engine: with the variant already set, Level 1
    # skips itself and logs nothing (the event above is the record of THIS
    # decision), while the pages get bound and scored by the shared rules.
    submission_matching_service.match_submission(
        db, submission, actor_user_id=user.id
    )

    db.commit()
    db.refresh(submission)
    return submission


def _invalid_status(submission: Submission, message: str) -> HTTPException:
    """The structured 409 ``INVALID_STATUS`` of the MATCH_FAILED recovery
    actions, naming the status found."""
    return submission_lock_service.structured_conflict(
        submission_lock_service.INVALID_STATUS,
        message,
        status=submission.status.value,
    )


# ---------------------------------------------------------------------------
# Unresolvable submissions
# ---------------------------------------------------------------------------


# The statuses an operator can mark unresolvable from: a submission whose
# variant nobody can tell, and an ungroupable sheet nobody can place. Each is
# otherwise stuck holding back the export.
UNRESOLVABLE_FROM_STATUSES = frozenset(
    {SubmissionStatus.MATCH_FAILED, SubmissionStatus.UNASSIGNED}
)


def mark_unresolvable(
    db: Session, submission_id: uuid.UUID, user: User
) -> Submission:
    """Close a MATCH_FAILED or UNASSIGNED submission nobody can sort out.

    MATCH_FAILED: the other way out next to :func:`assign_variant_manually`,
    when the sheets are too damaged for a person to name the variant either.
    UNASSIGNED: an ungroupable loose image, which cannot currently be
    reassigned to a submission, so marking it is the only decision a human can
    take on it. Either way the submission goes to UNRESOLVABLE — closed, never
    finalized, not blocking the batch's export — and matching leaves it alone
    from then on. The status it came from is kept in
    ``unresolvable_from_status``, and nothing else is touched (variant, failure
    reason, pages), so :func:`undo_unresolvable` brings it back exactly as it
    was.

    Same rules as the manual assignment: any project member, the ACTIVE gate
    NOT applied (recovery must stay possible after collection has closed).
    Only from ``UNRESOLVABLE_FROM_STATUSES``: anything else is the structured
    409 ``INVALID_STATUS`` with the ``status`` found, a second click included.
    Recorded as ``submission.marked_unresolvable``, with ``from_status``.

    The row is read ``FOR UPDATE``: two operators acting on the same submission
    are serialised, and the second sees the first one's status.

    Owns the transaction: commits and returns the refreshed submission.
    """
    submission, _project = _load_submission_authorized(
        db, submission_id, user, for_update=True
    )
    from_status = submission.status
    if from_status not in UNRESOLVABLE_FROM_STATUSES:
        raise _invalid_status(
            submission,
            "Only a MATCH_FAILED or UNASSIGNED submission can be marked "
            f"unresolvable; this one is {from_status.value}.",
        )

    submission.status = SubmissionStatus.UNRESOLVABLE
    submission.unresolvable_from_status = from_status
    return _record_unresolvable_move(
        db,
        submission,
        user,
        action=audit_service.SUBMISSION_MARKED_UNRESOLVABLE,
        from_status=from_status,
    )


def undo_unresolvable(
    db: Session, submission_id: uuid.UUID, user: User
) -> Submission:
    """Take an UNRESOLVABLE submission back to the status it was marked from.

    For an operator's mistake, or a sheet found again: back to MATCH_FAILED,
    open again to manual assignment and to matching, or back to UNASSIGNED.
    Same rules as :func:`mark_unresolvable`, mirrored: only from UNRESOLVABLE,
    otherwise the 409 ``INVALID_STATUS``. Recorded as
    ``submission.unresolvable_undone``, with ``from_status``.

    A NULL ``unresolvable_from_status`` should not occur: rows marked before
    the column existed were set to MATCH_FAILED when it was added. It is read
    as MATCH_FAILED anyway, the one status that could be marked back then.
    """
    submission, _project = _load_submission_authorized(
        db, submission_id, user, for_update=True
    )
    if submission.status is not SubmissionStatus.UNRESOLVABLE:
        raise _invalid_status(
            submission,
            "Only an UNRESOLVABLE submission can be taken back; this one is "
            f"{submission.status.value}.",
        )

    submission.status = (
        submission.unresolvable_from_status or SubmissionStatus.MATCH_FAILED
    )
    submission.unresolvable_from_status = None
    return _record_unresolvable_move(
        db,
        submission,
        user,
        action=audit_service.SUBMISSION_UNRESOLVABLE_UNDONE,
        from_status=SubmissionStatus.UNRESOLVABLE,
    )


def _record_unresolvable_move(
    db: Session,
    submission: Submission,
    user: User,
    *,
    action: str,
    from_status: SubmissionStatus,
) -> Submission:
    """The shared tail of :func:`mark_unresolvable` / :func:`undo_unresolvable`:
    audit, commit, refresh."""
    db.flush()
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=action,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={
            "status": submission.status.value,
            "from_status": from_status.value,
        },
    )

    db.commit()
    db.refresh(submission)
    return submission


# ---------------------------------------------------------------------------
# Detail (the batch's review queue)
# ---------------------------------------------------------------------------


def get_batch_detail(
    db: Session,
    batch_id: uuid.UUID,
    user: User,
    status_filter: SubmissionStatus | None = None,
) -> SubmissionBatchDetail:
    """A batch plus its submissions, their page counts and how many of their
    results still await review.

    *status_filter* narrows the submissions to one status — enough for an
    operator to pull up the MATCH_FAILED submissions of a batch and work
    through them. This is a per-batch view, not a cross-batch, paginated
    queue endpoint.

    This is the review queue, so each submission also carries its candidate
    identifier, its missing template pages and whether its variant expects a
    candidate at all. All of it comes from a CONSTANT number of queries,
    however many submissions the batch holds: the model's
    ``missing_page_indices`` property is deliberately not used, because it
    lazy-loads the pages of each submission — an N+1 over hundreds of rows.

    Each submission also carries who is reviewing it (its ACTIVE soft lock,
    null otherwise), at the cost of one more query only when something is
    locked.

    And whether a run has examined it and, for a MATCHED one it has, how many
    objects it left unread — what "Review anyway" would turn into
    placeholders. Three more queries, and only when such a submission is
    listed (``review_anyway_service.unread_counts_for_queue``).
    """
    batch, _project = _load_batch_authorized(db, batch_id, user)
    counts = submission_batch_repo.page_counts(db, batch_id)
    pending_counts = submission_object_result_repo.pending_review_counts_for_batch(
        db, batch_id
    )
    submissions = submission_batch_repo.list_submissions(db, batch_id, status_filter)

    # Only the variants the LISTED submissions matched: with a status filter that
    # may be fewer, and with none matched (a MATCH_FAILED view) both variant
    # queries are skipped by the repos.
    variant_ids = {
        s.matched_form_variant_id
        for s in submissions
        if s.matched_form_variant_id is not None
    }
    present_pages = submission_batch_repo.present_page_indices_for_batch(db, batch_id)
    expected_pages = form_variant_page_repo.page_indices_for_variants(db, variant_ids)
    variants_expecting_candidate = form_variant_role_binding_repo.variants_with_role(
        db, variant_ids, PageObjectRole.RESPONDENT_IDENTIFIER
    )

    # Who is reviewing what: one extra query, and only when
    # something listed is actively locked.
    locks = submission_lock_service.active_locks(db, submissions)
    unread_counts = review_anyway_service.unread_counts_for_queue(
        db, batch_id, submissions
    )
    # Over the WHOLE batch, whatever the status filter: one more
    # grouped query.
    readiness = export_readiness(
        submission_batch_repo.status_counts_for_batch(db, batch_id)
    )

    # Built field-by-field rather than model_validate(batch): the ORM batch has
    # its OWN ``submissions`` relationship, which from_attributes would pull in
    # and fail to validate (a Submission row carries no page_count).
    return SubmissionBatchDetail(
        id=batch.id,
        form_project_id=batch.form_project_id,
        created_by_user_id=batch.created_by_user_id,
        name=batch.name,
        description=batch.description,
        created_at=batch.created_at,
        confidence_threshold=batch.confidence_threshold,
        effective_threshold=confidence_threshold_service.effective_threshold(batch),
        export_readiness=readiness,
        submissions=[
            SubmissionSummary(
                id=submission.id,
                status=submission.status,
                matched_form_variant_id=submission.matched_form_variant_id,
                page_count=counts.get(submission.id, 0),
                created_at=submission.created_at,
                pending_review_count=pending_counts.get(submission.id, 0),
                respondent_identifier=submission.respondent_identifier,
                missing_page_indices=_missing_page_indices(
                    submission, expected_pages, present_pages
                ),
                respondent_identifier_expected=(
                    submission.matched_form_variant_id in variants_expecting_candidate
                ),
                lock=locks.get(submission.id),
                extraction_attempted=submission.extraction_attempted_at is not None,
                unread_object_count=unread_counts.get(submission.id, 0),
                match_failure_reason=submission.match_failure_reason,
                match_failure_code_value=submission.match_failure_code_value,
                unresolvable_from_status=submission.unresolvable_from_status,
            )
            for submission in submissions
        ],
    )


def _missing_page_indices(
    submission: Submission,
    expected_pages: Mapping[uuid.UUID, set[int]],
    present_pages: Mapping[uuid.UUID, set[int]],
) -> list[int]:
    """The template pages of *submission*'s variant that no sheet fills, sorted.

    Same rule as ``Submission.missing_page_indices``, fed from the batched
    lookups instead of lazy loads: [] with no matched variant, since with no
    template there is nothing to be missing.
    """
    if submission.matched_form_variant_id is None:
        return []
    expected = expected_pages.get(submission.matched_form_variant_id, set())
    present = present_pages.get(submission.id, set())
    return sorted(expected - present)


# ---------------------------------------------------------------------------
# A project's batches, with on-the-fly metrics
# ---------------------------------------------------------------------------

# Submissions a human has closed
# (``app.models.enums.CLOSED_SUBMISSION_STATUSES``). Used twice on purpose: it
# is what ``reviewed_count`` counts AND what drops a submission's results out
# of ``pending_review_count`` — a closed submission has no review work left, so
# the two metrics must never disagree about which submissions are closed.

# Submissions that are still somewhere in the pipeline, before a human has
# closed them: a batch holding any of these cannot be exported yet.
# UNRESOLVABLE is deliberately absent: it is closed and does not hold the
# batch back.
EXPORT_BLOCKING_STATUSES = frozenset(
    {
        SubmissionStatus.UPLOADED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.UNASSIGNED,
        SubmissionStatus.SCANNED,
    }
)


def _is_ready_for_export(status_counts: Mapping[SubmissionStatus, int]) -> bool:
    """Whether a batch with these per-status submission counts can be exported.

    True only if the batch holds at least one submission that is not
    UNRESOLVABLE AND none of them is in ``EXPORT_BLOCKING_STATUSES``. An EMPTY
    batch is not ready: there is nothing to export, and reporting it as ready
    would invite an empty export. Nor is a batch of UNRESOLVABLE submissions
    only, for the same reason: an UNRESOLVABLE submission does not block the
    export, but it does not make one either.

    This is THE rule. The export gate (:func:`export_readiness` and
    :func:`require_ready_for_export`) calls this function rather than restate
    it, so the list's badge, the batch detail and the gate can never disagree.
    """
    exportable = sum(status_counts.values()) - status_counts.get(
        SubmissionStatus.UNRESOLVABLE, 0
    )
    if exportable == 0:
        return False
    return not any(
        status_counts.get(blocking, 0) for blocking in EXPORT_BLOCKING_STATUSES
    )


def export_readiness(
    status_counts: Mapping[SubmissionStatus, int],
) -> ExportReadiness:
    """Whether a batch with these per-status counts can be exported, and if
    not, how many submissions hold it back and why.

    ``ready`` is :func:`_is_ready_for_export`, never restated. The four counts
    split ``EXPORT_BLOCKING_STATUSES`` by what each submission still needs:
    extraction (UPLOADED, MATCHED), review (SCANNED), a variant (MATCH_FAILED),
    a human decision on an ungroupable sheet (UNASSIGNED). They are all zero
    on a ready batch, and also on an empty one or one of UNRESOLVABLE
    submissions only — not ready, yet nothing in it is in progress.
    """
    def count(*statuses: SubmissionStatus) -> int:
        return sum(status_counts.get(s, 0) for s in statuses)

    return ExportReadiness(
        ready=_is_ready_for_export(status_counts),
        to_extract=count(SubmissionStatus.UPLOADED, SubmissionStatus.MATCHED),
        in_review=count(SubmissionStatus.SCANNED),
        to_assign=count(SubmissionStatus.MATCH_FAILED),
        unassigned=count(SubmissionStatus.UNASSIGNED),
    )


def require_ready_for_export(
    db: Session, batch_id: uuid.UUID, user: User
) -> SubmissionBatch:
    """The export gate: the batch, if it can be exported now.

    Otherwise the structured 409 ``NOT_EXPORT_READY``, carrying the counts of
    :func:`export_readiness` so the caller can say what is missing. For the
    export endpoints (Summary export and Detail export), which call it before
    building any file. Any project member, the ACTIVE gate NOT applied: a
    project's results are exported after collection has closed. 404 / 403 as
    for every other batch read.
    """
    batch, _project = _load_batch_authorized(db, batch_id, user)
    readiness = export_readiness(
        submission_batch_repo.status_counts_for_batch(db, batch.id)
    )
    if readiness.ready:
        return batch

    blocking = (
        readiness.to_extract
        + readiness.in_review
        + readiness.to_assign
        + readiness.unassigned
    )
    if blocking == 0:
        message = (
            "The batch cannot be exported: it has no submission closed by a "
            "review."
        )
    else:
        message = (
            f"The batch cannot be exported yet: {blocking} submission"
            f"{'s are' if blocking != 1 else ' is'} not complete."
        )
    raise submission_lock_service.structured_conflict(
        submission_lock_service.NOT_EXPORT_READY,
        message,
        **readiness.model_dump(exclude={"ready"}),
    )


def list_batches_for_project(
    db: Session, project_id: uuid.UUID, user: User
) -> list[SubmissionBatchListItem]:
    """Every batch of *project_id*, newest first, each with its metrics.

    Any member may list, and the ACTIVE project gate is deliberately NOT
    applied: this is a read, and a CLOSED or ARCHIVED project must still show
    the batches it collected — the same reasoning as ``run_batch_matching``.

    Exactly three queries, however many batches the project holds: the batches,
    their per-status submission counts, and their pending-review counts. The
    repo returns raw counts; which statuses are closed or block export is
    decided here, once.
    """
    form_project_service.require_project_member(db, project_id, user)

    batches = submission_batch_repo.list_batches_for_project(db, project_id)
    status_counts = submission_batch_repo.status_counts_for_project(db, project_id)
    pending_counts = (
        submission_object_result_repo.pending_review_counts_by_batch_for_project(
            db, project_id, CLOSED_SUBMISSION_STATUSES
        )
    )

    items: list[SubmissionBatchListItem] = []
    for batch in batches:
        counts = status_counts.get(batch.id, {})
        # Built on to_batch_read so the batch fields and the effective
        # threshold keep a single source.
        items.append(
            SubmissionBatchListItem(
                **to_batch_read(batch).model_dump(),
                total_submissions=sum(counts.values()),
                match_failed_count=counts.get(SubmissionStatus.MATCH_FAILED, 0),
                unassigned_count=counts.get(SubmissionStatus.UNASSIGNED, 0),
                unresolvable_count=counts.get(SubmissionStatus.UNRESOLVABLE, 0),
                reviewed_count=sum(
                    counts.get(closed, 0) for closed in CLOSED_SUBMISSION_STATUSES
                ),
                pending_review_count=pending_counts.get(batch.id, 0),
                ready_for_export=_is_ready_for_export(counts),
            )
        )
    return items
