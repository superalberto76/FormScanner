# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the explicit CODE role tag.

The EXPORT side: the payload of a CODE holding a special role is
self-describing.

    VARIANT_IDENTIFIER    -> "VID:<value>"  (or "VID:<value>|<page_index>")
    RESPONDENT_IDENTIFIER -> "RID:<value>"
    no role               -> "<value>"      (unchanged, no tag)

That removes a circular dependency in the scanning pipeline: without the tag,
distinguishing the two identifier codes would require knowing the FormVariant,
which is resolved only by matching — long after ingestion needs the answer.

Covered here:
  1. export tags a RESPONDENT_IDENTIFIER code as "RID:<value>";
  2. export leaves a role-less CODE completely untagged;
  3. a DETACHED copy of a shared identifier does not inherit the tag.

The VARIANT_IDENTIFIER + page-index export path lives in
``test_include_page_index_integration.py``; the payload contract itself in
``tests/test_code_payload_tags.py``; the tag-reading rules in
``tests/test_submission_identification.py``; and the READ side, end-to-end
through real ingestion, in ``test_submission_ingestion_integration.py``.
Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

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
from app.services import page_object_role_service
from app.services.form_variant_export_service import ExportMode, export_pdf
from app.utils import code_rendering


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _make_project(db, owner, *, name="Role tag IT project") -> FormProject:
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


def _add_page(db, variant, page_index=1) -> FormVariantPage:
    """A page of *variant*. Page indices are 1-based."""
    pg = FormVariantPage(form_variant_id=variant.id, page_index=page_index)
    db.add(pg)
    db.flush()
    return pg


def _add_code_object(
    db, page_id, *, label="C", value="X1", include_page_index=False,
    shared_group_id=None, shared_across_pages=False,
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.CODE,
        x=0, y=0, width=50, height=50, label=label,
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


def _spy_render_code(monkeypatch):
    """Record the payload every render_code call would encode."""
    calls: list[dict] = []

    def spy(
        renderer, x, y, w, h, *,
        code_type, value, human_readable, page_index=None, role=None,
        encoding=None,
    ):
        calls.append(
            {
                "value": value,
                "role": role,
                "payload": code_rendering.build_tagged_payload(role, value, page_index),
            }
        )
        return False

    monkeypatch.setattr(code_rendering, "render_code", spy)
    return calls


# ---------------------------------------------------------------------------
# 1. RESPONDENT_IDENTIFIER export -> "RID:<value>"
# ---------------------------------------------------------------------------
def test_export_tags_respondent_identifier_code(db, user, monkeypatch):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page = _add_page(db, variant)
    obj = _add_code_object(db, page.id, value="R1")

    # RESPONDENT_IDENTIFIER carries no "share across pages" requirement.
    page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )

    calls = _spy_render_code(monkeypatch)
    export_pdf(db, variant.id, user, ExportMode.STUDENT)

    code_calls = [c for c in calls if c["value"] == "R1"]
    assert len(code_calls) == 1
    assert code_calls[0]["role"] is PageObjectRole.RESPONDENT_IDENTIFIER
    assert code_calls[0]["payload"] == "RID:R1"


# ---------------------------------------------------------------------------
# 2. no role -> no tag at all (the untagged payload is byte-for-byte unchanged)
# ---------------------------------------------------------------------------
def test_export_leaves_a_role_less_code_untagged(db, user, monkeypatch):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page = _add_page(db, variant)
    _add_code_object(db, page.id, value="PLAIN")

    calls = _spy_render_code(monkeypatch)
    export_pdf(db, variant.id, user, ExportMode.STUDENT)

    code_calls = [c for c in calls if c["value"] == "PLAIN"]
    assert len(code_calls) == 1
    assert code_calls[0]["role"] is None
    assert code_calls[0]["payload"] == "PLAIN"
    assert ":" not in code_calls[0]["payload"]


# ---------------------------------------------------------------------------
# 3. the tag follows the LINKED shared group, not the single binding row
# ---------------------------------------------------------------------------
def test_detached_copy_does_not_inherit_the_role_tag(db, user, monkeypatch):
    """A shared identifier tags every LINKED copy; a detached one is a plain code.

    The role binding lives on one PageObject row, but a VARIANT_IDENTIFIER is
    printed on every page — so linked siblings must be tagged too, otherwise
    every sheet after the first would carry an untagged code and ingestion could
    not identify it. A detached copy only remembers its origin group and must
    stay untagged.
    """
    project = _make_project(db, user)
    variant = _make_variant(db, project, user)
    page1 = _add_page(db, variant, page_index=1)
    page2 = _add_page(db, variant, page_index=2)
    page3 = _add_page(db, variant, page_index=3)
    gid = uuid.uuid4()

    holder = _add_code_object(
        db, page1.id, label="holder", value="V",
        shared_group_id=gid, shared_across_pages=True,
    )
    _add_code_object(
        db, page2.id, label="linked", value="V",
        shared_group_id=gid, shared_across_pages=True,
    )
    _add_code_object(
        db, page3.id, label="detached", value="V",
        shared_group_id=gid, shared_across_pages=False,
    )
    page_object_role_service.assign_role(
        db, holder.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )

    calls = _spy_render_code(monkeypatch)
    export_pdf(db, variant.id, user, ExportMode.STUDENT)

    payloads = sorted(c["payload"] for c in calls if c["value"] == "V")
    # holder + linked sibling tagged; detached copy untagged. No page index here
    # (include_page_index defaults to False), so no "|" suffix either.
    assert payloads == ["V", "VID:V", "VID:V"]
