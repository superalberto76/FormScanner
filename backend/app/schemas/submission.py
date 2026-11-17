# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import (
    MatchFailureReason,
    PageAlignmentStatus,
    PageObjectRole,
    PageObjectType,
    PageOrderSource,
    ReviewOutcome,
    SubmissionResultType,
    SubmissionStatus,
)


# ---------------------------------------------------------------------------
# SubmissionBatch
# ---------------------------------------------------------------------------


class SubmissionBatchBase(BaseModel):
    name: str
    description: str | None = None


class SubmissionBatchCreate(SubmissionBatchBase):
    # form_project_id from URL path; created_by_user_id from auth.
    #
    # Optional confidence-threshold override for this batch.
    # Omitting it — or sending null — is not "no threshold" but "use the system
    # default", which is why None is the default and is stored verbatim.
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)


class ConfidenceThresholdUpdate(BaseModel):
    """Body of the PATCH that moves a batch's confidence threshold.

    ``None`` is a meaningful value here, not an omission: it CLEARS the override
    and puts the batch back on the system default. There is therefore no way to
    say "leave it alone" through this endpoint — the caller is asking for the
    threshold to become exactly what it sends.
    """

    confidence_threshold: float | None = Field(default=None, ge=0, le=1)


class SubmissionBatchRead(SubmissionBatchBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_project_id: uuid.UUID
    created_by_user_id: uuid.UUID
    created_at: datetime
    # This batch's own override, exactly as stored: None means "follow the
    # system default". Deliberately NOT range-validated on the way out — a Read
    # must be able to report whatever is in the row, not refuse to.
    confidence_threshold: float | None = None
    # …and the threshold that therefore actually applies, resolved through
    # ``confidence_threshold_service.effective_threshold``. Reported alongside
    # the raw override because the caller cannot derive it: the system default
    # is server-side configuration it never sees.
    effective_threshold: float


class SubmissionBatchListItem(SubmissionBatchRead):
    """One batch as listed under its project, with its metrics.

    The entry point of the Scanning tab: enough to tell, per batch, what is
    left to do without opening it. Every metric is computed on each read and
    never stored — the statuses and ``review_required`` it derives from move
    under it (matching re-runs, threshold changes), and a live count follows
    them for free where a persisted one would have to be kept in step.
    """

    # Submissions in the batch, whatever their status.
    total_submissions: int
    # …of which the matching engine could resolve no variant for.
    match_failed_count: int
    # …of which are ungroupable sheets awaiting a human decision. A per-BATCH
    # count of submissions currently UNASSIGNED — not to be confused with
    # ``SubmissionBatchIngestResult.unassigned_count``, which counts only the
    # singletons ONE upload call opened.
    unassigned_count: int
    # …of which an operator marked unresolvable: closed, not
    # counted in ``reviewed_count``, not blocking the export.
    unresolvable_count: int
    # …of which a human has closed: REVIEWED or FINALIZED.
    reviewed_count: int
    # Results still awaiting a human (review_required and not resolved, i.e.
    # review_outcome NULL), counted only in submissions NOT yet closed: once a
    # submission is REVIEWED/FINALIZED, whatever it still flags is no longer
    # work to do.
    pending_review_count: int
    # Whether the batch could be exported now: at least one submission, and none
    # still in a status that blocks export. An empty batch is NOT ready.
    ready_for_export: bool


class ExportReadiness(BaseModel):
    """Whether a batch can be exported now and, if not, why.

    Computed on every read from the submissions' statuses
    (``submission_batch_service.export_readiness``), never stored. ``ready`` is
    the same rule as ``SubmissionBatchListItem.ready_for_export``. The counts
    are the submissions holding the batch back, by what they still need; all
    zero when it is ready, and also when it is empty or holds UNRESOLVABLE
    submissions only — not ready, though nothing in it is in progress. Also the
    extras of the structured 409 ``NOT_EXPORT_READY`` (without ``ready``).
    """

    ready: bool
    # UPLOADED or MATCHED: not extracted yet ("Run extraction").
    to_extract: int
    # SCANNED: extracted, not closed by a review yet.
    in_review: int
    # MATCH_FAILED: no variant yet (assign one, or mark unresolvable).
    to_assign: int
    # UNASSIGNED: an ungroupable sheet (mark unresolvable).
    unassigned: int


class ExportFormat(str, enum.Enum):
    """File format of a batch results export."""

    XLSX = "xlsx"
    CSV = "csv"


class CsvDialect(str, enum.Enum):
    """How a CSV export writes its cells, chosen at export time.

    COMMA: ``,`` between fields and ``.`` as the decimal mark, the standard
    form other tools import. SEMICOLON: ``;`` and ``,``, the form an Italian
    Excel opens straight into columns. Both UTF-8 with a BOM.
    """

    COMMA = "comma"
    SEMICOLON = "semicolon"


class SubmissionBatchDetail(SubmissionBatchRead):
    """A batch plus its submissions — the minimal read-back after ingestion.

    Not the review page (``SubmissionReviewDetail``): each submission is
    summarised by its page count only, with no page payloads.
    """

    # Over the whole batch, whatever status filter narrowed ``submissions``.
    export_readiness: ExportReadiness

    # Forward reference: SubmissionSummary is defined below, resolved by the
    # model_rebuild() call at the end of this module.
    submissions: list["SubmissionSummary"] = []


# ---------------------------------------------------------------------------
# Submission (one candidate's whole response: 1 candidate, 1 FormVariant, N pages)
# ---------------------------------------------------------------------------


class SubmissionBase(BaseModel):
    # Human/respondent identifier resolved from the code or review; may be set
    # at creation or left for the pipeline to fill in.
    respondent_identifier: str | None = None


class SubmissionCreate(SubmissionBase):
    # submission_batch_id comes from the URL path param; physical pages are
    # uploaded separately as SubmissionPage rows.
    matched_form_variant_id: uuid.UUID | None = None


class SubmissionRead(SubmissionBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_batch_id: uuid.UUID
    # Stays None until the matching engine resolves the variant.
    matched_form_variant_id: uuid.UUID | None = None
    status: SubmissionStatus
    # Why matching left it without a variant; null once resolved, and on a
    # MATCH_FAILED submission matched before the reason was recorded.
    match_failure_reason: MatchFailureReason | None = None
    # With VID_NOT_RECOGNISED only: the identifier value read.
    match_failure_code_value: str | None = None
    # On an UNRESOLVABLE submission, the status it was marked from and "Undo"
    # returns it to: MATCH_FAILED or UNASSIGNED. Null on every other status.
    unresolvable_from_status: SubmissionStatus | None = None
    total_score: float | None = None
    created_at: datetime
    updated_at: datetime
    # Forward reference: SubmissionPageRead is defined below, resolved by the
    # model_rebuild() call at the end of this module.
    pages: list["SubmissionPageRead"] = []


class SubmissionVariantAssign(BaseModel):
    """Manual variant assignment for a MATCH_FAILED submission.

    The operator's answer to the question Level 1 could not: which printable
    variant these sheets are. Nothing else is asked — the page bindings are then
    derived by the engine's Level 2, exactly as they would have been on an
    automatic match.
    """

    form_variant_id: uuid.UUID


class RejectedImage(BaseModel):
    """One uploaded file that could not be ingested, and why.

    A rejection is per-FILE, never per-request: the other images in the same
    upload are ingested regardless. Reported explicitly because the counters
    alone cannot distinguish "you sent 4 files" from "one of your 5 was
    unreadable" — a silent shortfall is exactly what an operator must not get.
    """

    filename: str
    reason: str


class SubmissionBatchIngestResult(BaseModel):
    """Outcome of one loose-image upload call.

    Deliberately about the CALL rather than about one submission: unlike a PDF
    upload (one file = one Submission = one response), a single image upload can
    open several submissions, extend others opened by earlier calls, and reject a
    file or two along the way.
    """

    # Submissions this call opened, and submissions that already existed in the
    # batch and gained pages from it (cross-call merging). Disjoint by construction.
    submissions_created: list[uuid.UUID] = []
    submissions_updated: list[uuid.UUID] = []
    # SubmissionPage rows actually created (i.e. files ingested).
    pages_added: int = 0
    # How many of submissions_created are UNASSIGNED singletons — images that
    # could be grouped by neither a RID nor their filename.
    unassigned_count: int = 0
    # Files that were not ingested at all. Empty on a fully successful call.
    rejected: list[RejectedImage] = []


class SubmissionBatchMatchingResult(BaseModel):
    """Outcome of one manual matching re-run over a batch.

    About the CALL, in the same spirit as SubmissionBatchIngestResult: a re-run
    walks every submission of the batch that matching had not fully resolved.

    ``matched + match_failed == submissions_processed`` — the first two describe
    the state each examined submission was LEFT in, so one that was already
    matched and merely had a page left to bind counts as matched. Counts, not
    ids: a batch holds hundreds of submissions and the detail endpoint already
    lists them with their ``matched_form_variant_id``.
    """

    # Submissions this run examined (i.e. that were not already fully resolved).
    submissions_processed: int = 0
    # …of which left WITH a variant resolved, and left WITHOUT one.
    matched: int = 0
    match_failed: int = 0
    # SubmissionPage rows that gained a form_variant_page_id in THIS run.
    # Strictly what changed, so a re-run that finds nothing new reports 0.
    pages_resolved: int = 0


class ExtractionRunSummary(BaseModel):
    """Outcome of one manual extraction run over a batch.

    Same spirit as SubmissionBatchMatchingResult: about the CALL, in counts, not
    ids. The three "skipped" buckets are kept apart because they mean three
    different things to whoever pressed the button — nothing to wait for, not
    supported by this build yet, and already done — and collapsing them would make
    the number unreadable.
    """

    # Submissions examined: variant matched, and neither MATCH_FAILED nor
    # UNASSIGNED (an ungroupable sheet can be variant-matched and still must not
    # have answers attributed to it), nor closed by a human (REVIEWED /
    # FINALIZED: a run never adds readings to those).
    submissions_processed: int = 0
    # …and, of their sheets, those matching bound to a template page.
    pages_processed: int = 0
    # Results created by THIS run. Strictly what changed, so a re-run over a
    # settled batch reports 0.
    results_created: int = 0
    # Objects an earlier run had already read; they are never re-read.
    skipped_already_extracted: int = 0
    # Objects that carry no answer at all (TEXT, IMAGE, a CODE holding a
    # scanning role, which matching already consumed in full).
    skipped_not_applicable: int = 0
    # Objects that DO carry an answer this build cannot read yet. Every answer
    # type has an extractor (a FREE_TEXT_AREA holding RESPONDENT_IDENTIFIER
    # included), so this is 0 in practice; kept as the counter a future object
    # type with no extractor lands in. Not an error.
    skipped_not_yet_supported: int = 0
    # Objects whose extraction raised. One bad sheet never stops the run.
    errors: int = 0
    # The first few of them by coordinate, as
    # ``{"submission_page_id", "page_object_id", "error"}``. Capped; ``errors``
    # is always the true total.
    error_details: list[dict] = Field(default_factory=list)
    # Submissions this run moved from MATCHED to SCANNED — "extracted, ready
    # for review": every object on their bound pages was read or
    # skipped, none raised. Strictly what changed, so a re-run over a settled
    # batch reports 0.
    submissions_scanned: int = 0


class SubmissionLockInfo(BaseModel):
    """An ACTIVE soft lock on a submission: who holds it, and since when they
    were last active on it.

    Only ever built for a lock that has not expired — an expired lock is
    reported as no lock at all (null), never as this with an old timestamp.
    ``display_name`` and never the email: the project roster deliberately keeps
    members' emails out of sight, and the lock must not leak them back in.
    """

    user_id: uuid.UUID
    display_name: str
    # The holder's LAST activity (open, renewal or review save), not when they
    # first opened the submission. The FE derives "N min ago" from it.
    locked_at: datetime


class SubmissionLockState(BaseModel):
    """Outcome of acquiring or taking over a submission's soft lock.

    ``lock`` is the active lock AFTER the call: the caller's own when
    ``acquired``, otherwise the other operator's who holds it — exactly what
    the FE needs for "In review by X, N min ago" and its Take over button.
    """

    acquired: bool
    lock: SubmissionLockInfo | None = None
    # The version the caller must declare on its next review save. Reported
    # here, at open time, because that is the state the review screen shows.
    review_version: int


class ReviewAnywayResult(BaseModel):
    """Outcome of "Review anyway" on one submission."""

    # Always SCANNED on success.
    status: SubmissionStatus
    # Placeholder results created, one per unread object. 0 when a run read
    # everything in the meantime: the submission is still moved to SCANNED.
    placeholders_created: int


# The column size of ``submissions.respondent_identifier``.
RESPONDENT_IDENTIFIER_MAX_LENGTH = 500


class SubmissionCandidateUpdate(BaseModel):
    """Body of the manual candidate edit — a review save.

    ``respondent_identifier`` is trimmed; blank becomes null (clearing the
    candidate). ``review_version`` is the version the caller's screen was built
    on, checked by the review-save guard (``guard_review_save``).
    """

    respondent_identifier: str | None
    review_version: int

    @field_validator("respondent_identifier")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        # Trimmed BEFORE the length check: surrounding blanks are a typing
        # accident, not part of the identifier, and must not push a valid value
        # over the limit.
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > RESPONDENT_IDENTIFIER_MAX_LENGTH:
            raise ValueError(
                f"The candidate identifier is at most "
                f"{RESPONDENT_IDENTIFIER_MAX_LENGTH} characters."
            )
        return value


class SubmissionCandidateState(BaseModel):
    """What a candidate edit leaves behind: the stored value, the version to
    declare on the next save, and the lock — now the caller's — so the FE
    moves on without a refetch."""

    respondent_identifier: str | None = None
    review_version: int
    lock: SubmissionLockInfo | None = None


class SubmissionSummary(BaseModel):
    """One submission as listed inside a batch detail — no page payloads, just a
    count, which is all the minimal verification endpoint needs."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: SubmissionStatus
    matched_form_variant_id: uuid.UUID | None = None
    page_count: int
    created_at: datetime
    # Results of this submission still awaiting a human: review_required and
    # not resolved (review_outcome NULL). Computed on every read, never stored,
    # so it always reflects the batch's current threshold. 0 for a submission
    # with nothing to review, including one never extracted. Counted in every
    # status: on a closed submission these are the results left unresolved at
    # closing, not work to do.
    pending_review_count: int = 0
    # The raw candidate identifier stored on the submission, verbatim; null
    # when none was read yet.
    respondent_identifier: str | None = None
    # 1-based template page indices of the matched variant that no sheet of
    # this submission is bound to, sorted. Same meaning as the
    # ``Submission.missing_page_indices`` property, computed in batch rather
    # than per row. [] when no variant is matched: with no template there is
    # nothing to be missing.
    missing_page_indices: list[int] = Field(default_factory=list)
    # Whether the matched variant carries a RESPONDENT_IDENTIFIER role binding,
    # i.e. whether a candidate id is expected at all. False with no variant.
    # The queue shows "No candidate" when this is true and
    # ``respondent_identifier`` is null — a variant without the role has no
    # candidate to miss.
    respondent_identifier_expected: bool = False
    # Who is reviewing this submission right now, null when nobody —
    # including when a lock is still in the row but has expired. Not
    # ``review_version``: that belongs to the submission page, not the queue.
    lock: SubmissionLockInfo | None = None
    # Whether an extraction run has ever examined this submission.
    # Tells "never extracted" from "extracted, with errors" — both MATCHED.
    extraction_attempted: bool = False
    # Objects of its bound sheets that a run could read but has no result for
    # — what keeps it MATCHED. Computed ONLY for a MATCHED submission a run has
    # examined, 0 for every other one. The FE offers "Review anyway" when the
    # status is MATCHED, extraction_attempted is true and this is above 0.
    unread_object_count: int = 0
    # Same meaning as on ``SubmissionRead``: shown in the queue on
    # MATCH_FAILED and UNRESOLVABLE rows.
    match_failure_reason: MatchFailureReason | None = None
    match_failure_code_value: str | None = None
    # Same meaning as on ``SubmissionRead``.
    unresolvable_from_status: SubmissionStatus | None = None


# ---------------------------------------------------------------------------
# Submission review page — one submission, as an operator reviews it
# ---------------------------------------------------------------------------


class SubmissionReviewTemplatePage(BaseModel):
    """The template page a sheet was bound to, with its size.

    The size is what turns an object's ``geometry`` (template-page pixels) into
    percentages over the sheet's image. Either dimension may be
    null — a page authored before any background was rendered — and the FE
    then falls back to A4.
    """

    id: uuid.UUID
    # 1-based, like FormVariantPage.page_index.
    page_index: int
    width_px: int | None = None
    height_px: int | None = None


class SubmissionReviewGeometry(BaseModel):
    """A PageObject's rectangle, verbatim: template-page pixels, top-left
    origin. Not scaled to the scan — that mapping is the FE's."""

    x: float
    y: float
    width: float
    height: float


class SubmissionReviewObject(BaseModel):
    """One reading that needed a human, with why.

    Only results flagged ``review_required`` are listed; auto-accepted ones are
    not. ``resolved`` says whether a human has already ruled on it.
    """

    result_id: uuid.UUID
    page_object_id: uuid.UUID
    object_type: PageObjectType
    result_type: SubmissionResultType
    role: PageObjectRole | None = None
    label: str | None = None
    # The question's text for a QUESTION, null for every other type.
    question_text: str | None = None
    geometry: SubmissionReviewGeometry
    # The extractor's payload, verbatim; its shape depends on ``result_type``
    # (see ``app.schemas.submission_result``). Null for a PLACEHOLDER ("Review
    # anyway"): nothing was read, the answer comes from paper.
    detected_payload: dict[str, Any] | None = None
    confidence_score: float | None = None
    # Why the reading was flagged — codes from ``submission_review_service``
    # (LOW_CONFIDENCE, INVALID_ANSWER, ALWAYS_REVIEW, DECODE_FAILED,
    # AMBIGUOUS_DIGITS, MISSING_DIGITS, UNREADABLE_GRID, EXTRACTION_FAILED), derived
    # on read from the persisted axes and the batch's CURRENT threshold. Never
    # empty for an unresolved result; may be empty for a resolved one, whose
    # ``review_required`` is frozen at review time while the threshold moved.
    flag_reasons: list[str] = Field(default_factory=list)
    # Whether a human has ruled on it: ``review_outcome`` is set.
    # False again after an UNDO.
    resolved: bool
    # --- The CURRENT ruling; all null while unresolved -----------------------
    review_outcome: ReviewOutcome | None = None
    # The value a SUBSTITUTE entered, in the reviewed-payload shape of
    # ``app.schemas.submission_result``; null for any other outcome.
    reviewed_payload: dict[str, Any] | None = None
    # The note of the current ruling. Earlier notes stay in the review log,
    # which the API does not expose.
    review_note: str | None = None
    reviewed_by: "SubmissionReviewer | None" = None
    reviewed_at: datetime | None = None
    # The rules of ``review_rules``, applied by the server so the FE never
    # re-derives them. ``confirmable``: CONFIRM is allowed (otherwise only
    # SUBSTITUTE or FLAG). ``bulk_acceptable``: "Accept all remaining" would
    # confirm this reading if it were unresolved — a rule on the reading,
    # reported for resolved results too.
    confirmable: bool = False
    bulk_acceptable: bool = False
    # Whether CONFIRM / SUBSTITUTE of this reading sets the submission's
    # candidate: true only for the first identity reading in physical page
    # order (``review_rules.first_identifier_reading``). False for a
    # later copy or duplicate of the identifier, and for every answer.
    sets_candidate: bool = False


class SubmissionReviewPage(BaseModel):
    """One physical sheet of the submission, placed or not."""

    id: uuid.UUID
    source_page_order: int
    page_order_source: PageOrderSource
    # ALIGNED, NOT_ALIGNED, or None (never aligned: read before sheet
    # alignment was in place, or a scan that could not be read). The FE warns on
    # NOT_ALIGNED and keys the sheet image on it; which file is served is the
    # server's rule (``get_page_image``), so no path is exposed.
    # No default: the one builder (``_review_page``) always passes it, and a
    # new one that forgot it should fail at once rather than say "never aligned".
    alignment_status: PageAlignmentStatus | None
    # None for a sheet matching could not place.
    template_page: SubmissionReviewTemplatePage | None = None
    # Its flagged readings, in reading order (y, then x). Empty for an
    # unplaced sheet: nothing is extracted from a sheet with no template.
    objects: list[SubmissionReviewObject] = Field(default_factory=list)


class SubmissionScoreRead(BaseModel):
    """The score of a submission as a triple, from
    ``scoring_rules.submission_score``.

    ``percentage`` is 0–100 (negative with a negative total), null when
    the variant's maximum is 0. Floats as authored: rounding is the display's.
    """

    raw_score: float
    max_possible_score: float
    percentage: float | None = None


class SubmissionReviewDetail(BaseModel):
    """Everything the submission review page shows for one submission.

    A pure read, available in EVERY status: a closed submission opens read-only,
    and which statuses the FE links here is its decision. Reading it neither
    takes nor renews the soft lock — the FE calls ``POST /lock`` for that.
    """

    id: uuid.UUID
    submission_batch_id: uuid.UUID
    batch_name: str
    form_project_id: uuid.UUID
    status: SubmissionStatus
    created_at: datetime
    matched_form_variant_id: uuid.UUID | None = None
    variant_name: str | None = None
    # Why matching failed: as on ``SubmissionSummary``.
    match_failure_reason: MatchFailureReason | None = None
    match_failure_code_value: str | None = None
    # Same meaning as on ``SubmissionSummary`` (the batch queue).
    respondent_identifier: str | None = None
    respondent_identifier_expected: bool = False
    # The sheet holding the submission's first identity reading in physical
    # page order, the only one whose CONFIRM / SUBSTITUTE sets the candidate;
    # listed in review or not. Null when no reading holds an identity.
    candidate_reading_page_id: uuid.UUID | None = None
    missing_page_indices: list[int] = Field(default_factory=list)
    # The ACTIVE soft lock only; an expired one is null, as in the queue.
    lock: SubmissionLockInfo | None = None
    review_version: int
    # The batch's threshold in force, for the "why" text of LOW_CONFIDENCE.
    effective_threshold: float
    # Readings listed below, and those of them not resolved yet.
    review_count: int
    pending_review_count: int
    # Those unresolved readings "Accept all remaining" would confirm. 0 means
    # the button has nothing to do.
    bulk_acceptable_count: int = 0
    # The score triple, computed on the fly at every read and so after every
    # review action. Null outside SCANNED / REVIEWED / FINALIZED (an
    # unextracted submission has no score), with no
    # variant, or when a broken invariant stops the scoring (the read itself
    # never fails for it).
    score: SubmissionScoreRead | None = None
    # EVERY sheet, in (source_page_order, id) order.
    pages: list[SubmissionReviewPage] = Field(default_factory=list)


class SubmissionReviewer(BaseModel):
    """Who made a reading's current ruling. The display name, never
    the email — the same rule as the lock holder."""

    user_id: uuid.UUID
    display_name: str


class SubmissionAcceptRemainingResult(SubmissionReviewDetail):
    """Outcome of "Accept all remaining": the whole review page
    after it, plus how many readings it confirmed (possibly 0)."""

    accepted_count: int


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------

# The column size of ``review_note`` (on the result and on the log).
REVIEW_NOTE_MAX_LENGTH = 2000


class ReviewActionType(str, enum.Enum):
    """What an operator does to one reading. API-only: what is
    stored is ``ReviewOutcome`` on the result and ``ReviewAction`` on the log.

      * CONFIRM    — the reading is right;
      * SUBSTITUTE — the reading is wrong; ``value`` is the right one;
      * FLAG       — unresolvable: no answer, with a reason of its own;
      * UNDO       — back to "not reviewed".
    """

    CONFIRM = "CONFIRM"
    SUBSTITUTE = "SUBSTITUTE"
    FLAG = "FLAG"
    UNDO = "UNDO"


class SubmissionReviewActionRequest(BaseModel):
    """Body of one review action on one reading — a review save.

    ``value`` is required by SUBSTITUTE and refused with any other action; its
    shape depends on the reading's type and is validated by the service (see
    the SUBSTITUTE inputs of ``app.schemas.submission_result``). ``note`` is
    trimmed; blank becomes null.
    """

    action: ReviewActionType
    review_version: int
    note: str | None = None
    value: dict[str, Any] | None = None

    @field_validator("note")
    @classmethod
    def _normalise_note(cls, value: str | None) -> str | None:
        # Trimmed before the length check, as the candidate edit does.
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > REVIEW_NOTE_MAX_LENGTH:
            raise ValueError(
                f"The note is at most {REVIEW_NOTE_MAX_LENGTH} characters."
            )
        return value


class SubmissionAcceptRemainingRequest(BaseModel):
    """Body of "Accept all remaining" — a review save: just the version."""

    review_version: int


class SubmissionCloseRequest(BaseModel):
    """Body of "Close submission" — a review save: just the version
    the operator confirmed the closing on."""

    review_version: int


class SubmissionReopenRequest(BaseModel):
    """Body of "Reopen submission": the version of the closed
    submission the operator saw, so they reopen only what they looked at."""

    review_version: int


# ---------------------------------------------------------------------------
# SubmissionPage (a single physical scanned page of a submission)
# ---------------------------------------------------------------------------


class SubmissionPageBase(BaseModel):
    source_file_path: str
    source_filename: str
    # Code (QR/barcode) value read off this physical page, used for matching.
    identified_code_value: str | None = None


class SubmissionPageCreate(SubmissionPageBase):
    # submission_id comes from the URL path param; form_variant_page_id is
    # resolved later by matching (nullable until then).
    pass


class SubmissionPageRead(SubmissionPageBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_id: uuid.UUID
    # Stays None until the matching engine resolves the template page.
    form_variant_page_id: uuid.UUID | None = None
    # Physical 0-based position in the uploaded PDF; always present.
    source_page_order: int
    # Whether that position came from the page's code or from the PDF order.
    page_order_source: PageOrderSource
    # How much the binding above is worth: 1.0 the page's own code
    # declared its index, 0.5 it was deduced from the physical order, 0.0 the
    # wanted index does not exist in the matched variant (so the binding is
    # None). NULL means matching has never run on this page — not the same as
    # having run and placed nothing.
    match_confidence: float | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# SubmissionObjectResult
# ---------------------------------------------------------------------------


class SubmissionObjectResultBase(BaseModel):
    page_object_id: uuid.UUID
    result_type: SubmissionResultType
    detected_payload_json: dict[str, Any] | None = None
    reviewed_payload_json: dict[str, Any] | None = None
    auto_score: float | None = None
    final_score: float | None = None
    # The two axes review_required derives from. Both are facts of the optical
    # reading, written once by the extractor; a NULL
    # confidence means none was ever produced, not a bad reading.
    confidence_score: float | None = None
    validity_forces_review: bool = False
    review_required: bool = False
    reviewed: bool = False


class SubmissionObjectResultRead(SubmissionObjectResultBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_page_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# SubmissionReview
# ---------------------------------------------------------------------------


class SubmissionReviewBase(BaseModel):
    review_note: str | None = None


class SubmissionReviewCreate(SubmissionReviewBase):
    # submission_object_result_id from URL path; reviewer_user_id from auth.
    pass


class SubmissionReviewRead(SubmissionReviewBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_object_result_id: uuid.UUID
    reviewer_user_id: uuid.UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# Forward-reference resolution
#
# SubmissionBatchDetail.submissions and SubmissionRead.pages point at models
# declared further down the file (batch -> submission -> page reads top-down,
# which is the useful reading order). Rebuild them once everything exists.
# ---------------------------------------------------------------------------
SubmissionBatchDetail.model_rebuild()
SubmissionRead.model_rebuild()
SubmissionReviewObject.model_rebuild()
