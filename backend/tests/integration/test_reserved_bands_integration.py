# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reserved bands in authoring and publishing.

The four 15 mm bands along the sides of every page are for the alignment
marks: no object may sit in them, even partly, nor off the page.

  * **create** — each of the six object types is refused with 409
    ``OBJECT_IN_RESERVED_BAND`` (``page_index``) when it crosses any side or
    lies off the page, nothing saved; on the edge of the area it is accepted;
  * **PATCH** — a geometry change must leave the object wholly outside the
    bands; an older object already in a band still takes label, z-index,
    visibility and lock edits;
  * **linked copies** — the new geometry is checked on the page of every
    linked copy it propagates to, before anything is written;
  * **system copies** — clone, page duplication and a new page with linked
    replicas carry an object in a band as it is;
  * **publish** — refused with 409 ``OBJECTS_IN_RESERVED_BANDS`` listing every
    offending object (hidden ones too) by page and z-order; accepted once
    they are moved. A published variant is not touched by the rule.

Objects "authored before the rule" are written through the ORM, the only way
to put one in a band.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.enums import (
    CodeType,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectType,
    ProjectRole,
    QuestionType,
)
from app.models.form_variant import FormVariant
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.page_object import PageObject
from app.models.text_object_config import TextObjectConfig
from app.repositories import form_variant_page_repo, page_object_repo
from app.schemas.form_variant import FormVariantCreate
from app.schemas.matrix_object import (
    MatrixColDefCreate,
    MatrixObjectConfigCreate,
    MatrixPageObjectCreate,
    MatrixRowDefCreate,
)
from app.schemas.page_object import (
    CodeObjectConfigCreate,
    CodePageObjectCreate,
    FreeTextAreaConfigCreate,
    FreeTextAreaPageObjectCreate,
    ImagePageObjectCreate,
    PageObjectUpdate,
    TextObjectConfigCreate,
    TextPageObjectCreate,
)
from app.schemas.question_object import (
    QuestionObjectConfigCreate,
    QuestionOptionCreate,
    QuestionPageObjectCreate,
)
from app.services import (
    code_object_service,
    form_variant_export_service,
    form_variant_service,
    free_text_area_service,
    image_object_service,
    matrix_object_service,
    page_object_service,
    question_object_service,
    text_object_service,
)
from app.services.form_variant_export_service import ExportMode

SAVE_CODE = "OBJECT_IN_RESERVED_BAND"
PUBLISH_CODE = "OBJECTS_IN_RESERVED_BANDS"

# On an A4 page at 96 dpi the area is x 57..737, y 57..1066.
INSIDE = {"x": 100.0, "y": 100.0, "width": 100.0, "height": 50.0}
IN_BAND = {"x": 20.0, "y": 100.0, "width": 100.0, "height": 50.0}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_CREATORS = {
    "TEXT": lambda db, pid, g, u: text_object_service.create_text(
        db, pid, TextPageObjectCreate(**g, config=TextObjectConfigCreate(text="t")), u
    ),
    "CODE": lambda db, pid, g, u: code_object_service.create_code(
        db, pid,
        CodePageObjectCreate(
            **g, config=CodeObjectConfigCreate(code_type=CodeType.QR, value="V")
        ),
        u,
    ),
    "FREE_TEXT_AREA": lambda db, pid, g, u: free_text_area_service.create_free_text_area(
        db, pid, FreeTextAreaPageObjectCreate(**g, config=FreeTextAreaConfigCreate()), u
    ),
    "QUESTION": lambda db, pid, g, u: question_object_service.create_question(
        db, pid,
        QuestionPageObjectCreate(
            **g,
            config=QuestionObjectConfigCreate(
                question_type=QuestionType.SINGLE_CHOICE,
                question_text="Q?",
                options=[
                    QuestionOptionCreate(order_index=0, option_text="a", is_correct=True),
                    QuestionOptionCreate(order_index=1, option_text="b"),
                ],
            ),
        ),
        u,
    ),
    "MATRIX": lambda db, pid, g, u: matrix_object_service.create_matrix(
        db, pid,
        MatrixPageObjectCreate(
            **g,
            config=MatrixObjectConfigCreate(
                rows_count=2, cols_count=2, evaluative=False,
                selection_rule=MatrixSelectionRule.ONE_PER_ROW,
                rows=[MatrixRowDefCreate(order_index=i, label=f"R{i}") for i in range(2)],
                cols=[MatrixColDefCreate(order_index=i, label=f"C{i}") for i in range(2)],
            ),
        ),
        u,
    ),
    "IMAGE": lambda db, pid, g, u: image_object_service.create_image(
        db, pid, ImagePageObjectCreate(**g), u
    ),
}


@pytest.fixture
def variant(db, page) -> FormVariant:
    """The DRAFT variant of the conftest ``page`` (no size: A4 by default)."""
    return db.get(FormVariant, page.form_variant_id)


def _orm_text(db, page_id, *, x, y, width=100.0, height=50.0, label="OLD",
              z_index=0, visible=True, shared_group_id=None, linked=False) -> PageObject:
    """A TEXT written straight to the DB: an object authored before the rule."""
    obj = PageObject(
        form_variant_page_id=page_id, object_type=PageObjectType.TEXT,
        x=x, y=y, width=width, height=height, label=label, z_index=z_index,
        visible=visible, shared_group_id=shared_group_id,
        shared_across_pages=linked,
    )
    db.add(obj)
    db.flush()
    db.add(TextObjectConfig(page_object_id=obj.id, text="old"))
    db.flush()
    return obj


def _geometry(db, object_id) -> tuple[float, float, float, float]:
    obj = page_object_repo.get(db, object_id)
    db.refresh(obj)
    return (obj.x, obj.y, obj.width, obj.height)


def _conflict(exc: pytest.ExceptionInfo) -> dict:
    assert exc.value.status_code == 409
    return exc.value.detail


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

_REFUSED = {
    "left": {"x": 56.0, "y": 100.0, "width": 100.0, "height": 50.0},
    "top": {"x": 100.0, "y": 56.5, "width": 100.0, "height": 50.0},
    "right": {"x": 640.0, "y": 100.0, "width": 98.0, "height": 50.0},    # ends at 738
    "bottom": {"x": 100.0, "y": 1017.0, "width": 100.0, "height": 50.0},  # ends at 1067
    "off the page": {"x": 999.0, "y": 100.0, "width": 100.0, "height": 50.0},
}


@pytest.mark.parametrize("kind", sorted(_CREATORS))
@pytest.mark.parametrize("where", sorted(_REFUSED))
def test_create_in_a_band_is_refused_and_nothing_saved(db, page, user, kind, where):
    with pytest.raises(HTTPException) as exc:
        _CREATORS[kind](db, page.id, _REFUSED[where], user)
    detail = _conflict(exc)
    assert detail["code"] == SAVE_CODE
    assert detail["page_index"] == page.page_index
    assert "reserved bands" in detail["message"]
    assert page_object_repo.list_for_page(db, page.id) == []


@pytest.mark.parametrize("kind", sorted(_CREATORS))
def test_create_on_the_edge_of_the_area_is_accepted(db, page, user, kind):
    # The whole usable area: every side on its edge.
    edge = {"x": 57.0, "y": 57.0, "width": 680.0, "height": 1009.0}
    read = _CREATORS[kind](db, page.id, edge, user)
    assert (read.x, read.y, read.width, read.height) == (57.0, 57.0, 680.0, 1009.0)


def test_create_uses_the_size_of_its_page(db, user):
    """A page with its own size: the right band starts at width - 57."""
    variant = _services_variant(db, user)
    pg = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    pg.width_px = 600
    db.flush()
    with pytest.raises(HTTPException) as exc:
        _CREATORS["TEXT"](db, pg.id, {"x": 450.0, "y": 100.0, "width": 100.0, "height": 50.0}, user)
    assert _conflict(exc)["code"] == SAVE_CODE
    _CREATORS["TEXT"](db, pg.id, {"x": 443.0, "y": 100.0, "width": 100.0, "height": 50.0}, user)


def _services_variant(db, user) -> FormVariant:
    project = FormProject(owner_id=user.id, type=FormProjectType.EXAM, name="Bands")
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.OWNER))
    db.flush()
    return form_variant_service.create_variant(db, project.id, FormVariantCreate(name="v1"), user)


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


def test_patch_into_a_band_is_refused_and_nothing_changes(db, page, user):
    read = _CREATORS["TEXT"](db, page.id, INSIDE, user)
    with pytest.raises(HTTPException) as exc:
        page_object_service.update_object(
            db, read.id, PageObjectUpdate(x=10.0, label="moved"), user
        )
    assert _conflict(exc)["code"] == SAVE_CODE
    assert _geometry(db, read.id) == (100.0, 100.0, 100.0, 50.0)
    assert page_object_repo.get(db, read.id).label is None


def test_patch_growing_past_the_area_is_refused(db, page, user):
    read = _CREATORS["TEXT"](db, page.id, INSIDE, user)
    with pytest.raises(HTTPException) as exc:
        page_object_service.update_object(db, read.id, PageObjectUpdate(width=700.0), user)
    assert _conflict(exc)["code"] == SAVE_CODE


def test_old_object_in_a_band_takes_non_geometry_edits(db, page, user):
    old = _orm_text(db, page.id, x=20.0, y=100.0)
    read = page_object_service.update_object(
        db, old.id,
        PageObjectUpdate(label="renamed", z_index=3, visible=False, locked=True,
                         description="d"),
        user,
    )
    assert (read.label, read.z_index, read.visible, read.locked) == ("renamed", 3, False, True)
    assert _geometry(db, old.id) == (20.0, 100.0, 100.0, 50.0)


def test_old_object_in_a_band_refuses_a_geometry_that_stays_in_it(db, page, user):
    old = _orm_text(db, page.id, x=20.0, y=100.0)
    with pytest.raises(HTTPException) as exc:
        # moved down, still in the left band
        page_object_service.update_object(db, old.id, PageObjectUpdate(y=300.0), user)
    assert _conflict(exc)["code"] == SAVE_CODE
    assert _geometry(db, old.id) == (20.0, 100.0, 100.0, 50.0)


def test_old_object_moved_out_of_the_band_is_accepted(db, page, user):
    old = _orm_text(db, page.id, x=20.0, y=100.0)
    read = page_object_service.update_object(db, old.id, PageObjectUpdate(x=57.0), user)
    assert (read.x, read.y) == (57.0, 100.0)


# ---------------------------------------------------------------------------
# Linked copies
# ---------------------------------------------------------------------------


def test_linked_patch_is_checked_on_every_copy_page_before_writing(db, user):
    variant = _services_variant(db, user)
    page1 = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    page2 = form_variant_page_repo.add_blank_page(db, variant.id, page_index=2)
    page2.width_px = 600  # narrower: its right band starts at 543
    db.flush()
    gid = uuid.uuid4()
    a = _orm_text(db, page1.id, x=100.0, y=100.0, shared_group_id=gid, linked=True)
    b = _orm_text(db, page2.id, x=100.0, y=100.0, shared_group_id=gid, linked=True)

    with pytest.raises(HTTPException) as exc:
        # fine on page 1 (ends at 600 ≤ 737), in the band of page 2 (> 543)
        page_object_service.update_object(db, a.id, PageObjectUpdate(x=500.0), user)
    detail = _conflict(exc)
    assert detail["code"] == SAVE_CODE
    assert detail["page_index"] == 2
    assert _geometry(db, a.id) == (100.0, 100.0, 100.0, 50.0)
    assert _geometry(db, b.id) == (100.0, 100.0, 100.0, 50.0)


def test_linked_patch_ignores_detached_copies(db, user):
    variant = _services_variant(db, user)
    page1 = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    page2 = form_variant_page_repo.add_blank_page(db, variant.id, page_index=2)
    page2.width_px = 600
    db.flush()
    gid = uuid.uuid4()
    a = _orm_text(db, page1.id, x=100.0, y=100.0, shared_group_id=gid, linked=True)
    detached = _orm_text(db, page2.id, x=100.0, y=100.0, shared_group_id=gid, linked=False)

    page_object_service.update_object(db, a.id, PageObjectUpdate(x=500.0), user)
    assert _geometry(db, a.id)[0] == 500.0
    assert _geometry(db, detached.id)[0] == 100.0


# ---------------------------------------------------------------------------
# System copies
# ---------------------------------------------------------------------------


def test_clone_and_page_duplication_carry_a_band_object_as_it_is(db, user):
    variant = _services_variant(db, user)
    page1 = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    _orm_text(db, page1.id, x=20.0, y=100.0, label="IN-BAND")

    clone = form_variant_service.clone_variant(db, variant.id, user)
    clone_page = form_variant_page_repo.list_for_variant(db, clone.id)[0]
    (copied,) = page_object_repo.list_for_page(db, clone_page.id)
    assert (copied.label, copied.x) == ("IN-BAND", 20.0)

    dup = form_variant_service.duplicate_page(db, variant.id, page1.id, user)
    (dup_obj,) = page_object_repo.list_for_page(db, dup.id)
    assert (dup_obj.label, dup_obj.x) == ("IN-BAND", 20.0)


def test_new_page_replicates_a_linked_band_object_as_it_is(db, user):
    variant = _services_variant(db, user)
    page1 = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    _orm_text(db, page1.id, x=20.0, y=100.0, label="HEADER",
              shared_group_id=uuid.uuid4(), linked=True)

    new_page = form_variant_service.add_page(db, variant.id, user)
    (replica,) = page_object_repo.list_for_page(db, new_page.id)
    assert (replica.label, replica.x) == ("HEADER", 20.0)


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def test_publish_lists_every_object_in_the_bands(db, user):
    variant = _services_variant(db, user)
    page1 = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    page3 = form_variant_page_repo.add_blank_page(db, variant.id, page_index=3)
    form_variant_page_repo.add_blank_page(db, variant.id, page_index=2)
    q01 = _orm_text(db, page1.id, x=20.0, y=100.0, label="Q01")
    _orm_text(db, page1.id, x=100.0, y=300.0, label="OK")  # inside: not listed
    mm = _orm_text(db, page3.id, x=700.0, y=100.0, label="M-MULTI", z_index=2)
    hidden = _orm_text(db, page3.id, x=100.0, y=1100.0, label=None, z_index=1,
                       visible=False)

    with pytest.raises(HTTPException) as exc:
        form_variant_service.publish_variant(db, variant.id, user)
    detail = _conflict(exc)
    assert detail["code"] == PUBLISH_CODE
    assert detail["objects"] == [
        {"id": str(q01.id), "label": "Q01", "object_type": "TEXT", "page_index": 1},
        {"id": str(hidden.id), "label": None, "object_type": "TEXT", "page_index": 3},
        {"id": str(mm.id), "label": "M-MULTI", "object_type": "TEXT", "page_index": 3},
    ]
    assert detail["message"] == (
        "Cannot publish: 3 objects are in the reserved bands: Q01 (page 1), "
        "TEXT (page 3), M-MULTI (page 3). Move them inside the page area."
    )
    db.refresh(variant)
    assert variant.status is FormVariantStatus.DRAFT


def test_publish_succeeds_once_the_objects_are_moved(db, user):
    variant = _services_variant(db, user)
    page1 = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    old = _orm_text(db, page1.id, x=20.0, y=100.0, label="Q01")

    with pytest.raises(HTTPException) as exc:
        form_variant_service.publish_variant(db, variant.id, user)
    assert _conflict(exc)["message"] == (
        "Cannot publish: 1 object is in the reserved bands: Q01 (page 1). "
        "Move it inside the page area."
    )

    page_object_service.update_object(db, old.id, PageObjectUpdate(x=60.0), user)
    published = form_variant_service.publish_variant(db, variant.id, user)
    assert published.status is FormVariantStatus.PUBLISHED


def test_a_published_variant_with_a_band_object_is_left_as_it_is(db, user):
    """The rule never moves anything: a variant published before it keeps its
    status and its object, and still exports."""
    variant = _services_variant(db, user)
    page1 = form_variant_page_repo.list_for_variant(db, variant.id)[0]
    _orm_text(db, page1.id, x=100.0, y=100.0, label="OK")
    form_variant_service.publish_variant(db, variant.id, user)
    old = _orm_text(db, page1.id, x=20.0, y=100.0, label="OLD")  # authored before the rule

    pdf, _ = form_variant_export_service.export_pdf(db, variant.id, user, ExportMode.STUDENT)
    assert pdf.startswith(b"%PDF")
    db.refresh(variant)
    assert variant.status is FormVariantStatus.PUBLISHED
    assert _geometry(db, old.id) == (20.0, 100.0, 100.0, 50.0)


def test_conftest_page_without_size_counts_as_a4(db, page, variant, user):
    """A page with no ``width_px`` / ``height_px`` is A4 at 96 dpi, as in the
    export: 737 is the right edge of its area."""
    assert page.width_px is None
    _CREATORS["TEXT"](db, page.id, {"x": 637.0, "y": 100.0, "width": 100.0, "height": 50.0}, user)
    with pytest.raises(HTTPException):
        _CREATORS["TEXT"](db, page.id, {"x": 638.0, "y": 100.0, "width": 100.0, "height": 50.0}, user)
