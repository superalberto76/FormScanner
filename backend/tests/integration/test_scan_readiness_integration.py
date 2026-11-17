# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the scan-readiness pre-check.

Before a scan batch is started, the FE queries
GET /form-projects/{id}/scan-readiness to learn whether the project's PUBLISHED
variants are coherently configured for automatic sheet→variant matching (batch
creation and uploads enforce the same rule — see
test_scan_readiness_gate_integration.py). The rule:

  * 0 published variants → not ready (nothing to scan);
  * exactly 1            → always ready (no matching ambiguity);
  * >1                   → ready only if one PUBLISHED variant carries a
                           VARIANT_IDENTIFIER binding, whose id is
                           surfaced so the FE can point at it.

The endpoint is ALWAYS 200 — "not ready" is a payload value (``ready=False`` +
``reason``), never a 409. Only ``require_project_member`` gates access (403 for a
non-member). These are service-level tests against a real Postgres (the shared
``db`` fixture from conftest.py), with the endpoint function called directly to
pin the "always 200, never raises" contract — mirroring how the lifecycle suite
exercises ``get_form_project``. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.form_projects import get_form_project_scan_readiness
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject
from app.models.user import User
from app.schemas.form_project import ScanReadiness
from app.services import form_project_service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, *, role=UserRole.USER) -> User:
    u = User(
        email=f"scan-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=role,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(db, owner, *, name="Scan readiness project") -> FormProject:
    """A project with an OWNER membership row for *owner* (the membership gate)."""
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


def _make_variant(
    db, project, owner, *, status=FormVariantStatus.PUBLISHED, version_number
) -> FormVariant:
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name=f"v{version_number}",
        status=status,
        version_number=version_number,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    return variant


def _bind_variant_identifier(db, variant) -> uuid.UUID:
    """Attach a VARIANT_IDENTIFIER binding to a fresh CODE object on *variant*.

    Built directly (page → CODE object → binding) rather than via assign_role,
    which requires a DRAFT variant and a shared object: here we
    exercise the readiness query over PUBLISHED variants in isolation.
    """
    page = FormVariantPage(form_variant_id=variant.id, page_index=0)
    db.add(page)
    db.flush()
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.CODE,
        x=0, y=0, width=50, height=50, label="ident",
    )
    db.add(obj)
    db.flush()
    db.add(CodeObjectConfig(page_object_id=obj.id, code_type=CodeType.QR, value="X"))
    db.add(
        FormVariantRoleBinding(
            form_variant_id=variant.id,
            page_object_id=obj.id,
            role=PageObjectRole.VARIANT_IDENTIFIER,
        )
    )
    db.flush()
    return variant.id


# ---------------------------------------------------------------------------
# 0 published variants -> not ready
# ---------------------------------------------------------------------------
def test_zero_published_not_ready(db, user):
    project = _make_project(db, user)
    # A DRAFT variant does not count as published content.
    _make_variant(db, project, user, status=FormVariantStatus.DRAFT, version_number=1)

    result = form_project_service.get_scan_readiness(db, project.id, user)
    assert result.ready is False
    assert result.published_variant_count == 0
    assert result.reason == "No published variant exists for this project."
    assert result.variant_identifier_variant_id is None


# ---------------------------------------------------------------------------
# exactly 1 published variant -> always ready, no constraint
# ---------------------------------------------------------------------------
def test_one_published_ready_no_identifier(db, user):
    project = _make_project(db, user)
    _make_variant(db, project, user, version_number=1)

    result = form_project_service.get_scan_readiness(db, project.id, user)
    assert result.ready is True
    assert result.published_variant_count == 1
    assert result.reason is None
    # No constraint with a single variant: the identifier field is not applicable.
    assert result.variant_identifier_variant_id is None


# ---------------------------------------------------------------------------
# >1 published, none with VARIANT_IDENTIFIER -> not ready
# ---------------------------------------------------------------------------
def test_multiple_published_without_identifier_not_ready(db, user):
    project = _make_project(db, user)
    _make_variant(db, project, user, version_number=1)
    _make_variant(db, project, user, version_number=2)

    result = form_project_service.get_scan_readiness(db, project.id, user)
    assert result.ready is False
    assert result.published_variant_count == 2
    assert result.reason == (
        "Multiple published variants exist but none has a "
        "VARIANT_IDENTIFIER role assigned; scanning cannot "
        "automatically match sheets to a variant."
    )
    assert result.variant_identifier_variant_id is None


# ---------------------------------------------------------------------------
# >1 published, one with VARIANT_IDENTIFIER -> ready, names the holder
# ---------------------------------------------------------------------------
def test_multiple_published_with_identifier_ready(db, user):
    project = _make_project(db, user)
    _make_variant(db, project, user, version_number=1)
    holder = _make_variant(db, project, user, version_number=2)
    holder_id = _bind_variant_identifier(db, holder)

    result = form_project_service.get_scan_readiness(db, project.id, user)
    assert result.ready is True
    assert result.published_variant_count == 2
    assert result.reason is None
    assert result.variant_identifier_variant_id == holder_id


def test_identifier_on_draft_variant_does_not_satisfy(db, user):
    """A VARIANT_IDENTIFIER on a DRAFT variant is ignored: the readiness rule
    only considers PUBLISHED variants (the ones that will actually be scanned)."""
    project = _make_project(db, user)
    _make_variant(db, project, user, version_number=1)
    _make_variant(db, project, user, version_number=2)
    draft = _make_variant(
        db, project, user, status=FormVariantStatus.DRAFT, version_number=3
    )
    _bind_variant_identifier(db, draft)

    result = form_project_service.get_scan_readiness(db, project.id, user)
    # Two PUBLISHED variants, and the only identifier sits on the DRAFT one.
    assert result.ready is False
    assert result.published_variant_count == 2
    assert result.variant_identifier_variant_id is None


# ---------------------------------------------------------------------------
# Endpoint contract: ALWAYS 200 (never 409), in every readiness state
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("published,with_identifier", [
    (0, False),   # not ready — no published variant
    (1, False),   # ready — single variant
    (2, False),   # not ready — no identifier
    (2, True),    # ready — identifier present
])
def test_endpoint_always_returns_200_payload(db, user, published, with_identifier):
    """The endpoint returns a ScanReadiness payload (never raises / 409) whether
    ready is True or False. Calling the route function directly, as the lifecycle
    suite does, pins that the read-only diagnostic has no HTTP failure path."""
    project = _make_project(db, user)
    variants = [
        _make_variant(db, project, user, version_number=i + 1)
        for i in range(published)
    ]
    if with_identifier and variants:
        _bind_variant_identifier(db, variants[-1])

    result = get_form_project_scan_readiness(project_id=project.id, db=db, user=user)
    assert isinstance(result, ScanReadiness)
    assert result.published_variant_count == published
    # Sanity: the expected readiness for this shape.
    expected_ready = published == 1 or (published > 1 and with_identifier)
    assert result.ready is expected_ready


# ---------------------------------------------------------------------------
# Access control: require_project_member is unchanged (non-member -> 403)
# ---------------------------------------------------------------------------
def test_non_member_is_denied_403(db, user):
    project = _make_project(db, user)
    _make_variant(db, project, user, version_number=1)
    stranger = _make_user(db)  # not a member of the project

    with pytest.raises(HTTPException) as exc:
        form_project_service.get_scan_readiness(db, project.id, stranger)
    assert exc.value.status_code == 403


def test_missing_project_is_404(db, user):
    with pytest.raises(HTTPException) as exc:
        form_project_service.get_scan_readiness(db, uuid.uuid4(), user)
    assert exc.value.status_code == 404
