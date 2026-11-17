# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the candidate in review when a submission
holds several readings of its identifier.

Every linked copy of a shared respondent ID or RESPONDENT_IDENTIFIER free text
is an identity reading, one per page; duplicate sheets give two readings of
the same object even without sharing. At extraction only the FIRST reading in
physical page order sets the candidate; review follows the same rule, so that
a CONFIRM or a SUBSTITUTE on a later reading, or "Accept all remaining"
walking them in turn, does not let the last one win. What this suite pins
down:

  * **later copies** — a SUBSTITUTE on a later copy does not overwrite the
    candidate the first one set; "Accept all remaining" takes the first one's;
  * **physical order** — the first sheet scanned, not the lowest page;
  * **no fallback** — with the first reading flagged, a later one sets nothing;
  * **duplicate sheets** — the same rule without any sharing;
  * **free text** — a shared RESPONDENT_IDENTIFIER free text alike;
  * **the ruling stays** — the later copy's outcome is recorded as before;
  * **the read** — ``sets_candidate`` per object and
    ``candidate_reading_page_id``, also when the first reading is not listed
    because it needed no review;
  * **statement counts** — a save and an accept-remaining on an identity
    reading cost the same however many copies the submission holds.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, func, select

from app.api.v1.endpoints.submissions import (
    accept_remaining_submission_results,
    get_submission_review,
    review_submission_result,
)
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    PageOrderSource,
    ProjectRole,
    ReviewOutcome,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import (
    SubmissionAcceptRemainingRequest,
    SubmissionReviewActionRequest,
    SubmissionReviewDetail,
)


PAGE_COUNT = 3
THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"first-reading-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def batch(db, actor) -> SubmissionBatch:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="First reading project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER
        )
    )
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch",
        created_by_user_id=actor.id,
        confidence_threshold=THRESHOLD,
    )
    db.add(batch)
    db.flush()
    return batch


def _make_variant(db, batch, actor) -> list[FormVariantPage]:
    """A published variant of PAGE_COUNT pages (page_index 1-based)."""
    variant = FormVariant(
        form_project_id=batch.form_project_id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name="A",
        status=FormVariantStatus.PUBLISHED,
        # Unique per project: the statement-count tests need two variants.
        version_number=1 + db.scalar(
            select(func.count(FormVariant.id)).where(
                FormVariant.form_project_id == batch.form_project_id
            )
        ),
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    pages = []
    for index in range(1, PAGE_COUNT + 1):
        page = FormVariantPage(
            form_variant_id=variant.id, page_index=index, width_px=794, height_px=1123
        )
        db.add(page)
        pages.append(page)
    db.flush()
    return pages


@pytest.fixture
def template_pages(db, batch, actor) -> list[FormVariantPage]:
    return _make_variant(db, batch, actor)


def _add_identifier(
    db,
    template_pages: list[FormVariantPage],
    object_type: PageObjectType = PageObjectType.MATRIX,
    *,
    shared: bool = True,
) -> dict[int, PageObject]:
    """The variant's RESPONDENT_IDENTIFIER, bound on page 1: one linked copy
    per page when *shared*, otherwise only the object of page 1.

    Returns {page_index: object}."""
    group_id = uuid.uuid4() if shared else None
    objects: dict[int, PageObject] = {}
    for page in template_pages if shared else template_pages[:1]:
        obj = PageObject(
            form_variant_page_id=page.id,
            object_type=object_type,
            x=500.0, y=100.0, width=200.0, height=200.0,
            shared_group_id=group_id,
            shared_across_pages=shared,
        )
        db.add(obj)
        db.flush()
        if object_type is PageObjectType.MATRIX:
            db.add(
                MatrixObjectConfig(
                    page_object_id=obj.id,
                    rows_count=10,
                    cols_count=6,
                    selection_rule=MatrixSelectionRule.ONE_PER_COLUMN,
                )
            )
        else:
            db.add(FreeTextAreaConfig(page_object_id=obj.id))
        objects[page.page_index] = obj
    db.add(
        FormVariantRoleBinding(
            form_variant_id=template_pages[0].form_variant_id,
            page_object_id=objects[1].id,
            role=PageObjectRole.RESPONDENT_IDENTIFIER,
        )
    )
    db.flush()
    for obj in objects.values():
        db.refresh(obj)
    return objects


def _make_submission(
    db, batch, template_pages, sheets: tuple[int, ...]
) -> tuple[Submission, list[SubmissionPage]]:
    """A SCANNED submission, no candidate, with one sheet bound to each page
    index in *sheets*, in that physical order."""
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_pages[0].form_variant_id,
        status=SubmissionStatus.SCANNED,
    )
    db.add(submission)
    db.flush()
    by_index = {p.page_index: p for p in template_pages}
    sheets_out = []
    for order, index in enumerate(sheets):
        sheet = SubmissionPage(
            submission_id=submission.id,
            form_variant_page_id=by_index[index].id,
            source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
            source_filename=f"scan-{order}.png",
            source_page_order=order,
            page_order_source=PageOrderSource.CODE,
            match_confidence=1.0,
        )
        db.add(sheet)
        sheets_out.append(sheet)
    db.flush()
    return submission, sheets_out


def _read(
    db,
    sheet: SubmissionPage,
    obj: PageObject,
    value: str,
    *,
    review_required: bool = True,
) -> SubmissionObjectResult:
    """The identity reading of *obj* on *sheet*: a clean grid with a low
    confidence (bulk-acceptable when in review), or a free-text suggestion."""
    grid = obj.object_type is PageObjectType.MATRIX
    result = SubmissionObjectResult(
        submission_page_id=sheet.id,
        page_object_id=obj.id,
        result_type=(
            SubmissionResultType.RESPONDENT_IDENTIFIER
            if grid
            else SubmissionResultType.FREE_TEXT
        ),
        detected_payload_json=(
            {"raw_value": value, "digits": [], "ambiguous_columns": []}
            if grid
            else {"suggested_text": value}
        ),
        confidence_score=0.5 if review_required else 1.0,
        validity_forces_review=not grid,
        review_required=review_required,
    )
    db.add(result)
    db.flush()
    return result


def _shared_submission(
    db, batch, template_pages, sheets=(1, 2, 3), object_type=PageObjectType.MATRIX
) -> tuple[Submission, dict[int, SubmissionObjectResult]]:
    """A shared identifier read on every sheet with a different value per page
    ("10000<page>"). Returns the submission and {page_index: reading}."""
    objects = _add_identifier(db, template_pages, object_type)
    submission, sheet_list = _make_submission(db, batch, template_pages, sheets)
    by_page_id = {p.id: p.page_index for p in template_pages}
    readings = {}
    for sheet in sheet_list:
        index = by_page_id[sheet.form_variant_page_id]
        readings[index] = _read(db, sheet, objects[index], f"10000{index}")
    return submission, readings


def _act(db, submission, result, action, user, value=None) -> SubmissionReviewDetail:
    return review_submission_result(
        submission_id=submission.id,
        result_id=result.id,
        data=SubmissionReviewActionRequest(
            action=action, review_version=submission.review_version, value=value
        ),
        db=db,
        user=user,
    )


def _accept(db, submission, user):
    return accept_remaining_submission_results(
        submission_id=submission.id,
        data=SubmissionAcceptRemainingRequest(review_version=submission.review_version),
        db=db,
        user=user,
    )


def _objects(detail: SubmissionReviewDetail) -> dict[uuid.UUID, object]:
    return {o.result_id: o for p in detail.pages for o in p.objects}


# ---------------------------------------------------------------------------
# Later copies
# ---------------------------------------------------------------------------


def test_a_later_copy_does_not_overwrite_the_candidate(db, actor, batch, template_pages):
    """CONFIRM the copy of page 1, then SUBSTITUTE the copy of page 3 with
    another value. The candidate stays the first copy's; the substitution is
    still recorded on the later copy."""
    submission, readings = _shared_submission(db, batch, template_pages)

    _act(db, submission, readings[1], "CONFIRM", actor)
    detail = _act(
        db, submission, readings[3], "SUBSTITUTE", actor, value={"value": "999999"}
    )

    assert detail.respondent_identifier == "100001"
    later = _objects(detail)[readings[3].id]
    assert later.review_outcome is ReviewOutcome.SUBSTITUTED
    assert later.reviewed_payload == {"value": "999999"}


def test_accept_all_remaining_takes_the_first_copy(db, actor, batch, template_pages):
    """Three bulk-acceptable copies with three values: the first one's wins,
    whatever order the bulk confirmation walks them in."""
    submission, _readings = _shared_submission(db, batch, template_pages)

    out = _accept(db, submission, actor)

    assert out.accepted_count == PAGE_COUNT
    assert out.respondent_identifier == "100001"


def test_accept_all_remaining_leaves_the_candidate_of_a_first_copy_read_clean(
    db, actor, batch, template_pages
):
    """The first copy read clean set the candidate at extraction and is not in
    review; the later copies, in review with other values, are bulk-confirmed
    and must not replace it."""
    objects = _add_identifier(db, template_pages)
    submission, sheets = _make_submission(db, batch, template_pages, (1, 2, 3))
    _read(db, sheets[0], objects[1], "100001", review_required=False)
    _read(db, sheets[1], objects[2], "100002")
    _read(db, sheets[2], objects[3], "100003")
    submission.respondent_identifier = "100001"
    db.flush()

    out = _accept(db, submission, actor)

    assert out.accepted_count == 2
    assert out.respondent_identifier == "100001"


def test_accept_all_remaining_after_the_first_copy_is_flagged_sets_nothing(
    db, actor, batch, template_pages
):
    submission, readings = _shared_submission(db, batch, template_pages)
    _act(db, submission, readings[1], "FLAG", actor)

    out = _accept(db, submission, actor)

    assert out.accepted_count == 2
    assert out.respondent_identifier is None


def test_the_first_copy_is_the_first_sheet_scanned(db, actor, batch, template_pages):
    """Sheets scanned 2, 1, 3: the copy of page 2 is the first reading, and the
    copy of page 1 — lowest page, physically second — sets nothing."""
    submission, readings = _shared_submission(
        db, batch, template_pages, sheets=(2, 1, 3)
    )

    assert _act(db, submission, readings[1], "CONFIRM", actor).respondent_identifier is None
    assert _act(db, submission, readings[2], "CONFIRM", actor).respondent_identifier == "100002"


def test_with_the_first_copy_flagged_no_later_copy_sets_the_candidate(
    db, actor, batch, template_pages
):
    submission, readings = _shared_submission(db, batch, template_pages)

    _act(db, submission, readings[1], "FLAG", actor)
    detail = _act(db, submission, readings[2], "CONFIRM", actor)

    assert detail.respondent_identifier is None
    assert _objects(detail)[readings[2].id].review_outcome is ReviewOutcome.CONFIRMED


def test_duplicate_sheets_follow_the_same_rule(db, actor, batch, template_pages):
    """No sharing: page 1 scanned twice gives two readings of one object."""
    objects = _add_identifier(db, template_pages, shared=False)
    submission, (first_sheet, second_sheet) = _make_submission(
        db, batch, template_pages, (1, 1)
    )
    first = _read(db, first_sheet, objects[1], "111111")
    second = _read(db, second_sheet, objects[1], "222222")

    assert _act(db, submission, second, "CONFIRM", actor).respondent_identifier is None
    assert _act(db, submission, first, "CONFIRM", actor).respondent_identifier == "111111"
    assert _act(db, submission, second, "CONFIRM", actor).respondent_identifier == "111111"


def test_a_shared_free_text_identifier_follows_the_same_rule(
    db, actor, batch, template_pages
):
    submission, readings = _shared_submission(
        db, batch, template_pages, object_type=PageObjectType.FREE_TEXT_AREA
    )

    after_later = _act(
        db, submission, readings[3], "SUBSTITUTE", actor, value={"value": "Mario Rossi"}
    )
    after_first = _act(
        db, submission, readings[1], "SUBSTITUTE", actor, value={"value": "Anna Bianchi"}
    )

    assert after_later.respondent_identifier is None
    assert after_first.respondent_identifier == "Anna Bianchi"


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


def test_the_read_says_which_reading_sets_the_candidate(
    db, actor, batch, template_pages
):
    submission, readings = _shared_submission(
        db, batch, template_pages, sheets=(2, 1, 3)
    )
    first_sheet_id = readings[2].submission_page_id

    detail = get_submission_review(submission_id=submission.id, db=db, user=actor)

    objects = _objects(detail)
    assert objects[readings[2].id].sets_candidate is True
    assert objects[readings[1].id].sets_candidate is False
    assert objects[readings[3].id].sets_candidate is False
    assert detail.candidate_reading_page_id == first_sheet_id


def test_the_first_reading_counts_even_when_it_needed_no_review(
    db, actor, batch, template_pages
):
    """The copy of page 1 read clean is not listed in review; the listed copies
    still do not set the candidate, and the read still names its sheet."""
    objects = _add_identifier(db, template_pages)
    submission, sheets = _make_submission(db, batch, template_pages, (1, 2, 3))
    clean = _read(db, sheets[0], objects[1], "100001", review_required=False)
    later = _read(db, sheets[1], objects[2], "100002")
    _read(db, sheets[2], objects[3], "100003")

    detail = get_submission_review(submission_id=submission.id, db=db, user=actor)

    assert clean.id not in _objects(detail)
    assert {o.sets_candidate for o in _objects(detail).values()} == {False}
    assert detail.candidate_reading_page_id == sheets[0].id
    assert _act(db, submission, later, "CONFIRM", actor).respondent_identifier is None


def test_a_submission_without_an_identity_reading_names_no_sheet(
    db, actor, batch, template_pages
):
    submission, _sheets = _make_submission(db, batch, template_pages, (1,))

    detail = get_submission_review(submission_id=submission.id, db=db, user=actor)

    assert detail.candidate_reading_page_id is None


# ---------------------------------------------------------------------------
# Statement counts
# ---------------------------------------------------------------------------


def _count(db, call) -> int:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    db.expire_all()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements)


def test_a_save_on_an_identity_reading_costs_the_same_however_many_copies(
    db, actor, batch, template_pages
):
    # Two variants: a variant binds RESPONDENT_IDENTIFIER to one object.
    small, small_readings = _shared_submission(db, batch, template_pages, sheets=(1,))
    large, large_readings = _shared_submission(
        db, batch, _make_variant(db, batch, actor), sheets=(1, 2, 3)
    )

    small_count = _count(db, lambda: _act(db, small, small_readings[1], "CONFIRM", actor))
    large_count = _count(db, lambda: _act(db, large, large_readings[1], "CONFIRM", actor))

    assert small_count == large_count


def test_accept_remaining_on_identity_readings_costs_the_same_however_many_copies(
    db, actor, batch, template_pages
):
    small, _ = _shared_submission(db, batch, template_pages, sheets=(1,))
    large, _ = _shared_submission(
        db, batch, _make_variant(db, batch, actor), sheets=(1, 2, 3)
    )

    assert _count(db, lambda: _accept(db, small, actor)) == _count(
        db, lambda: _accept(db, large, actor)
    )
