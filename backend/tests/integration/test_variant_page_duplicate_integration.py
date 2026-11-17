# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for variant page duplication.

Covers the service contract against real Postgres (the models use postgresql.UUID
+ native enums, so SQLite is not an option). duplicate_page is
"identical to source": it replicates EXACTLY the source page's objects onto a new
page placed immediately AFTER the source, then renumbers 1..n — with a PER-OBJECT
group rule (page_object_service.duplicate_objects_to_page):

  - a LIVE linked-group member → the copy JOINS THE SAME group as a new linked,
    synced member (shared_group_id PRESERVED, shared_across_pages=True);
  - an ordinary object or a detached copy → an INDEPENDENT page-local deep copy
    (shared_group_id=None, shared_across_pages=False);
  - IMAGE binaries are duplicated per page (the copy gets its OWN file);
  - NO lazy "show on all pages" replication runs, so a group active on the variant
    but NOT present on the source page does NOT appear on the duplicate.

Gating mirrors add_page: DRAFT only (else 409); a foreign page id -> 404.

Requires TEST_DATABASE_URL — see conftest.py (the suite is skipped without it).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.enums import (
    FormProjectType,
    FormVariantStatus,
    PageObjectType,
    ProjectRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.image_object_config import ImageObjectConfig
from app.models.page_object import PageObject
from app.models.text_object_config import TextObjectConfig
from app.repositories import form_variant_page_repo, page_object_repo
from app.schemas.form_variant import FormVariantCreate
from app.schemas.page_object import PageObjectUpdate
from app.services import form_variant_service, page_object_service, storage_service


# ---------------------------------------------------------------------------
# Builders (mirror test_variant_page_reorder_remove_integration.py)
# ---------------------------------------------------------------------------


def _make_project(db, user, *, name="Duplicate IT project") -> FormProject:
    project = FormProject(owner_id=user.id, type=FormProjectType.EXAM, name=name)
    db.add(project)
    db.flush()
    # Membership is the access source of truth: the owner needs an
    # OWNER row so ``require_project_member`` authorizes the page operations.
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=user.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_draft_variant(db, project, user):
    """A DRAFT variant with its auto-provisioned first page (page_index 1)."""
    variant = form_variant_service.create_variant(
        db, project.id, FormVariantCreate(name="v1"), user
    )
    pages = form_variant_page_repo.list_for_variant(db, variant.id)
    return variant, pages[0]


def _add_text_object(
    db, page_id, *, label="T", text="hello",
    shared_group_id=None, shared_across_pages=False,
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.TEXT,
        x=10.0, y=20.0, width=50.0, height=20.0,
        label=label,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    db.add(TextObjectConfig(page_object_id=obj.id, text=text))
    db.flush()
    return obj


def _add_image_object(db, page_id, *, content, ext, label="IMG") -> PageObject:
    """An IMAGE object with a real on-disk file (settings.object_images_dir)."""
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.IMAGE,
        x=0, y=0, width=100, height=100,
        label=label,
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
    """Fresh read of a page's objects (with typed configs eager-loaded)."""
    db.expire_all()
    return page_object_repo.list_for_page_with_configs(db, page_id)


def _by_label(objects, label) -> PageObject:
    return next(o for o in objects if o.label == label)


def _reload(db, object_id) -> PageObject | None:
    db.expire_all()
    return db.get(PageObject, object_id)


def _indices(db, variant_id) -> list[tuple[uuid.UUID, int]]:
    """(page_id, page_index) for the variant, in page_index order."""
    return [
        (p.id, p.page_index)
        for p in form_variant_page_repo.list_for_variant(db, variant_id)
    ]


# ===========================================================================
# Content replication + per-object group rule
# ===========================================================================


def test_duplicate_replicates_with_per_object_group_rule(
    db, user, tmp_path, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "object_images_dir", str(tmp_path))

    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    ordinary = _add_text_object(db, page1.id, label="plain", text="ord")
    img = _add_image_object(db, page1.id, content=b"PNGDATA", ext=".png")
    linked = _add_text_object(db, page1.id, label="banner", text="hdr")
    page_object_service.share_object(db, linked.id, user)
    group_id = _reload(db, linked.id).shared_group_id
    detached = _add_text_object(
        db, page1.id, label="det", text="dy",
        shared_group_id=uuid.uuid4(), shared_across_pages=False,
    )

    new_page = form_variant_service.duplicate_page(db, variant.id, page1.id, user)

    copies = _objects_on(db, new_page.id)
    # Identical to source: exactly the four source objects, no extra/duplicated.
    assert len(copies) == 4
    assert {o.id for o in copies}.isdisjoint(
        {ordinary.id, img.id, linked.id, detached.id}
    )

    # The linked member's copy JOINS THE SAME group as a live linked member.
    linked_copy = _by_label(copies, "banner")
    assert linked_copy.shared_group_id == group_id
    assert linked_copy.shared_across_pages is True
    assert linked_copy.text_config.text == "hdr"

    # Ordinary, detached and image copies are INDEPENDENT page-local copies.
    for label in ("plain", "det", "IMG"):
        copy = _by_label(copies, label)
        assert copy.shared_group_id is None, label
        assert copy.shared_across_pages is False, label

    # The IMAGE copy has its OWN duplicated binary at a new (id-keyed) path; the
    # source file is untouched (binary duplicated, not moved).
    img_copy = _by_label(copies, "IMG")
    assert img_copy.image_config.file_path != img.image_config.file_path
    assert storage_service.assert_within_object_images_dir(
        img_copy.image_config.file_path
    ).exists()
    assert storage_service.assert_within_object_images_dir(
        img.image_config.file_path
    ).exists()

    # The source page is unchanged: still four objects, the linked one still linked.
    src_after = _objects_on(db, page1.id)
    assert len(src_after) == 4
    assert _reload(db, linked.id).shared_group_id == group_id
    assert _reload(db, linked.id).shared_across_pages is True


def test_duplicate_ordinary_copy_is_independent_of_source(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    ordinary = _add_text_object(db, page1.id, label="plain", text="original")

    new_page = form_variant_service.duplicate_page(db, variant.id, page1.id, user)
    copy = _by_label(_objects_on(db, new_page.id), "plain")
    assert copy.id != ordinary.id

    # Edit the copy's geometry/label (base) AND its typed config (content).
    page_object_service.update_object(
        db, copy.id, PageObjectUpdate(label="EDITED", x=600.0, y=100.0), user
    )
    reloaded_copy = page_object_repo.get_with_configs(db, copy.id)
    reloaded_copy.text_config.text = "changed"
    db.flush()

    # The source object is untouched — independent rows + typed config.
    src = _reload(db, ordinary.id)
    assert src.label == "plain"
    assert src.x == 10.0
    src_cfg = page_object_repo.get_with_configs(db, ordinary.id)
    assert src_cfg.text_config.text == "original"


def test_duplicate_does_not_replicate_group_absent_from_source(db, user):
    """Option (a): a group active on the variant but NOT on the source page does
    NOT get a member on the duplicate (no lazy replication runs)."""
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    ordinary = _add_text_object(db, page1.id, label="plain", text="x")

    # A linked group whose only member lives on a DIFFERENT page (page2), so the
    # group is active on the variant but absent from the source page (page1).
    page2 = form_variant_service.add_page(db, variant.id, user)
    other = _add_text_object(db, page2.id, label="banner", text="hdr")
    page_object_service.share_object(db, other.id, user)
    group_id = _reload(db, other.id).shared_group_id
    # Sanity: the group really is active on the variant (add_page WOULD replicate it).
    assert group_id in page_object_repo.linked_group_templates_for_variant(
        db, variant.id
    )

    new_page = form_variant_service.duplicate_page(db, variant.id, page1.id, user)

    copies = _objects_on(db, new_page.id)
    # Exactly the source page's one object — and NOT a member of the absent group.
    assert len(copies) == 1
    assert copies[0].label == "plain"
    assert all(o.shared_group_id != group_id for o in copies)
    assert copies[0].id != ordinary.id


# ===========================================================================
# Placement + renumbering
# ===========================================================================


def test_duplicate_inserts_after_source_and_renumbers_contiguous(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    page3 = form_variant_service.add_page(db, variant.id, user)
    assert [idx for _, idx in _indices(db, variant.id)] == [1, 2, 3]

    # Duplicate the MIDDLE page: the copy must land immediately after it.
    new_page = form_variant_service.duplicate_page(db, variant.id, page2.id, user)

    # New page's index == source.page_index + 1; the rest renumber contiguous 1..n.
    assert new_page.page_index == page2.page_index + 1 == 3
    assert _indices(db, variant.id) == [
        (page1.id, 1),
        (page2.id, 2),
        (new_page.id, 3),
        (page3.id, 4),
    ]


# ===========================================================================
# Gating (mirrors add_page)
# ===========================================================================


@pytest.mark.parametrize(
    "bad_status", [FormVariantStatus.PUBLISHED, FormVariantStatus.ARCHIVED]
)
def test_duplicate_on_non_draft_rejected_409(db, user, bad_status):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    variant.status = bad_status
    db.flush()

    with pytest.raises(HTTPException) as exc:
        form_variant_service.duplicate_page(db, variant.id, page1.id, user)
    assert exc.value.status_code == 409
    # No page added.
    assert [idx for _, idx in _indices(db, variant.id)] == [1]


def test_duplicate_foreign_page_rejected_404(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    with pytest.raises(HTTPException) as exc:
        form_variant_service.duplicate_page(db, variant.id, uuid.uuid4(), user)
    assert exc.value.status_code == 404
    # Untouched.
    assert [idx for _, idx in _indices(db, variant.id)] == [1]
