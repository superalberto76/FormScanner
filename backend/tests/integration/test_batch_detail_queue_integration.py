# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the review-queue fields of the batch detail.

``GET /submission-batches/{batch_id}`` is the review queue, so every
``SubmissionSummary`` also says who the candidate is, which template pages
are missing, and whether a candidate identifier is expected at all. What this
suite pins down:

  * ``respondent_identifier`` is reported verbatim, null when absent;
  * ``missing_page_indices`` follows the ``Submission.missing_page_indices``
    property exactly — the 1-based template pages no sheet is bound to, sorted,
    [] with no variant — and an extra sheet matching left unbound (confidence
    0.0) fills no slot;
  * ``respondent_identifier_expected`` is true only when the matched variant
    binds RESPONDENT_IDENTIFIER — not with no variant, not with only a
    VARIANT_IDENTIFIER;
  * ``lock`` reports a submission's ACTIVE soft lock with its
    holder's display name, and null for an expired lock or none;
  * **the query budget**: the statement count does not grow with the number of
    submissions — locked ones, and ones whose unread objects are
    counted, included — which is the whole reason the model property is not
    used here.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select

from app.api.v1.endpoints.submission_batches import get_submission_batch
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import SubmissionSummary
from app.services import submission_lock_service

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"queue-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Review-queue project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_variant(db, project, actor, *, pages: int = 2) -> tuple[FormVariant, list[FormVariantPage]]:
    """A PUBLISHED variant with *pages* template pages, indexed 1..pages.

    Each call adds the project's next version, since a version number is
    unique per project."""
    version = 1 + db.scalar(
        select(func.count(FormVariant.id)).where(
            FormVariant.form_project_id == project.id
        )
    )
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name=f"v{version}",
        status=FormVariantStatus.PUBLISHED,
        version_number=version,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    template_pages = []
    for index in range(1, pages + 1):
        page = FormVariantPage(form_variant_id=variant.id, page_index=index)
        db.add(page)
        template_pages.append(page)
    db.flush()
    return variant, template_pages


def _bind_role(db, template_page: FormVariantPage, role: PageObjectRole) -> None:
    """Author a CODE on *template_page* and give it *role* in its variant."""
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.CODE,
        x=0,
        y=0,
        width=10,
        height=10,
    )
    db.add(obj)
    db.flush()
    db.add(
        FormVariantRoleBinding(
            form_variant_id=template_page.form_variant_id,
            page_object_id=obj.id,
            role=role,
        )
    )
    db.flush()


@pytest.fixture
def batch(db, project, actor) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch 1", created_by_user_id=actor.id
    )
    db.add(batch)
    db.flush()
    return batch


def _make_submission(
    db,
    batch,
    variant: FormVariant | None,
    bound_to: list[FormVariantPage | None],
    *,
    respondent_identifier: str | None = None,
) -> Submission:
    """A submission with one sheet per entry of *bound_to*; a None entry is a
    sheet matching left unbound (at confidence 0.0, like an extra sheet beyond
    the variant's last page)."""
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=variant.id if variant is not None else None,
        status=SubmissionStatus.MATCHED if variant is not None else SubmissionStatus.MATCH_FAILED,
        respondent_identifier=respondent_identifier,
    )
    db.add(submission)
    db.flush()
    for order, template_page in enumerate(bound_to):
        db.add(
            SubmissionPage(
                submission_id=submission.id,
                source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
                source_filename=f"page{order}.png",
                source_page_order=order,
                form_variant_page_id=template_page.id if template_page else None,
                match_confidence=(
                    1.0 if template_page else (0.0 if variant is not None else None)
                ),
            )
        )
    db.flush()
    return submission


def _summaries(db, batch, actor) -> dict[uuid.UUID, SubmissionSummary]:
    detail = get_submission_batch(batch_id=batch.id, status=None, db=db, user=actor)
    return {s.id: s for s in detail.submissions}


# ---------------------------------------------------------------------------
# respondent_identifier
# ---------------------------------------------------------------------------
def test_the_candidate_identifier_is_reported_verbatim_or_null(
    db, project, actor, batch
):
    variant, (p1, p2) = _make_variant(db, project, actor)
    known = _make_submission(
        db, batch, variant, [p1, p2], respondent_identifier="  MAT-00042 "
    )
    unknown = _make_submission(db, batch, variant, [p1, p2])

    summaries = _summaries(db, batch, actor)

    assert summaries[known.id].respondent_identifier == "  MAT-00042 "
    assert summaries[unknown.id].respondent_identifier is None


# ---------------------------------------------------------------------------
# missing_page_indices
# ---------------------------------------------------------------------------
def test_a_complete_submission_misses_nothing(db, project, actor, batch):
    variant, (p1, p2) = _make_variant(db, project, actor)
    submission = _make_submission(db, batch, variant, [p1, p2])

    assert _summaries(db, batch, actor)[submission.id].missing_page_indices == []


def test_a_missing_template_page_is_reported_by_its_1_based_index(
    db, project, actor, batch
):
    variant, (p1, _p2, p3) = _make_variant(db, project, actor, pages=3)
    submission = _make_submission(db, batch, variant, [p1, p3])

    assert _summaries(db, batch, actor)[submission.id].missing_page_indices == [2]


def test_missing_pages_come_back_sorted(db, project, actor, batch):
    variant, (_p1, p2, _p3, _p4) = _make_variant(db, project, actor, pages=4)
    submission = _make_submission(db, batch, variant, [p2])

    assert _summaries(db, batch, actor)[submission.id].missing_page_indices == [1, 3, 4]


def test_an_unbound_extra_sheet_does_not_fill_a_slot(db, project, actor, batch):
    """Two sheets for a two-page variant, but the second was unplaceable
    (confidence 0.0, no template page): page 2 is still missing. Counting
    sheets rather than bound slots would hide it."""
    variant, (p1, _p2) = _make_variant(db, project, actor)
    submission = _make_submission(db, batch, variant, [p1, None])

    summary = _summaries(db, batch, actor)[submission.id]

    assert summary.page_count == 2
    assert summary.missing_page_indices == [2]


def test_no_variant_means_nothing_missing(db, project, actor, batch):
    submission = _make_submission(db, batch, None, [None])

    assert _summaries(db, batch, actor)[submission.id].missing_page_indices == []


def test_the_batched_computation_agrees_with_the_model_property(
    db, project, actor, batch
):
    """Same semantics as ``Submission.missing_page_indices``, by construction:
    the property is the reference, the batch detail only avoids its N+1."""
    variant, (p1, p2, p3) = _make_variant(db, project, actor, pages=3)
    submissions = [
        _make_submission(db, batch, variant, [p1, p2, p3]),
        _make_submission(db, batch, variant, [p3, None]),
        _make_submission(db, batch, None, [None]),
    ]
    for submission in submissions:
        db.refresh(submission)

    summaries = _summaries(db, batch, actor)

    for submission in submissions:
        assert (
            summaries[submission.id].missing_page_indices
            == submission.missing_page_indices
        )


# ---------------------------------------------------------------------------
# respondent_identifier_expected
# ---------------------------------------------------------------------------
def test_a_candidate_is_expected_only_when_the_variant_binds_the_role(
    db, project, actor, batch
):
    with_role, (w1, _w2) = _make_variant(db, project, actor)
    _bind_role(db, w1, PageObjectRole.RESPONDENT_IDENTIFIER)
    without_role, (n1, n2) = _make_variant(db, project, actor)

    expected = _make_submission(db, batch, with_role, [w1])
    not_expected = _make_submission(db, batch, without_role, [n1, n2])

    summaries = _summaries(db, batch, actor)

    assert summaries[expected.id].respondent_identifier_expected is True
    assert summaries[not_expected.id].respondent_identifier_expected is False


def test_no_variant_expects_no_candidate(db, project, actor, batch):
    submission = _make_submission(db, batch, None, [None])

    assert _summaries(db, batch, actor)[submission.id].respondent_identifier_expected is False


def test_a_variant_identifier_alone_does_not_make_a_candidate_expected(
    db, project, actor, batch
):
    """The role is what matters, not "has a role binding": a variant printing
    only its own VID code has no candidate identifier to miss."""
    variant, (p1, p2) = _make_variant(db, project, actor)
    _bind_role(db, p1, PageObjectRole.VARIANT_IDENTIFIER)
    submission = _make_submission(db, batch, variant, [p1, p2])

    assert _summaries(db, batch, actor)[submission.id].respondent_identifier_expected is False


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------
@pytest.fixture
def frozen_now(monkeypatch) -> datetime:
    monkeypatch.setattr(submission_lock_service, "_now", lambda: NOW)
    return NOW


def _make_holder(db, first: str = "Anna", last: str = "Verdi") -> User:
    u = User(
        email=f"holder-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
        first_name=first,
        last_name=last,
    )
    db.add(u)
    db.flush()
    return u


def test_an_active_lock_is_reported_with_its_holder(
    db, project, actor, batch, frozen_now
):
    variant, (p1, p2) = _make_variant(db, project, actor)
    submission = _make_submission(db, batch, variant, [p1, p2])
    holder = _make_holder(db)
    submission.locked_by = holder.id
    submission.locked_at = NOW - timedelta(minutes=5)
    db.flush()

    lock = _summaries(db, batch, actor)[submission.id].lock

    assert lock is not None
    assert lock.user_id == holder.id
    assert lock.display_name == "Anna Verdi"
    assert lock.locked_at == NOW - timedelta(minutes=5)


def test_an_expired_lock_is_reported_as_none(db, project, actor, batch, frozen_now):
    """Still in the row — expired locks are never cleaned up — but no longer a
    lock anyone should be shown."""
    variant, (p1, p2) = _make_variant(db, project, actor)
    submission = _make_submission(db, batch, variant, [p1, p2])
    submission.locked_by = _make_holder(db).id
    submission.locked_at = NOW - timedelta(minutes=16)
    db.flush()

    assert _summaries(db, batch, actor)[submission.id].lock is None


def test_no_lock_is_reported_as_none(db, project, actor, batch, frozen_now):
    variant, (p1, p2) = _make_variant(db, project, actor)
    submission = _make_submission(db, batch, variant, [p1, p2])

    assert _summaries(db, batch, actor)[submission.id].lock is None


# ---------------------------------------------------------------------------
# Query budget
# ---------------------------------------------------------------------------
def _count_statements(db, batch, actor) -> int:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        # A fresh identity map, so nothing a builder left loaded can hide a
        # lazy load the endpoint would do in production.
        db.expire_all()
        _summaries(db, batch, actor)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements)


@pytest.mark.parametrize("attempted", [False, True], ids=["never-extracted", "extracted"])
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_the_statement_count_does_not_grow_with_the_submissions(
    db, project, actor, frozen_now, locked, attempted
):
    """2 submissions or 6, the same number of statements: every
    per-submission field comes from a batched query, never from a per-row lazy
    load.

    *locked*: every submission but the unmatched one is actively locked, each
    by a DIFFERENT user, so loading the holders one by one would show up here.

    *attempted*: every matched submission has been examined by an extraction
    run and has unread objects, so the unread count is computed for each of
    them — from four batched queries, not per submission (the fourth
    select-in loads the role bindings of the objects' linked groups)."""

    def _batch_with(n: int, lock: bool, examined: bool) -> SubmissionBatch:
        batch = SubmissionBatch(
            form_project_id=project.id, name=f"Batch {n}", created_by_user_id=actor.id
        )
        db.add(batch)
        db.flush()
        variant, (p1, p2) = _make_variant(db, project, actor)
        _bind_role(db, p1, PageObjectRole.RESPONDENT_IDENTIFIER)
        # Two readable objects, neither ever read: unread on every sheet.
        for template_page in (p1, p2):
            db.add(
                PageObject(
                    form_variant_page_id=template_page.id,
                    object_type=PageObjectType.QUESTION,
                    x=0, y=50, width=10, height=10,
                )
            )
        db.flush()
        for i in range(n):
            submission = _make_submission(
                db, batch, variant, [p1, p2], respondent_identifier=f"R{i}"
            )
            if lock:
                submission.locked_by = _make_holder(db, f"Holder{i}", f"N{n}").id
                submission.locked_at = NOW - timedelta(minutes=1)
            if examined:
                submission.extraction_attempted_at = NOW
        db.flush()
        _make_submission(db, batch, None, [None])  # an unmatched one too
        return batch

    small = _batch_with(2, locked, attempted)
    large = _batch_with(6, locked, attempted)

    small_count = _count_statements(db, small, actor)
    large_count = _count_statements(db, large, actor)

    assert small_count == large_count
    rows = _summaries(db, large, actor)
    matched = [s for s in rows.values() if s.matched_form_variant_id is not None]
    assert [s.unread_object_count for s in matched] == [2 if attempted else 0] * 6
    assert all(s.extraction_attempted is attempted for s in matched)
    baseline = _batch_with(6, False, False)
    baseline_count = _count_statements(db, baseline, actor)
    # The holders cost ONE statement, and only when something is locked; the
    # unread count FOUR (the fourth: the role bindings of the objects' linked
    # groups), and only when a submission qualifies for it.
    assert large_count == baseline_count + (1 if locked else 0) + (4 if attempted else 0)
