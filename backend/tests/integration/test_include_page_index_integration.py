# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the CODE "include page number" option.

Covers the VARIANT_IDENTIFIER page-index feature against real Postgres
(the models use postgresql.UUID + native enums, so SQLite is not an option):

  1. creating a CODE with include_page_index=True but no VARIANT_IDENTIFIER role
     is refused (409) and nothing is persisted;
  2. VARIANT_IDENTIFIER can only be assigned to an object shared across all pages
     (409 otherwise); RESPONDENT_IDENTIFIER keeps no such requirement;
  3. the full happy flow (create -> share -> assign role -> include_page_index=True
     -> resync) and the export then encode "VID:<value>|<page_index>" on each page;
  4. resync propagates include_page_index to linked siblings (a sibling
     still False flips to True when the template is True);
  5. unshare is blocked (409) while any member of the group still holds a role;
  6. a CODE with include_page_index=False (default) keeps the plain behaviour —
     the export payload is the bare value, no page index appended.

The payload of a CODE holding a special role also carries a
self-describing role tag ("VID:" / "RID:"), so the expected payloads below are
tagged; a CODE with no role is unchanged. The tag contract itself is unit-tested
in ``tests/test_code_payload_tags.py``.

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
    PageObjectRole,
    PageObjectType,
    ProjectRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.page_object import PageObject
from app.repositories import form_variant_role_binding_repo, page_object_repo
from app.schemas.page_object import (
    CodeObjectConfigCreate,
    CodeObjectConfigUpdate,
    CodePageObjectCreate,
)
from app.services import (
    code_object_service,
    form_variant_service,
    page_object_role_service,
    page_object_service,
)
from app.services.form_variant_export_service import ExportMode, export_pdf
from app.utils import code_rendering


# ---------------------------------------------------------------------------
# Builders (self-contained, mirroring test_page_object_role_integration.py)
# ---------------------------------------------------------------------------


def _make_project(db, owner, *, name="Page-index IT project") -> FormProject:
    """A DRAFT project with an OWNER membership row for *owner* (membership gate)."""
    project = FormProject(owner_id=owner.id, type=FormProjectType.EXAM, name=name)
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_variant(db, project, owner) -> FormVariant:
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
    return variant


def _add_page(db, variant, page_index=0) -> FormVariantPage:
    pg = FormVariantPage(form_variant_id=variant.id, page_index=page_index)
    db.add(pg)
    db.flush()
    return pg


def _setup(db, owner):
    """project (+OWNER member) -> DRAFT variant -> first page."""
    project = _make_project(db, owner)
    variant = _make_variant(db, project, owner)
    page = _add_page(db, variant)
    return project, variant, page


def _add_code_object(
    db, page_id, *, label="C", value="X1", include_page_index=False,
    shared_group_id=None, shared_across_pages=False,
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.CODE,
        x=60, y=60, width=50, height=50, label=label,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    db.add(
        CodeObjectConfig(
            page_object_id=obj.id,
            code_type=CodeType.QR,
            value=value,
            include_page_index=include_page_index,
        )
    )
    db.flush()
    return obj


def _reload(db, object_id) -> PageObject | None:
    db.expire_all()
    return db.get(PageObject, object_id)


def _reload_with_configs(db, object_id) -> PageObject | None:
    db.expire_all()
    return page_object_repo.get_with_configs(db, object_id)


def _spy_render_code(monkeypatch):
    """Record every render_code call (value + resolved page_index/role + payload).

    Returns the list the export appends to; the CODE image itself is not drawn
    (the spy returns False, the placeholder path), which is irrelevant here. The
    payload is rebuilt with the real encoder, so the assertions below check what
    the export actually asked for AND what that encodes to.
    """
    calls: list[dict] = []

    def spy(
        renderer, x, y, w, h, *,
        code_type, value, human_readable, page_index=None, role=None,
        encoding=None,
    ):
        calls.append(
            {
                "value": value,
                "page_index": page_index,
                "role": role,
                "payload": code_rendering.build_tagged_payload(
                    role, value, page_index
                ),
            }
        )
        return False

    monkeypatch.setattr(code_rendering, "render_code", spy)
    return calls


# ---------------------------------------------------------------------------
# 0. the pure encoding contract, as the export uses it
# ---------------------------------------------------------------------------
def test_variant_identifier_payload_is_tagged_and_carries_the_page_index():
    vid = PageObjectRole.VARIANT_IDENTIFIER
    assert code_rendering.build_tagged_payload(vid, "V1", None) == "VID:V1"
    assert code_rendering.build_tagged_payload(vid, "V1", 0) == "VID:V1|0"
    assert code_rendering.build_tagged_payload(vid, "V1", 3) == "VID:V1|3"
    assert code_rendering.PAGE_INDEX_SEPARATOR == "|"
    assert code_rendering.ROLE_TAG_SEPARATOR == ":"


# ---------------------------------------------------------------------------
# 1. create CODE with include_page_index=True but no role -> 409, nothing saved
# ---------------------------------------------------------------------------
def test_create_code_with_include_page_index_without_role_is_409(db, user):
    _, _, page = _setup(db, user)

    with pytest.raises(HTTPException) as exc:
        code_object_service.create_code(
            db,
            page.id,
            CodePageObjectCreate(
                x=60, y=60, width=50, height=50,
                config=CodeObjectConfigCreate(
                    code_type=CodeType.QR, value="V", include_page_index=True
                ),
            ),
            user,
        )
    assert exc.value.status_code == 409
    # Validation runs BEFORE the row is added, so no object leaks into the page.
    assert page_object_repo.list_for_page(db, page.id) == []


def test_create_code_without_flag_succeeds(db, user):
    """The default (False) create path is unaffected by the new validation."""
    _, _, page = _setup(db, user)

    read = code_object_service.create_code(
        db,
        page.id,
        CodePageObjectCreate(
            x=60, y=60, width=50, height=50,
            config=CodeObjectConfigCreate(code_type=CodeType.QR, value="V"),
        ),
        user,
    )
    assert read.code_config.include_page_index is False


# ---------------------------------------------------------------------------
# 2. VARIANT_IDENTIFIER requires a shared object; RESPONDENT_IDENTIFIER does not
# ---------------------------------------------------------------------------
def test_variant_identifier_rejects_non_shared_object(db, user):
    _, _, page = _setup(db, user)
    obj = _add_code_object(db, page.id)  # not shared

    with pytest.raises(HTTPException) as exc:
        page_object_role_service.assign_role(
            db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user
        )
    assert exc.value.status_code == 409
    assert "Share this object" in exc.value.detail
    assert form_variant_role_binding_repo.get_by_page_object(db, obj.id) is None


def test_respondent_identifier_allows_non_shared_object(db, user):
    """The share requirement is VARIANT_IDENTIFIER-only — no regression here."""
    _, _, page = _setup(db, user)
    obj = _add_code_object(db, page.id)  # not shared

    read = page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )
    assert read.role is PageObjectRole.RESPONDENT_IDENTIFIER


def test_variant_identifier_accepts_shared_object(db, user):
    _, _, page = _setup(db, user)
    gid = uuid.uuid4()
    obj = _add_code_object(db, page.id, shared_group_id=gid, shared_across_pages=True)

    read = page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )
    assert read.role is PageObjectRole.VARIANT_IDENTIFIER


# ---------------------------------------------------------------------------
# 3. happy flow end-to-end: create -> share -> assign role -> include_page_index
#    -> resync, then the export encodes "VID:<value>|<page_index>" per page
# ---------------------------------------------------------------------------
def test_full_flow_export_encodes_page_index(db, user, monkeypatch):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page1 = _add_page(db, variant, page_index=0)

    # create (plain) -> share -> a second page receives a linked sibling.
    created = code_object_service.create_code(
        db,
        page1.id,
        CodePageObjectCreate(
            x=60, y=60, width=50, height=50,
            config=CodeObjectConfigCreate(code_type=CodeType.QR, value="V"),
        ),
        user,
    )
    page_object_service.share_object(db, created.id, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    sibling_id = page_object_repo.list_for_page(db, page2.id)[0].id

    # assign the role (allowed: the object is shared) -> enable the option.
    page_object_role_service.assign_role(
        db, created.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )
    read = code_object_service.update_code_config(
        db, created.id, CodeObjectConfigUpdate(include_page_index=True), user
    )
    assert read.code_config.include_page_index is True

    # resync fans the flag out to the linked sibling.
    page_object_service.resync_shared_group_typed_config(db, created.id, user)
    assert _reload_with_configs(db, sibling_id).code_config.include_page_index is True

    # Export: each page's identifier code carries its own page index, behind the
    # explicit VID role tag — the role travels with the payload, so
    # the linked sibling on page 1 is tagged too.
    calls = _spy_render_code(monkeypatch)
    export_pdf(db, variant.id, user, ExportMode.STUDENT)

    by_index = {c["page_index"]: c["payload"] for c in calls if c["value"] == "V"}
    assert by_index == {0: "VID:V|0", 1: "VID:V|1"}
    assert {c["role"] for c in calls if c["value"] == "V"} == {
        PageObjectRole.VARIANT_IDENTIFIER
    }


# ---------------------------------------------------------------------------
# 4. resync propagates include_page_index to a historically-False sibling
# ---------------------------------------------------------------------------
def test_resync_propagates_include_page_index_to_siblings(db, user):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page1 = _add_page(db, variant, page_index=0)
    page2 = _add_page(db, variant, page_index=1)
    gid = uuid.uuid4()

    template = _add_code_object(
        db, page1.id, label="tpl", value="V", include_page_index=True,
        shared_group_id=gid, shared_across_pages=True,
    )
    sibling = _add_code_object(
        db, page2.id, label="sib", value="V", include_page_index=False,
        shared_group_id=gid, shared_across_pages=True,
    )

    page_object_service.resync_shared_group_typed_config(db, template.id, user)

    assert _reload_with_configs(db, sibling.id).code_config.include_page_index is True


# ---------------------------------------------------------------------------
# 4b. removing the VARIANT_IDENTIFIER role resets include_page_index
# ---------------------------------------------------------------------------
def test_remove_variant_identifier_resets_include_page_index(db, user):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page = _add_page(db, variant, page_index=0)
    gid = uuid.uuid4()
    obj = _add_code_object(
        db, page.id, value="V", shared_group_id=gid, shared_across_pages=True
    )

    page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )
    read = code_object_service.update_code_config(
        db, obj.id, CodeObjectConfigUpdate(include_page_index=True), user
    )
    assert read.code_config.include_page_index is True

    # Freeing the role clears the now-unsupported flag in the same transaction.
    page_object_role_service.remove_role(db, obj.id, user)
    assert _reload_with_configs(db, obj.id).code_config.include_page_index is False

    # No 409 lingers: a later config PATCH passes the merged-state validation
    # (the flag is False again, so the VARIANT_IDENTIFIER requirement is moot).
    read2 = code_object_service.update_code_config(
        db, obj.id, CodeObjectConfigUpdate(value="V2"), user
    )
    assert read2.code_config.value == "V2"
    assert read2.code_config.include_page_index is False


# ---------------------------------------------------------------------------
# 5. unshare is blocked while any member of the group holds a role
# ---------------------------------------------------------------------------
def test_unshare_blocked_when_member_holds_role(db, user):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page1 = _add_page(db, variant, page_index=0)
    page2 = _add_page(db, variant, page_index=1)
    gid = uuid.uuid4()

    m1 = _add_code_object(
        db, page1.id, label="m1", value="V",
        shared_group_id=gid, shared_across_pages=True,
    )
    m2 = _add_code_object(
        db, page2.id, label="m2", value="V",
        shared_group_id=gid, shared_across_pages=True,
    )
    page_object_role_service.assign_role(
        db, m1.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )

    # Unsharing via EITHER member is refused; the message names the role holder.
    with pytest.raises(HTTPException) as exc:
        page_object_service.unshare_object(db, m2.id, user)
    assert exc.value.status_code == 409
    assert str(m1.id) in exc.value.detail
    # The whole group stays linked.
    assert _reload(db, m1.id).shared_across_pages is True
    assert _reload(db, m2.id).shared_across_pages is True


# ---------------------------------------------------------------------------
# 6. default (False) is a no-op in the export — no regression
# ---------------------------------------------------------------------------
def test_export_without_flag_encodes_bare_value(db, user, monkeypatch):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page = _add_page(db, variant, page_index=0)
    _add_code_object(db, page.id, value="V")  # include_page_index defaults False

    calls = _spy_render_code(monkeypatch)
    export_pdf(db, variant.id, user, ExportMode.STUDENT)

    code_calls = [c for c in calls if c["value"] == "V"]
    assert len(code_calls) == 1
    assert code_calls[0]["page_index"] is None
    assert code_calls[0]["payload"] == "V"  # bare value, no "|" appended
