# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for VARIANT_IDENTIFIER value uniqueness among a
project's PUBLISHED variants — the prevention side of the matching ambiguity.

Two PUBLISHED variants of one project may carry the SAME VARIANT_IDENTIFIER
value. Nothing about that looks broken: an identifier IS configured, exports
render, ingestion decodes. But the two variants are then indistinguishable on
paper, so matching resolves the value deterministically (lowest
``version_number`` wins) and roughly half the submissions are attributed to the
wrong variant with no signal anywhere. Matching has an a-posteriori DIAGNOSTIC
(the ambiguity is recorded on the matched submission); this suite pins the two
gates that stop the collision from being created in the first place:

  * ``publish_variant`` → 409 when the value is already published by a SIBLING
    variant of the same project. DRAFT siblings do not count (they are never
    scanned, so there is no real ambiguity), and a variant with no
    VARIANT_IDENTIFIER is not checked at all — the role stays optional.
  * ``get_scan_readiness`` → ``ready=False`` with ``colliding_variant_ids`` when a
    collision already exists in the data. Defence in depth: publish refuses to
    create one, but data written outside that gate can carry it, so the test
    builds such data DIRECTLY via the ORM, bypassing publish_variant.

Variants are built directly (variant → page → shared CODE object → binding)
rather than through the authoring services, exactly as the matching suite does:
what these gates read is the stored end state. The clone test is the exception —
it walks the real ``clone_variant`` path, because "clone a published variant and
publish the copy untouched" is the concrete scenario that produces a collision.
Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
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
from app.repositories import code_object_repo
from app.services import (
    form_project_service,
    form_variant_service,
    page_object_role_service,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(db) -> User:
    u = User(
        email=f"vid-uniq-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(db, owner, *, status=FormProjectStatus.DRAFT) -> FormProject:
    """A project with an OWNER membership row (the membership gate).

    DRAFT by default: publish AND clone are both allowed there, so one status
    covers every flow exercised here.
    """
    project = FormProject(
        owner_id=owner.id,
        type=FormProjectType.EXAM,
        name="Identifier uniqueness project",
        status=status,
    )
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
    db,
    project,
    owner,
    *,
    version_number: int,
    status=FormVariantStatus.PUBLISHED,
    identifier_value: str | None = None,
) -> FormVariant:
    """A one-page variant, optionally carrying a VARIANT_IDENTIFIER.

    The identifier CODE object is created shared-across-pages (a shared group of
    one page), which is what ``assign_role`` requires and what a
    clone therefore reproduces.
    """
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
    page = FormVariantPage(form_variant_id=variant.id, page_index=1)
    db.add(page)
    db.flush()

    if identifier_value is not None:
        obj = PageObject(
            form_variant_page_id=page.id,
            object_type=PageObjectType.CODE,
            x=60, y=60, width=50, height=50, label="ident",
            shared_group_id=uuid.uuid4(),
            shared_across_pages=True,
        )
        db.add(obj)
        db.flush()
        db.add(
            CodeObjectConfig(
                page_object_id=obj.id,
                code_type=CodeType.QR,
                value=identifier_value,
            )
        )
        db.add(
            FormVariantRoleBinding(
                form_variant_id=variant.id,
                page_object_id=obj.id,
                role=PageObjectRole.VARIANT_IDENTIFIER,
            )
        )
        db.flush()
    return variant


def _code_object_of(db, variant_id: uuid.UUID) -> PageObject:
    """The (single) CODE object of *variant_id*, whichever page hosts it."""
    stmt = (
        select(PageObject)
        .join(FormVariantPage, FormVariantPage.id == PageObject.form_variant_page_id)
        .where(
            FormVariantPage.form_variant_id == variant_id,
            PageObject.object_type == PageObjectType.CODE,
        )
    )
    return db.scalars(stmt).one()


# ---------------------------------------------------------------------------
# publish_variant — the gate
# ---------------------------------------------------------------------------


def test_publish_colliding_with_published_sibling_is_409(db, owner):
    """The core case: the value is already in print on a PUBLISHED variant."""
    project = _make_project(db, owner)
    published = _make_variant(
        db, project, owner, version_number=1, identifier_value="EXAM-A"
    )
    candidate = _make_variant(
        db,
        project,
        owner,
        version_number=2,
        status=FormVariantStatus.DRAFT,
        identifier_value="EXAM-A",
    )

    with pytest.raises(HTTPException) as exc:
        form_variant_service.publish_variant(db, candidate.id, owner)

    assert exc.value.status_code == 409
    # The message must name the value AND the variant holding it, so the author
    # knows which one to look at without a second lookup.
    assert "EXAM-A" in exc.value.detail
    assert str(published.id) in exc.value.detail
    # Refused, not partially applied.
    db.refresh(candidate)
    assert candidate.status is FormVariantStatus.DRAFT
    assert candidate.published_at is None


def test_publish_colliding_only_with_draft_sibling_succeeds(db, owner):
    """A DRAFT sibling holding the same value is NOT a collision: it is never
    scanned, so there is no real ambiguity. Blocking here would make the gate
    fire on work in progress."""
    project = _make_project(db, owner)
    _make_variant(
        db,
        project,
        owner,
        version_number=1,
        status=FormVariantStatus.DRAFT,
        identifier_value="EXAM-A",
    )
    candidate = _make_variant(
        db,
        project,
        owner,
        version_number=2,
        status=FormVariantStatus.DRAFT,
        identifier_value="EXAM-A",
    )

    result = form_variant_service.publish_variant(db, candidate.id, owner)
    assert result.status is FormVariantStatus.PUBLISHED


def test_publish_without_variant_identifier_is_unchanged(db, owner):
    """No VARIANT_IDENTIFIER → no check at all. The role is optional,
    and without an identifier there is nothing that could collide."""
    project = _make_project(db, owner)
    _make_variant(
        db, project, owner, version_number=1, identifier_value="EXAM-A"
    )
    candidate = _make_variant(
        db,
        project,
        owner,
        version_number=2,
        status=FormVariantStatus.DRAFT,
        identifier_value=None,
    )

    result = form_variant_service.publish_variant(db, candidate.id, owner)
    assert result.status is FormVariantStatus.PUBLISHED
    assert result.published_at is not None


def test_publish_with_distinct_identifier_value_succeeds(db, owner):
    """Non-regression: distinct values are exactly what the feature is for."""
    project = _make_project(db, owner)
    _make_variant(
        db, project, owner, version_number=1, identifier_value="EXAM-A"
    )
    candidate = _make_variant(
        db,
        project,
        owner,
        version_number=2,
        status=FormVariantStatus.DRAFT,
        identifier_value="EXAM-B",
    )

    result = form_variant_service.publish_variant(db, candidate.id, owner)
    assert result.status is FormVariantStatus.PUBLISHED


def test_publish_ignores_same_value_in_another_project(db, owner):
    """Uniqueness is scoped to the project: matching only ever looks at the
    variants of the project being scanned, so a homonym elsewhere is harmless."""
    other_project = _make_project(db, owner)
    _make_variant(
        db, other_project, owner, version_number=1, identifier_value="EXAM-A"
    )
    project = _make_project(db, owner)
    candidate = _make_variant(
        db,
        project,
        owner,
        version_number=1,
        status=FormVariantStatus.DRAFT,
        identifier_value="EXAM-A",
    )

    result = form_variant_service.publish_variant(db, candidate.id, owner)
    assert result.status is FormVariantStatus.PUBLISHED


def test_publish_clone_of_published_variant_untouched_is_409(db, owner):
    """The scenario that produces a collision, through the REAL clone path.

    ``clone_variant`` deep-copies every PageObject with its typed config, so the
    copy carries the source's VARIANT_IDENTIFIER value VERBATIM. Role bindings
    themselves are not copied, so the author re-assigns the role on the clone
    (the normal authoring step) and then publishes without editing the value —
    which is precisely the silent collision this gate exists to stop.
    """
    project = _make_project(db, owner)
    source = _make_variant(
        db, project, owner, version_number=1, identifier_value="EXAM-A"
    )

    clone = form_variant_service.clone_variant(db, source.id, owner)
    assert clone.status is FormVariantStatus.DRAFT

    cloned_code = _code_object_of(db, clone.id)
    # Copied verbatim — the whole point of the scenario.
    assert cloned_code.code_config.value == "EXAM-A"
    page_object_role_service.assign_role(
        db, cloned_code.id, PageObjectRole.VARIANT_IDENTIFIER, owner
    )

    with pytest.raises(HTTPException) as exc:
        form_variant_service.publish_variant(db, clone.id, owner)
    assert exc.value.status_code == 409
    assert "EXAM-A" in exc.value.detail
    assert str(source.id) in exc.value.detail


# ---------------------------------------------------------------------------
# find_duplicate_variant_identifier_values — the query behind scan-readiness
# ---------------------------------------------------------------------------


def test_repo_finds_no_duplicates_when_values_differ(db, owner):
    project = _make_project(db, owner)
    _make_variant(db, project, owner, version_number=1, identifier_value="EXAM-A")
    _make_variant(db, project, owner, version_number=2, identifier_value="EXAM-B")

    assert code_object_repo.find_duplicate_variant_identifier_values(
        db, project.id
    ) == []


def test_repo_groups_by_value_and_orders_by_version_number(db, owner):
    """One match per shared value, candidate_ids ordered by version_number — the
    same ordering as the single-value lookup, so ``variant_id`` still names the
    variant matching would deterministically pick."""
    project = _make_project(db, owner)
    # Created out of version order on purpose: the ordering must come from the
    # query, not from insertion order.
    a3 = _make_variant(db, project, owner, version_number=3, identifier_value="EXAM-A")
    a1 = _make_variant(db, project, owner, version_number=1, identifier_value="EXAM-A")
    b5 = _make_variant(db, project, owner, version_number=5, identifier_value="EXAM-B")
    b4 = _make_variant(db, project, owner, version_number=4, identifier_value="EXAM-B")
    # A lone value and a DRAFT duplicate are both invisible to the query.
    _make_variant(db, project, owner, version_number=6, identifier_value="EXAM-C")
    _make_variant(
        db,
        project,
        owner,
        version_number=7,
        status=FormVariantStatus.DRAFT,
        identifier_value="EXAM-C",
    )

    matches = code_object_repo.find_duplicate_variant_identifier_values(
        db, project.id
    )
    assert [m.candidate_ids for m in matches] == [
        (a1.id, a3.id),
        (b4.id, b5.id),
    ]
    assert [m.variant_id for m in matches] == [a1.id, b4.id]
    assert all(m.is_ambiguous for m in matches)


# ---------------------------------------------------------------------------
# get_scan_readiness — defence in depth over pre-existing data
# ---------------------------------------------------------------------------


def test_scan_readiness_reports_preexisting_collision(db, owner):
    """A collision built DIRECTLY via the ORM — i.e. data as it could exist
    outside the publish gate — makes the project not ready and names the
    variants."""
    project = _make_project(db, owner, status=FormProjectStatus.ACTIVE)
    first = _make_variant(
        db, project, owner, version_number=1, identifier_value="EXAM-A"
    )
    second = _make_variant(
        db, project, owner, version_number=2, identifier_value="EXAM-A"
    )

    result = form_project_service.get_scan_readiness(db, project.id, owner)
    assert result.ready is False
    assert result.reason == (
        "Multiple published variants share the same "
        "VARIANT_IDENTIFIER value; scanning cannot reliably "
        "match sheets to a variant."
    )
    assert result.published_variant_count == 2
    assert result.colliding_variant_ids == sorted([first.id, second.id])


def test_scan_readiness_collision_wins_over_the_identifier_rule(db, owner):
    """The collision is checked FIRST. Both colliding variants DO carry a
    VARIANT_IDENTIFIER, so the plain ">1 published needs an identifier"
    rule would have answered ``ready=True`` — the worse answer, since matching
    cannot actually tell those variants apart."""
    project = _make_project(db, owner, status=FormProjectStatus.ACTIVE)
    _make_variant(db, project, owner, version_number=1, identifier_value="EXAM-A")
    _make_variant(db, project, owner, version_number=2, identifier_value="EXAM-A")
    # A third, correctly-identified variant does not redeem the project either.
    _make_variant(db, project, owner, version_number=3, identifier_value="EXAM-B")

    result = form_project_service.get_scan_readiness(db, project.id, owner)
    assert result.ready is False
    assert result.published_variant_count == 3
    assert len(result.colliding_variant_ids) == 2


def test_scan_readiness_without_collision_is_unchanged(db, owner):
    """Non-regression: with no collision the field stays empty and the existing
    readiness verdict is untouched."""
    project = _make_project(db, owner, status=FormProjectStatus.ACTIVE)
    _make_variant(db, project, owner, version_number=1, identifier_value=None)
    holder = _make_variant(
        db, project, owner, version_number=2, identifier_value="EXAM-A"
    )

    result = form_project_service.get_scan_readiness(db, project.id, owner)
    assert result.ready is True
    assert result.reason is None
    assert result.published_variant_count == 2
    assert result.variant_identifier_variant_id == holder.id
    assert result.colliding_variant_ids == []


def test_scan_readiness_colliding_ids_empty_when_not_ready_for_other_reasons(
    db, owner
):
    """The new field is scoped to collisions: the older "no identifier" failure
    keeps its own reason and reports no colliding ids."""
    project = _make_project(db, owner, status=FormProjectStatus.ACTIVE)
    _make_variant(db, project, owner, version_number=1, identifier_value=None)
    _make_variant(db, project, owner, version_number=2, identifier_value=None)

    result = form_project_service.get_scan_readiness(db, project.id, owner)
    assert result.ready is False
    assert result.colliding_variant_ids == []
    assert "none has a" in result.reason
