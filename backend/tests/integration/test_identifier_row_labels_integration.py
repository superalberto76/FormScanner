# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the row labels of a respondent-ID grid.

The label of the marked row is the character a respondent-ID reading writes for
its column (``respondent_identifier_extraction_service``). On a MATRIX holding
RESPONDENT_IDENTIFIER, a blank label drops a digit unnoticed and a "?" looks
like an empty column without forcing review. What this suite pins down:

  * **assigning the role** — refused (409) on a MATRIX with a defined row label
    that is empty, blank or contains "?"; accepted with valid labels and with
    rows that have no definition (they read as their index);
  * **editing the rows** — on a respondent-ID grid with the role, creating or
    relabelling a row with such a label is refused (409) and nothing changes;
    moving a row or giving it a valid label is accepted;
  * **no role, no rule** — the same labels are accepted on a MATRIX without the
    role, and on a detached copy of a shared respondent-ID grid;
  * **linked copies** — a linked copy carries the group's role, so it is
    refused there too;
  * **re-linking** — sharing a group again, or RECONNECT, is refused when a copy
    would end up linked to the role with such a label (a detached copy's labels
    are free); a group without the role re-links freely.

Requires TEST_DATABASE_URL — see conftest.py (the suite is skipped without it).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.repositories import form_variant_role_binding_repo
from app.schemas.matrix_object import MatrixRowDefCreate, MatrixRowDefUpdate
from app.services import (
    matrix_object_service,
    page_object_role_service,
    page_object_service,
)

INVALID_LABELS = ["", "   ", "?", "1?"]
ROWS = 10


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _setup(db, owner, *, pages: int = 1) -> list[FormVariantPage]:
    """project (+OWNER member) -> DRAFT variant -> *pages* pages."""
    project = FormProject(owner_id=owner.id, type=FormProjectType.EXAM, name="Labels")
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER))
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.DRAFT,
        version_number=1,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    result = []
    for index in range(pages):
        page = FormVariantPage(form_variant_id=variant.id, page_index=index)
        db.add(page)
        result.append(page)
    db.flush()
    return result


def _add_matrix(
    db,
    page: FormVariantPage,
    labels: list[str] | None = None,
    *,
    shared_group_id: uuid.UUID | None = None,
    shared_across_pages: bool = False,
) -> PageObject:
    """A 10x6 MATRIX; *labels* defines rows 0..len-1, the others have no row."""
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.MATRIX,
        x=0, y=0, width=200, height=200, label="RESPONDENT ID",
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    config = MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=ROWS, cols_count=6,
        evaluative=False, selection_rule=MatrixSelectionRule.ONE_PER_COLUMN,
    )
    db.add(config)
    db.flush()
    for index, label in enumerate(labels or []):
        db.add(MatrixRowDef(matrix_object_config_id=config.id, order_index=index, label=label))
    db.flush()
    db.refresh(obj)
    return obj


def _bind(db, obj: PageObject) -> None:
    db.add(
        FormVariantRoleBinding(
            form_variant_id=obj.form_variant_page.form_variant_id,
            page_object_id=obj.id,
            role=PageObjectRole.RESPONDENT_IDENTIFIER,
        )
    )
    db.flush()
    db.expire_all()


def _digits(count: int = ROWS) -> list[str]:
    return [str(d) for d in range(count)]


def _row(db, obj: PageObject, order_index: int) -> MatrixRowDef:
    db.expire_all()
    return next(
        r for r in db.get(PageObject, obj.id).matrix_config.rows
        if r.order_index == order_index
    )


def _assign(db, obj: PageObject, user):
    return page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )


# ---------------------------------------------------------------------------
# Assigning the role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", INVALID_LABELS, ids=["empty", "blank", "question", "with-question"])
def test_the_role_is_refused_on_a_grid_with_an_invalid_label(db, user, bad):
    (page,) = _setup(db, user)
    labels = _digits()
    labels[3] = bad
    obj = _add_matrix(db, page, labels)

    with pytest.raises(HTTPException) as exc:
        _assign(db, obj, user)

    assert exc.value.status_code == 409
    # Numbered from 1, as the editor names the rows (R1, R2, …).
    assert "(row 4)" in exc.value.detail
    assert form_variant_role_binding_repo.get_by_page_object(db, obj.id) is None


def test_the_role_is_assigned_on_a_grid_with_valid_labels(db, user):
    (page,) = _setup(db, user)
    obj = _add_matrix(db, page, _digits())

    read = _assign(db, obj, user)

    assert read.role is PageObjectRole.RESPONDENT_IDENTIFIER


def test_rows_with_no_definition_are_valid(db, user):
    """Undefined rows read as their index (``str(order_index)``), never blank."""
    (page,) = _setup(db, user)
    obj = _add_matrix(db, page, ["A", "B"])

    read = _assign(db, obj, user)

    assert read.role is PageObjectRole.RESPONDENT_IDENTIFIER


def test_the_rule_does_not_touch_other_types(db, user):
    """RESPONDENT_IDENTIFIER on a CODE is unaffected (only a MATRIX has rows)."""
    (page,) = _setup(db, user)
    code = PageObject(
        form_variant_page_id=page.id, object_type=PageObjectType.CODE,
        x=0, y=0, width=50, height=50, label="?",
    )
    db.add(code)
    db.flush()
    db.add(CodeObjectConfig(page_object_id=code.id, code_type=CodeType.QR, value="?"))
    db.flush()

    read = _assign(db, code, user)

    assert read.role is PageObjectRole.RESPONDENT_IDENTIFIER


# ---------------------------------------------------------------------------
# Editing the rows of a respondent-ID grid with the role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", INVALID_LABELS, ids=["empty", "blank", "question", "with-question"])
def test_relabelling_a_row_of_an_id_grid_is_refused(db, user, bad):
    (page,) = _setup(db, user)
    obj = _add_matrix(db, page, _digits())
    _bind(db, obj)
    row = _row(db, obj, 4)

    with pytest.raises(HTTPException) as exc:
        matrix_object_service.update_row(db, row.id, MatrixRowDefUpdate(label=bad), user)

    assert exc.value.status_code == 409
    assert "(row 5)" in exc.value.detail
    assert _row(db, obj, 4).label == "4"


@pytest.mark.parametrize("bad", INVALID_LABELS, ids=["empty", "blank", "question", "with-question"])
def test_creating_a_row_of_an_id_grid_with_an_invalid_label_is_refused(db, user, bad):
    (page,) = _setup(db, user)
    obj = _add_matrix(db, page, _digits(9))
    _bind(db, obj)

    with pytest.raises(HTTPException) as exc:
        matrix_object_service.create_row(
            db, obj.id, MatrixRowDefCreate(order_index=9, label=bad), user
        )

    assert exc.value.status_code == 409
    assert "(row 10)" in exc.value.detail
    db.expire_all()
    assert [r.order_index for r in db.get(PageObject, obj.id).matrix_config.rows] == list(range(9))


def test_a_valid_label_or_a_move_is_accepted_on_an_id_grid(db, user):
    (page,) = _setup(db, user)
    obj = _add_matrix(db, page, _digits(8))  # rows 8 and 9 have no definition
    _bind(db, obj)

    created = matrix_object_service.create_row(
        db, obj.id, MatrixRowDefCreate(order_index=8, label="8"), user
    )
    relabelled = matrix_object_service.update_row(
        db, _row(db, obj, 0).id, MatrixRowDefUpdate(label="X"), user
    )
    moved = matrix_object_service.update_row(
        db, created.id, MatrixRowDefUpdate(order_index=9), user
    )

    assert relabelled.label == "X"
    assert (moved.order_index, moved.label) == (9, "8")


# ---------------------------------------------------------------------------
# No role, no rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", INVALID_LABELS, ids=["empty", "blank", "question", "with-question"])
def test_a_matrix_without_the_role_accepts_any_label(db, user, bad):
    (page,) = _setup(db, user)
    obj = _add_matrix(db, page, _digits(9))

    created = matrix_object_service.create_row(
        db, obj.id, MatrixRowDefCreate(order_index=9, label=bad), user
    )
    relabelled = matrix_object_service.update_row(
        db, _row(db, obj, 0).id, MatrixRowDefUpdate(label=bad), user
    )

    assert created.label == bad
    assert relabelled.label == bad


# ---------------------------------------------------------------------------
# Shared respondent-ID grid: linked and detached copies
# ---------------------------------------------------------------------------


def _shared_id_grid(db, user, *, detached_on: tuple[int, ...] = ()) -> dict[int, PageObject]:
    """A respondent-ID grid shown on two pages, the role bound on the page-0 copy."""
    pages = _setup(db, user, pages=2)
    group_id = uuid.uuid4()
    copies = {
        page.page_index: _add_matrix(
            db, page, _digits(),
            shared_group_id=group_id,
            shared_across_pages=page.page_index not in detached_on,
        )
        for page in pages
    }
    _bind(db, copies[0])
    return copies


def test_a_linked_copy_of_an_id_grid_is_refused(db, user):
    copies = _shared_id_grid(db, user)
    row = _row(db, copies[1], 2)

    with pytest.raises(HTTPException) as exc:
        matrix_object_service.update_row(db, row.id, MatrixRowDefUpdate(label="?"), user)

    assert exc.value.status_code == 409
    assert _row(db, copies[1], 2).label == "2"


def test_a_detached_copy_of_an_id_grid_accepts_any_label(db, user):
    copies = _shared_id_grid(db, user, detached_on=(1,))
    row = _row(db, copies[1], 2)

    relabelled = matrix_object_service.update_row(
        db, row.id, MatrixRowDefUpdate(label="?"), user
    )

    assert relabelled.label == "?"


# ---------------------------------------------------------------------------
# Re-linking a group whose copy holds the role
# ---------------------------------------------------------------------------


def _detached_id_grid_with_bad_copy(db, user) -> dict[int, PageObject]:
    """Two detached copies of one respondent-ID grid: the role on page 0
    (allowed, it needs no sharing), a "?" label on page 1 (allowed, a detached
    copy has no role)."""
    copies = _shared_id_grid(db, user, detached_on=(0, 1))
    matrix_object_service.update_row(
        db, _row(db, copies[1], 2).id, MatrixRowDefUpdate(label="?"), user
    )
    db.expire_all()
    return copies


def test_sharing_again_is_refused_when_a_copy_has_an_invalid_label(db, user):
    """Re-sharing keeps every copy's rows: the page-1 copy would carry the role
    with its "?"."""
    copies = _detached_id_grid_with_bad_copy(db, user)

    with pytest.raises(HTTPException) as exc:
        page_object_service.share_object(db, copies[0].id, user)

    assert exc.value.status_code == 409
    assert "(row 3)" in exc.value.detail
    db.expire_all()
    assert not db.get(PageObject, copies[1].id).shared_across_pages


def test_reconnecting_from_a_copy_with_an_invalid_label_is_refused(db, user):
    """RECONNECT rebuilds every copy from the page-1 one: the role holder
    would get its "?"."""
    copies = _detached_id_grid_with_bad_copy(db, user)

    with pytest.raises(HTTPException) as exc:
        page_object_service.relink_object(db, copies[1].id, "RECONNECT", user)

    assert exc.value.status_code == 409
    assert _row(db, copies[0], 2).label == "2"


def test_reconnecting_from_the_valid_copy_is_accepted(db, user):
    """RECONNECT from the role holder overwrites the "?" copy with valid rows."""
    copies = _detached_id_grid_with_bad_copy(db, user)

    page_object_service.relink_object(db, copies[0].id, "RECONNECT", user)

    assert _row(db, copies[1], 2).label == "2"


def test_re_linking_a_group_without_the_role_is_free(db, user):
    pages = _setup(db, user, pages=2)
    group_id = uuid.uuid4()
    copies = [
        _add_matrix(db, page, _digits(), shared_group_id=group_id)
        for page in pages
    ]
    matrix_object_service.update_row(
        db, _row(db, copies[1], 2).id, MatrixRowDefUpdate(label="?"), user
    )

    page_object_service.share_object(db, copies[0].id, user)

    db.expire_all()
    assert db.get(PageObject, copies[1].id).shared_across_pages
