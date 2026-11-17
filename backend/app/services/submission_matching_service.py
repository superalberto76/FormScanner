# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Variant matching + page binding.

The step ingestion deliberately leaves undone. Ingestion answers "which
physical sheets are one submission, and in what order?"; matching answers the
two questions that need the AUTHORED side of the domain:

  LEVEL 1 — which printable FormVariant is this submission?  (once per Submission)
  LEVEL 2 — which FormVariantPage is each physical sheet?    (once per page)

Level 1 first, always: a page cannot be bound to a template page until the
template is known, so a submission whose variant does not resolve leaves all
its pages unbound and goes to ``MATCH_FAILED`` as a whole.

Level 1, in order:
  1. The project has exactly ONE published variant → that is the answer, with no
     code read at all (the "no ambiguity" case of the scan-readiness rule).
     A submission with not one readable code still matches.
  2. Otherwise the submission is scanned page by page in ``source_page_order``
     until one page's stored ``identified_code_value`` re-parses to a ``VID``
     whose value is the VARIANT_IDENTIFIER of a PUBLISHED variant of the
     project. ONE readable page is enough for the whole submission: the
     VARIANT_IDENTIFIER is a shared object, printed identically on every page of
     the variant, so page 7 answers the question just as well as page 1.
  3. No page answers → the whole Submission becomes ``MATCH_FAILED``,
     ``matched_form_variant_id`` stays NULL. Recovering such a submission
     (manual variant assignment) is deliberately absent here.

Level 2, per page, once the variant is known:
  1. The page's own VID payload carries a page index → used DIRECTLY. Both it and
     ``FormVariantPage.page_index`` are 1-BASED (the export encodes the latter
     verbatim, see the model docstrings), so there is NO conversion here.
     ``match_confidence`` 1.0 — the sheet declared its own position.
  2. Otherwise the physical position is the only signal:
     ``source_page_order + 1``. That +1 is a BASE CONVERSION, not an off-by-one:
     ``SubmissionPage.source_page_order`` is 0-based (the physical position in
     the file/request) while ``FormVariantPage.page_index`` is 1-based, and the
     two are deliberately different concepts. ``match_confidence`` 0.5 — a
     plausible deduction that a missing or reordered sheet silently breaks.
  3. The wanted page_index does not exist in the matched variant (more physical
     sheets than the variant has pages, or a code claiming an out-of-range
     index) → ``form_variant_page_id`` stays NULL with ``match_confidence`` 0.0.
     One unplaceable sheet never stops the others being placed.

Transactions and idempotency
----------------------------
Nothing here commits — only ``flush()``. Both entry points run INSIDE somebody
else's transaction: the ingestion services call ``match_submission`` before their
own commit (so ingesting and matching a submission is one atomic operation), and
the manual re-run endpoint commits once for the whole batch.

Both functions are idempotent, which is what makes re-running safe:
``match_submission`` skips Level 1 when the variant is already resolved and skips
any page whose ``form_variant_page_id`` is already set, so an existing resolution
is never recomputed and never overwritten. A re-run therefore only ever ADDS: a
second upload that brings the readable page a submission was missing turns a
MATCH_FAILED into a MATCHED, while everything already resolved stays put.

UNASSIGNED is never overwritten
-------------------------------
``SubmissionStatus.UNASSIGNED`` answers a DIFFERENT question from
MATCHED / MATCH_FAILED: it says the sheet could not be attributed to a
submission at all, not that its variant is unknown. Matching happily resolves
the variant and the pages of such a Submission — that is useful evidence for
the human who will have to place it — but it leaves the STATUS alone, because
the grouping is still open and only a human can close it: the recovery
workflow (manual variant assignment), or an operator marking it unresolvable.
Writing MATCHED over UNASSIGNED would claim a submission had been sorted out
when nobody has yet decided whose it is.

UNRESOLVABLE is never touched
-----------------------------
An operator marked the submission unresolvable: matching skips it ENTIRELY —
no variant, no page binding, no status, no audit — even when a re-run could
now resolve it (a variant published since, a sheet added). The human verdict
stands until a human undoes it. The batch re-run does not even list it
(``list_unresolved_submissions``); the guard in ``_match_submission`` covers
the ingestion paths, which match the submission they extend.

Why it failed
-------------
When Level 1 fails, the reason is written on the submission
(``match_failure_reason``, plus the value read for VID_NOT_RECOGNISED) as well
as in the audit trail; when it resolves, both are cleared.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.enums import MatchFailureReason, SubmissionStatus
from app.models.submission import Submission
from app.models.submission_page import SubmissionPage
from app.repositories import (
    code_object_repo,
    form_variant_page_repo,
    form_variant_repo,
    submission_batch_repo,
)
from app.services import audit_service
from app.utils.code_rendering import TAG_VARIANT_IDENTIFIER, parse_tagged_payload


# Confidence written on a page by Level 2 — see SubmissionPage.match_confidence.
CONFIDENCE_DECLARED = 1.0  # the page's own code stated its page index
CONFIDENCE_DEDUCED = 0.5  # deduced from the physical order (source_page_order+1)
CONFIDENCE_UNPLACED = 0.0  # no such page_index in the matched variant

# Why Level 1 could not resolve a variant — recorded in the audit trail so a
# MATCH_FAILED submission says WHY it failed, which is what the recovery
# workflow (and any operator) needs first.
REASON_NO_PUBLISHED_VARIANT = "no_published_variant"
REASON_NO_VID_FOUND = "no_vid_found"
REASON_VID_NOT_RECOGNISED = "vid_not_recognised"
# The persisted form of the three failures (Submission.match_failure_reason).
FAILURE_REASONS = {
    REASON_NO_PUBLISHED_VARIANT: MatchFailureReason.NO_PUBLISHED_VARIANT,
    REASON_NO_VID_FOUND: MatchFailureReason.NO_VID_FOUND,
    REASON_VID_NOT_RECOGNISED: MatchFailureReason.VID_NOT_RECOGNISED,
}
# …and how it DID resolve.
REASON_SINGLE_PUBLISHED_VARIANT = "single_published_variant"
REASON_VID_MATCHED = "vid_matched"


@dataclass
class MatchOutcome:
    """What one ``match_submission`` call actually did to one submission."""

    # The variant the submission is bound to AFTER the call (None = unresolved).
    variant_id: uuid.UUID | None = None
    # False when Level 1 was skipped because the variant was already resolved —
    # the difference between "matched now" and "was already matched", which is
    # what keeps a re-run from logging a second audit event for the same fact.
    resolved_now: bool = False
    # Why it resolved / failed (one of the REASON_* constants), or None when
    # Level 1 did not run at all.
    reason: str | None = None
    # SubmissionPage rows that gained a form_variant_page_id in THIS call.
    pages_resolved: int = 0
    # Pages Level 2 examined but could not place (confidence 0.0).
    pages_unplaced: int = 0
    # source_page_order of every page whose position was DEDUCED rather than
    # declared. Surfaced because those bindings are the fragile ones.
    deduced_positions: list[int] = field(default_factory=list)
    # Non-empty ONLY when the VID value that resolved this submission is claimed
    # by several published variants at once — every candidate, winner included.
    # The choice is unaffected (see ``_resolve_variant``); this exists so the
    # anomaly is visible instead of silent.
    ambiguous_variant_ids: list[uuid.UUID] = field(default_factory=list)
    # With REASON_VID_NOT_RECOGNISED only: the first identifier value read, in
    # physical page order, that matched no published variant.
    unrecognised_code_value: str | None = None

    @property
    def matched(self) -> bool:
        return self.variant_id is not None


# ---------------------------------------------------------------------------
# Level 1 — which variant is this submission?
# ---------------------------------------------------------------------------


def _ordered_pages(submission: Submission) -> list[SubmissionPage]:
    """The submission's pages in a STABLE physical order.

    ``source_page_order`` is explicitly not unique within a submission (two
    sheets can declare the same position — see
    ``_GroupResolver.take_page_order``), so the id breaks ties. Without it, "the
    first page carrying a readable VID" — and therefore which variant a
    misprinted submission matches — would depend on row order. ``created_at``
    is useless as a tie-breaker here: it comes from a transaction-scoped
    ``now()``, identical for every page of one upload.
    """
    return sorted(submission.pages, key=lambda p: (p.source_page_order, str(p.id)))


def _read_variant_identifier(page: SubmissionPage) -> tuple[str | None, int | None]:
    """The (value, page_index) this page's stored payload claims, or (None, None).

    Re-parses ``identified_code_value`` rather than trusting a derived column:
    ingestion stored the payload RAW precisely so this decision could be made
    here, with the authored side in hand. A payload that is absent, untagged or
    respondent-only identifies no variant.
    """
    raw = page.identified_code_value
    if not raw:
        return None, None
    tag, value, page_index = parse_tagged_payload(raw)
    if tag != TAG_VARIANT_IDENTIFIER:
        return None, None
    return value, page_index


def _resolve_variant(
    db: Session, submission: Submission, form_project_id: uuid.UUID, outcome: MatchOutcome
) -> tuple[uuid.UUID | None, str]:
    """Level 1 proper: the variant for *submission*, plus the REASON.

    Reads only — the sole thing it writes is ``outcome.ambiguous_variant_ids``,
    the diagnostic recorded when the matching value turns out to belong to more
    than one published variant. Returns ``(variant_id, reason)`` with variant_id
    None when the submission could not be resolved.
    """
    published_ids = form_variant_repo.list_published_variant_ids(db, form_project_id)
    if not published_ids:
        # Nothing to match against at all. Distinct from "the code was
        # unreadable": the project is not scan-ready, which is a
        # configuration problem, not a scanning one.
        return None, REASON_NO_PUBLISHED_VARIANT
    if len(published_ids) == 1:
        # The unambiguous case: whatever the sheets say (or fail to say), there
        # is exactly one variant they can belong to. No code is read, so a
        # submission with no readable code at all still matches.
        return published_ids[0], REASON_SINGLE_PUBLISHED_VARIANT

    saw_a_variant_identifier = False
    for page in _ordered_pages(submission):
        value, _page_index = _read_variant_identifier(page)
        if value is None:
            continue
        saw_a_variant_identifier = True
        match = code_object_repo.find_published_variant_by_identifier_value(
            db, form_project_id, value
        )
        if match is None and outcome.unrecognised_code_value is None:
            outcome.unrecognised_code_value = value
        if match is not None:
            if match.is_ambiguous:
                # Two published variants printed with the SAME identifier value:
                # the sheets are literally indistinguishable, so no rule could
                # pick the right one. The deterministic winner stands (lowest
                # version_number, unchanged) and the anomaly is recorded, in the
                # same "resolve AND report" spirit as every other clash on this
                # path — never blocking the submission, never silent either.
                outcome.ambiguous_variant_ids = list(match.candidate_ids)
            return match.variant_id, REASON_VID_MATCHED

    # Two genuinely different failures, worth telling apart in the audit trail:
    # nothing readable on any sheet (scan quality / codes not printed) versus a
    # value that WAS read but belongs to no published variant (wrong project,
    # archived variant, a sheet from another campaign).
    return None, (
        REASON_VID_NOT_RECOGNISED if saw_a_variant_identifier else REASON_NO_VID_FOUND
    )


# ---------------------------------------------------------------------------
# Level 2 — which template page is each sheet?
# ---------------------------------------------------------------------------


def _bind_pages(
    db: Session, submission: Submission, variant_id: uuid.UUID, outcome: MatchOutcome
) -> None:
    """Bind every still-unbound page of *submission* to a page of *variant_id*.

    The variant's pages are read ONCE into an index map rather than queried per
    sheet: a submission is small but a batch is not, and this is the inner loop
    of a re-run over hundreds of them.
    """
    pages_by_index = {
        page.page_index: page.id
        for page in form_variant_page_repo.list_for_variant(db, variant_id)
    }

    for page in submission.pages:
        if page.form_variant_page_id is not None:
            # Idempotency: an already-bound page is never re-examined, so a
            # re-run can neither overwrite a binding nor downgrade the
            # confidence that was recorded with it.
            continue

        _value, declared_index = _read_variant_identifier(page)
        if declared_index is not None:
            # Already 1-based — FormVariantPage.page_index is encoded verbatim
            # into the payload by the export. Deliberately NO conversion.
            candidate_index = declared_index
            confidence = CONFIDENCE_DECLARED
        else:
            # 0-based physical position -> 1-based template page index.
            candidate_index = page.source_page_order + 1
            confidence = CONFIDENCE_DEDUCED

        target_id = pages_by_index.get(candidate_index)
        if target_id is None:
            # The submission has a sheet the variant has no page for. Recorded
            # as examined-but-unplaced rather than left NULL/NULL, and it never
            # stops the remaining sheets being bound.
            page.match_confidence = CONFIDENCE_UNPLACED
            outcome.pages_unplaced += 1
            continue

        page.form_variant_page_id = target_id
        page.match_confidence = confidence
        outcome.pages_resolved += 1
        if confidence == CONFIDENCE_DEDUCED:
            outcome.deduced_positions.append(page.source_page_order)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _match_submission(
    db: Session, submission: Submission, *, actor_user_id: uuid.UUID | None = None
) -> MatchOutcome:
    """The whole engine for one submission; see :func:`match_submission`."""
    outcome = MatchOutcome(variant_id=submission.matched_form_variant_id)
    if submission.status is SubmissionStatus.UNRESOLVABLE:
        # A human verdict: see the module docstring. Nothing is written.
        return outcome

    if outcome.variant_id is None:
        form_project_id = submission.submission_batch.form_project_id
        variant_id, reason = _resolve_variant(
            db, submission, form_project_id, outcome
        )
        outcome.variant_id = variant_id
        outcome.reason = reason
        outcome.resolved_now = True

        if variant_id is not None:
            submission.matched_form_variant_id = variant_id
            submission.match_failure_reason = None
            submission.match_failure_code_value = None
            _set_status(submission, SubmissionStatus.MATCHED)
        else:
            submission.match_failure_reason = FAILURE_REASONS[reason]
            submission.match_failure_code_value = outcome.unrecognised_code_value
            _set_status(submission, SubmissionStatus.MATCH_FAILED)

    if outcome.variant_id is not None:
        _bind_pages(db, submission, outcome.variant_id, outcome)

    if outcome.resolved_now:
        _log_outcome(db, submission, outcome, actor_user_id)

    db.flush()
    return outcome


def _set_status(submission: Submission, new_status: SubmissionStatus) -> None:
    """Write the Level 1 outcome onto the submission, sparing UNASSIGNED.

    See the module docstring: UNASSIGNED answers "which submission is this
    sheet?" and is still awaiting a human, so a matching verdict must not speak
    over it.
    """
    if submission.status is SubmissionStatus.UNASSIGNED:
        return
    submission.status = new_status


def _log_outcome(
    db: Session,
    submission: Submission,
    outcome: MatchOutcome,
    actor_user_id: uuid.UUID | None,
) -> None:
    """One audit event per submission whose variant was DECIDED by this call."""
    metadata: dict = {
        "reason": outcome.reason,
        "status": submission.status.value,
        "pages_resolved": outcome.pages_resolved,
        "pages_unplaced": outcome.pages_unplaced,
        # The bindings that were deduced from the physical order rather than
        # declared by a code: correct in the ordinary case, and the first thing
        # to review when a submission scores oddly.
        "pages_with_deduced_position": outcome.deduced_positions,
    }
    if outcome.variant_id is not None:
        metadata["variant_id"] = str(outcome.variant_id)
    if outcome.unrecognised_code_value is not None and not outcome.matched:
        metadata["code_value"] = outcome.unrecognised_code_value
    if outcome.ambiguous_variant_ids:
        # The submission IS matched and the winner is deterministic — but the
        # identifier value it matched on belongs to several published variants,
        # so roughly half the submissions carrying it are attributed to the
        # wrong one, with nothing else in the data to betray it. Flagged as an
        # anomaly to review, exactly like pages_with_conflicting_codes.
        metadata["identifier_value_ambiguous"] = True
        metadata["ambiguous_variant_ids"] = [
            str(variant_id) for variant_id in outcome.ambiguous_variant_ids
        ]
    audit_service.log_event(
        db,
        actor_user_id=actor_user_id,
        action=(
            audit_service.SUBMISSION_MATCHED
            if outcome.matched
            else audit_service.SUBMISSION_MATCH_FAILED
        ),
        entity_type="Submission",
        entity_id=submission.id,
        metadata=metadata,
    )


def match_submission(
    db: Session, submission: Submission, *, actor_user_id: uuid.UUID | None = None
) -> None:
    """Run both levels over one submission.

    Does NOT commit — flushes only; the caller owns the transaction, whether that
    caller is automatic ingestion (which matches inside its own upload
    transaction) or the manual re-run endpoint.

    Idempotent: Level 1 is skipped when the variant is already resolved, and
    Level 2 skips every page already bound, so calling this twice changes nothing
    the first call decided. *actor_user_id* attributes the audit events; it is
    optional so the engine can also run unattended.
    """
    _match_submission(db, submission, actor_user_id=actor_user_id)


def match_batch(
    db: Session, batch_id: uuid.UUID, *, actor_user_id: uuid.UUID | None = None
) -> dict:
    """Run :func:`match_submission` over every not-fully-resolved submission.

    "Not fully resolved" means no variant yet, OR at least one page still without
    a ``form_variant_page_id`` — the second case matters because a submission
    can be matched and still gain pages later (a second image upload), and
    because a page the previous run could not place may be placeable now.

    Returns a summary of the CALL, for the manual endpoint's response::

        {"submissions_processed": int,  # submissions examined
         "matched": int,                # …left with a variant resolved
         "match_failed": int,           # …left without one
         "pages_resolved": int}         # pages bound by THIS call

    ``matched + match_failed == submissions_processed``: they describe the state
    each examined submission was LEFT in, not only what changed, so a submission
    that was already matched and merely gained a page counts as matched.
    ``pages_resolved`` is the opposite — strictly what this call did, so a re-run
    that finds nothing new honestly reports 0.

    Does NOT commit; the caller does.
    """
    submissions = submission_batch_repo.list_unresolved_submissions(db, batch_id)

    matched = 0
    pages_resolved = 0
    for submission in submissions:
        outcome = _match_submission(db, submission, actor_user_id=actor_user_id)
        matched += 1 if outcome.matched else 0
        pages_resolved += outcome.pages_resolved

    return {
        "submissions_processed": len(submissions),
        "matched": matched,
        "match_failed": len(submissions) - matched,
        "pages_resolved": pages_resolved,
    }
