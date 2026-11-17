# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the export gate of a batch.

A batch can be exported only once it is complete; until then, it says how many
submissions hold it back and why. What this suite pins down:

  * ``export_readiness``: ``ready`` is the same rule as the
    batch list's ``ready_for_export``; the four counts split the blocking
    statuses into to_extract (UPLOADED, MATCHED), in_review (SCANNED),
    to_assign (MATCH_FAILED), unassigned (UNASSIGNED); UNRESOLVABLE, REVIEWED
    and FINALIZED are in none of them;
  * the batch detail carries it over the WHOLE batch, whatever status filter
    narrows the listed submissions;
  * ``require_ready_for_export``: the batch when ready, otherwise the
    structured 409 ``NOT_EXPORT_READY`` with the counts — an empty batch and a
    batch of UNRESOLVABLE submissions only included, all counts zero; any
    project member, the project need NOT be ACTIVE; 403 / 404 as usual;
  * an UNASSIGNED submission marked unresolvable stops holding the batch
    back, and holds it back again once undone.

The rule ``_is_ready_for_export`` itself is in
test_submission_batch_list_integration.py. Statuses are set directly on the
rows. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.submission_batches import (
    get_submission_batch,
    list_submission_batches,
)
from app.api.v1.endpoints.submissions import (
    mark_submission_unresolvable,
    undo_submission_unresolvable,
)
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.user import User
from app.schemas.submission import ExportReadiness
from app.services import submission_batch_service

ZERO = {"to_extract": 0, "in_review": 0, "to_assign": 0, "unassigned": 0}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, prefix: str) -> User:
    u = User(
        email=f"{prefix}-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def actor(db) -> User:
    return _make_user(db, "export-readiness")


def _make_project(db, owner, *, status=FormProjectStatus.ACTIVE) -> FormProject:
    project = FormProject(
        owner_id=owner.id,
        type=FormProjectType.EXAM,
        name="Export readiness",
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


def _make_batch(db, project, owner) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch 1", created_by_user_id=owner.id
    )
    db.add(batch)
    db.flush()
    return batch


@pytest.fixture
def batch(db, actor) -> SubmissionBatch:
    return _make_batch(db, _make_project(db, actor), actor)


def _add(db, batch, *statuses: SubmissionStatus) -> list[Submission]:
    """One page-less submission per status: the gate reads statuses only."""
    submissions = [
        Submission(submission_batch_id=batch.id, status=status) for status in statuses
    ]
    db.add_all(submissions)
    db.flush()
    return submissions


def _require(db, batch, user):
    return submission_batch_service.require_ready_for_export(db, batch.id, user)


def _refusal(db, batch, user) -> dict:
    with pytest.raises(HTTPException) as exc:
        _require(db, batch, user)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "NOT_EXPORT_READY"
    return exc.value.detail


# ---------------------------------------------------------------------------
# export_readiness — the counts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status, field",
    [
        (SubmissionStatus.UPLOADED, "to_extract"),
        (SubmissionStatus.MATCHED, "to_extract"),
        (SubmissionStatus.SCANNED, "in_review"),
        (SubmissionStatus.MATCH_FAILED, "to_assign"),
        (SubmissionStatus.UNASSIGNED, "unassigned"),
    ],
)
def test_each_blocking_status_is_counted_under_its_reason(status, field):
    readiness = submission_batch_service.export_readiness(
        {SubmissionStatus.REVIEWED: 3, status: 2}
    )

    assert readiness == ExportReadiness(ready=False, **{**ZERO, field: 2})


def test_closed_and_unresolvable_submissions_are_counted_nowhere():
    readiness = submission_batch_service.export_readiness(
        {
            SubmissionStatus.REVIEWED: 2,
            SubmissionStatus.FINALIZED: 1,
            SubmissionStatus.UNRESOLVABLE: 4,
        }
    )

    assert readiness == ExportReadiness(ready=True, **ZERO)


def test_uploaded_and_matched_add_up_in_to_extract():
    readiness = submission_batch_service.export_readiness(
        {
            SubmissionStatus.UPLOADED: 1,
            SubmissionStatus.MATCHED: 2,
            SubmissionStatus.SCANNED: 3,
            SubmissionStatus.MATCH_FAILED: 4,
            SubmissionStatus.UNASSIGNED: 5,
        }
    )

    assert readiness == ExportReadiness(
        ready=False, to_extract=3, in_review=3, to_assign=4, unassigned=5
    )


@pytest.mark.parametrize(
    "counts",
    [{}, {SubmissionStatus.UNRESOLVABLE: 2}],
    ids=["empty", "unresolvable-only"],
)
def test_nothing_to_export_is_not_ready_with_zero_counts(counts):
    """Not ready, yet nothing in progress."""
    assert submission_batch_service.export_readiness(counts) == ExportReadiness(
        ready=False, **ZERO
    )


# ---------------------------------------------------------------------------
# The batch detail
# ---------------------------------------------------------------------------
def test_the_batch_detail_carries_the_readiness_of_the_whole_batch(
    db, actor, batch
):
    _add(
        db,
        batch,
        SubmissionStatus.REVIEWED,
        SubmissionStatus.SCANNED,
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.UNASSIGNED,
    )

    detail = get_submission_batch(batch_id=batch.id, status=None, db=db, user=actor)
    assert detail.export_readiness == ExportReadiness(
        ready=False, to_extract=0, in_review=1, to_assign=1, unassigned=1
    )

    # The status filter narrows the list, never the readiness.
    filtered = get_submission_batch(
        batch_id=batch.id, status=SubmissionStatus.REVIEWED, db=db, user=actor
    )
    assert len(filtered.submissions) == 1
    assert filtered.export_readiness == detail.export_readiness


def test_the_batch_detail_and_the_batch_list_agree(db, actor):
    project = _make_project(db, actor)
    ready = _make_batch(db, project, actor)
    _add(db, ready, SubmissionStatus.REVIEWED, SubmissionStatus.UNRESOLVABLE)
    blocked = _make_batch(db, project, actor)
    _add(db, blocked, SubmissionStatus.REVIEWED, SubmissionStatus.MATCHED)
    empty = _make_batch(db, project, actor)

    listed = {
        item.id: item.ready_for_export
        for item in list_submission_batches(project_id=project.id, db=db, user=actor)
    }
    for b in (ready, blocked, empty):
        detail = get_submission_batch(batch_id=b.id, status=None, db=db, user=actor)
        assert detail.export_readiness.ready is listed[b.id]
    assert listed == {ready.id: True, blocked.id: False, empty.id: False}


# ---------------------------------------------------------------------------
# require_ready_for_export — the gate
# ---------------------------------------------------------------------------
def test_a_ready_batch_passes_the_gate(db, actor, batch):
    _add(
        db,
        batch,
        SubmissionStatus.REVIEWED,
        SubmissionStatus.FINALIZED,
        SubmissionStatus.UNRESOLVABLE,
    )

    assert _require(db, batch, actor).id == batch.id


def test_a_batch_in_progress_is_refused_with_its_counts(db, actor, batch):
    _add(
        db,
        batch,
        SubmissionStatus.REVIEWED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.SCANNED,
        SubmissionStatus.SCANNED,
        SubmissionStatus.UNASSIGNED,
    )

    detail = _refusal(db, batch, actor)

    assert detail == {
        "code": "NOT_EXPORT_READY",
        "message": "The batch cannot be exported yet: 4 submissions are not complete.",
        "to_extract": 1,
        "in_review": 2,
        "to_assign": 0,
        "unassigned": 1,
    }


def test_one_blocking_submission_is_named_in_the_singular(db, actor, batch):
    _add(db, batch, SubmissionStatus.REVIEWED, SubmissionStatus.MATCH_FAILED)

    detail = _refusal(db, batch, actor)

    assert detail["message"] == (
        "The batch cannot be exported yet: 1 submission is not complete."
    )
    assert detail["to_assign"] == 1


@pytest.mark.parametrize(
    "statuses",
    [(), (SubmissionStatus.UNRESOLVABLE, SubmissionStatus.UNRESOLVABLE)],
    ids=["empty", "unresolvable-only"],
)
def test_nothing_to_export_is_refused_with_its_own_message(
    db, actor, batch, statuses
):
    _add(db, batch, *statuses)

    detail = _refusal(db, batch, actor)

    assert detail == {
        "code": "NOT_EXPORT_READY",
        "message": (
            "The batch cannot be exported: it has no submission closed by a review."
        ),
        **ZERO,
    }


def test_a_closed_project_can_still_export(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.CLOSED)
    batch = _make_batch(db, project, actor)
    _add(db, batch, SubmissionStatus.REVIEWED)

    assert _require(db, batch, actor).id == batch.id


def test_a_non_member_is_refused_before_the_gate(db, actor, batch):
    _add(db, batch, SubmissionStatus.REVIEWED)
    stranger = _make_user(db, "stranger")

    with pytest.raises(HTTPException) as exc:
        _require(db, batch, stranger)

    assert exc.value.status_code == 403


def test_an_unknown_batch_is_404(db, actor):
    with pytest.raises(HTTPException) as exc:
        submission_batch_service.require_ready_for_export(db, uuid.uuid4(), actor)

    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# UNASSIGNED marked unresolvable
# ---------------------------------------------------------------------------
def test_marking_an_unassigned_submission_releases_the_batch(db, actor, batch):
    _reviewed, unassigned = _add(
        db, batch, SubmissionStatus.REVIEWED, SubmissionStatus.UNASSIGNED
    )
    assert _refusal(db, batch, actor)["unassigned"] == 1

    mark_submission_unresolvable(submission_id=unassigned.id, db=db, user=actor)
    assert _require(db, batch, actor).id == batch.id

    undo_submission_unresolvable(submission_id=unassigned.id, db=db, user=actor)
    assert _refusal(db, batch, actor)["unassigned"] == 1
