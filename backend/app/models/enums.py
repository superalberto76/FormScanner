# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
import enum


class UserRole(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class UITheme(str, enum.Enum):
    """Per-user UI colour-theme preference.

    The MODE is stored, never the resolved colour: ``SYSTEM`` follows the OS
    ``prefers-color-scheme`` at runtime and is the default for new users.
    """

    LIGHT = "LIGHT"
    DARK = "DARK"
    SYSTEM = "SYSTEM"


class MembershipRole(str, enum.Enum):
    MEMBER = "MEMBER"
    MANAGER = "MANAGER"


class ProjectRole(str, enum.Enum):
    """Role of a user within a FormProject.

    Source of truth for FormProject access via FormProjectMember rows:
      * OWNER        — full control (delete, future member management).
      * COLLABORATOR — shares all content work (view/author/export), but not
        ownership actions.
    Distinct from ``MembershipRole`` (groups) so the two evolve
    independently.
    """

    OWNER = "OWNER"
    COLLABORATOR = "COLLABORATOR"


class CodeType(str, enum.Enum):
    QR = "QR"
    BARCODE = "BARCODE"


class CodeEncoding(str, enum.Enum):
    """Barcode symbology for a BARCODE CodeObjectConfig.

    Python-only enum: deliberately NOT bound to a Postgres enum type — the
    ``code_object_configs.encoding`` column stays a nullable string and no
    migration is required. Meaningful only when ``code_type`` is BARCODE; QR
    codes carry no encoding. Validation of the encoding⇄code_type contract
    lives in ``code_object_service``.
    """

    CODE128 = "CODE128"
    CODE39 = "CODE39"
    EAN13 = "EAN13"
    EAN8 = "EAN8"
    UPCA = "UPCA"


class MatrixSelectionRule(str, enum.Enum):
    ONE_PER_COLUMN = "ONE_PER_COLUMN"
    ONE_PER_ROW = "ONE_PER_ROW"
    MANY_PER_COLUMN = "MANY_PER_COLUMN"
    MANY_PER_ROW = "MANY_PER_ROW"
    FREE = "FREE"


# ---------------------------------------------------------------------------
# Form/exam authoring + scanning model.
#
# These enums power the FormProject → FormVariant → PageObject → Submission
# hierarchy. Where an enum above already covers a need (e.g. CodeType =
# QR|BARCODE, MatrixSelectionRule), the models reuse it instead of
# duplicating it.
# ---------------------------------------------------------------------------


class FormProjectType(str, enum.Enum):
    """Top-level kind of a FormProject (campaign)."""

    EXAM = "EXAM"
    SURVEY = "SURVEY"
    QUESTIONNAIRE = "QUESTIONNAIRE"
    GENERIC_FORM = "GENERIC_FORM"


class FormProjectStatus(str, enum.Enum):
    """Lifecycle of a FormProject."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class FormVariantStatus(str, enum.Enum):
    """Publish lifecycle of a single printable variant."""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class PageObjectType(str, enum.Enum):
    """Discriminator for the typed config attached to a PageObject."""

    TEXT = "TEXT"
    CODE = "CODE"
    FREE_TEXT_AREA = "FREE_TEXT_AREA"
    QUESTION = "QUESTION"
    MATRIX = "MATRIX"
    IMAGE = "IMAGE"


class PageObjectRole(str, enum.Enum):
    """Optional special role of a PageObject within its FormVariant
    (scanning pipeline, Identification).
    At most one binding per role per FormVariant, and at most one role
    per PageObject; enforced via FormVariantRoleBinding, not here."""
    VARIANT_IDENTIFIER = "VARIANT_IDENTIFIER"
    RESPONDENT_IDENTIFIER = "RESPONDENT_IDENTIFIER"


class QuestionType(str, enum.Enum):
    """Answer model for a QUESTION PageObject."""

    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"


class SubmissionStatus(str, enum.Enum):
    """Lifecycle of a single scanned submission sheet.

    ``UNASSIGNED`` is not a lifecycle STEP: it is the state a submission is
    BORN in when loose-image ingestion could not group the sheet at all —
    neither a readable RID code nor a filename following the naming convention
    (``app.utils.image_filename``). Such a Submission is a single-page
    singleton awaiting a human decision.

    ``MATCHED`` / ``MATCH_FAILED`` are the two outcomes of the variant
    matching engine, written DIRECTLY by ``submission_matching_service`` — there
    is still no transition machine; each status is written directly by the
    step that decides it (SCANNED by the extraction run — see below). They
    answer "which printable variant is this submission?", which is a different
    question from UNASSIGNED's "which submission does this sheet belong to?": a
    sheet that could not be grouped keeps its UNASSIGNED status even when its
    variant resolves, because the grouping is still the open question and only
    the recovery workflow can close it.

    ``MATCH_FAILED`` means the engine ran over the whole submission and no page
    yielded a VARIANT_IDENTIFIER matching a PUBLISHED variant of the project.
    ``matched_form_variant_id`` stays NULL; re-running matching on the submission
    is always allowed (a later upload may add the readable page that was
    missing).

    ``SCANNED`` means "extracted, ready for review". It is written by the
    extraction run (``extraction_orchestrator_service``), from MATCHED ONLY,
    when that run walked every object of the submission's bound pages without
    a single error; objects skipped as not-yet-supported do not hold it back.
    A submission with an error stays MATCHED until a later run reads it
    cleanly. Matching never writes over SCANNED, because it writes a status
    only when it RESOLVES a variant, and a SCANNED submission already has one.
    Known limitation: a SCANNED submission that later gains a page (a
    loose-image upload extending it) stays SCANNED until the next run extracts
    that page; with the recommended input, one PDF per submission, this
    cannot happen.

    SCANNED is also written by "Review anyway", from MATCHED only and only
    after a run has examined the submission: an operator brings a submission
    whose objects keep failing extraction into review, with a placeholder
    result for every object no run could read.

    ``UNRESOLVABLE`` is an operator's verdict on a MATCH_FAILED submission
    whose sheets are too damaged to tell the variant, or on an UNASSIGNED
    sheet nobody can place:
    it is closed — never finalized, never resumed, and it does not hold back
    the batch's export (``submission_batch_service._is_ready_for_export``). It
    is NOT one of ``CLOSED_SUBMISSION_STATUSES``: those were closed by a
    review, this one never had any. Reversible ("Undo" puts it back to the
    status it was marked from, ``Submission.unresolvable_from_status``), and
    matching never touches it, not even a re-run: the
    human decision stands until a human undoes it.
    """

    UPLOADED = "UPLOADED"
    UNASSIGNED = "UNASSIGNED"
    MATCHED = "MATCHED"
    MATCH_FAILED = "MATCH_FAILED"
    UNRESOLVABLE = "UNRESOLVABLE"
    SCANNED = "SCANNED"
    REVIEWED = "REVIEWED"
    FINALIZED = "FINALIZED"


# Submissions a human has closed. One set for every reader that asks "is it
# closed?": the batch metrics (``submission_batch_service``) and the
# extraction run, which never reads a closed submission nor writes its status
# (``extraction_orchestrator_service``). REVIEWED is written by closing a
# submission (``submission_close_service``).
CLOSED_SUBMISSION_STATUSES = frozenset(
    {SubmissionStatus.REVIEWED, SubmissionStatus.FINALIZED}
)


class MatchFailureReason(str, enum.Enum):
    """Why matching left a submission in MATCH_FAILED.

    Stored on the Submission (``match_failure_reason``) because it depends on
    the variants published WHEN matching ran, so it cannot be recomputed later.
    The persisted form of ``submission_matching_service``'s failure reasons,
    which keep their lowercase spelling in the audit trail.
    """

    # The project has no published variant at all: a configuration problem.
    NO_PUBLISHED_VARIANT = "NO_PUBLISHED_VARIANT"
    # No sheet carries a readable VARIANT_IDENTIFIER (F18).
    NO_VID_FOUND = "NO_VID_FOUND"
    # A VARIANT_IDENTIFIER was read but belongs to no published variant (F20);
    # the value read is kept in ``match_failure_code_value``.
    VID_NOT_RECOGNISED = "VID_NOT_RECOGNISED"


class PageOrderSource(str, enum.Enum):
    """How a SubmissionPage's position inside its submission was determined
    (scanning pipeline, Identification).

      * CODE      — a page index was successfully read from the page's QR /
                    barcode payload (``VID:<value>|<page_index>``, see
                    ``app.utils.code_rendering.build_tagged_payload``).
      * FILENAME  — no page index in the code, but the uploaded image was named
                    per the convention ``<submission>_page_<n>.<ext>`` and
                    the position came from ``<n>`` (loose-image ingestion; see
                    ``app.utils.image_filename``).
      * PDF_ORDER — nothing DECLARED the position, so a mere sequence was used.
                    For a PDF that sequence is the physical page order; for loose
                    images it is the order of arrival within the upload request,
                    among the images sharing the same grouping key. The name is
                    kept for continuity: it is the generic "no declared order,
                    just a sequence" bucket, deliberately NOT renamed.

    A VARIANT_IDENTIFIER that decodes but carries NO page index is not CODE: the
    value is still recorded in ``identified_code_value``, but the ORDER did not
    come from it. This enum answers "where did the order come from", not "was a
    code readable".
    """

    CODE = "CODE"
    FILENAME = "FILENAME"
    PDF_ORDER = "PDF_ORDER"


class PageAlignmentStatus(str, enum.Enum):
    """Whether a scanned page was aligned to its template from the four
    printed marks before its objects were read.

      * ALIGNED     — the marks were found and the transformation is
                      plausible: the objects are read on the straightened
                      image (``SubmissionPage.aligned_file_path``).
      * NOT_ALIGNED — fewer than 3 marks, an unknown orientation or an
                      implausible transformation: the page is read in
                      proportion, without alignment, and every reading on it
                      goes to review ("Sheet not aligned").

    NULL on the page means "never attempted": not extracted yet, or read
    before alignment existed.
    """

    ALIGNED = "ALIGNED"
    NOT_ALIGNED = "NOT_ALIGNED"


class SubmissionResultType(str, enum.Enum):
    """Discriminator for a per-object detection result on a submission."""

    QUESTION = "QUESTION"
    MATRIX = "MATRIX"
    FREE_TEXT = "FREE_TEXT"
    CODE = "CODE"
    # A MATRIX carrying the RESPONDENT_IDENTIFIER role: a respondent-ID grid,
    # written ONLY by the extraction engine, never set by a user and with no
    # impact on any FE screen or on the role-assignment mechanism. Carries no
    # Validity — it is not a question, it has no scoring.
    RESPONDENT_IDENTIFIER = "RESPONDENT_IDENTIFIER"


class Validity(str, enum.Enum):
    """Discrete axis: whether the combination of options READ on a sheet
    respects the rules of the question (extraction).

    Independent from Confidence: a reading can be confident and not valid at
    the same time (e.g. two blackened bubbles on a single-choice question),
    and vice versa.

    Python-only enum: it lives solely inside the ``detected_payload_json``
    JSON of a ``SubmissionObjectResult``, never in a DB column — deliberately
    NOT bound to a Postgres enum type, so no migration is required. Same
    pattern as ``CodeEncoding`` above. The shape of that JSON is described by
    ``app.schemas.submission_result``.
    """

    VALID = "VALID"
    NO_ANSWER = "NO_ANSWER"
    TOO_MANY_ANSWERS = "TOO_MANY_ANSWERS"


class ReviewOutcome(str, enum.Enum):
    """A human's CURRENT ruling on one reading (review).

    Stored on ``SubmissionObjectResult.review_outcome``; NULL there means "not
    resolved" — never reviewed, or reviewed and then undone. That column, not
    the existence of a ``SubmissionReview`` row, is what "resolved" means
    everywhere.

      * CONFIRMED   — the reading is right as read;
      * SUBSTITUTED — the reading was wrong, and ``reviewed_payload_json``
                      holds the value the operator entered;
      * FLAGGED     — unresolvable from the sheet: treated as no answer, with a
                      reason of its own (distinct from a SUBSTITUTE with nothing
                      selected, which says "the candidate did not answer").
    """

    CONFIRMED = "CONFIRMED"
    SUBSTITUTED = "SUBSTITUTED"
    FLAGGED = "FLAGGED"


class ReviewAction(str, enum.Enum):
    """What one ``SubmissionReview`` log row records.

    The three outcomes plus UNDONE: the log is append-only, so taking a ruling
    back is itself an entry rather than a deletion. Rows written before the
    action column existed were set to CONFIRMED when it was added.
    """

    CONFIRMED = "CONFIRMED"
    SUBSTITUTED = "SUBSTITUTED"
    FLAGGED = "FLAGGED"
    UNDONE = "UNDONE"
