# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for shared "template page" objects.

Covers the sharing contract against real Postgres (the models use postgresql.UUID
+ native enums, so SQLite is not an option):

  0. creating with the "show on all pages" flag makes the object a linked
     template (group_id + shared_across_pages); without it the object is plain;
  1. share() relinks / links a fresh object (group_id + shared_across_pages)
     WITHOUT copying it onto existing pages;
  2. add_page lazily copies each linked group's template onto the new page as a
     linked member (typed config included);
  3. PATCH of base fields (geometry + z_index/label/…) on a linked member
     propagates to the linked siblings; detached members are untouched;
  4. deleting a linked member removes the whole linked group but leaves detached
     members in place;
  5. unshare() detaches every member (keeps group_id); new pages stop receiving
     copies;
  6. cloning a variant does NOT leak shared_group_id — groups are remapped to
     fresh ids, stay internal to the clone, and preserve shared_across_pages.

Typed-config resync on save is also covered:

  7. resyncing a linked member rebuilds every linked sibling's typed config (+
     children: QUESTION options / MATRIX rows/cols/cells) verbatim from it, with
     no orphans; for IMAGE the binary is duplicated and a stale file removed;
     detached members, other groups and non-linked objects are untouched.

Relink with a yes/no choice is also covered:

  8. relink-info counts the still-detached siblings; RECONNECT re-links them and
     overwrites each (base + typed config, IMAGE binary duplicated) from the active
     object without copying onto existing pages; FRESH leaves the siblings
     independent and starts a new group for the active object; the single-member
     case relinks directly.

Requires TEST_DATABASE_URL — see conftest.py (the suite is skipped without it).
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.models.enums import (
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectType,
    ProjectRole,
    QuestionType,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.image_object_config import ImageObjectConfig
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.text_object_config import TextObjectConfig
from app.repositories import form_variant_page_repo, page_object_repo
from app.schemas.form_variant import FormVariantCreate
from app.schemas.page_object import (
    PageObjectUpdate,
    TextObjectConfigCreate,
    TextPageObjectCreate,
)
from app.services import (
    form_variant_service,
    page_object_service,
    storage_service,
    text_object_service,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_project(db, user, *, name="Shared IT project") -> FormProject:
    project = FormProject(owner_id=user.id, type=FormProjectType.EXAM, name=name)
    db.add(project)
    db.flush()
    # Membership is the access source of truth: the owner needs an
    # OWNER row so ``require_project_member`` authorizes the object operations.
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=user.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_draft_variant(db, project, user):
    """A DRAFT variant with its auto-provisioned first page, via the service."""
    variant = form_variant_service.create_variant(
        db, project.id, FormVariantCreate(name="v1"), user
    )
    pages = form_variant_page_repo.list_for_variant(db, variant.id)
    return variant, pages[0]


def _blank_page(db, variant_id, page_index):
    """A second authoring page built directly (no add_page lazy replication)."""
    return form_variant_page_repo.add_blank_page(db, variant_id, page_index=page_index)


def _shared(obj, group_id, *, linked):
    """Pin an object's shared-group fields and flush."""
    obj.shared_group_id = group_id
    obj.shared_across_pages = linked
    return obj


def _add_text_object(
    db, page_id, *, label="T", x=10.0, y=20.0, text="hello",
    shared_group_id=None, shared_across_pages=False,
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.TEXT,
        x=x,
        y=y,
        width=50.0,
        height=20.0,
        label=label,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    db.add(TextObjectConfig(page_object_id=obj.id, text=text))
    db.flush()
    return obj


def _add_question_object(
    db, page_id, *, question_text, options, shared_group_id=None, shared_across_pages=False,
) -> PageObject:
    """options: list of (order_index, option_text, is_correct, score)."""
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.QUESTION,
        x=0, y=0, width=200, height=80,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    cfg = QuestionObjectConfig(
        page_object_id=obj.id,
        question_type=QuestionType.SINGLE_CHOICE,
        question_text=question_text,
    )
    db.add(cfg)
    db.flush()
    for order_index, text, is_correct, score in options:
        db.add(QuestionOption(
            question_object_config_id=cfg.id,
            order_index=order_index, option_text=text,
            is_correct=is_correct, score=score,
        ))
    db.flush()
    return obj


def _add_matrix_object(
    db, page_id, *, rows, cols, cells, rule=MatrixSelectionRule.ONE_PER_ROW,
    shared_group_id=None, shared_across_pages=False,
) -> PageObject:
    """rows/cols: list of (order_index, label). cells: list of (row, col, is_correct)."""
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.MATRIX,
        x=0, y=0, width=200, height=120,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    cfg = MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=len(rows), cols_count=len(cols),
        evaluative=True, selection_rule=rule,
    )
    db.add(cfg)
    db.flush()
    for order_index, label in rows:
        db.add(MatrixRowDef(matrix_object_config_id=cfg.id, order_index=order_index, label=label))
    for order_index, label in cols:
        db.add(MatrixColDef(matrix_object_config_id=cfg.id, order_index=order_index, label=label))
    for row_index, col_index, is_correct in cells:
        db.add(MatrixCellDef(
            matrix_object_config_id=cfg.id,
            row_index=row_index, col_index=col_index, is_correct=is_correct,
        ))
    db.flush()
    return obj


def _add_image_object(
    db, page_id, *, content, ext, shared_group_id=None, shared_across_pages=False,
) -> PageObject:
    """An IMAGE object with a real on-disk file (settings.object_images_dir)."""
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.IMAGE,
        x=0, y=0, width=100, height=100,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    path = storage_service.save_object_image(content, obj.id, ext)
    db.add(ImageObjectConfig(
        page_object_id=obj.id, file_path=str(path),
        original_filename=f"img{ext}", mime_type="image/png",
    ))
    db.flush()
    return obj


def _objects_on(db, page_id) -> list[PageObject]:
    return page_object_repo.list_for_page(db, page_id)


def _reload(db, object_id) -> PageObject | None:
    db.expire_all()
    return db.get(PageObject, object_id)


def _reload_with_configs(db, object_id) -> PageObject | None:
    db.expire_all()
    return page_object_repo.get_with_configs(db, object_id)


# ---------------------------------------------------------------------------
# 0. create with the "show on all pages" flag -> linked template; new pages copy
# ---------------------------------------------------------------------------
def test_create_with_flag_makes_linked_template(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    read = text_object_service.create_text(
        db,
        page1.id,
        TextPageObjectCreate(
            x=60, y=60, width=50, height=20,
            shared_across_pages=True,
            config=TextObjectConfigCreate(text="header"),
        ),
        user,
    )
    created = _reload(db, read.id)
    assert created.shared_group_id is not None
    assert created.shared_across_pages is True
    # The page it was created on is not duplicated; a new page gets a linked copy.
    assert len(_objects_on(db, page1.id)) == 1
    new_page = form_variant_service.add_page(db, variant.id, user)
    copies = _objects_on(db, new_page.id)
    assert len(copies) == 1
    assert copies[0].shared_group_id == created.shared_group_id
    assert copies[0].shared_across_pages is True


def test_create_without_flag_is_plain(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    read = text_object_service.create_text(
        db,
        page1.id,
        TextPageObjectCreate(
            x=60, y=60, width=50, height=20,
            config=TextObjectConfigCreate(text="local"),
        ),
        user,
    )
    created = _reload(db, read.id)
    assert created.shared_group_id is None
    assert created.shared_across_pages is False
    # A new page receives no copy of a non-shared object.
    new_page = form_variant_service.add_page(db, variant.id, user)
    assert len(_objects_on(db, new_page.id)) == 0


# ---------------------------------------------------------------------------
# 1. share() links fresh; existing pages are NOT copied to
# ---------------------------------------------------------------------------
def test_share_links_object_without_copying_existing_pages(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    # A second page that already exists at share-time (must stay empty).
    page2 = form_variant_service.add_page(db, variant.id, user)

    obj = _add_text_object(db, page1.id, label="shared")
    obj_id = obj.id

    page_object_service.share_object(db, obj_id, user)

    shared = _reload(db, obj_id)
    assert shared.shared_group_id is not None
    assert shared.shared_across_pages is True
    # No copy was made onto the page that existed before sharing.
    assert len(_objects_on(db, page1.id)) == 1
    assert len(_objects_on(db, page2.id)) == 0


# ---------------------------------------------------------------------------
# 2. add_page copies the linked template onto the new page as a linked member
# ---------------------------------------------------------------------------
def test_add_page_replicates_linked_template(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    obj = _add_text_object(db, page1.id, label="banner", text="page header")
    page_object_service.share_object(db, obj.id, user)
    group_id = _reload(db, obj.id).shared_group_id

    new_page = form_variant_service.add_page(db, variant.id, user)

    copies = _objects_on(db, new_page.id)
    assert len(copies) == 1
    copy = copies[0]
    # Linked member of the SAME group, fresh id, config deep-copied.
    assert copy.id != obj.id
    assert copy.shared_group_id == group_id
    assert copy.shared_across_pages is True
    assert copy.label == "banner"
    assert copy.text_config is not None
    assert copy.text_config.text == "page header"
    assert copy.text_config.id != obj.text_config.id


# ---------------------------------------------------------------------------
# 3. PATCH base fields on a linked member propagates to linked siblings
# ---------------------------------------------------------------------------
def test_base_field_patch_propagates_to_linked_siblings(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    obj = _add_text_object(db, page1.id, label="orig", x=10.0, y=20.0)
    page_object_service.share_object(db, obj.id, user)
    new_page = form_variant_service.add_page(db, variant.id, user)
    sibling_id = _objects_on(db, new_page.id)[0].id

    # Geometry (drag/resize) + z-order + a presentation field, one PATCH.
    page_object_service.update_object(
        db,
        obj.id,
        PageObjectUpdate(x=111.0, y=222.0, width=80.0, height=90.0, z_index=5, label="renamed"),
        user,
    )

    sib = _reload(db, sibling_id)
    assert (sib.x, sib.y, sib.width, sib.height) == (111.0, 222.0, 80.0, 90.0)
    assert sib.z_index == 5
    assert sib.label == "renamed"


def test_patch_does_not_propagate_to_detached_members(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    linked = _add_text_object(db, page1.id, label="linked", x=1.0, y=1.0)
    detached = _add_text_object(db, page1.id, label="detached", x=2.0, y=2.0)
    # Same group, but one member is detached (shared_across_pages=False) — the
    # mixed state that resync and relink can produce; the propagation must skip it.
    gid = uuid.uuid4()
    linked.shared_group_id = gid
    linked.shared_across_pages = True
    detached.shared_group_id = gid
    detached.shared_across_pages = False
    db.flush()

    page_object_service.update_object(db, linked.id, PageObjectUpdate(x=600.0, y=100.0), user)

    assert _reload(db, detached.id).x == 2.0  # untouched


# ---------------------------------------------------------------------------
# 4. delete a linked member -> whole linked group gone; detached survive
# ---------------------------------------------------------------------------
def test_delete_linked_member_removes_whole_group(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    obj = _add_text_object(db, page1.id, label="del")
    page_object_service.share_object(db, obj.id, user)
    new_page = form_variant_service.add_page(db, variant.id, user)
    sibling_id = _objects_on(db, new_page.id)[0].id

    page_object_service.delete_object(db, obj.id, user)

    assert _reload(db, obj.id) is None
    assert _reload(db, sibling_id) is None
    assert len(_objects_on(db, page1.id)) == 0
    assert len(_objects_on(db, new_page.id)) == 0


def test_delete_linked_member_leaves_detached_members(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    linked = _add_text_object(db, page1.id, label="linked")
    detached = _add_text_object(db, page1.id, label="detached")
    gid = uuid.uuid4()
    linked.shared_group_id = gid
    linked.shared_across_pages = True
    detached.shared_group_id = gid
    detached.shared_across_pages = False
    db.flush()

    page_object_service.delete_object(db, linked.id, user)

    assert _reload(db, linked.id) is None
    survivor = _reload(db, detached.id)
    assert survivor is not None
    assert survivor.shared_group_id == gid  # still remembers its origin group


# ---------------------------------------------------------------------------
# 5. unshare() detaches all members (keeps group_id); new pages get no copies
# ---------------------------------------------------------------------------
def test_unshare_detaches_all_and_stops_replication(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    obj = _add_text_object(db, page1.id, label="u")
    page_object_service.share_object(db, obj.id, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    sibling_id = _objects_on(db, page2.id)[0].id
    group_id = _reload(db, obj.id).shared_group_id

    page_object_service.unshare_object(db, obj.id, user)

    a, b = _reload(db, obj.id), _reload(db, sibling_id)
    assert a.shared_across_pages is False and b.shared_across_pages is False
    # group_id is kept (detached, not cleared).
    assert a.shared_group_id == group_id and b.shared_group_id == group_id

    # A page added after unlink receives no copies.
    page3 = form_variant_service.add_page(db, variant.id, user)
    assert len(_objects_on(db, page3.id)) == 0


# ---------------------------------------------------------------------------
# 6. clone variant -> no group_id leak; groups remapped + internal to the clone
# ---------------------------------------------------------------------------
def test_clone_variant_remaps_group_ids_without_leak(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    obj = _add_text_object(db, page1.id, label="shared")
    page_object_service.share_object(db, obj.id, user)
    # A second linked member on a second page (same group spans two pages).
    form_variant_service.add_page(db, variant.id, user)
    source_group_ids = {
        o.shared_group_id
        for p in form_variant_page_repo.list_for_variant(db, variant.id)
        for o in _objects_on(db, p.id)
        if o.shared_group_id is not None
    }
    assert len(source_group_ids) == 1  # one shared group, two linked members

    clone = form_variant_service.clone_variant(db, variant.id, user)

    clone_objects = [
        o
        for p in form_variant_page_repo.list_for_variant(db, clone.id)
        for o in _objects_on(db, p.id)
    ]
    assert len(clone_objects) == 2  # both pages mirrored, both members copied

    clone_group_ids = {o.shared_group_id for o in clone_objects}
    # All copies are linked and share ONE new group id, distinct from the source.
    assert all(o.shared_across_pages is True for o in clone_objects)
    assert len(clone_group_ids) == 1
    assert clone_group_ids.isdisjoint(source_group_ids)  # no leak

    # The remapped group is internal to the clone: no source object shares it.
    new_group_id = clone_group_ids.pop()
    members = page_object_repo.list_by_shared_group(db, new_group_id)
    member_pages = {m.form_variant_page_id for m in members}
    clone_page_ids = {p.id for p in form_variant_page_repo.list_for_variant(db, clone.id)}
    assert member_pages.issubset(clone_page_ids)


# ===========================================================================
# Typed-config resync to linked siblings on save
# ===========================================================================


# ---------------------------------------------------------------------------
# 7a. TEXT resync: siblings match the template; detached + other groups untouched
# ---------------------------------------------------------------------------
def test_resync_text_aligns_linked_only(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    gid, other_gid = uuid.uuid4(), uuid.uuid4()

    template = _add_text_object(db, page1.id, text="A", shared_group_id=gid, shared_across_pages=True)
    sibling = _add_text_object(db, page2.id, text="B", shared_group_id=gid, shared_across_pages=True)
    detached = _add_text_object(db, page1.id, text="D", shared_group_id=gid, shared_across_pages=False)
    other = _add_text_object(db, page1.id, text="O", shared_group_id=other_gid, shared_across_pages=True)

    page_object_service.resync_shared_group_typed_config(db, template.id, user)

    assert _reload_with_configs(db, sibling.id).text_config.text == "A"  # aligned
    assert _reload_with_configs(db, detached.id).text_config.text == "D"  # untouched
    assert _reload_with_configs(db, other.id).text_config.text == "O"     # untouched


# ---------------------------------------------------------------------------
# 7b. QUESTION resync: options replaced wholesale, no orphans
# ---------------------------------------------------------------------------
def test_resync_question_replaces_options(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    gid = uuid.uuid4()

    template = _add_question_object(
        db, page1.id, question_text="T1",
        options=[(0, "a", False, None), (1, "b", True, 1.0)],
        shared_group_id=gid, shared_across_pages=True,
    )
    sibling = _add_question_object(
        db, page2.id, question_text="OLD",
        options=[(0, "x", False, None)],  # different text + a single option
        shared_group_id=gid, shared_across_pages=True,
    )

    page_object_service.resync_shared_group_typed_config(db, template.id, user)

    sib = _reload_with_configs(db, sibling.id)
    assert sib.question_config.question_text == "T1"
    opts = sorted(sib.question_config.options, key=lambda o: o.order_index)
    assert [(o.order_index, o.option_text, o.is_correct, o.score) for o in opts] == [
        (0, "a", False, None),
        (1, "b", True, 1.0),
    ]
    # No orphaned option rows survive for this object's (new) config.
    assert len(opts) == 2


# ---------------------------------------------------------------------------
# 7c. MATRIX resync: rows/cols/cells replaced wholesale
# ---------------------------------------------------------------------------
def test_resync_matrix_replaces_children(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    gid = uuid.uuid4()

    template = _add_matrix_object(
        db, page1.id,
        rows=[(0, "R0"), (1, "R1")], cols=[(0, "C0"), (1, "C1")],
        cells=[(0, 0, True), (1, 1, True)],
        shared_group_id=gid, shared_across_pages=True,
    )
    sibling = _add_matrix_object(
        db, page2.id,
        rows=[(0, "old")], cols=[(0, "old")], cells=[(0, 0, False)],
        shared_group_id=gid, shared_across_pages=True,
    )

    page_object_service.resync_shared_group_typed_config(db, template.id, user)

    sib = _reload_with_configs(db, sibling.id)
    cfg = sib.matrix_config
    assert (cfg.rows_count, cfg.cols_count) == (2, 2)
    assert sorted((r.order_index, r.label) for r in cfg.rows) == [(0, "R0"), (1, "R1")]
    assert sorted((c.order_index, c.label) for c in cfg.cols) == [(0, "C0"), (1, "C1")]
    assert sorted((c.row_index, c.col_index, c.is_correct) for c in cfg.cells) == [
        (0, 0, True),
        (1, 1, True),
    ]


# ---------------------------------------------------------------------------
# 7d. IMAGE resync: binary duplicated onto the sibling, stale file removed
# ---------------------------------------------------------------------------
def test_resync_image_duplicates_binary_and_removes_orphan(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "object_images_dir", str(tmp_path))
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    gid = uuid.uuid4()

    template = _add_image_object(db, page1.id, content=b"AAA", ext=".png",
                                 shared_group_id=gid, shared_across_pages=True)
    # Sibling starts with DIFFERENT bytes AND a different extension, so the rebuild
    # lands at a new path and the old .jpg becomes an orphan to remove.
    sibling = _add_image_object(db, page2.id, content=b"BBB", ext=".jpg",
                                shared_group_id=gid, shared_across_pages=True)
    old_jpg = sibling.image_config.file_path

    page_object_service.resync_shared_group_typed_config(db, template.id, user)

    sib = _reload_with_configs(db, sibling.id)
    new_path = sib.image_config.file_path
    assert new_path.endswith(f"{sibling.id}.png")          # template's extension
    assert storage_service.read_object_image(new_path) == b"AAA"  # template's bytes
    assert not storage_service.assert_within_object_images_dir(old_jpg).exists()  # orphan gone


# ---------------------------------------------------------------------------
# 7e. resync on a non-linked object is a no-op
# ---------------------------------------------------------------------------
def test_resync_noop_on_non_linked(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    # Ordinary object (no group) and a detached one (group, but not linked).
    plain = _add_text_object(db, page1.id, text="plain")
    detached = _add_text_object(db, page1.id, text="det", shared_group_id=uuid.uuid4())

    # Neither raises nor changes anything; both return the object's typed read.
    page_object_service.resync_shared_group_typed_config(db, plain.id, user)
    page_object_service.resync_shared_group_typed_config(db, detached.id, user)

    assert _reload_with_configs(db, plain.id).text_config.text == "plain"
    assert _reload_with_configs(db, detached.id).text_config.text == "det"


# ===========================================================================
# Relink with RECONNECT (yes) / FRESH (no) choice
# ===========================================================================


# ---------------------------------------------------------------------------
# 8a. relink-info reports the number of still-detached siblings
# ---------------------------------------------------------------------------
def test_relink_info_counts_detached_siblings(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    gid = uuid.uuid4()

    active = _add_text_object(db, page1.id, text="A", shared_group_id=gid, shared_across_pages=False)
    _add_text_object(db, page2.id, text="B", shared_group_id=gid, shared_across_pages=False)
    plain = _add_text_object(db, page1.id, text="P")  # never shared

    assert page_object_service.relink_info(db, active.id, user) == {
        "is_detached": True,
        "detached_siblings": 1,
    }
    assert page_object_service.relink_info(db, plain.id, user) == {
        "is_detached": False,
        "detached_siblings": 0,
    }


# ---------------------------------------------------------------------------
# 8b. RECONNECT: re-links + overwrites siblings; existing empty pages stay empty;
#     new pages then receive the copy
# ---------------------------------------------------------------------------
def test_relink_reconnect_overwrites_and_relinks(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    page3 = _blank_page(db, variant.id, 3)  # has NO copy of the group
    gid = uuid.uuid4()

    active = _add_text_object(db, page1.id, label="ACT", x=11.0, y=22.0, text="ACTIVE",
                              shared_group_id=gid, shared_across_pages=False)
    sibling = _add_text_object(db, page2.id, label="OLD", x=99.0, y=88.0, text="OLD",
                               shared_group_id=gid, shared_across_pages=False)

    page_object_service.relink_object(db, active.id, "RECONNECT", user)

    a = _reload(db, active.id)
    s = _reload_with_configs(db, sibling.id)
    assert a.shared_across_pages is True and a.shared_group_id == gid
    # Sibling re-linked AND overwritten from the active object (base + typed).
    assert s.shared_across_pages is True
    assert (s.x, s.y, s.label) == (11.0, 22.0, "ACT")
    assert s.text_config.text == "ACTIVE"
    # No copy was created on the page that had none.
    assert len(_objects_on(db, page3.id)) == 0
    # A page added now receives a linked copy.
    new_page = form_variant_service.add_page(db, variant.id, user)
    copies = _objects_on(db, new_page.id)
    assert len(copies) == 1
    assert copies[0].shared_group_id == gid and copies[0].text_config.text == "ACTIVE"


# ---------------------------------------------------------------------------
# 8c. FRESH: siblings stay independent; active starts a new group; only new pages
#     get the new group's copy
# ---------------------------------------------------------------------------
def test_relink_fresh_keeps_siblings_independent(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    gid = uuid.uuid4()

    active = _add_text_object(db, page1.id, text="ACTIVE", shared_group_id=gid, shared_across_pages=False)
    sibling = _add_text_object(db, page2.id, text="OLD", shared_group_id=gid, shared_across_pages=False)

    page_object_service.relink_object(db, active.id, "FRESH", user)

    a = _reload_with_configs(db, active.id)
    s = _reload_with_configs(db, sibling.id)
    # Sibling untouched: still detached, same old group, same config.
    assert s.shared_across_pages is False and s.shared_group_id == gid
    assert s.text_config.text == "OLD"
    # Active relinked on a brand-new group.
    assert a.shared_across_pages is True
    assert a.shared_group_id is not None and a.shared_group_id != gid

    # A new page receives ONLY the active's new-group copy (the old group has no
    # linked members, so it is not replicated).
    new_page = form_variant_service.add_page(db, variant.id, user)
    copies = _objects_on(db, new_page.id)
    assert len(copies) == 1
    assert copies[0].shared_group_id == a.shared_group_id
    assert copies[0].text_config.text == "ACTIVE"


# ---------------------------------------------------------------------------
# 8d. RECONNECT on the only remaining member: direct relink, no error
# ---------------------------------------------------------------------------
def test_relink_reconnect_single_member(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    gid = uuid.uuid4()

    active = _add_text_object(db, page1.id, text="solo", shared_group_id=gid, shared_across_pages=False)
    assert page_object_service.relink_info(db, active.id, user)["detached_siblings"] == 0

    page_object_service.relink_object(db, active.id, "RECONNECT", user)

    a = _reload(db, active.id)
    assert a.shared_across_pages is True and a.shared_group_id == gid
    new_page = form_variant_service.add_page(db, variant.id, user)
    assert len(_objects_on(db, new_page.id)) == 1


# ---------------------------------------------------------------------------
# 8e. RECONNECT IMAGE: sibling binary duplicated from the active object, no orphan
# ---------------------------------------------------------------------------
def test_relink_reconnect_image_independent_binaries(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "object_images_dir", str(tmp_path))
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = _blank_page(db, variant.id, 2)
    gid = uuid.uuid4()

    active = _add_image_object(db, page1.id, content=b"AAA", ext=".png",
                               shared_group_id=gid, shared_across_pages=False)
    sibling = _add_image_object(db, page2.id, content=b"BBB", ext=".png",
                                shared_group_id=gid, shared_across_pages=False)

    page_object_service.relink_object(db, active.id, "RECONNECT", user)

    s = _reload_with_configs(db, sibling.id)
    assert s.shared_across_pages is True
    # Sibling's binary now matches the active object's, but is its OWN file.
    assert s.image_config.file_path.endswith(f"{sibling.id}.png")
    assert storage_service.read_object_image(s.image_config.file_path) == b"AAA"
    # The active object keeps its independent file (deleting one never hits the other).
    active_path = _reload_with_configs(db, active.id).image_config.file_path
    assert active_path != s.image_config.file_path
    assert storage_service.read_object_image(active_path) == b"AAA"
