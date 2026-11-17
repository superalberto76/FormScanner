# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for unresolvable submissions.

The other way out of MATCH_FAILED, next to the manual assignment: the sheets
are too damaged for anyone to name the variant. What this suite pins down:

  * marking moves MATCH_FAILED → UNRESOLVABLE, keeps the failure reason and is
    recorded as ``submission.marked_unresolvable``;
  * undoing moves it back to MATCH_FAILED, recorded as
    ``submission.unresolvable_undone``, after which the submission can be
    assigned a variant again;
  * an UNASSIGNED submission can be marked too, and undoing brings it back to
    UNASSIGNED, variant and reason untouched; the status it was marked from is
    ``unresolvable_from_status``, NULL otherwise;
  * each action only from its own status; anything else, a second click
    included, is the structured 409 ``INVALID_STATUS`` with the ``status``
    found, and nothing changes;
  * any project member, the project need NOT be ACTIVE; 403 / 404 as usual;
  * the batch queue and the review detail carry the failure reason.

Matching never touching an UNRESOLVABLE submission is in
test_submission_matching_integration.py; the export gate in
test_submission_batch_list_integration.py. Submissions are built directly, as
in test_submission_manual_assignment_integration.py. Skipped without
TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.endpoints.submission_batches import (
    get_submission_batch,
    run_batch_matching,
)
from app.api.v1.endpoints.submissions import (
    assign_submission_variant,
    get_submission_review,
    mark_submission_unresolvable,
    undo_submission_unresolvable,
)
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatchFailureReason,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import SubmissionVariantAssign
from app.services import audit_service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"unresolvable-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(db, owner, *, status=FormProjectStatus.ACTIVE) -> FormProject:
    project = FormProject(
        owner_id=owner.id,
        type=FormProjectType.EXAM,
        name="Unresolvable project",
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


def _make_variant(db, project, owner, *, version_number: int) -> FormVariant:
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name=f"v{version_number}",
        status=FormVariantStatus.PUBLISHED,
        version_number=version_number,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    db.add(FormVariantPage(form_variant_id=variant.id, page_index=1))
    db.flush()
    return variant


def _make_batch(db, project, owner) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch 1", created_by_user_id=owner.id
    )
    db.add(batch)
    db.flush()
    return batch


def _make_submission(
    db,
    batch,
    *,
    status=SubmissionStatus.MATCH_FAILED,
    reason: MatchFailureReason | None = MatchFailureReason.VID_NOT_RECOGNISED,
    code_value: str | None = "D-DRAFT",
) -> Submission:
    """A one-sheet submission, by default MATCH_FAILED on an unrecognised code
    (the F20 case)."""
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=None,
        status=status,
        match_failure_reason=reason,
        match_failure_code_value=code_value,
    )
    db.add(submission)
    db.flush()
    db.add(
        SubmissionPage(
            submission_id=submission.id,
            source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
            source_filename="page0.png",
            source_page_order=0,
            identified_code_value=None,
        )
    )
    db.flush()
    return submission


@pytest.fixture
def setup(db, actor):
    project = _make_project(db, actor)
    # Two published variants, so a variant is not implied by the project.
    _make_variant(db, project, actor, version_number=1)
    variant = _make_variant(db, project, actor, version_number=2)
    batch = _make_batch(db, project, actor)
    return project, variant, batch


def _mark(db, submission, user):
    return mark_submission_unresolvable(submission_id=submission.id, db=db, user=user)


def _undo(db, submission, user):
    return undo_submission_unresolvable(submission_id=submission.id, db=db, user=user)


def _events(db, action: str, submission) -> list[AuditLog]:
    db.flush()
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == action,
                AuditLog.entity_id == str(submission.id),
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Mark and undo
# ---------------------------------------------------------------------------
def test_marking_closes_the_submission_and_keeps_its_reason(db, actor, setup):
    _project, _variant, batch = setup
    submission = _make_submission(db, batch)

    result = _mark(db, submission, actor)

    assert result.status is SubmissionStatus.UNRESOLVABLE
    assert result.match_failure_reason is MatchFailureReason.VID_NOT_RECOGNISED
    assert result.match_failure_code_value == "D-DRAFT"
    assert result.matched_form_variant_id is None
    assert result.unresolvable_from_status is SubmissionStatus.MATCH_FAILED
    events = _events(db, audit_service.SUBMISSION_MARKED_UNRESOLVABLE, submission)
    assert len(events) == 1
    assert events[0].actor_user_id == actor.id
    assert events[0].metadata_json == {
        "status": "UNRESOLVABLE",
        "from_status": "MATCH_FAILED",
    }


def test_undo_brings_it_back_to_match_failed_and_assignable(db, actor, setup):
    """The verdict is reversible, and once undone the submission is an
    ordinary MATCH_FAILED again — reason included, variant assignable."""
    _project, variant, batch = setup
    submission = _make_submission(db, batch)
    _mark(db, submission, actor)

    result = _undo(db, submission, actor)

    assert result.status is SubmissionStatus.MATCH_FAILED
    assert result.match_failure_reason is MatchFailureReason.VID_NOT_RECOGNISED
    assert result.match_failure_code_value == "D-DRAFT"
    assert result.unresolvable_from_status is None
    events = _events(db, audit_service.SUBMISSION_UNRESOLVABLE_UNDONE, submission)
    assert len(events) == 1
    assert events[0].metadata_json == {
        "status": "MATCH_FAILED",
        "from_status": "UNRESOLVABLE",
    }

    assigned = assign_submission_variant(
        submission_id=submission.id,
        data=SubmissionVariantAssign(form_variant_id=variant.id),
        db=db,
        user=actor,
    )
    assert assigned.status is SubmissionStatus.MATCHED


def test_an_unassigned_submission_is_marked_and_undone_back_to_unassigned(
    db, actor, setup
):
    """An ungroupable sheet can be marked unresolvable, and
    "Undo" brings it back to UNASSIGNED, not to MATCH_FAILED. Matching may
    have set a variant and a reason on it (it spares only the status): both
    survive the round trip."""
    _project, variant, batch = setup
    submission = _make_submission(
        db,
        batch,
        status=SubmissionStatus.UNASSIGNED,
        reason=None,
        code_value=None,
    )
    submission.matched_form_variant_id = variant.id
    db.flush()

    marked = _mark(db, submission, actor)

    assert marked.status is SubmissionStatus.UNRESOLVABLE
    assert marked.unresolvable_from_status is SubmissionStatus.UNASSIGNED
    assert marked.matched_form_variant_id == variant.id
    events = _events(db, audit_service.SUBMISSION_MARKED_UNRESOLVABLE, submission)
    assert [e.metadata_json for e in events] == [
        {"status": "UNRESOLVABLE", "from_status": "UNASSIGNED"}
    ]

    undone = _undo(db, submission, actor)

    assert undone.status is SubmissionStatus.UNASSIGNED
    assert undone.unresolvable_from_status is None
    assert undone.matched_form_variant_id == variant.id
    assert undone.match_failure_reason is None
    events = _events(db, audit_service.SUBMISSION_UNRESOLVABLE_UNDONE, submission)
    assert [e.metadata_json for e in events] == [
        {"status": "UNASSIGNED", "from_status": "UNRESOLVABLE"}
    ]

    # And it can be marked again.
    assert _mark(db, submission, actor).status is SubmissionStatus.UNRESOLVABLE


def test_a_rerun_leaves_an_unresolvable_from_unassigned_alone(db, actor, setup):
    """Matching skips an UNRESOLVABLE submission whatever it was marked from:
    the re-run neither lists it nor rewrites its origin."""
    _project, _variant, batch = setup
    submission = _make_submission(
        db, batch, status=SubmissionStatus.UNASSIGNED, reason=None, code_value=None
    )
    _mark(db, submission, actor)

    result = run_batch_matching(batch_id=batch.id, db=db, user=actor)

    assert result.submissions_processed == 0
    db.refresh(submission)
    assert submission.status is SubmissionStatus.UNRESOLVABLE
    assert submission.unresolvable_from_status is SubmissionStatus.UNASSIGNED
    assert submission.matched_form_variant_id is None


def test_an_unresolvable_without_its_origin_is_undone_to_match_failed(
    db, actor, setup
):
    """A NULL origin should never be stored; were it to happen, "Undo" falls
    back on MATCH_FAILED, from which the submission can still be assigned a
    variant or marked again."""
    _project, _variant, batch = setup
    submission = _make_submission(db, batch, status=SubmissionStatus.UNRESOLVABLE)
    assert submission.unresolvable_from_status is None

    assert _undo(db, submission, actor).status is SubmissionStatus.MATCH_FAILED


@pytest.mark.parametrize(
    "current_status",
    [
        SubmissionStatus.UPLOADED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.SCANNED,
        SubmissionStatus.REVIEWED,
        SubmissionStatus.FINALIZED,
        SubmissionStatus.UNRESOLVABLE,
    ],
)
def test_only_a_match_failed_or_unassigned_submission_can_be_marked(
    db, actor, setup, current_status
):
    """UNRESOLVABLE included: a second click is refused, not repeated."""
    _project, _variant, batch = setup
    submission = _make_submission(db, batch, status=current_status)

    with pytest.raises(HTTPException) as exc:
        _mark(db, submission, actor)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "INVALID_STATUS"
    assert exc.value.detail["status"] == current_status.value
    assert submission.status is current_status
    assert _events(db, audit_service.SUBMISSION_MARKED_UNRESOLVABLE, submission) == []


@pytest.mark.parametrize(
    "current_status",
    [
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.UNASSIGNED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.REVIEWED,
    ],
)
def test_only_an_unresolvable_submission_can_be_undone(
    db, actor, setup, current_status
):
    _project, _variant, batch = setup
    submission = _make_submission(db, batch, status=current_status)

    with pytest.raises(HTTPException) as exc:
        _undo(db, submission, actor)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "INVALID_STATUS"
    assert exc.value.detail["status"] == current_status.value
    assert submission.status is current_status


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
def test_a_closed_project_still_allows_marking_and_undoing(db, actor):
    """Like the manual assignment, recovery stays possible after collection."""
    project = _make_project(db, actor, status=FormProjectStatus.CLOSED)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch)

    assert _mark(db, submission, actor).status is SubmissionStatus.UNRESOLVABLE
    assert _undo(db, submission, actor).status is SubmissionStatus.MATCH_FAILED

    unassigned = _make_submission(db, batch, status=SubmissionStatus.UNASSIGNED)
    assert _mark(db, unassigned, actor).status is SubmissionStatus.UNRESOLVABLE
    assert _undo(db, unassigned, actor).status is SubmissionStatus.UNASSIGNED


def test_a_non_member_cannot_mark_nor_undo(db, actor, setup):
    _project, _variant, batch = setup
    submission = _make_submission(db, batch)
    stranger = User(
        email=f"stranger-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(stranger)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        _mark(db, submission, stranger)
    assert exc.value.status_code == 403
    assert submission.status is SubmissionStatus.MATCH_FAILED

    submission.status = SubmissionStatus.UNRESOLVABLE
    db.flush()
    with pytest.raises(HTTPException) as exc:
        _undo(db, submission, stranger)
    assert exc.value.status_code == 403


def test_an_unknown_submission_is_404(db, actor):
    for action in (mark_submission_unresolvable, undo_submission_unresolvable):
        with pytest.raises(HTTPException) as exc:
            action(submission_id=uuid.uuid4(), db=db, user=actor)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# What the queue and the review page show
# ---------------------------------------------------------------------------
def test_queue_and_review_detail_carry_the_failure_reason(db, actor, setup):
    _project, _variant, batch = setup
    unrecognised = _make_submission(db, batch)
    unreadable = _make_submission(
        db, batch, reason=MatchFailureReason.NO_VID_FOUND, code_value=None
    )
    # Matched before the reason was recorded: nothing to show.
    unrecorded = _make_submission(db, batch, reason=None, code_value=None)
    _mark(db, unreadable, actor)

    queue = {
        s.id: s
        for s in get_submission_batch(
            batch_id=batch.id, status=None, db=db, user=actor
        ).submissions
    }

    assert queue[unrecognised.id].match_failure_reason is (
        MatchFailureReason.VID_NOT_RECOGNISED
    )
    assert queue[unrecognised.id].match_failure_code_value == "D-DRAFT"
    assert queue[unreadable.id].status is SubmissionStatus.UNRESOLVABLE
    assert queue[unreadable.id].match_failure_reason is MatchFailureReason.NO_VID_FOUND
    assert queue[unrecorded.id].match_failure_reason is None

    detail = get_submission_review(submission_id=unrecognised.id, db=db, user=actor)
    assert detail.match_failure_reason is MatchFailureReason.VID_NOT_RECOGNISED
    assert detail.match_failure_code_value == "D-DRAFT"


def test_the_queue_carries_the_status_it_was_marked_from(db, actor, setup):
    """The FE words an UNRESOLVABLE row and its "Undo" toast
    from this, without guessing."""
    _project, _variant, batch = setup
    from_failed = _make_submission(db, batch)
    from_unassigned = _make_submission(
        db, batch, status=SubmissionStatus.UNASSIGNED, reason=None, code_value=None
    )
    untouched = _make_submission(db, batch)
    _mark(db, from_failed, actor)
    _mark(db, from_unassigned, actor)

    queue = {
        s.id: s
        for s in get_submission_batch(
            batch_id=batch.id, status=None, db=db, user=actor
        ).submissions
    }

    assert queue[from_failed.id].unresolvable_from_status is (
        SubmissionStatus.MATCH_FAILED
    )
    assert queue[from_unassigned.id].unresolvable_from_status is (
        SubmissionStatus.UNASSIGNED
    )
    assert queue[untouched.id].unresolvable_from_status is None
