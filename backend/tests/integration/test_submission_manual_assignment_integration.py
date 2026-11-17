# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for manual variant assignment.

The human end of the matching engine: Level 1 could read nothing off the sheets,
the submission went to MATCH_FAILED, and an operator now names the variant. What
this suite pins down:

  * a valid assignment moves the submission to MATCHED **and really binds its
    pages** — the whole point is that the engine's Level 2 finishes the job, so
    the bindings and their confidences are asserted, never just the status;
  * only MATCH_FAILED can be assigned; every other status is 409, UNASSIGNED
    included and with NO special handling (its grouping problem belongs to a
    different workflow, not to this endpoint);
  * the variant must exist, belong to the SAME project (404 for both, so a
    foreign variant is not even acknowledged) and be PUBLISHED (409);
  * the action is recorded as ``submission.manually_matched``, distinct from the
    automatic ``submission.matched``;
  * the ``?status=`` filter on the batch detail lists only that status, and
    omitting it changes nothing.

Submissions are built directly (Submission + SubmissionPage rows carrying the
``identified_code_value`` ingestion would have stored), the same approach as
test_submission_matching_integration.py: what matters here is the recovery
decision, not how the sheets got in. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from app.api.v1.endpoints.submission_batches import get_submission_batch
from app.api.v1.endpoints.submissions import assign_submission_variant
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
from app.schemas.submission import SubmissionRead, SubmissionVariantAssign
from app.services import audit_service, submission_matching_service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"assign-{uuid.uuid4().hex}@example.com",
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
        name="Assignment project",
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
    page_count: int = 3,
) -> FormVariant:
    """A variant with ``page_count`` pages indexed 1..n (1-BASED, as provisioned).

    No VARIANT_IDENTIFIER is ever set up here: these tests are about the case
    where nothing on the sheets identifies the variant, which is exactly what
    puts a submission in MATCH_FAILED in the first place.
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
    for index in range(1, page_count + 1):
        db.add(FormVariantPage(form_variant_id=variant.id, page_index=index))
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
    codes: list[str | None],
    *,
    status=SubmissionStatus.MATCH_FAILED,
) -> Submission:
    """A submission whose pages carry what ingestion would have stored.

    Defaults to MATCH_FAILED — the state this endpoint exists to recover from.
    """
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=None,
        status=status,
    )
    db.add(submission)
    db.flush()
    for order, raw in enumerate(codes):
        db.add(
            SubmissionPage(
                submission_id=submission.id,
                source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
                source_filename=f"page{order}.png",
                source_page_order=order,
                identified_code_value=raw,
            )
        )
    db.flush()
    return submission


def _pages_in_order(submission) -> list[SubmissionPage]:
    return sorted(submission.pages, key=lambda p: p.source_page_order)


def _bound_indices(db, submission) -> list[int | None]:
    """The 1-based template page_index each sheet ended up bound to."""
    return [
        None
        if page.form_variant_page_id is None
        else db.get(FormVariantPage, page.form_variant_page_id).page_index
        for page in _pages_in_order(submission)
    ]


def _assign(db, submission, variant, user) -> SubmissionRead:
    return assign_submission_variant(
        submission_id=submission.id,
        data=SubmissionVariantAssign(form_variant_id=variant.id),
        db=db,
        user=user,
    )


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
# The happy path — status AND the page bindings behind it
# ---------------------------------------------------------------------------
def test_manual_assignment_matches_the_submission_and_binds_its_pages(db, actor):
    """The whole contract in one test. Assigning does not merely stamp MATCHED:
    it hands the submission to the engine, whose Level 2 places every sheet on a
    template page. Here nothing declared a position, so the bindings come from
    the ``source_page_order + 1`` fallback at confidence 0.5 — the SAME rules an
    automatic match would have applied, which is why the logic is reused and not
    duplicated."""
    project = _make_project(db, actor)
    # Two published variants, so Level 1 could never have resolved this
    # submission by itself — a human decision is genuinely required.
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=3)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None, None])

    result = _assign(db, submission, target, actor)

    assert isinstance(result, SubmissionRead)
    assert result.status is SubmissionStatus.MATCHED
    assert result.matched_form_variant_id == target.id
    db.refresh(submission)
    assert submission.status is SubmissionStatus.MATCHED
    assert submission.matched_form_variant_id == target.id
    # Level 2 really ran: the pages are bound, and scored as deduced.
    assert _bound_indices(db, submission) == [1, 2, 3]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [0.5, 0.5, 0.5]


def test_manual_assignment_honours_a_page_index_the_sheets_declared(db, actor):
    """A submission can fail Level 1 and still carry positional information: its
    codes name a variant the project does not publish (a sheet from another
    campaign), but the page index inside them is still good. Once the operator
    supplies the variant, those declared positions are used verbatim at 1.0."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=3)
    batch = _make_batch(db, project, actor)
    # Physical order 0,1,2 but the codes claim pages 3,1,2 — so a binding taken
    # from the physical order would be visibly wrong.
    submission = _make_submission(
        db, batch, ["VID:UNKNOWN|3", "VID:UNKNOWN|1", "VID:UNKNOWN|2"]
    )

    _assign(db, submission, target, actor)

    assert _bound_indices(db, submission) == [3, 1, 2]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [1.0, 1.0, 1.0]


def test_a_sheet_the_variant_has_no_page_for_does_not_block_the_others(db, actor):
    """No page-count validation is done on purpose: the operator is looking at
    the paper. A submission with more sheets than the chosen variant has pages
    is accepted, the extra sheet is scored 0.0 and left unbound, and its
    siblings are bound normally — the engine's ordinary behaviour, unchanged."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=2)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None, None])

    _assign(db, submission, target, actor)

    assert submission.status is SubmissionStatus.MATCHED
    assert _bound_indices(db, submission) == [1, 2, None]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [0.5, 0.5, 0.0]


def test_manual_assignment_does_not_require_an_active_project(db, actor):
    """Deliberately NOT gated on ACTIVE, like the matching re-run: a submission
    discovered to be unmatched after collection closed is exactly the one that
    must still be recoverable."""
    project = _make_project(db, actor, status=FormProjectStatus.CLOSED)
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])

    _assign(db, submission, target, actor)

    assert submission.status is SubmissionStatus.MATCHED
    assert submission.matched_form_variant_id == target.id


def test_the_assignment_reads_the_row_for_update(db, actor):
    """An assignment and a concurrent "mark unresolvable" must serialise on the
    row, so the second gets the 409 INVALID_STATUS instead of overwriting the
    first."""
    project = _make_project(db, actor)
    target = _make_variant(db, project, actor, version_number=1, page_count=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])
    db.flush()
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        _assign(db, submission, target, actor)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert "FROM submissions" in reads[0]
    assert reads[0].rstrip().endswith("FOR UPDATE")


def test_the_assignment_clears_the_failure_reason(db, actor):
    """Once the variant is named, why matching had failed no longer describes
    the submission — on it and in the response."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:OLD-CAMPAIGN|1"])
    submission.match_failure_reason = MatchFailureReason.VID_NOT_RECOGNISED
    submission.match_failure_code_value = "OLD-CAMPAIGN"
    db.flush()

    result = _assign(db, submission, target, actor)

    assert submission.match_failure_reason is None
    assert submission.match_failure_code_value is None
    assert result.match_failure_reason is None
    assert result.match_failure_code_value is None


# ---------------------------------------------------------------------------
# Only MATCH_FAILED may be assigned
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current_status",
    [
        SubmissionStatus.UPLOADED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.UNRESOLVABLE,
    ],
)
def test_a_submission_not_in_match_failed_is_409(db, actor, current_status):
    """This is a recovery action, not a re-assignment tool: anything already
    settled (or not yet processed) is refused, and the structured 409 names the
    status found so the operator knows why. UNRESOLVABLE included: its verdict
    must be undone before a variant can be assigned."""
    project = _make_project(db, actor)
    target = _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None], status=current_status)

    with pytest.raises(HTTPException) as exc:
        _assign(db, submission, target, actor)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "INVALID_STATUS"
    assert exc.value.detail["status"] == current_status.value
    assert current_status.value in exc.value.detail["message"]
    assert submission.matched_form_variant_id is None


def test_an_unassigned_submission_takes_the_same_409_path(db, actor):
    """UNASSIGNED is a GROUPING statement — nobody has decided which
    submission the sheet belongs to — and answering it is a different
    workflow, not this one. It gets NO special case here: same status check,
    same 409, same message shape, and its status is left untouched."""
    project = _make_project(db, actor)
    target = _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, batch, [None], status=SubmissionStatus.UNASSIGNED
    )

    with pytest.raises(HTTPException) as exc:
        _assign(db, submission, target, actor)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "INVALID_STATUS"
    assert exc.value.detail["status"] == SubmissionStatus.UNASSIGNED.value
    assert submission.status is SubmissionStatus.UNASSIGNED
    assert submission.matched_form_variant_id is None


# ---------------------------------------------------------------------------
# The variant must exist, be OURS, and be PUBLISHED
# ---------------------------------------------------------------------------
def test_an_unknown_variant_is_404(db, actor):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])

    with pytest.raises(HTTPException) as exc:
        assign_submission_variant(
            submission_id=submission.id,
            data=SubmissionVariantAssign(form_variant_id=uuid.uuid4()),
            db=db,
            user=actor,
        )

    assert exc.value.status_code == 404
    assert submission.status is SubmissionStatus.MATCH_FAILED


def test_a_variant_of_another_project_is_404(db, actor):
    """An id that exists but hangs off the wrong parent is 404, not 403 — the
    same answer an unknown id gets, so the endpoint never confirms that another
    project's variant exists."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])

    other_project = _make_project(db, actor)
    foreign = _make_variant(db, other_project, actor, version_number=1)

    with pytest.raises(HTTPException) as exc:
        _assign(db, submission, foreign, actor)

    assert exc.value.status_code == 404
    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.matched_form_variant_id is None


@pytest.mark.parametrize(
    "variant_status", [FormVariantStatus.DRAFT, FormVariantStatus.ARCHIVED]
)
def test_a_variant_that_is_not_published_is_409(db, actor, variant_status):
    """Only PUBLISHED variants have printed, scannable sheets, so assigning any
    other kind describes something that cannot be on the operator's desk. The
    variant IS in the project, so this is a conflict, not a 404."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    unpublished = _make_variant(
        db, project, actor, version_number=2, status=variant_status
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])

    with pytest.raises(HTTPException) as exc:
        _assign(db, submission, unpublished, actor)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "VARIANT_NOT_PUBLISHED"
    assert exc.value.detail["variant_status"] == variant_status.value
    assert variant_status.value in exc.value.detail["message"]
    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.matched_form_variant_id is None


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
def test_a_non_member_cannot_assign(db, actor):
    project = _make_project(db, actor)
    target = _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])
    stranger = User(
        email=f"stranger-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(stranger)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        _assign(db, submission, target, stranger)

    assert exc.value.status_code == 403
    assert submission.status is SubmissionStatus.MATCH_FAILED


def test_an_unknown_submission_is_404(db, actor):
    project = _make_project(db, actor)
    target = _make_variant(db, project, actor, version_number=1)

    with pytest.raises(HTTPException) as exc:
        assign_submission_variant(
            submission_id=uuid.uuid4(),
            data=SubmissionVariantAssign(form_variant_id=target.id),
            db=db,
            user=actor,
        )

    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def test_the_assignment_is_recorded_as_manually_matched(db, actor):
    """A human decision must be distinguishable from an automatic one in the
    audit trail: they rest on completely different evidence. So the event is
    ``submission.manually_matched``, carrying the chosen variant, and the
    automatic ``submission.matched`` is NOT logged (Level 1 never ran — the
    variant was already set when the engine was called)."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=2)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None])

    _assign(db, submission, target, actor)

    events = _events(db, audit_service.SUBMISSION_MANUALLY_MATCHED, submission)
    assert len(events) == 1
    assert events[0].metadata_json["variant_id"] == str(target.id)
    assert events[0].actor_user_id == actor.id
    assert _events(db, audit_service.SUBMISSION_MATCHED, submission) == []


# ---------------------------------------------------------------------------
# Interaction with a later automatic re-run
# ---------------------------------------------------------------------------
def test_a_later_matching_run_does_not_undo_the_manual_decision(db, actor):
    """The operator's answer must survive: matching is idempotent, so a re-run
    over a manually assigned submission skips Level 1 (the variant is set) and
    re-binds nothing."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=2)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None])
    _assign(db, submission, target, actor)
    before = _bound_indices(db, submission)

    outcome = submission_matching_service._match_submission(db, submission)

    assert submission.matched_form_variant_id == target.id
    assert submission.status is SubmissionStatus.MATCHED
    assert _bound_indices(db, submission) == before
    assert outcome.resolved_now is False
    assert outcome.pages_resolved == 0


# ---------------------------------------------------------------------------
# The queue view: ?status= on the existing batch detail
# ---------------------------------------------------------------------------
def test_status_filter_lists_only_submissions_in_that_status(db, actor):
    """The "recovery queue": the operator asks a batch for its MATCH_FAILED
    submissions and gets exactly those."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    _make_variant(db, project, actor, version_number=2, page_count=1)
    batch = _make_batch(db, project, actor)
    failed_a = _make_submission(db, batch, [None])
    failed_b = _make_submission(db, batch, [None])
    _make_submission(db, batch, [None], status=SubmissionStatus.MATCHED)
    _make_submission(db, batch, [None], status=SubmissionStatus.UPLOADED)
    _make_submission(db, batch, [None], status=SubmissionStatus.UNASSIGNED)

    detail = get_submission_batch(
        batch_id=batch.id,
        status=SubmissionStatus.MATCH_FAILED,
        db=db,
        user=actor,
    )

    # Compared as a SET: the ordering is ``created_at, id`` and every row of one
    # test shares a transaction-scoped created_at, so the tie-breaker between
    # two same-instant submissions is a random uuid. What the filter must
    # guarantee is membership, which is what is asserted.
    assert {s.id for s in detail.submissions} == {failed_a.id, failed_b.id}
    assert all(s.status is SubmissionStatus.MATCH_FAILED for s in detail.submissions)
    # The page counts the unfiltered view reports are unaffected by the filter.
    assert [s.page_count for s in detail.submissions] == [1, 1]


def test_without_the_filter_every_submission_is_still_listed(db, actor):
    """The parameter is optional: omitting it lists every submission,
    whatever its status."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    everything = [
        _make_submission(db, batch, [None]),
        _make_submission(db, batch, [None], status=SubmissionStatus.MATCHED),
        _make_submission(db, batch, [None], status=SubmissionStatus.UPLOADED),
        _make_submission(db, batch, [None], status=SubmissionStatus.UNASSIGNED),
    ]

    detail = get_submission_batch(batch_id=batch.id, db=db, user=actor)

    assert {s.id for s in detail.submissions} == {s.id for s in everything}


def test_the_filter_reflects_an_assignment_that_just_happened(db, actor):
    """Queue and action agree: a submission recovered through the endpoint
    leaves the MATCH_FAILED list and appears in the MATCHED one."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    target = _make_variant(db, project, actor, version_number=2, page_count=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])
    still_failed = _make_submission(db, batch, [None])

    _assign(db, submission, target, actor)

    failed = get_submission_batch(
        batch_id=batch.id, status=SubmissionStatus.MATCH_FAILED, db=db, user=actor
    )
    matched = get_submission_batch(
        batch_id=batch.id, status=SubmissionStatus.MATCHED, db=db, user=actor
    )

    assert [s.id for s in failed.submissions] == [still_failed.id]
    assert [s.id for s in matched.submissions] == [submission.id]


def test_the_filtered_detail_is_denied_to_a_non_member(db, actor):
    project = _make_project(db, actor)
    batch = _make_batch(db, project, actor)
    stranger = User(
        email=f"stranger-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(stranger)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        get_submission_batch(
            batch_id=batch.id,
            status=SubmissionStatus.MATCH_FAILED,
            db=db,
            user=stranger,
        )

    assert exc.value.status_code == 403
