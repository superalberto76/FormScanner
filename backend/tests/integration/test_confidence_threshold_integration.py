# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the confidence threshold.

The threshold decides which extracted results a human has to look at. It is a
system-wide default that a SubmissionBatch may override, and moving it acts
RETROACTIVELY on results already stored — but selectively. What this suite pins
down:

  * a batch created with an explicit threshold stores it, and the effective
    threshold reported back reflects it; created without one, it stores NULL and
    the effective threshold falls through to ``settings.default_confidence_threshold``;
  * moving the threshold re-derives ``review_required`` across the batch's
    not-yet-reviewed results, in both directions;
  * **a result a human has ruled on (``review_outcome`` set) is NOT
    touched** — its flag stays exactly what it was, even when the new threshold
    would imply the opposite. This is the heart of the rule: the flag is part
    of the record of a human decision, and a threshold change must not rewrite
    history. An UNDONE result is fair game again, and a review LOG row alone
    shields nothing;
  * the two axes are OR-ed — ``validity_forces_review`` sends a result to review
    however confident the reading was;
  * a NULL ``confidence_score`` (never extracted) contributes nothing and raises
    nothing;
  * the [0, 1] range is enforced at the API boundary;
  * the audit trail records the old and the new threshold and how many results
    changed side.

Results are built directly via the ORM, on purpose: the rows are created here
in the states the extractor produces (``confidence_score`` and
``validity_forces_review`` included), so each test controls its starting values
without running the extraction pipeline. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1.endpoints.submission_batches import (
    create_submission_batch,
    get_submission_batch,
    update_batch_confidence_threshold,
)
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectType,
    ProjectRole,
    ReviewAction,
    ReviewOutcome,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.submission_review import SubmissionReview
from app.models.user import User
from app.schemas.submission import (
    ConfidenceThresholdUpdate,
    SubmissionBatchCreate,
    SubmissionBatchRead,
)
from app.services import audit_service, confidence_threshold_service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"threshold-{uuid.uuid4().hex}@example.com",
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
        name="Threshold project",
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


def _make_page_object(db, project, owner) -> PageObject:
    """One authored QUESTION object, the thing a result is a result OF.

    Its own configuration is irrelevant here — nothing in this suite reads it;
    a result only needs a PageObject to point at.
    """
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(form_variant_id=variant.id, page_index=1)
    db.add(page)
    db.flush()
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.QUESTION,
        x=0,
        y=0,
        width=10,
        height=10,
    )
    db.add(obj)
    db.flush()
    return obj


def _make_batch(db, project, owner, *, threshold: float | None = None) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=owner.id,
        confidence_threshold=threshold,
    )
    db.add(batch)
    db.flush()
    return batch


def _make_submission_page(
    db, batch, status: SubmissionStatus = SubmissionStatus.MATCHED
) -> SubmissionPage:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=None,
        status=status,
    )
    db.add(submission)
    db.flush()
    page = SubmissionPage(
        submission_id=submission.id,
        source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
        source_filename="page0.png",
        source_page_order=0,
    )
    db.add(page)
    db.flush()
    return page


def _make_result(
    db,
    page: SubmissionPage,
    page_object: PageObject,
    *,
    confidence_score: float | None,
    validity_forces_review: bool = False,
    review_required: bool = False,
) -> SubmissionObjectResult:
    """One extracted result, in the state the extractor leaves it.

    ``review_required`` is passed explicitly rather than derived: these tests
    are about what the RECOMPUTATION does to a stored value, so the starting
    value has to be under the test's control.

    Each result gets its OWN PageObject, a sibling of *page_object* on the same
    template page. A result is unique per ``(submission_page_id,
    page_object_id)`` — one authored object read off one sheet is one reading
    — so several results on one page have to point at several objects. Which
    object is irrelevant here: this suite only ever reads a result's two axes,
    never what it is a result OF.
    """
    own_object = PageObject(
        form_variant_page_id=page_object.form_variant_page_id,
        object_type=PageObjectType.QUESTION,
        x=0,
        y=0,
        width=10,
        height=10,
    )
    db.add(own_object)
    db.flush()
    result = SubmissionObjectResult(
        submission_page_id=page.id,
        page_object_id=own_object.id,
        result_type=SubmissionResultType.QUESTION,
        confidence_score=confidence_score,
        validity_forces_review=validity_forces_review,
        review_required=review_required,
    )
    db.add(result)
    db.flush()
    return result


def _review(
    db,
    result: SubmissionObjectResult,
    reviewer: User,
    action: ReviewAction = ReviewAction.CONFIRMED,
) -> SubmissionReview:
    """A human review action on *result*, as ``review_action_service`` writes
    it: the log row, and the current ruling on the result — which is what
    freezes it. UNDONE clears the ruling and only logs."""
    review = SubmissionReview(
        submission_object_result_id=result.id,
        reviewer_user_id=reviewer.id,
        action=action,
        review_note="looked at it",
    )
    db.add(review)
    if action is ReviewAction.UNDONE:
        result.review_outcome = None
        result.reviewed_by = None
        result.reviewed_at = None
    else:
        result.review_outcome = ReviewOutcome(action.value)
        result.reviewed_by = reviewer.id
        result.reviewed_at = datetime.now(timezone.utc)
    db.flush()
    return review


def _patch(db, batch, threshold: float | None, user) -> SubmissionBatchRead:
    return update_batch_confidence_threshold(
        batch_id=batch.id,
        data=ConfidenceThresholdUpdate(confidence_threshold=threshold),
        db=db,
        user=user,
    )


def _threshold_events(db, batch) -> list[AuditLog]:
    db.flush()
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action
                == audit_service.SUBMISSION_BATCH_CONFIDENCE_THRESHOLD_UPDATED,
                AuditLog.entity_id == str(batch.id),
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Creation: the override, and what "no override" resolves to
# ---------------------------------------------------------------------------
def test_batch_created_with_an_explicit_threshold_stores_and_reports_it(db, actor):
    project = _make_project(db, actor)
    _make_page_object(db, project, actor)  # one published variant: ready to scan

    result = create_submission_batch(
        project_id=project.id,
        data=SubmissionBatchCreate(name="Batch", confidence_threshold=0.42),
        db=db,
        user=actor,
    )

    assert result.confidence_threshold == 0.42
    # The effective threshold is the override, not the system default.
    assert result.effective_threshold == 0.42
    batch = db.get(SubmissionBatch, result.id)
    assert batch.confidence_threshold == 0.42


def test_batch_created_without_a_threshold_follows_the_system_default(db, actor):
    """NULL is not 'no threshold': it is 'follow the default', resolved at read
    time so a later change to the default reaches this batch."""
    project = _make_project(db, actor)
    _make_page_object(db, project, actor)  # one published variant: ready to scan

    result = create_submission_batch(
        project_id=project.id,
        data=SubmissionBatchCreate(name="Batch"),
        db=db,
        user=actor,
    )

    assert result.confidence_threshold is None
    assert result.effective_threshold == settings.default_confidence_threshold
    batch = db.get(SubmissionBatch, result.id)
    assert batch.confidence_threshold is None
    assert confidence_threshold_service.effective_threshold(batch) == (
        settings.default_confidence_threshold
    )


def test_the_shipped_default_threshold_is_zero_point_seven(db):
    """Pinned deliberately: it is the value every batch without an override
    inherits, so a silent change to it silently re-scopes every review queue."""
    assert settings.default_confidence_threshold == 0.7


# ---------------------------------------------------------------------------
# Recomputation over not-yet-reviewed results
# ---------------------------------------------------------------------------
def test_raising_the_threshold_flags_results_that_now_fall_below_it(db, actor):
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    # Both were above 0.5 and therefore clean; 0.6 is below the new 0.8.
    low = _make_result(db, page, page_object, confidence_score=0.6)
    high = _make_result(db, page, page_object, confidence_score=0.95)

    read = _patch(db, batch, 0.8, actor)

    assert read.confidence_threshold == 0.8
    assert read.effective_threshold == 0.8
    db.refresh(low)
    db.refresh(high)
    assert low.review_required is True
    assert high.review_required is False


def test_lowering_the_threshold_clears_results_that_now_clear_it(db, actor):
    """The recomputation runs in BOTH directions: a result flagged only because
    the bar was high must be released when the bar comes down, or the review
    queue would only ever grow."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.9)
    page = _make_submission_page(db, batch)
    result = _make_result(
        db, page, page_object, confidence_score=0.6, review_required=True
    )

    _patch(db, batch, 0.5, actor)

    db.refresh(result)
    assert result.review_required is False


def test_a_confidence_exactly_on_the_threshold_does_not_need_review(db, actor):
    """Strictly BELOW triggers review: a reading exactly as confident as the bar
    is, by definition, confident enough."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor)
    page = _make_submission_page(db, batch)
    result = _make_result(
        db, page, page_object, confidence_score=0.7, review_required=True
    )

    _patch(db, batch, 0.7, actor)

    db.refresh(result)
    assert result.review_required is False


def test_clearing_the_override_recomputes_against_the_system_default(db, actor):
    """Sending null is a decision, not an omission: the batch goes back on the
    system default and its results are re-derived against THAT."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.2)
    page = _make_submission_page(db, batch)
    # 0.5 clears the batch's 0.2 but not the system default of 0.7.
    result = _make_result(db, page, page_object, confidence_score=0.5)

    read = _patch(db, batch, None, actor)

    assert read.confidence_threshold is None
    assert read.effective_threshold == settings.default_confidence_threshold
    db.refresh(result)
    assert result.review_required is True


# ---------------------------------------------------------------------------
# The heart of the rule: a reviewed result is frozen
# ---------------------------------------------------------------------------
def test_a_result_already_reviewed_by_a_human_is_never_recomputed(db, actor):
    """THE test of this suite. Two results in the SAME state, under the same new
    threshold; the only difference is that a human ruled on one
    (``review_outcome`` set). The reviewed one keeps the flag it had when the
    human ruled on it, even though the new threshold implies the opposite —
    that flag is part of the record of the review, and moving a threshold must
    not rewrite history."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    reviewed = _make_result(
        db, page, page_object, confidence_score=0.6, review_required=False
    )
    untouched_by_review = _make_result(
        db, page, page_object, confidence_score=0.6, review_required=False
    )
    _review(db, reviewed, actor)

    # 0.6 < 0.9, so both WOULD be flagged if the review did not shield one.
    _patch(db, batch, 0.9, actor)

    db.refresh(reviewed)
    db.refresh(untouched_by_review)
    assert reviewed.review_required is False, (
        "a reviewed result must keep the flag it had at review time"
    )
    assert untouched_by_review.review_required is True


@pytest.mark.parametrize(
    "action", [ReviewAction.SUBSTITUTED, ReviewAction.FLAGGED]
)
def test_every_outcome_freezes_the_flag(db, actor, action):
    """Not only CONFIRMED: any current ruling means a human looked at it."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    reviewed = _make_result(
        db, page, page_object, confidence_score=0.6, review_required=False
    )
    _review(db, reviewed, actor, action)

    _patch(db, batch, 0.9, actor)

    db.refresh(reviewed)
    assert reviewed.review_required is False


def test_an_undone_result_is_recomputed_again(db, actor):
    """UNDO puts a result back to "not reviewed" — so it moves with the
    threshold again, exactly as one never reviewed. Its CONFIRMED sibling, in
    the same state, stays frozen."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    undone = _make_result(
        db, page, page_object, confidence_score=0.6, review_required=False
    )
    confirmed = _make_result(
        db, page, page_object, confidence_score=0.6, review_required=False
    )
    _review(db, undone, actor)
    _review(db, undone, actor, ReviewAction.UNDONE)
    _review(db, confirmed, actor)

    _patch(db, batch, 0.9, actor)

    db.refresh(undone)
    db.refresh(confirmed)
    assert undone.review_required is True
    assert confirmed.review_required is False


def test_a_review_log_row_alone_no_longer_shields_a_result(db, actor):
    """A ``submission_reviews`` row alone is not a ruling. The log is
    append-only and records UNDO too, so a row without a current ruling must
    not freeze anything."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    result = _make_result(
        db, page, page_object, confidence_score=0.6, review_required=False
    )
    db.add(
        SubmissionReview(
            submission_object_result_id=result.id,
            reviewer_user_id=actor.id,
            action=ReviewAction.CONFIRMED,
        )
    )
    db.flush()

    _patch(db, batch, 0.9, actor)

    db.refresh(result)
    assert result.review_outcome is None
    assert result.review_required is True


def test_the_freeze_also_holds_in_the_clearing_direction(db, actor):
    """Symmetrical, and worth its own test: the exclusion is not "never flag a
    reviewed result" but "never touch one". A reviewed result flagged at review
    time stays flagged when the threshold drops far enough to clear it."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.9)
    page = _make_submission_page(db, batch)
    reviewed = _make_result(
        db, page, page_object, confidence_score=0.8, review_required=True
    )
    _review(db, reviewed, actor)

    _patch(db, batch, 0.1, actor)

    db.refresh(reviewed)
    assert reviewed.review_required is True


def test_the_reviewed_boolean_is_not_what_shields_a_result(db, actor):
    """The signal is the ``submission_reviews`` row, NOT the ``reviewed``
    boolean on the result — which nothing writes today and which would be a
    second, divergeable copy of the same fact. A result claiming reviewed=True
    with no review row behind it is recomputed like any other."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    result = _make_result(db, page, page_object, confidence_score=0.6)
    result.reviewed = True
    db.flush()

    _patch(db, batch, 0.9, actor)

    db.refresh(result)
    assert result.review_required is True


# ---------------------------------------------------------------------------
# The two axes are independent, and OR-ed
# ---------------------------------------------------------------------------
def test_validity_forces_review_wins_over_a_confidence_above_the_threshold(db, actor):
    """The axes are independent: a reading can be perfectly confident
    and still invalid, or belong to a type that always needs a human. Neither
    axis can subsume the other, so the combination is an OR."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    result = _make_result(
        db,
        page,
        page_object,
        confidence_score=0.99,
        validity_forces_review=True,
        review_required=True,
    )

    # A threshold change that clears the confidence axis entirely.
    _patch(db, batch, 0.1, actor)

    db.refresh(result)
    assert result.review_required is True
    # The axes themselves are facts of the optical reading: recomputation reads
    # them and rewrites neither.
    assert result.confidence_score == 0.99
    assert result.validity_forces_review is True


def test_a_never_extracted_result_is_left_alone_and_raises_nothing(db, actor):
    """A NULL confidence means no confidence was ever produced — not a bad
    reading. An unknown value must never, on its own, send a result to review,
    and it must certainly not blow up the recomputation."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor)
    page = _make_submission_page(db, batch)
    result = _make_result(
        db, page, page_object, confidence_score=None, validity_forces_review=False
    )

    _patch(db, batch, 1.0, actor)

    db.refresh(result)
    assert result.review_required is False
    assert result.confidence_score is None


def test_a_never_extracted_result_still_obeys_the_validity_axis(db, actor):
    """With no confidence, ``validity_forces_review`` is the only thing that can
    decide — and it still does."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor)
    page = _make_submission_page(db, batch)
    result = _make_result(
        db,
        page,
        page_object,
        confidence_score=None,
        validity_forces_review=True,
        review_required=False,
    )

    _patch(db, batch, 0.0, actor)

    db.refresh(result)
    assert result.review_required is True


# ---------------------------------------------------------------------------
# Scope: the recomputation stops at the batch boundary
# ---------------------------------------------------------------------------
def test_results_of_another_batch_are_not_touched(db, actor):
    """The threshold is per batch, so the rewrite must be too — the join goes
    submission_object_results -> submission_pages -> submissions and filters on
    submission_batch_id."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    target = _make_batch(db, project, actor, threshold=0.5)
    other = _make_batch(db, project, actor, threshold=0.5)
    mine = _make_result(
        db, _make_submission_page(db, target), page_object, confidence_score=0.6
    )
    theirs = _make_result(
        db, _make_submission_page(db, other), page_object, confidence_score=0.6
    )

    _patch(db, target, 0.9, actor)

    db.refresh(mine)
    db.refresh(theirs)
    assert mine.review_required is True
    assert theirs.review_required is False


@pytest.mark.parametrize("closed", [SubmissionStatus.REVIEWED, SubmissionStatus.FINALIZED])
def test_results_of_a_closed_submission_are_not_touched(db, actor, closed):
    """A closed submission's unresolved results stay unresolved, frozen at
    closing — in both directions — while an open submission of the same batch
    still moves with the threshold."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    closed_page = _make_submission_page(db, batch, closed)
    open_page = _make_submission_page(db, batch, SubmissionStatus.SCANNED)
    # Clean at 0.5; flagged at 0.9 — but not on the closed submission.
    left_clean = _make_result(db, closed_page, page_object, confidence_score=0.6)
    moves = _make_result(db, open_page, page_object, confidence_score=0.6)
    # Flagged at 0.9; released at 0.1 — but not on the closed submission.
    left_pending = _make_result(
        db, closed_page, page_object, confidence_score=0.3, review_required=True
    )

    _patch(db, batch, 0.9, actor)
    _patch(db, batch, 0.1, actor)

    for result in (left_clean, moves, left_pending):
        db.refresh(result)
    assert left_clean.review_required is False
    assert left_pending.review_required is True
    assert moves.review_required is False  # raised to True, then released


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_a_threshold_outside_zero_one_is_rejected(bad):
    """Enforced at the API boundary (Field(ge=0, le=1)), not by the DB — the
    ValidationError below is what FastAPI renders as a 422. Same rule on the
    creation body."""
    with pytest.raises(ValidationError):
        ConfidenceThresholdUpdate(confidence_threshold=bad)
    with pytest.raises(ValidationError):
        SubmissionBatchCreate(name="Batch", confidence_threshold=bad)


@pytest.mark.parametrize("ok", [0.0, 0.5, 1.0])
def test_the_bounds_themselves_are_accepted(ok):
    """0 and 1 are legitimate settings: 'never ask for review on confidence
    grounds' and 'always ask unless the reading was perfect'."""
    assert ConfidenceThresholdUpdate(confidence_threshold=ok).confidence_threshold == ok


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def test_the_audit_trail_records_both_thresholds_and_the_rows_that_changed(db, actor):
    """A rewrite that touches many rows and none of them individually leaves no
    other trace, so the event has to carry it: where the threshold came from,
    where it went, and how many results changed side."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    # Two will flip; one is already above the new bar and one is shielded by a
    # review — neither counts.
    _make_result(db, page, page_object, confidence_score=0.6)
    _make_result(db, page, page_object, confidence_score=0.7)
    _make_result(db, page, page_object, confidence_score=0.95)
    shielded = _make_result(db, page, page_object, confidence_score=0.6)
    _review(db, shielded, actor)

    _patch(db, batch, 0.9, actor)

    events = _threshold_events(db, batch)
    assert len(events) == 1
    metadata = events[0].metadata_json
    assert metadata["old_threshold"] == 0.5
    assert metadata["new_threshold"] == 0.9
    assert metadata["results_recomputed"] == 2


def test_the_audit_trail_reports_zero_when_nothing_changed_side(db, actor):
    """The count is of rows that actually CHANGED, not of rows examined: moving
    the threshold within a range where every result stays put is recorded as the
    no-op it was."""
    project = _make_project(db, actor)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    _make_result(db, page, page_object, confidence_score=0.95)

    _patch(db, batch, 0.6, actor)

    metadata = _threshold_events(db, batch)[0].metadata_json
    assert metadata["results_recomputed"] == 0


def test_clearing_the_override_is_recorded_as_a_null_new_threshold(db, actor):
    project = _make_project(db, actor)
    batch = _make_batch(db, project, actor, threshold=0.3)

    _patch(db, batch, None, actor)

    metadata = _threshold_events(db, batch)[0].metadata_json
    assert metadata["old_threshold"] == 0.3
    assert metadata["new_threshold"] is None


# ---------------------------------------------------------------------------
# Reachability: a concluded project must stay adjustable
# ---------------------------------------------------------------------------
def test_the_threshold_can_still_be_moved_on_a_non_active_project(db, actor):
    """No ACTIVE gate, deliberately — the same reasoning as run_batch_matching.
    That gate stops sheets being COLLECTED outside the collection phase; this
    collects nothing and only re-reads what is stored. A batch whose collection
    is over is exactly the one whose results may deserve a second look."""
    project = _make_project(db, actor, status=FormProjectStatus.CLOSED)
    page_object = _make_page_object(db, project, actor)
    batch = _make_batch(db, project, actor, threshold=0.5)
    page = _make_submission_page(db, batch)
    result = _make_result(db, page, page_object, confidence_score=0.6)

    read = _patch(db, batch, 0.9, actor)

    assert read.effective_threshold == 0.9
    db.refresh(result)
    assert result.review_required is True


# ---------------------------------------------------------------------------
# The batch detail reports the pair too
# ---------------------------------------------------------------------------
def test_the_batch_detail_reports_the_override_and_the_effective_threshold(db, actor):
    project = _make_project(db, actor)
    batch = _make_batch(db, project, actor, threshold=0.25)

    detail = get_submission_batch(batch_id=batch.id, db=db, user=actor)

    assert detail.confidence_threshold == 0.25
    assert detail.effective_threshold == 0.25
