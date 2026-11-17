# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the maximum score of a variant.

``GET /form-variants/{id}/max-score`` loads the variant's scorable objects
(``page_object_repo.list_scorable_for_variant``) and sums them with
``scoring_rules.variant_max_score``. The formula itself is pinned down by the
unit tests of ``test_scoring_rules.py``; what this suite pins down is what the
query hands it:

  * **which objects** — QUESTIONs and evaluative MATRIXes; a non-evaluative
    MATRIX, a respondent-ID grid (own role or the linked group's) and every other
    type add nothing;
  * **linked copies** — a group shared across pages counts once, a detached
    copy on its own;
  * **any status** — DRAFT and PUBLISHED alike; an empty variant is worth 0;
  * **cost** — a constant number of statements however many objects;
  * **access** — 404 / 403 as reading the variant.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.api.v1.endpoints.form_variants import get_variant_max_score
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    QuestionType,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.image_object_config import ImageObjectConfig
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.user import User
from app.services import variant_max_score_service


PAGE_COUNT = 3


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _user(db, role: UserRole = UserRole.USER) -> User:
    user = User(
        email=f"max-score-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=role,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def actor(db) -> User:
    return _user(db)


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Max score project",
        status=FormProjectStatus.DRAFT,
    )
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER))
    db.flush()
    return project


def _variant(db, project, actor, status=FormVariantStatus.DRAFT) -> list[FormVariantPage]:
    """A variant of PAGE_COUNT pages (page_index 1-based); returns its pages.

    Each variant of the project gets the next version number (unique per
    project).
    """
    version = len(db.query(FormVariant).filter_by(form_project_id=project.id).all()) + 1
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name="A",
        status=status,
        version_number=version,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    pages = [
        FormVariantPage(
            form_variant_id=variant.id, page_index=index, width_px=794, height_px=1123
        )
        for index in range(1, PAGE_COUNT + 1)
    ]
    db.add_all(pages)
    db.flush()
    return pages


@pytest.fixture
def pages(db, project, actor) -> list[FormVariantPage]:
    return _variant(db, project, actor)


def _object(
    db,
    page: FormVariantPage,
    object_type: PageObjectType,
    *,
    group: uuid.UUID | None = None,
    linked: bool = False,
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=object_type,
        x=10.0,
        y=10.0,
        width=100.0,
        height=100.0,
        shared_group_id=group,
        shared_across_pages=linked,
    )
    db.add(obj)
    db.flush()
    return obj


def _question(
    db,
    page: FormVariantPage,
    *scores: float | None,
    question_type: QuestionType = QuestionType.SINGLE_CHOICE,
    group: uuid.UUID | None = None,
    linked: bool = False,
) -> PageObject:
    obj = _object(db, page, PageObjectType.QUESTION, group=group, linked=linked)
    config = QuestionObjectConfig(
        page_object_id=obj.id, question_type=question_type, question_text="Q"
    )
    db.add(config)
    db.flush()
    db.add_all(
        QuestionOption(
            question_object_config_id=config.id,
            order_index=index,
            option_text=f"option {index}",
            is_correct=score is not None and score > 0,
            score=score,
        )
        for index, score in enumerate(scores)
    )
    db.flush()
    return obj


def _matrix(
    db,
    page: FormVariantPage,
    cells: dict[tuple[int, int], float | None],
    *,
    rule: MatrixSelectionRule = MatrixSelectionRule.ONE_PER_ROW,
    rows: int = 2,
    cols: int = 3,
    evaluative: bool = True,
    group: uuid.UUID | None = None,
    linked: bool = False,
) -> PageObject:
    obj = _object(db, page, PageObjectType.MATRIX, group=group, linked=linked)
    config = MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=rows,
        cols_count=cols,
        evaluative=evaluative,
        selection_rule=rule,
    )
    db.add(config)
    db.flush()
    db.add_all(
        MatrixCellDef(
            matrix_object_config_id=config.id,
            row_index=row,
            col_index=col,
            is_correct=score is not None and score > 0,
            score=score,
        )
        for (row, col), score in cells.items()
    )
    db.flush()
    return obj


def _bind(db, obj: PageObject, pages: list[FormVariantPage], role: PageObjectRole) -> None:
    db.add(
        FormVariantRoleBinding(
            form_variant_id=pages[0].form_variant_id, page_object_id=obj.id, role=role
        )
    )
    db.flush()


def _max(db, pages: list[FormVariantPage], user: User) -> float:
    db.expire_all()
    return get_variant_max_score(pages[0].form_variant_id, db=db, user=user).max_score


# ---------------------------------------------------------------------------
# Which objects
# ---------------------------------------------------------------------------


def test_questions_and_evaluative_matrices_add_up(db, actor, pages):
    _question(db, pages[0], 1.0, 2.0, -1.0)  # single: 2
    _question(
        db, pages[1], 1.0, 1.0, 1.0, question_type=QuestionType.MULTIPLE_CHOICE
    )  # FULL_ANSWER leaves one out: 2
    _matrix(db, pages[2], {(0, 0): 1.0, (0, 1): 3.0, (1, 2): 0.5})  # 3 + 0.5

    assert _max(db, pages, actor) == pytest.approx(7.5)


def test_a_column_rule_matrix_is_summed_by_column(db, actor, pages):
    _matrix(
        db,
        pages[0],
        {(0, 0): 1.0, (1, 0): 2.0, (0, 1): 1.0},
        rule=MatrixSelectionRule.ONE_PER_COLUMN,
        rows=2,
        cols=2,
    )
    assert _max(db, pages, actor) == pytest.approx(3.0)


def test_a_non_evaluative_matrix_adds_nothing(db, actor, pages):
    _matrix(db, pages[0], {(0, 0): 5.0}, evaluative=False)
    assert _max(db, pages, actor) == 0.0


def test_a_respondent_id_grid_adds_nothing_even_when_flagged_evaluative(db, actor, pages):
    grid = _matrix(db, pages[0], {(0, 0): 5.0})
    _bind(db, grid, pages, PageObjectRole.RESPONDENT_IDENTIFIER)
    assert _max(db, pages, actor) == 0.0


def test_the_role_of_the_linked_group_excludes_every_copy(db, actor, pages):
    # The binding lives on the copy of page 1; pages 2 and 3 inherit it.
    group = uuid.uuid4()
    copies = [
        _matrix(db, page, {(0, 0): 5.0}, group=group, linked=True) for page in pages
    ]
    _bind(db, copies[0], pages, PageObjectRole.RESPONDENT_IDENTIFIER)
    assert _max(db, pages, actor) == 0.0


def test_other_object_types_add_nothing(db, actor, pages):
    code = _object(db, pages[0], PageObjectType.CODE)
    db.add(CodeObjectConfig(page_object_id=code.id, code_type=CodeType.QR, value="X"))
    free = _object(db, pages[0], PageObjectType.FREE_TEXT_AREA)
    db.add(FreeTextAreaConfig(page_object_id=free.id))
    _object(db, pages[0], PageObjectType.TEXT)
    image = _object(db, pages[0], PageObjectType.IMAGE)
    db.add(ImageObjectConfig(page_object_id=image.id))
    db.flush()
    assert _max(db, pages, actor) == 0.0


def test_an_empty_variant_is_worth_zero(db, actor, pages):
    assert _max(db, pages, actor) == 0.0


# ---------------------------------------------------------------------------
# Linked copies
# ---------------------------------------------------------------------------


def test_a_linked_question_counts_once_and_a_detached_copy_on_its_own(db, actor, pages):
    group = uuid.uuid4()
    _question(db, pages[0], 2.0, 0.0, group=group, linked=True)
    _question(db, pages[1], 2.0, 0.0, group=group, linked=True)
    _question(db, pages[2], 2.0, 0.0, group=group, linked=False)
    assert _max(db, pages, actor) == pytest.approx(4.0)


def test_a_linked_matrix_counts_once(db, actor, pages):
    group = uuid.uuid4()
    for page in pages:
        _matrix(db, page, {(0, 0): 1.0, (1, 0): 1.0}, group=group, linked=True)
    assert _max(db, pages, actor) == pytest.approx(2.0)


def test_the_copy_on_the_lowest_page_is_the_one_counted(db, actor, pages):
    # Created out of page order: the query, not the insertion, decides.
    group = uuid.uuid4()
    _question(db, pages[2], 1.0, 0.0, group=group, linked=True)
    _question(db, pages[0], 3.0, 0.0, group=group, linked=True)
    assert _max(db, pages, actor) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Status and access
# ---------------------------------------------------------------------------


def test_a_published_variant_has_its_maximum_too(db, actor, project):
    published = _variant(db, project, actor, status=FormVariantStatus.PUBLISHED)
    _question(db, published[0], 1.0, 4.0)
    assert _max(db, published, actor) == pytest.approx(4.0)


def test_only_the_objects_of_the_variant_count(db, actor, project, pages):
    other = _variant(db, project, actor)
    _question(db, other[0], 10.0)
    _question(db, pages[0], 1.0)
    assert _max(db, pages, actor) == pytest.approx(1.0)


def test_an_unknown_variant_is_not_found(db, actor):
    with pytest.raises(HTTPException) as exc:
        variant_max_score_service.get_max_score(db, uuid.uuid4(), actor)
    assert exc.value.status_code == 404


def test_a_user_outside_the_project_is_forbidden(db, pages):
    with pytest.raises(HTTPException) as exc:
        _max(db, pages, _user(db))
    assert exc.value.status_code == 403


def test_an_admin_outside_the_project_may_read_it(db, pages):
    _question(db, pages[0], 2.0)
    assert _max(db, pages, _user(db, role=UserRole.ADMIN)) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Statement count
# ---------------------------------------------------------------------------


def _count(db, call) -> tuple[int, object]:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    db.expire_all()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        out = call()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements), out


def _populate(db, pages: list[FormVariantPage], n: int) -> None:
    for i in range(n):
        page = pages[i % PAGE_COUNT]
        _question(db, page, 1.0, 0.0)
        _matrix(db, page, {(0, 0): 1.0})
        group = uuid.uuid4()
        shared = _matrix(db, pages[0], {(0, 0): 1.0}, group=group, linked=True)
        _matrix(db, pages[1], {(0, 0): 1.0}, group=group, linked=True)
        if i == 1:  # one RESPONDENT_IDENTIFIER per variant
            _bind(db, shared, pages, PageObjectRole.RESPONDENT_IDENTIFIER)


def test_the_maximum_costs_the_same_however_many_objects(db, actor, project):
    small, large = _variant(db, project, actor), _variant(db, project, actor)
    _populate(db, small, 2)
    _populate(db, large, 9)

    small_count, small_out = _count(
        db, lambda: get_variant_max_score(small[0].form_variant_id, db=db, user=actor)
    )
    large_count, large_out = _count(
        db, lambda: get_variant_max_score(large[0].form_variant_id, db=db, user=actor)
    )

    # n questions + n matrices + the linked groups but the respondent-ID one.
    assert (small_out.max_score, large_out.max_score) == pytest.approx((5.0, 26.0))
    assert small_count == large_count
