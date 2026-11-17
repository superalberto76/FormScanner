# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests: a respondent ID read clean and outside review
becomes the submission's candidate.

``Submission.respondent_identifier`` is also written by a RID code at upload,
by CONFIRM / SUBSTITUTE / "Accept all remaining" and by the manual edit; a
respondent-ID grid read cleanly by extraction must not leave the submission on
"No candidate" either. What this suite pins down:

  * **extraction** — a clean, complete, unambiguous respondent ID above the
    threshold sets the candidate; a "?" column, an ambiguous column or a
    confidence below the threshold does not;
  * **never overwritten, never cleared** — a candidate already there (RID at
    upload, manual edit) stays; a submission a human has already saved on
    (``review_version`` > 0) is left alone even with no candidate; a closed
    submission is not touched;
  * **a second run** — a submission extracted without its candidate gets it
    on the next run, which reads no sheet twice;
  * **the threshold** — raising it sends the respondent ID to review and
    leaves the candidate as it is; lowering it lets a complete respondent ID
    out of review and sets the candidate; reopening a closed submission
    catches its respondent ID up with the threshold but sets no candidate:
    closing was a review save;
  * **duplicate sheets** — only the first respondent-ID reading in page order
    speaks: if it does not qualify, there is no candidate;
  * **a review save during the run** — the run decides on the submission as
    it is in the database, so a save made meanwhile is never overwritten;
  * **traceability** — the count of candidates set rides in the existing audit
    events, and the extraction response is unchanged.

Real synthetic scans for the extraction path (the same geometry as
``test_respondent_identifier_extraction_integration``); stored results for the
threshold path, which never reads a sheet. Skipped without
TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select, update

from app.api.v1.endpoints.submission_batches import (
    run_batch_extraction,
    update_batch_confidence_threshold,
)
from app.api.v1.endpoints.submissions import close_submission, reopen_submission
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.repositories import submission_batch_repo
from app.schemas.submission import (
    ConfidenceThresholdUpdate,
    SubmissionCloseRequest,
    SubmissionReopenRequest,
)
from app.services import audit_service
from app.utils import alignment_marks, question_layout

TEMPLATE_W = 794
TEMPLATE_H = 1123

# A respondent-ID grid: 10 digit rows ("0".."9"), 5 columns.
OBJ_X = 60.0
OBJ_Y = 100.0
OBJ_W = 400.0
OBJ_H = 900.0
ROWS = 10
COLS = 5

WHITE = 255
BLACK = 0

# Column c inked on row CLEAN_ROWS[c]: the respondent ID "37095".
CLEAN_ROWS = (3, 7, 0, 9, 5)
CLEAN_VALUE = "37095"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    target = tmp_path / "submission_pages"
    target.mkdir()
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    return target


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"cand-reading-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    p = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Candidate from reading",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(p)
    db.flush()
    db.add(
        FormProjectMember(project_id=p.id, user_id=actor.id, role=ProjectRole.OWNER)
    )
    db.flush()
    return p


@pytest.fixture
def template_page(db, project, actor) -> FormVariantPage:
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(
        form_variant_id=variant.id,
        page_index=1,
        width_px=TEMPLATE_W,
        height_px=TEMPLATE_H,
    )
    db.add(page)
    db.flush()
    return page


@pytest.fixture
def respondent_id(db, template_page) -> PageObject:
    """The respondent-ID grid: a MATRIX holding RESPONDENT_IDENTIFIER, rows
    labelled "0".."9"."""
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.MATRIX,
        x=OBJ_X,
        y=OBJ_Y,
        width=OBJ_W,
        height=OBJ_H,
    )
    db.add(obj)
    db.flush()
    config = MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=ROWS,
        cols_count=COLS,
        selection_rule=MatrixSelectionRule.FREE,
    )
    db.add(config)
    db.flush()
    for index in range(ROWS):
        db.add(
            MatrixRowDef(
                matrix_object_config_id=config.id, order_index=index, label=str(index)
            )
        )
    for index in range(COLS):
        db.add(
            MatrixColDef(
                matrix_object_config_id=config.id, order_index=index, label=f"C{index}"
            )
        )
    db.add(
        FormVariantRoleBinding(
            form_variant_id=template_page.form_variant_id,
            page_object_id=obj.id,
            role=PageObjectRole.RESPONDENT_IDENTIFIER,
        )
    )
    db.flush()
    db.refresh(obj)
    return obj


def _make_batch(db, project, actor, *, threshold: float | None = 0.3) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        confidence_threshold=threshold,
    )
    db.add(batch)
    db.flush()
    return batch


def _cell_center(row: int, col: int) -> tuple[float, float]:
    """Where the export prints the (row, col) bubble, in template space — the
    raw formula, as in ``test_respondent_identifier_extraction_integration``."""
    label_w = min(OBJ_W * 0.25, 120.0)
    grid_x = OBJ_X + label_w
    grid_y = OBJ_Y + 16 + 16  # LABEL_H + HEADER_H
    cell_w = max(OBJ_W - label_w, 1.0) / COLS
    cell_h = max((OBJ_Y + OBJ_H) - grid_y, 1.0) / ROWS
    return (
        grid_x + col * cell_w + cell_w / 2,
        grid_y + row * cell_h + cell_h / 2,
    )


def _draw_alignment_marks(draw: ImageDraw.ImageDraw, scale: float) -> None:
    """The four marks every exported page prints. Without them the page is
    NOT_ALIGNED and its respondent ID goes to review whatever its marks, which
    is not what these tests are about."""
    for corner, (x, y, w, h) in alignment_marks.mark_rects(TEMPLATE_W, TEMPLATE_H).items():
        draw.rectangle(
            [x * scale, y * scale, (x + w) * scale - 1, (y + h) * scale - 1], fill=BLACK
        )
        if corner == alignment_marks.DISTINCT_CORNER:
            hx, hy, hw, hh = alignment_marks.hole_rect((x, y, w, h))
            draw.rectangle(
                [hx * scale, hy * scale, (hx + hw) * scale - 1, (hy + hh) * scale - 1],
                fill=WHITE,
            )


def _draw_scan(filled: dict[tuple[int, int], float], *, scale: float = 4.0) -> bytes:
    """A synthetic scan: every bubble outlined, *filled* inked to a fraction of
    the printed radius."""
    img = Image.new("L", (int(TEMPLATE_W * scale), int(TEMPLATE_H * scale)), WHITE)
    draw = ImageDraw.Draw(img)
    printed_radius = question_layout.CIRCLE_R * scale
    for row in range(ROWS):
        for col in range(COLS):
            cx, cy = _cell_center(row, col)
            cx, cy = cx * scale, cy * scale
            draw.ellipse(
                [cx - printed_radius, cy - printed_radius,
                 cx + printed_radius, cy + printed_radius],
                outline=BLACK,
                width=max(int(scale / 2), 1),
            )
    for (row, col), fraction in filled.items():
        cx, cy = _cell_center(row, col)
        cx, cy = cx * scale, cy * scale
        r = printed_radius * fraction
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK)
    _draw_alignment_marks(draw, scale)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _clean_marks() -> dict[tuple[int, int], float]:
    return {(row, col): 1.0 for col, row in enumerate(CLEAN_ROWS)}


def _make_submission(
    db,
    pages_dir,
    batch,
    template_page,
    *,
    scan: bytes,
    status: SubmissionStatus = SubmissionStatus.MATCHED,
    candidate: str | None = None,
    review_version: int = 0,
) -> Submission:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=status,
        respondent_identifier=candidate,
        review_version=review_version,
    )
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    path = pages_dir / f"{page_id}.png"
    path.write_bytes(scan)
    db.add(
        SubmissionPage(
            id=page_id,
            submission_id=submission.id,
            form_variant_page_id=template_page.id,
            source_file_path=str(path),
            source_filename="scan.png",
            source_page_order=0,
        )
    )
    db.flush()
    return submission


def _run(db, batch, user):
    return run_batch_extraction(batch_id=batch.id, db=db, user=user)


def _patch(db, batch, threshold: float | None, user):
    return update_batch_confidence_threshold(
        batch_id=batch.id,
        data=ConfidenceThresholdUpdate(confidence_threshold=threshold),
        db=db,
        user=user,
    )


def _respondent_id_result(db, submission: Submission) -> SubmissionObjectResult:
    return db.scalars(
        select(SubmissionObjectResult)
        .join(SubmissionPage)
        .where(
            SubmissionPage.submission_id == submission.id,
            SubmissionObjectResult.result_type
            == SubmissionResultType.RESPONDENT_IDENTIFIER,
        )
    ).one()


def _events(db, batch, action: str) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.action == action, AuditLog.entity_id == str(batch.id))
            .order_by(AuditLog.created_at)
        ).all()
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def test_a_clean_respondent_id_out_of_review_becomes_the_candidate(
    db, pages_dir, project, actor, template_page, respondent_id
):
    """Read clean, outside review: the submission must not stay on "No
    candidate"."""
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan(_clean_marks())
    )

    _run(db, batch, actor)
    db.refresh(submission)

    result = _respondent_id_result(db, submission)
    assert result.review_required is False
    assert submission.respondent_identifier == CLEAN_VALUE
    # Neither a review save nor a lock: the run does not touch the version.
    assert submission.review_version == 0


def test_a_candidate_already_there_is_never_overwritten(
    db, pages_dir, project, actor, template_page, respondent_id
):
    """A RID read at upload or a manual edit keeps precedence, as the RID rule
    at upload already does."""
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(_clean_marks()), candidate="FROM-RID",
    )

    _run(db, batch, actor)
    db.refresh(submission)

    assert submission.respondent_identifier == "FROM-RID"


def test_an_empty_column_sets_no_candidate(
    db, pages_dir, project, actor, template_page, respondent_id
):
    """A "?" makes the reading not confirmable, so not a candidate either. Even
    under a threshold low enough for its mean confidence, it stays in review:
    an empty column forces review on the other axis."""
    marks = _clean_marks()
    del marks[(CLEAN_ROWS[2], 2)]
    batch = _make_batch(db, project, actor, threshold=0.1)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan(marks)
    )

    _run(db, batch, actor)
    db.refresh(submission)

    result = _respondent_id_result(db, submission)
    assert "?" in result.detected_payload_json["raw_value"]
    assert result.confidence_score > 0.1
    assert result.review_required is True
    assert submission.respondent_identifier is None


def test_an_ambiguous_respondent_id_sets_no_candidate(
    db, pages_dir, project, actor, template_page, respondent_id
):
    marks = _clean_marks()
    marks[(1, 0)] = 0.75  # a second, fainter mark in column 0
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan(marks)
    )

    _run(db, batch, actor)
    db.refresh(submission)

    assert _respondent_id_result(db, submission).review_required is True
    assert submission.respondent_identifier is None


def test_a_respondent_id_below_the_threshold_sets_no_candidate(
    db, pages_dir, project, actor, template_page, respondent_id
):
    batch = _make_batch(db, project, actor, threshold=1.0)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan(_clean_marks())
    )

    _run(db, batch, actor)
    db.refresh(submission)

    assert _respondent_id_result(db, submission).review_required is True
    assert submission.respondent_identifier is None


def test_a_closed_submission_is_not_touched(
    db, pages_dir, project, actor, template_page, respondent_id
):
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(_clean_marks()), status=SubmissionStatus.REVIEWED,
    )

    _run(db, batch, actor)
    db.refresh(submission)

    assert submission.respondent_identifier is None


def test_a_submission_a_human_has_saved_on_is_left_without_candidate(
    db, pages_dir, project, actor, template_page, respondent_id
):
    """Once a review save has happened (``review_version`` > 0), an empty
    candidate is the operator's state — for instance a candidate cleared by
    hand — and no run fills it back."""
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(_clean_marks()),
        status=SubmissionStatus.SCANNED,
        review_version=1,
    )

    _run(db, batch, actor)
    db.refresh(submission)

    assert submission.respondent_identifier is None


def test_a_second_run_sets_the_candidate_of_a_submission_extracted_before(
    db, pages_dir, project, actor, template_page, respondent_id
):
    """A submission already extracted may have its respondent-ID result but
    no candidate. The next run reads nothing again (the result exists) and
    still sets the candidate, because it looks at every respondent-ID result
    of the submission, not only those created by the run."""
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan(_clean_marks())
    )
    _run(db, batch, actor)
    # A submission extracted without its candidate.
    submission.respondent_identifier = None
    db.commit()

    summary = _run(db, batch, actor)
    db.refresh(submission)

    assert summary.results_created == 0
    assert submission.status is SubmissionStatus.SCANNED
    assert submission.respondent_identifier == CLEAN_VALUE


def test_the_run_event_counts_the_candidates_set_and_the_response_does_not(
    db, pages_dir, project, actor, template_page, respondent_id
):
    """The count is in the existing audit event; no API change."""
    batch = _make_batch(db, project, actor)
    _make_submission(db, pages_dir, batch, template_page, scan=_draw_scan(_clean_marks()))
    _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(_clean_marks()), candidate="FROM-RID",
    )

    response = _run(db, batch, actor)
    _run(db, batch, actor)

    events = _events(db, batch, audit_service.SUBMISSION_BATCH_EXTRACTION_RUN)
    assert [e.metadata_json["candidates_set"] for e in events] == [1, 0]
    assert "candidates_set" not in response.model_dump()


# ---------------------------------------------------------------------------
# The threshold
# ---------------------------------------------------------------------------
def _stored_submission(
    db,
    batch,
    template_page,
    respondent_id,
    *,
    raw_value: str = CLEAN_VALUE,
    confidence: float = 0.8,
    review_required: bool = False,
    status: SubmissionStatus = SubmissionStatus.SCANNED,
    candidate: str | None = None,
) -> Submission:
    """A submission already extracted: one respondent-ID result as the
    extractor leaves it, no sheet to read."""
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=status,
        respondent_identifier=candidate,
    )
    db.add(submission)
    db.flush()
    page = SubmissionPage(
        submission_id=submission.id,
        form_variant_page_id=template_page.id,
        source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
        source_filename="scan.png",
        source_page_order=0,
    )
    db.add(page)
    db.flush()
    db.add(
        SubmissionObjectResult(
            submission_page_id=page.id,
            page_object_id=respondent_id.id,
            result_type=SubmissionResultType.RESPONDENT_IDENTIFIER,
            detected_payload_json={
                "raw_value": raw_value,
                "digits": [],
                "ambiguous_columns": [],
            },
            confidence_score=confidence,
            validity_forces_review=False,
            review_required=review_required,
        )
    )
    db.flush()
    return submission


def test_raising_the_threshold_keeps_the_candidate_and_sends_the_respondent_id_to_review(
    db, project, actor, template_page, respondent_id
):
    """The candidate stays; CONFIRM / SUBSTITUTE update it."""
    batch = _make_batch(db, project, actor, threshold=0.7)
    submission = _stored_submission(
        db, batch, template_page, respondent_id, candidate=CLEAN_VALUE
    )

    _patch(db, batch, 0.9, actor)
    db.refresh(submission)

    assert _respondent_id_result(db, submission).review_required is True
    assert submission.respondent_identifier == CLEAN_VALUE


def test_lowering_the_threshold_sets_the_candidate(
    db, project, actor, template_page, respondent_id
):
    """The same rule as extraction."""
    batch = _make_batch(db, project, actor, threshold=0.9)
    submission = _stored_submission(
        db, batch, template_page, respondent_id, review_required=True
    )

    _patch(db, batch, 0.7, actor)
    db.refresh(submission)

    assert _respondent_id_result(db, submission).review_required is False
    assert submission.respondent_identifier == CLEAN_VALUE
    assert submission.review_version == 0
    events = _events(
        db, batch, audit_service.SUBMISSION_BATCH_CONFIDENCE_THRESHOLD_UPDATED
    )
    assert events[-1].metadata_json["candidates_set"] == 1


def test_lowering_the_threshold_leaves_closed_and_incomplete_submissions_alone(
    db, project, actor, template_page, respondent_id
):
    batch = _make_batch(db, project, actor, threshold=0.9)
    closed = _stored_submission(
        db, batch, template_page, respondent_id,
        review_required=True, status=SubmissionStatus.FINALIZED,
    )
    incomplete = _stored_submission(
        db, batch, template_page, respondent_id,
        raw_value="37?95", review_required=True,
    )
    other = _stored_submission(
        db, batch, template_page, respondent_id,
        review_required=True, candidate="SOMEONE",
    )

    _patch(db, batch, 0.7, actor)
    for submission in (closed, incomplete, other):
        db.refresh(submission)

    assert closed.respondent_identifier is None
    assert incomplete.respondent_identifier is None
    assert other.respondent_identifier == "SOMEONE"
    events = _events(
        db, batch, audit_service.SUBMISSION_BATCH_CONFIDENCE_THRESHOLD_UPDATED
    )
    assert events[-1].metadata_json["candidates_set"] == 0


def test_lowering_the_threshold_leaves_a_submission_already_saved_on_alone(
    db, project, actor, template_page, respondent_id
):
    """On the threshold side too: once a review save has happened, an empty
    candidate is the operator's state."""
    batch = _make_batch(db, project, actor, threshold=0.9)
    submission = _stored_submission(
        db, batch, template_page, respondent_id, review_required=True
    )
    submission.review_version = 2
    db.flush()

    _patch(db, batch, 0.7, actor)
    db.refresh(submission)

    assert _respondent_id_result(db, submission).review_required is False
    assert submission.respondent_identifier is None


# ---------------------------------------------------------------------------
# Duplicate sheets: only the first reading speaks
# ---------------------------------------------------------------------------
def _add_duplicate_sheet(
    db, submission, template_page, respondent_id, *, raw_value: str, confidence: float
) -> None:
    """A second sheet of the same template page, later in physical order, with
    its own respondent-ID reading."""
    page = SubmissionPage(
        submission_id=submission.id,
        form_variant_page_id=template_page.id,
        source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
        source_filename="scan-2.png",
        source_page_order=1,
    )
    db.add(page)
    db.flush()
    db.add(
        SubmissionObjectResult(
            submission_page_id=page.id,
            page_object_id=respondent_id.id,
            result_type=SubmissionResultType.RESPONDENT_IDENTIFIER,
            detected_payload_json={
                "raw_value": raw_value,
                "digits": [],
                "ambiguous_columns": [],
            },
            confidence_score=confidence,
            validity_forces_review=False,
            review_required=True,
        )
    )
    db.flush()


def test_a_first_sheet_still_in_review_gives_no_candidate_whatever_the_next_reads(
    db, project, actor, template_page, respondent_id
):
    batch = _make_batch(db, project, actor, threshold=0.9)
    submission = _stored_submission(
        db, batch, template_page, respondent_id,
        raw_value="37?95", review_required=True,
    )
    _add_duplicate_sheet(
        db, submission, template_page, respondent_id, raw_value="11111", confidence=0.8
    )

    _patch(db, batch, 0.7, actor)
    db.refresh(submission)

    assert submission.respondent_identifier is None


def test_the_first_sheet_in_page_order_gives_the_candidate(
    db, project, actor, template_page, respondent_id
):
    batch = _make_batch(db, project, actor, threshold=0.9)
    submission = _stored_submission(
        db, batch, template_page, respondent_id, review_required=True
    )
    _add_duplicate_sheet(
        db, submission, template_page, respondent_id, raw_value="11111", confidence=0.8
    )

    _patch(db, batch, 0.7, actor)
    db.refresh(submission)

    assert submission.respondent_identifier == CLEAN_VALUE


# ---------------------------------------------------------------------------
# A review save made while the run is going
# ---------------------------------------------------------------------------
def test_a_review_save_made_during_the_run_is_not_overwritten(
    db, pages_dir, project, actor, template_page, respondent_id, monkeypatch
):
    """The run loads its submissions at the start. An operator saving on one
    of them afterwards (simulated by a database write the session's copy does
    not see) must win: the run decides on the submission as it is now."""
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(_clean_marks()), status=SubmissionStatus.SCANNED,
    )
    original = submission_batch_repo.list_extractable_submissions

    def load_then_operator_saves(session, batch_id):
        loaded = original(session, batch_id)
        session.execute(
            update(Submission)
            .where(Submission.id == submission.id)
            .values(respondent_identifier="OPERATOR", review_version=1)
            .execution_options(synchronize_session=False)
        )
        return loaded

    monkeypatch.setattr(
        submission_batch_repo, "list_extractable_submissions", load_then_operator_saves
    )

    _run(db, batch, actor)
    db.expire_all()

    assert db.get(Submission, submission.id).respondent_identifier == "OPERATOR"


def test_reopening_after_a_lower_threshold_sets_no_candidate(
    db, project, actor, template_page, respondent_id
):
    """The reopen recomputes the unresolved respondent ID against the
    threshold in force, which lets it out of review — but the candidate stays
    empty, as a threshold change would leave it on an open submission: the
    close was a review save (``review_version`` > 0). If that rule ever
    changes, this is where the reopen must be looked at again."""
    batch = _make_batch(db, project, actor, threshold=0.9)
    submission = _stored_submission(
        db, batch, template_page, respondent_id, review_required=True
    )
    close_submission(
        submission_id=submission.id,
        data=SubmissionCloseRequest(review_version=0),
        db=db,
        user=actor,
    )
    _patch(db, batch, 0.7, actor)
    assert _respondent_id_result(db, submission).review_required is True  # closed

    reopen_submission(
        submission_id=submission.id,
        data=SubmissionReopenRequest(review_version=1),
        db=db,
        user=actor,
    )
    db.refresh(submission)

    assert submission.status is SubmissionStatus.SCANNED
    assert _respondent_id_result(db, submission).review_required is False
    assert submission.respondent_identifier is None
