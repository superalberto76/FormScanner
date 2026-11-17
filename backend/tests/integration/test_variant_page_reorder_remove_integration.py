# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for variant page reorder + removal.

Covers the service contract against real Postgres (the models use postgresql.UUID
+ native enums, so SQLite is not an option):

Reorder:
  - persists the requested order and renumbers page_index 1..n contiguous;
  - a missing/foreign id in the permutation -> 400;
  - reorder on a non-DRAFT variant -> 409.

Removal (the scoped delete — NEVER goes through delete_object, which would
remove a linked member's whole group):
  - a linked member on the removed page loses only that page's copy; the siblings
    on other pages stay linked and intact;
  - removing the page that held the lowest-page_index member keeps the group alive
    (the template is dynamic): a page added afterwards replicates from the NEW
    lowest-index member;
  - ordinary/detached objects on the page are deleted (no orphan rows);
  - removing the only page -> 409; renumbering stays contiguous after a removal;
  - removal on a non-DRAFT variant -> 409;
  - an IMAGE object's on-disk binary is unlinked via
    storage_service.delete_object_image, on the right path, after the commit.

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
from app.services import form_variant_service, page_object_service, storage_service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_project(db, user, *, name="Reorder/Remove IT project") -> FormProject:
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


def _add_image_object(db, page_id, *, content, ext) -> PageObject:
    """An IMAGE object with a real on-disk file (settings.object_images_dir)."""
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.IMAGE,
        x=0, y=0, width=100, height=100,
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


def _indices(db, variant_id) -> list[tuple[uuid.UUID, int]]:
    """(page_id, page_index) for the variant, in page_index order."""
    return [
        (p.id, p.page_index)
        for p in form_variant_page_repo.list_for_variant(db, variant_id)
    ]


# ===========================================================================
# Reorder
# ===========================================================================


def test_reorder_persists_order_and_renumbers_contiguous(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    page3 = form_variant_service.add_page(db, variant.id, user)
    assert [idx for _, idx in _indices(db, variant.id)] == [1, 2, 3]

    new_order = [page3.id, page1.id, page2.id]
    result = form_variant_service.reorder_pages(db, variant.id, new_order, user)

    # The returned list is in page_index order and contiguous 1..n.
    assert [r.page_index for r in result] == [1, 2, 3]
    assert [r.id for r in result] == new_order
    # Persisted state agrees after a fresh read.
    assert _indices(db, variant.id) == [
        (page3.id, 1),
        (page1.id, 2),
        (page2.id, 3),
    ]


def test_reorder_with_foreign_id_rejected_400(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)

    # A foreign id replaces a real one: not a permutation of the variant pages.
    bad_order = [page1.id, uuid.uuid4()]
    with pytest.raises(HTTPException) as exc:
        form_variant_service.reorder_pages(db, variant.id, bad_order, user)
    assert exc.value.status_code == 400
    # Order untouched.
    assert _indices(db, variant.id) == [(page1.id, 1), (page2.id, 2)]


def test_reorder_with_missing_id_rejected_400(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)

    # Too few ids (drops page2) — wrong length and not a permutation.
    with pytest.raises(HTTPException) as exc:
        form_variant_service.reorder_pages(db, variant.id, [page1.id], user)
    assert exc.value.status_code == 400
    assert _indices(db, variant.id) == [(page1.id, 1), (page2.id, 2)]


@pytest.mark.parametrize(
    "bad_status", [FormVariantStatus.PUBLISHED, FormVariantStatus.ARCHIVED]
)
def test_reorder_on_non_draft_rejected_409(db, user, bad_status):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    variant.status = bad_status
    db.flush()

    with pytest.raises(HTTPException) as exc:
        form_variant_service.reorder_pages(
            db, variant.id, [page2.id, page1.id], user
        )
    assert exc.value.status_code == 409
    # Nothing renumbered.
    assert _indices(db, variant.id) == [(page1.id, 1), (page2.id, 2)]


# ===========================================================================
# Removal — scoped delete
# ===========================================================================


def test_delete_page_scoped_keeps_linked_siblings(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    obj = _add_text_object(db, page1.id, label="banner", text="header")
    page_object_service.share_object(db, obj.id, user)
    group_id = _reload(db, obj.id).shared_group_id
    # add_page lazily replicates the linked template onto page2.
    page2 = form_variant_service.add_page(db, variant.id, user)
    sibling_id = _objects_on(db, page2.id)[0].id

    # Remove page1 (which holds the original linked member).
    form_variant_service.delete_page(db, variant.id, page1.id, user)

    # page1 and its copy are gone; the sibling on page2 survives and stays linked.
    assert form_variant_page_repo.get(db, page1.id) is None
    assert _reload(db, obj.id) is None
    survivor = _reload(db, sibling_id)
    assert survivor is not None
    assert survivor.shared_group_id == group_id
    assert survivor.shared_across_pages is True


def test_delete_lowest_index_member_keeps_dynamic_template(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    obj = _add_text_object(db, page1.id, label="banner", text="header")
    page_object_service.share_object(db, obj.id, user)
    group_id = _reload(db, obj.id).shared_group_id
    page2 = form_variant_service.add_page(db, variant.id, user)

    # Remove the page hosting the lowest-index member (page1). The page2 member
    # becomes the new lowest-index member and hence the dynamic template.
    form_variant_service.delete_page(db, variant.id, page1.id, user)

    # A page added now must receive a linked copy replicated from page2's member.
    templates = page_object_repo.linked_group_templates_for_variant(db, variant.id)
    assert set(templates.keys()) == {group_id}
    page3 = form_variant_service.add_page(db, variant.id, user)
    copies = _objects_on(db, page3.id)
    assert len(copies) == 1
    assert copies[0].shared_group_id == group_id
    assert copies[0].shared_across_pages is True
    assert copies[0].text_config.text == "header"


def test_delete_page_removes_ordinary_and_detached_objects(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)

    ordinary = _add_text_object(db, page2.id, label="plain", text="x")
    detached = _add_text_object(
        db, page2.id, label="det", text="y",
        shared_group_id=uuid.uuid4(), shared_across_pages=False,
    )

    form_variant_service.delete_page(db, variant.id, page2.id, user)

    # Both rows are gone — no orphans left for the removed page.
    assert _reload(db, ordinary.id) is None
    assert _reload(db, detached.id) is None
    assert form_variant_page_repo.get(db, page2.id) is None


def test_delete_only_page_rejected_409(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)

    with pytest.raises(HTTPException) as exc:
        form_variant_service.delete_page(db, variant.id, page1.id, user)
    assert exc.value.status_code == 409
    assert form_variant_page_repo.get(db, page1.id) is not None


def test_delete_foreign_page_rejected_404(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    form_variant_service.add_page(db, variant.id, user)

    with pytest.raises(HTTPException) as exc:
        form_variant_service.delete_page(db, variant.id, uuid.uuid4(), user)
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "bad_status", [FormVariantStatus.PUBLISHED, FormVariantStatus.ARCHIVED]
)
def test_delete_page_on_non_draft_rejected_409(db, user, bad_status):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    variant.status = bad_status
    db.flush()

    with pytest.raises(HTTPException) as exc:
        form_variant_service.delete_page(db, variant.id, page2.id, user)
    assert exc.value.status_code == 409
    assert form_variant_page_repo.get(db, page2.id) is not None


def test_delete_page_renumbers_remaining_contiguous(db, user):
    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    page3 = form_variant_service.add_page(db, variant.id, user)
    assert [idx for _, idx in _indices(db, variant.id)] == [1, 2, 3]

    # Remove the middle page; the survivors must renumber to 1..2 contiguous.
    form_variant_service.delete_page(db, variant.id, page2.id, user)

    assert _indices(db, variant.id) == [(page1.id, 1), (page3.id, 2)]


def test_delete_page_image_binary_unlinked(db, user, tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "object_images_dir", str(tmp_path))
    # Spy on the storage unlink so we can assert it ran on the right path, and that
    # the DB rows are already gone by the time it runs (i.e. after the commit).
    calls: list[str] = []
    real_delete = storage_service.delete_object_image

    def _spy(path):
        calls.append(path)
        real_delete(path)

    monkeypatch.setattr(storage_service, "delete_object_image", _spy)

    project = _make_project(db, user)
    variant, page1 = _make_draft_variant(db, project, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    img = _add_image_object(db, page2.id, content=b"PNGDATA", ext=".png")
    file_path = img.image_config.file_path
    assert storage_service.assert_within_object_images_dir(file_path).exists()

    form_variant_service.delete_page(db, variant.id, page2.id, user)

    # Unlink invoked exactly once, on this object's file, and the file is gone.
    assert calls == [file_path]
    assert not storage_service.assert_within_object_images_dir(file_path).exists()
    # The page + object rows are gone (delete committed before the unlink).
    assert form_variant_page_repo.get(db, page2.id) is None
    assert _reload(db, img.id) is None
