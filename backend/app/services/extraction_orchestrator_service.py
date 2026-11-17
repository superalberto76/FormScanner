# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who runs extraction, over what, and in what order.

Each extractor is a pure function that reads ONE authored object off ONE
scanned sheet. This module is the part that walks the scanning domain and
decides what to hand it:

    batch → Submission → SubmissionPage → (its template page's) PageObject

and, per object, which extraction function applies.

The iteration lives here, once, rather than inside each extractor's caller:
written once per extractor, the copies would disagree about which submissions
are eligible, about what a failure does to its siblings, and about what counts
as already done.

MANUAL, not automatic
---------------------
Nothing here runs at the end of an upload, unlike matching, which the
ingestion paths invoke inside their own transaction. The asymmetry is
deliberate and is about COST: matching compares strings already stored, while
extraction opens every page image and reads pixels out of it. A batch of five
hundred submissions is a few milliseconds of matching and minutes of extraction
— work an operator should ask for, at a moment of their choosing, not something
an upload silently does to them. Hence the manual endpoint
(``POST /submission-batches/{id}/run-extraction``).

The dispatch table
------------------
:data:`_EXTRACTORS` maps a ``PageObjectType`` to the function that reads it —
QUESTION, MATRIX, FREE_TEXT_AREA and a role-less CODE today. A type with no
extractor is a NORMAL, counted outcome — ``skipped_not_yet_supported`` — never
an error and never a stop. The whole point of the table is that a new
extractor adds a line to it and touches no iteration logic at all.

The table is keyed by TYPE, but the type is not always the whole answer: a
MATRIX holding the RESPONDENT_IDENTIFIER role is a respondent-ID grid, not an
evaluable question, and is routed to a different extractor than a role-less
MATRIX even though both share one entry in the table. A FREE_TEXT_AREA holding
that same role is NOT redirected: it is read by the ordinary OCR extractor, as
FREE_TEXT, and like every free-text reading it is always reviewed — where the
operator confirms or corrects the identity it spells. :func:`_resolve_extractor`
is where that routing lives, so the walk below stays a lookup.

Three kinds of "did nothing", kept apart
----------------------------------------
Lumping them together would make the summary useless, because they call for
three different reactions:

  * ``skipped_not_applicable`` — the object is not an answer and never will be
    (a TEXT label, an IMAGE, a CODE carrying a scanning role, which matching has
    already consumed in full). Nothing to wait for. Expected in every run.
  * ``skipped_not_yet_supported`` — the object IS an answer, and this build
    cannot read it yet. It will be read once its extractor ships, and the count
    is how an operator sees how much of the batch is still pending. Every
    answer type has an extractor today, so this is 0 in practice; the counter
    stays for the next object type that ships without one.
  * ``skipped_already_extracted`` — read by an earlier run. This is what makes
    the whole thing re-runnable.

Idempotency, and why it is the interesting property
---------------------------------------------------
An object that already has a ``SubmissionObjectResult`` is skipped, never
re-read and never overwritten — the pair ``(submission_page_id, page_object_id)``
is unique in the database, so a re-run could not append a second reading even
if this module forgot to check.

The consequence is worth stating plainly: once a new extractor ships, pressing
run-extraction again on a batch already extracted picks the newly readable
objects up where they were left and does not touch a single existing result.
No migration, no backfill script, no "re-extract from scratch" that would
discard readings a human may already have reviewed.

One failure never stops the run
-------------------------------
Every extractor call is wrapped. A page whose image is missing, corrupt, or
geometrically impossible produces one counted error with its coordinates
recorded, and the walk continues with the next object — the same contract
``code_rendering.decode_codes_from_image`` applies to unreadable codes at
ingestion. A batch of five hundred submissions must not be lost to one bad
file.

This is safe for the transaction because extractors neither flush nor commit:
a failure is a Python-level one (an unreadable file, an impossible geometry)
raised BEFORE anything reached the database, so the session is never left in
the aborted state a failed flush would cause.

Writing SCANNED
---------------
The run is also what tells the review queue a submission is ready: at the end
of walking one submission, if its status is MATCHED and NO object on its
template-bound pages raised during THIS run, it moves to SCANNED. With zero
errors, every object of every bound page was either skipped (not applicable,
already extracted, not yet supported) or read — so everything readable has
been read, and a human can start reviewing.

  * One error anywhere in the submission keeps it MATCHED. The next run
    retries exactly that object (the others are already extracted) and, if it
    now succeeds, moves the submission then.
  * ``skipped_not_yet_supported`` does NOT block it: an object this build
    cannot read is not a failure, and holding the submission back for a
    future extractor would empty the review queue for no reason. Re-running
    after that extractor ships still picks the object up — status plays no
    part in eligibility.
  * A submission with no bound page at all moves too, vacuously: there was
    nothing to read, and its missing pages stay visible in the batch detail.
  * ONLY MATCHED moves. SCANNED is walked but its status is left alone, and
    REVIEWED / FINALIZED never reach the walk at all (below), nor do
    UNASSIGNED / MATCH_FAILED.
  * MATCHED as it is in the database WHEN the status is written, not as it was
    loaded. A run takes minutes and holds one transaction: meanwhile an
    operator may "Review anyway" the submission (→ SCANNED) or close it. So
    before writing, the status is re-read row-locked and SCANNED is written
    only over a MATCHED one; ``submissions_scanned`` counts only the moves the
    run actually made. The lock is held until the run commits, on submissions
    no operator can have in review (review needs SCANNED); a "Review anyway"
    arriving meanwhile waits, then finds SCANNED and refuses.

Closed submissions are not read
-------------------------------
A REVIEWED or FINALIZED submission is skipped by the walk entirely
(``submission_batch_repo.list_extractable_submissions``): a re-run must never
add readings to a submission a human has closed — a new reading there would
sit unreviewed behind a "reviewed" status, or change what was finalised. Not
counted in ``submissions_processed`` and its ``extraction_attempted_at`` is not
touched. Reopening brings it back to SCANNED first, after which a run reads
whatever it is missing.

The list is loaded once, at the start; a submission closed while the run reads
the ones before it would still be walked. So the walk re-reads each
submission's status as it reaches it and skips one closed meanwhile exactly as
if it had been closed at the start: not counted, nothing read, the attempt
time untouched. That re-read takes no lock — locking every submission walked
until the run commits would make every review save on them wait minutes — and
does not flush the work staged so far, for the same reason.
Known limitation, accepted: a submission closed while the run is reading IT (a
few seconds) may receive that walk's readings, and its
``extraction_attempted_at`` is written; its status stays closed (see "Writing
SCANNED"). With the recommended input, one PDF per submission, a SCANNED
submission has nothing left to read, so in practice there are no such
readings.

Matching never writes over SCANNED: it writes a status only when it resolves a
variant, and a SCANNED submission already has one.

Known limitation, accepted: a SCANNED submission that later GAINS a page (a
loose-image upload extending it) stays SCANNED until the next run extracts the
new page. The recommended input is one PDF per submission, where this cannot
happen.

No backfill: submissions extracted before this rule existed become SCANNED on
the next run, which is idempotent and reads nothing twice.

"Review anyway" is the other writer of SCANNED: an operator brings a MATCHED
submission into review with a PLACEHOLDER result (NULL payload) for each
object no run could read. A later run does NOT retry those objects — a
placeholder is a result, so the pair counts as ``skipped_already_extracted``.
That is intended: the operator chose to read them from the paper sheet, and a
run overwriting that choice behind their back would be worse than a reading
that never arrives.

Recording the attempt
---------------------
Every submission the walk reaches gets ``extraction_attempted_at`` set to now,
whatever the outcome — errors included, and even when nothing was left to
read. It means "the last time a run examined this submission", which is the
one thing a MATCHED status cannot say on its own: a submission never extracted
and one extracted with errors are both MATCHED, and only the second may be
reviewed anyway. Submissions the walk never reaches (MATCH_FAILED,
UNASSIGNED) are not touched. No backfill: a submission extracted before the
column existed gets a value on its next run.

It is set once the walk has ended, on every submission it examined, not as
each one is reached. The candidate flushes during the walk (below), and a
flush writes everything staged: an attempt time set on the way would write the
row of every SCANNED submission walked so far and hold it until the run
commits, and every review open or save on it would wait minutes.

The candidate
-------------
A respondent ID read with no need for review becomes the submission's
candidate: at the end of each submission the walk calls
``submission_candidate_service.apply_candidate_from_extraction``, which never
overwrites a candidate, never touches a closed submission or one a human has
already saved on, and reads EVERY respondent-ID reading of the submission —
not only those of this run — so a submission whose reading came from an
earlier run still gets its candidate without anything being read twice. For a
submission that may receive one it flushes (the query has to see what the walk
just staged) and, only when the reading does give a candidate, re-reads the
submission row-locked, so a review save made during a long run is never
overwritten. That flush writes the readings staged so far (rows no other
transaction sees) and rows the run holds locked anyway — the MATCHED → SCANNED
moves and the candidates set — and nothing else: the attempt times are set
after the walk. A submission whose respondent ID gives no candidate, or which
has none (an anonymous questionnaire), is never locked by the candidate rule.

Aligning the page
-----------------
Before reading the objects of a bound page, the run aligns it to its template
from the four printed marks (``page_alignment_service``): only when the page
has at least one object to read IN THIS RUN (extractable, not already
extracted) and has never been aligned (``alignment_status`` NULL). Computed
once and reused by every later run, so all the readings of a page stand on the
same image; a page already fully read is not touched, so a re-run over a
settled batch writes no page row. A page that cannot be aligned (NOT_ALIGNED)
is a normal outcome: its objects are read in proportion and carry
``sheet_not_aligned``, and the submission still moves to SCANNED. So is a
failure of the alignment on a readable scan (NOT_ALIGNED, reason
``ALIGNMENT_ERROR``): the extractors read that scan anyway. A failure to READ
the file is logged and leaves the status NULL: the extractors then fail on the
same file and are counted as errors, and the next run retries both. A page
ALIGNED whose straightened image is gone is aligned again. The VID plays no
part: matching decoded it at ingestion, on the whole image.

The page rows written here are those of pages extracted in this run, on
submissions no review save can write (review needs SCANNED, and no review
service writes ``submission_pages``); they stay locked until the run commits,
like the readings. Known, accepted: a loose-image upload EXTENDING a
submission the run is reading re-runs its matching, which may then wait for
the run on those rows (the recommended input is one PDF per submission).

Which objects are readable
--------------------------
:func:`is_extractable` is the eligibility rule the walk applies, made public so
the "unread objects" of a submission are computed by the same rule rather than
a copy of it: an object is extractable when it is not never evaluable and an
extractor resolves for it. :func:`result_type_for` is the result type such an
object's reading carries.

Transactions: this module does not commit. ``submission_batch_service``
.run_extraction_for_batch`` owns the transaction, so a whole run lands, or none
of it does. It also takes the batch for the run (``batch_operation_lock``)
before this module reads anything: the already-extracted pairs and the
threshold read at the start stay true for the whole run, because no second
run, "Review anyway" or threshold change can land on the batch meanwhile.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    CLOSED_SUBMISSION_STATUSES,
    PageObjectRole,
    PageObjectType,
    SubmissionResultType,
    SubmissionStatus,
)
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.repositories import submission_batch_repo, submission_object_result_repo
from app.services import (
    code_extraction_service,
    free_text_extraction_service,
    matrix_extraction_service,
    page_alignment_service,
    question_extraction_service,
    respondent_identifier_extraction_service as respondent_identifier_service,
    submission_candidate_service,
)

logger = logging.getLogger(__name__)


# Every extractor takes the same four arguments and returns the staged result:
#
#     (db, submission_page, page_object, batch) -> SubmissionObjectResult
#
# and neither flushes nor commits. Keeping the signature uniform is what lets
# the dispatch below be a plain dict lookup instead of a chain of ifs.
Extractor = Callable[
    [Session, SubmissionPage, PageObject, SubmissionBatch], SubmissionObjectResult
]

# The dispatch table. EXTEND THIS when the next extractor ships, and change
# nothing else in this module.
# A type absent from here is "not yet supported", which is counted and reported,
# never an error.
#
# A type present here is not on its own enough to say WHICH extractor reads an
# object: see ``_resolve_extractor``, which is what a ROLE can still redirect.
_EXTRACTORS: dict[PageObjectType, Extractor] = {
    PageObjectType.QUESTION: question_extraction_service.extract_question_result,
    PageObjectType.MATRIX: matrix_extraction_service.extract_matrix_result,
    PageObjectType.FREE_TEXT_AREA: free_text_extraction_service.extract_free_text_result,
    PageObjectType.CODE: code_extraction_service.extract_code_result,
}

# How many failures the summary names individually. A run over a broken batch
# could produce thousands, and neither an HTTP response nor an audit row is the
# place for all of them; ``errors`` always reports the true total.
MAX_ERROR_DETAILS = 20


@dataclass
class ExtractionRunSummary:
    """What one run actually did to one batch.

    Counts, not ids — a batch holds hundreds of submissions — with the single
    exception of ``error_details``, where the coordinates ARE the useful part.
    """

    # Submissions eligible for extraction and therefore examined: variant
    # matched, and neither MATCH_FAILED, UNASSIGNED nor closed (REVIEWED /
    # FINALIZED) — see ``submission_batch_repo.list_extractable_submissions``.
    # One closed while the run was going is not counted either.
    submissions_processed: int = 0
    # Sheets of those submissions that are bound to a template page. A sheet
    # matching left unbound is not counted: nothing was examined on it.
    pages_processed: int = 0
    # Results actually created by THIS run — strictly what changed, so a re-run
    # over a settled batch honestly reports 0.
    results_created: int = 0
    # Objects an earlier run had already read. The idempotency counter.
    skipped_already_extracted: int = 0
    # Objects that are not answers and never will be (TEXT, IMAGE, a CODE with a
    # scanning role).
    skipped_not_applicable: int = 0
    # Objects that ARE answers but have no extractor in this build yet. Today
    # none: every answer type has one. Kept for the next object type that
    # ships without an extractor. Not an error.
    skipped_not_yet_supported: int = 0
    # Objects whose extraction raised. Never fatal to the run.
    errors: int = 0
    # The first MAX_ERROR_DETAILS of them, as
    # ``{"submission_page_id", "page_object_id", "error"}``.
    error_details: list[dict] = field(default_factory=list)
    # Submissions moved MATCHED → SCANNED by THIS run (see "Writing SCANNED" in
    # the module docstring). Strictly what changed, like ``results_created``:
    # a re-run over a settled batch reports 0.
    submissions_scanned: int = 0
    # Submissions whose candidate THIS run set from their respondent-ID reading
    # (see "The candidate" in the module docstring). Recorded in the run's
    # audit event only: not part of the API response.
    candidates_set: int = 0


def _is_never_evaluable(page_object: PageObject) -> bool:
    """Whether *page_object* carries no answer to read, now or ever.

    TEXT and IMAGE are decoration: they are printed for the candidate to read,
    not filled in. A CODE holding a scanning role (VARIANT_IDENTIFIER /
    RESPONDENT_IDENTIFIER) is not decoration but is already consumed IN FULL by
    matching and identification: whatever it had to say was read at ingestion
    and written onto the Submission itself, so extracting it again would store
    a second, divergeable copy of an answered question. That holds for every
    LINKED copy of a shared one too — the VID replicated on each page — which
    carries the group's role though its binding sits on one copy only
    (``PageObject.effective_role``).

    A CODE with NO role is a different thing — a printed value that is part of
    the form's content — and falls through to dispatch, where
    ``code_extraction_service`` reads it, rather than being dismissed here.
    """
    if page_object.object_type in (PageObjectType.TEXT, PageObjectType.IMAGE):
        return True
    if (
        page_object.object_type is PageObjectType.CODE
        and page_object.effective_role is not None
    ):
        return True
    return False


def _resolve_extractor(page_object: PageObject) -> Extractor | None:
    """Which extractor reads *page_object*, if any — type AND role.

    A MATRIX holding the RESPONDENT_IDENTIFIER role is a respondent-ID grid:
    its cells spell a candidate's id and carry no ``Validity``, so it must NOT
    be handed to the evaluable-MATRIX extractor even though MATRIX is in the
    dispatch table. This is why dispatch is not a plain type lookup: the role
    routes it to ``respondent_identifier_extraction_service`` instead, so it
    is read, not skipped.

    A FREE_TEXT_AREA holding RESPONDENT_IDENTIFIER (a role allowed on it, not
    only on CODE/MATRIX) is NOT redirected: it goes to the ordinary OCR
    extractor and yields a FREE_TEXT reading. An OCR "answer" holding an
    identity is safe there because review is where it goes — free text is
    always reviewed (ALWAYS_REVIEW), the operator confirms or corrects the
    identifier there, and that sets the submission's candidate
    (``review_action_service``). A handwritten identity is never attributed
    unseen.
    """
    if (
        page_object.object_type is PageObjectType.MATRIX
        and page_object.effective_role is PageObjectRole.RESPONDENT_IDENTIFIER
    ):
        return respondent_identifier_service.extract_respondent_identifier_result
    return _EXTRACTORS.get(page_object.object_type)


def is_extractable(page_object: PageObject) -> bool:
    """Whether a run would READ *page_object* — the walk's eligibility rule.

    Not never evaluable (:func:`_is_never_evaluable`) and an extractor resolves
    for it (:func:`_resolve_extractor`). An object failing the first is not an
    answer; one failing the second is an answer this build cannot read yet
    ("not yet supported"), which never held a submission back and so is not
    "unread" either. Reads ``effective_role``: ``role_binding`` and
    ``group_role_bindings`` are best loaded with the object (they load lazily
    otherwise — right, one query each).
    """
    return not _is_never_evaluable(page_object) and _resolve_extractor(page_object) is not None


# The result type a reading of each extractable object type carries. A MATRIX
# holding RESPONDENT_IDENTIFIER is the exception, handled in result_type_for.
_RESULT_TYPES: dict[PageObjectType, SubmissionResultType] = {
    PageObjectType.QUESTION: SubmissionResultType.QUESTION,
    PageObjectType.MATRIX: SubmissionResultType.MATRIX,
    PageObjectType.FREE_TEXT_AREA: SubmissionResultType.FREE_TEXT,
    PageObjectType.CODE: SubmissionResultType.CODE,
}


def result_type_for(page_object: PageObject) -> SubmissionResultType:
    """The ``result_type`` a reading of *page_object* carries.

    The same routing as :func:`_resolve_extractor`, stated as the type each
    extractor writes: a MATRIX holding RESPONDENT_IDENTIFIER is a
    respondent-ID grid (RESPONDENT_IDENTIFIER), any other MATRIX an evaluable
    one (MATRIX); QUESTION, FREE_TEXT_AREA (FREE_TEXT, with or without the
    RESPONDENT_IDENTIFIER role) and a role-less CODE map one to one.
    Used to type a placeholder ("Review anyway") exactly as the
    reading it stands in for would have been typed, so review and scoring
    treat both alike. Only meaningful for an extractable object; raises
    otherwise, since there is no reading for it to stand in for.
    """
    if not is_extractable(page_object):
        raise ValueError(f"PageObject {page_object.id} is not extractable")
    if (
        page_object.object_type is PageObjectType.MATRIX
        and page_object.effective_role is PageObjectRole.RESPONDENT_IDENTIFIER
    ):
        return SubmissionResultType.RESPONDENT_IDENTIFIER
    return _RESULT_TYPES[page_object.object_type]


def _extract_one(
    db: Session,
    submission_page: SubmissionPage,
    page_object: PageObject,
    batch: SubmissionBatch,
    extractor: Extractor,
    summary: ExtractionRunSummary,
) -> bool:
    """Run one extractor, converting any failure into a counted error.

    Returns True when a result was staged. The ``except Exception`` is as broad
    as it looks and is meant to be: the failure modes are open-ended (a missing
    or truncated file, an image library refusing a format, geometry that maps
    outside the sheet) and NONE of them is a reason to abandon the other
    submissions of the batch.
    """
    try:
        extractor(db, submission_page, page_object, batch)
    except Exception as exc:  # noqa: BLE001 — one bad object never stops the run
        summary.errors += 1
        logger.warning(
            "Extraction failed for page_object %s on submission_page %s",
            page_object.id,
            submission_page.id,
            exc_info=True,
        )
        if len(summary.error_details) < MAX_ERROR_DETAILS:
            summary.error_details.append(
                {
                    "submission_page_id": str(submission_page.id),
                    "page_object_id": str(page_object.id),
                    # The message, not the traceback: the response and the audit
                    # row want to say WHAT failed; the traceback is in the log.
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        return False
    summary.results_created += 1
    return True


def run_extraction_for_batch(
    db: Session, batch_id: uuid.UUID
) -> ExtractionRunSummary:
    """Extract every readable, not-yet-extracted answer in *batch_id*.

    Walks the eligible submissions (see
    ``submission_batch_repo.list_extractable_submissions`` — never a REVIEWED
    or FINALIZED one, see "Closed submissions are not read"), then each of their
    sheets that matching bound to a template page, then every PageObject
    authored on that template page — and for each object, in this order:

      1. never evaluable (TEXT / IMAGE / a role-carrying CODE) → counted as
         ``skipped_not_applicable``;
      2. already has a result for this (sheet, object) → counted as
         ``skipped_already_extracted``;
      3. no extractor resolves for it in this build — no entry for its type
         (:func:`_resolve_extractor`) → counted as
         ``skipped_not_yet_supported``;
      4. otherwise extracted, with any failure counted and stepped over.

    The order matters for the counters to mean anything: an already-extracted
    TEXT object cannot exist, but an already-extracted MATRIX object does, and
    it must count as done rather than as pending.

    A submission closed since the list was loaded is skipped ("Closed
    submissions are not read"). After each submission is walked, one still
    MATCHED in the database that raised no error in this run is moved to
    SCANNED — see "Writing SCANNED" in the module docstring — and its
    respondent-ID reading may set its candidate ("The candidate").

    Does NOT commit — the caller owns the transaction (see
    ``submission_batch_service.run_batch_extraction``), so a run lands whole or
    not at all.
    """
    summary = ExtractionRunSummary()

    batch = submission_batch_repo.get(db, batch_id)
    if batch is None:
        # Defensive only: the caller resolves and authorises the batch first.
        return summary

    # The batch's whole already-extracted set, in one query rather than one per
    # object. Newly staged pairs are added to it as the run goes, so the walk
    # stays correct without a flush.
    already_extracted = submission_object_result_repo.existing_object_pairs_for_batch(
        db, batch_id
    )

    submissions = submission_batch_repo.list_extractable_submissions(db, batch_id)
    # One instant for the whole run: every submission it examined, examined
    # "now".
    attempted_at = datetime.now(timezone.utc)

    # The submissions examined, whatever happens below; their attempt time is
    # set once the walk ends (see "Recording the attempt").
    walked: list[Submission] = []

    for submission in submissions:
        if _closed_since_loaded(db, submission):
            continue
        summary.submissions_processed += 1
        walked.append(submission)
        errors_before = summary.errors
        for page in submission.pages:
            if page.form_variant_page_id is None:
                # Matching examined this sheet and could not place it. There is
                # no template geometry to read it against; its siblings are
                # unaffected.
                continue
            summary.pages_processed += 1
            # Before its objects, once per page (see "Aligning the page").
            _align_if_needed(page, already_extracted)

            for page_object in page.form_variant_page.page_objects:
                if _is_never_evaluable(page_object):
                    summary.skipped_not_applicable += 1
                    continue

                pair = (page.id, page_object.id)
                if pair in already_extracted:
                    summary.skipped_already_extracted += 1
                    continue

                extractor = _resolve_extractor(page_object)
                if extractor is None:
                    summary.skipped_not_yet_supported += 1
                    continue

                if _extract_one(db, page, page_object, batch, extractor, summary):
                    already_extracted.add(pair)

        _mark_scanned_if_complete(db, submission, summary, errors_before)
        # See "The candidate": flushes and locks only when it may set one.
        if submission_candidate_service.apply_candidate_from_extraction(
            db, submission
        ):
            summary.candidates_set += 1

    # Not before: a flush during the walk would write these rows, and hold
    # them, until the run commits.
    for submission in walked:
        submission.extraction_attempted_at = attempted_at

    return summary


def _align_if_needed(
    page: SubmissionPage, already_extracted: set[tuple[uuid.UUID, uuid.UUID]]
) -> None:
    """Align *page* to its template when this run will read an object on it
    and it needs it — never aligned, or its straightened image gone (see
    "Aligning the page", ``page_alignment_service.needs_alignment``)."""
    if not page_alignment_service.needs_alignment(page):
        return
    if not any(
        (page.id, obj.id) not in already_extracted and is_extractable(obj)
        for obj in page.form_variant_page.page_objects
    ):
        return
    try:
        page_alignment_service.align_page(page)
    except Exception:  # noqa: BLE001 — only an unreadable scan gets here
        logger.warning(
            "Scan unreadable for alignment, submission_page %s",
            page.id,
            exc_info=True,
        )


def _closed_since_loaded(db: Session, submission: Submission) -> bool:
    """Whether *submission* has been closed since the run loaded it (see
    "Closed submissions are not read"): its status read from the database,
    without a lock and without flushing what the run has staged so far."""
    with db.no_autoflush:
        status = db.scalar(
            select(Submission.status).where(Submission.id == submission.id)
        )
    return status in CLOSED_SUBMISSION_STATUSES


def _mark_scanned_if_complete(
    db: Session,
    submission: Submission,
    summary: ExtractionRunSummary,
    errors_before: int,
) -> None:
    """Move a fully-read MATCHED submission to SCANNED (see the module
    docstring).

    *errors_before* is ``summary.errors`` as it stood before the walk reached
    this submission, so the difference is exactly the errors raised on ITS
    objects in this run. The copy loaded at the start can only be stale
    towards a status written since, so it is re-read, row-locked, only when it
    still says MATCHED and the walk raised nothing. Staged only: the
    caller owns the commit.
    """
    if submission.status is not SubmissionStatus.MATCHED:
        return
    if summary.errors != errors_before:
        return
    # Without flushing what the run has staged so far, like
    # ``_closed_since_loaded``: whatever the session's autoflush setting, only
    # THIS row is locked here.
    with db.no_autoflush:
        db.refresh(submission, attribute_names=["status"], with_for_update=True)
    if submission.status is not SubmissionStatus.MATCHED:
        return
    submission.status = SubmissionStatus.SCANNED
    summary.submissions_scanned += 1
