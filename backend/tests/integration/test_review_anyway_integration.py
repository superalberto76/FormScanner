# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for extraction attempts, unread objects and
"Review anyway".

What this suite pins down:

  * **extraction attempts** — every submission the walk reaches gets
    ``extraction_attempted_at``, errors included; MATCH_FAILED / UNASSIGNED,
    never walked, do not; a re-run moves it forward;
  * **unread objects** — exactly the objects the walk could read and has no
    result for: an object whose extractor raised is unread; an extracted one, a
    not-yet-supported one, a never-evaluable one and one on an unbound sheet
    are not;
  * **the queue** — ``extraction_attempted`` and ``unread_object_count`` on
    each ``SubmissionSummary``;
  * **Review anyway** — placeholders typed like the reading they stand in for,
    with a NULL payload; SCANNED; the audit event; the structured 409s; the
    0-unread race; the submission is then lockable; a later run skips the
    placeholders.

The extractors are STUBBED: this suite is about which objects get read, not
about reading pixels, so a stub that either writes a result or raises is all
the walk needs to be exercised.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.endpoints.submission_batches import get_submission_batch
from app.api.v1.endpoints.submissions import review_submission_anyway
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    SubmissionResultType,
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
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.repositories import submission_object_result_repo
from app.services import (
    audit_service,
    extraction_orchestrator_service as orchestrator,
    respondent_identifier_extraction_service,
    review_anyway_service,
    submission_lock_service,
)
from app.services.review_anyway_service import (
    NOT_ELIGIBLE,
    REASON_NOT_EXTRACTED,
    REASON_STATUS,
)

LONG_AGO = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"anyway-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
        first_name="Mario",
        last_name="Rossi",
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Review-anyway project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER))
    db.flush()
    return project


@pytest.fixture
def batch(db, project, actor) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch", created_by_user_id=actor.id
    )
    db.add(batch)
    db.flush()
    return batch


def _make_template_page(db, project, actor) -> FormVariantPage:
    """A one-page PUBLISHED variant of its own, so role bindings never collide."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name="Variant",
        status=FormVariantStatus.PUBLISHED,
        # Unique per project; a random one keeps the builder free of counting.
        version_number=uuid.uuid4().int % 1_000_000,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(form_variant_id=variant.id, page_index=1)
    db.add(page)
    db.flush()
    return page


@pytest.fixture
def template_page(db, project, actor) -> FormVariantPage:
    return _make_template_page(db, project, actor)


def _add_object(
    db, template_page, object_type: PageObjectType, *, role: PageObjectRole | None = None
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=object_type,
        x=10,
        y=10,
        width=50,
        height=50,
    )
    db.add(obj)
    db.flush()
    if role is not None:
        db.add(
            FormVariantRoleBinding(
                form_variant_id=template_page.form_variant_id,
                page_object_id=obj.id,
                role=role,
            )
        )
        db.flush()
    db.refresh(obj)
    return obj


def _make_submission(
    db,
    batch,
    template_page: FormVariantPage,
    *,
    status: SubmissionStatus = SubmissionStatus.MATCHED,
    sheets: int = 1,
    bound: bool = True,
    attempted_at: datetime | None = None,
) -> Submission:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=status,
        extraction_attempted_at=attempted_at,
    )
    db.add(submission)
    db.flush()
    for order in range(sheets):
        db.add(
            SubmissionPage(
                submission_id=submission.id,
                form_variant_page_id=template_page.id if bound else None,
                source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
                source_filename=f"page{order}.png",
                source_page_order=order,
            )
        )
    db.flush()
    db.refresh(submission)
    return submission


@pytest.fixture
def failing(monkeypatch) -> set[uuid.UUID]:
    """Stub every extractor: write a result, unless the object's id is in the
    returned set, in which case raise. Tests add ids to make objects fail."""
    fail_ids: set[uuid.UUID] = set()

    def stub(db, page, page_object, batch):
        if page_object.id in fail_ids:
            raise RuntimeError("unreadable")
        return submission_object_result_repo.add(
            db,
            SubmissionObjectResult(
                submission_page_id=page.id,
                page_object_id=page_object.id,
                result_type=orchestrator.result_type_for(page_object),
                detected_payload_json={"stub": True},
            ),
        )

    for object_type in list(orchestrator._EXTRACTORS):
        monkeypatch.setitem(orchestrator._EXTRACTORS, object_type, stub)
    monkeypatch.setattr(
        respondent_identifier_extraction_service, "extract_respondent_identifier_result", stub
    )
    return fail_ids


def _run(db, batch) -> orchestrator.ExtractionRunSummary:
    summary = orchestrator.run_extraction_for_batch(db, batch.id)
    db.flush()
    return summary


def _unread_ids(db, submission) -> set[uuid.UUID]:
    db.expire_all()
    return {
        obj.id
        for _page, obj in review_anyway_service.unread_objects_for_submission(db, submission)
    }


def _results(db, submission) -> list[SubmissionObjectResult]:
    return list(
        db.scalars(
            select(SubmissionObjectResult)
            .join(SubmissionPage, SubmissionPage.id == SubmissionObjectResult.submission_page_id)
            .where(SubmissionPage.submission_id == submission.id)
        ).all()
    )


# ---------------------------------------------------------------------------
# Extraction attempts
# ---------------------------------------------------------------------------


def test_every_walked_submission_is_stamped_errors_included(
    db, batch, template_page, failing
):
    question = _add_object(db, template_page, PageObjectType.QUESTION)
    clean = _make_submission(db, batch, template_page)
    other_page = _make_template_page(db, batch.form_project, batch.created_by_user)
    doomed = _add_object(db, other_page, PageObjectType.QUESTION)
    broken = _make_submission(db, batch, other_page)
    failing.add(doomed.id)

    summary = _run(db, batch)

    assert summary.errors == 1
    assert clean.status is SubmissionStatus.SCANNED
    assert broken.status is SubmissionStatus.MATCHED
    assert clean.extraction_attempted_at is not None
    assert broken.extraction_attempted_at == clean.extraction_attempted_at
    assert question.id not in _unread_ids(db, clean)


@pytest.mark.parametrize(
    "status", [SubmissionStatus.MATCH_FAILED, SubmissionStatus.UNASSIGNED]
)
def test_a_submission_never_walked_is_not_stamped(db, batch, template_page, failing, status):
    _add_object(db, template_page, PageObjectType.QUESTION)
    skipped = _make_submission(db, batch, template_page, status=status)

    _run(db, batch)

    assert skipped.extraction_attempted_at is None


def test_a_rerun_moves_the_stamp_forward(db, batch, template_page, failing):
    _add_object(db, template_page, PageObjectType.QUESTION)
    submission = _make_submission(db, batch, template_page, attempted_at=LONG_AGO)

    _run(db, batch)

    assert submission.extraction_attempted_at > LONG_AGO


def test_a_submission_with_nothing_left_to_read_is_still_stamped(
    db, batch, template_page, failing
):
    _add_object(db, template_page, PageObjectType.TEXT)
    submission = _make_submission(db, batch, template_page, status=SubmissionStatus.SCANNED)

    _run(db, batch)

    assert submission.extraction_attempted_at is not None


# ---------------------------------------------------------------------------
# Unread objects
# ---------------------------------------------------------------------------


def test_an_object_whose_extractor_raised_is_unread_an_extracted_one_is_not(
    db, batch, template_page, failing
):
    read = _add_object(db, template_page, PageObjectType.QUESTION)
    doomed = _add_object(db, template_page, PageObjectType.MATRIX)
    submission = _make_submission(db, batch, template_page)
    failing.add(doomed.id)

    _run(db, batch)

    assert _unread_ids(db, submission) == {doomed.id}
    assert read.id not in _unread_ids(db, submission)


def test_not_yet_supported_and_never_evaluable_objects_are_not_unread(
    db, batch, template_page, failing, monkeypatch
):
    # Not yet supported: an answer type with no extractor. Every type has one,
    # so the case is simulated by emptying FREE_TEXT_AREA's slot.
    monkeypatch.delitem(orchestrator._EXTRACTORS, PageObjectType.FREE_TEXT_AREA)
    _add_object(db, template_page, PageObjectType.FREE_TEXT_AREA)
    # Never evaluable: decoration and a role-carrying CODE.
    _add_object(db, template_page, PageObjectType.TEXT)
    _add_object(db, template_page, PageObjectType.IMAGE)
    _add_object(db, template_page, PageObjectType.CODE, role=PageObjectRole.VARIANT_IDENTIFIER)
    submission = _make_submission(db, batch, template_page)

    summary = _run(db, batch)

    assert summary.skipped_not_yet_supported == 1
    assert summary.skipped_not_applicable == 3
    assert _unread_ids(db, submission) == set()


def test_objects_of_an_unbound_sheet_are_not_unread(db, batch, template_page, failing):
    _add_object(db, template_page, PageObjectType.QUESTION)
    submission = _make_submission(db, batch, template_page, bound=False)

    assert _unread_ids(db, submission) == set()


def test_never_extracted_every_extractable_object_is_unread(db, batch, template_page):
    """The rule itself does not look at the attempt — the queue and the action
    do. Unread before any run is every object a run could read."""
    question = _add_object(db, template_page, PageObjectType.QUESTION)
    code = _add_object(db, template_page, PageObjectType.CODE)
    _add_object(db, template_page, PageObjectType.TEXT)
    submission = _make_submission(db, batch, template_page, sheets=2)

    unread = review_anyway_service.unread_objects_for_submission(db, submission)

    assert len(unread) == 4  # two objects on each of two sheets
    assert {obj.id for _p, obj in unread} == {question.id, code.id}


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def _summaries(db, batch, actor):
    db.expire_all()
    detail = get_submission_batch(batch_id=batch.id, status=None, db=db, user=actor)
    return {s.id: s for s in detail.submissions}


def test_queue_fields(db, actor, batch, template_page, failing):
    question = _add_object(db, template_page, PageObjectType.QUESTION)
    matrix = _add_object(db, template_page, PageObjectType.MATRIX)
    with_errors = _make_submission(db, batch, template_page, sheets=2)
    failing.update({question.id, matrix.id})
    _run(db, batch)
    # Created after the run: matched, never examined.
    never = _make_submission(db, batch, template_page)
    failing.clear()
    scanned_page = _make_template_page(db, batch.form_project, actor)
    _add_object(db, scanned_page, PageObjectType.QUESTION)
    scanned = _make_submission(
        db, batch, scanned_page, status=SubmissionStatus.SCANNED, attempted_at=LONG_AGO
    )

    rows = _summaries(db, batch, actor)

    assert (rows[never.id].extraction_attempted, rows[never.id].unread_object_count) == (False, 0)
    assert (
        rows[with_errors.id].extraction_attempted,
        rows[with_errors.id].unread_object_count,
    ) == (True, 4)
    assert (rows[scanned.id].extraction_attempted, rows[scanned.id].unread_object_count) == (True, 0)


# ---------------------------------------------------------------------------
# Review anyway
# ---------------------------------------------------------------------------


def _anyway(db, submission, user):
    return review_submission_anyway(submission_id=submission.id, db=db, user=user)


def _conflict(excinfo, code: str) -> dict:
    exc = excinfo.value
    assert exc.status_code == 409
    assert exc.detail["code"] == code
    assert exc.detail["message"]
    return exc.detail


def test_review_anyway_creates_typed_placeholders_and_scans(
    db, actor, batch, template_page, failing
):
    objects = {
        SubmissionResultType.QUESTION: _add_object(db, template_page, PageObjectType.QUESTION),
        SubmissionResultType.MATRIX: _add_object(db, template_page, PageObjectType.MATRIX),
        SubmissionResultType.RESPONDENT_IDENTIFIER: _add_object(
            db, template_page, PageObjectType.MATRIX, role=PageObjectRole.RESPONDENT_IDENTIFIER
        ),
        SubmissionResultType.FREE_TEXT: _add_object(db, template_page, PageObjectType.FREE_TEXT_AREA),
        SubmissionResultType.CODE: _add_object(db, template_page, PageObjectType.CODE),
    }
    read = _add_object(db, template_page, PageObjectType.QUESTION)
    submission = _make_submission(db, batch, template_page)
    failing.update(o.id for o in objects.values())
    _run(db, batch)
    assert submission.status is SubmissionStatus.MATCHED

    outcome = _anyway(db, submission, actor)

    assert outcome.status is SubmissionStatus.SCANNED
    assert outcome.placeholders_created == 5
    db.expire_all()
    assert db.get(Submission, submission.id).status is SubmissionStatus.SCANNED
    placeholders = {r.page_object_id: r for r in _results(db, submission) if r.detected_payload_json is None}
    assert set(placeholders) == {o.id for o in objects.values()}
    for result_type, obj in objects.items():
        placeholder = placeholders[obj.id]
        assert placeholder.result_type is result_type
        assert placeholder.confidence_score is None
        assert placeholder.validity_forces_review is True
        assert placeholder.review_required is True
    # The object a run did read keeps its reading, untouched.
    assert read.id not in placeholders

    event = db.scalars(
        select(AuditLog).where(
            AuditLog.action == audit_service.SUBMISSION_REVIEWED_ANYWAY,
            AuditLog.entity_id == str(submission.id),
        )
    ).one()
    assert event.metadata_json["placeholders_created"] == 5
    assert sorted(event.metadata_json["page_object_ids"]) == sorted(
        str(o.id) for o in objects.values()
    )


def test_review_anyway_outside_matched_is_not_eligible(db, actor, batch, template_page):
    submission = _make_submission(
        db, batch, template_page, status=SubmissionStatus.SCANNED, attempted_at=LONG_AGO
    )

    with pytest.raises(HTTPException) as excinfo:
        _anyway(db, submission, actor)

    detail = _conflict(excinfo, NOT_ELIGIBLE)
    assert detail["reason"] == REASON_STATUS
    assert detail["status"] == "SCANNED"


def test_review_anyway_before_any_run_is_not_eligible(db, actor, batch, template_page):
    _add_object(db, template_page, PageObjectType.QUESTION)
    submission = _make_submission(db, batch, template_page)

    with pytest.raises(HTTPException) as excinfo:
        _anyway(db, submission, actor)

    detail = _conflict(excinfo, NOT_ELIGIBLE)
    assert detail["reason"] == REASON_NOT_EXTRACTED
    assert detail["message"] == "Run extraction at least once before reviewing anyway."
    db.expire_all()
    assert db.get(Submission, submission.id).status is SubmissionStatus.MATCHED


def test_review_anyway_with_nothing_unread_still_scans(db, actor, batch, template_page):
    """The race: a run read everything between the queue and the click."""
    submission = _make_submission(db, batch, template_page, attempted_at=LONG_AGO)

    outcome = _anyway(db, submission, actor)

    assert (outcome.status, outcome.placeholders_created) == (SubmissionStatus.SCANNED, 0)


def test_after_review_anyway_the_submission_is_lockable(
    db, actor, batch, template_page, failing
):
    doomed = _add_object(db, template_page, PageObjectType.QUESTION)
    submission = _make_submission(db, batch, template_page)
    failing.add(doomed.id)
    _run(db, batch)
    _anyway(db, submission, actor)

    assert submission_lock_service.acquire(db, submission.id, actor).acquired is True


def test_a_later_run_skips_the_placeholders(db, actor, batch, template_page, failing):
    doomed = _add_object(db, template_page, PageObjectType.QUESTION)
    submission = _make_submission(db, batch, template_page)
    failing.add(doomed.id)
    _run(db, batch)
    _anyway(db, submission, actor)
    # The object would now read fine — it is still not retried.
    failing.clear()

    summary = _run(db, batch)

    assert summary.skipped_already_extracted == 1
    assert summary.results_created == 0
    (placeholder,) = _results(db, submission)
    assert placeholder.detected_payload_json is None


def test_a_concurrent_write_of_the_same_result_is_submission_changed(
    db, actor, batch, template_page, monkeypatch
):
    """A run committing a reading of the same (sheet, object) first surfaces as
    the uniqueness violation, refused in the shape the FE already handles."""
    obj = _add_object(db, template_page, PageObjectType.QUESTION)
    submission = _make_submission(db, batch, template_page, attempted_at=LONG_AGO)
    (sheet,) = submission.pages
    db.add(
        SubmissionObjectResult(
            submission_page_id=sheet.id,
            page_object_id=obj.id,
            result_type=SubmissionResultType.QUESTION,
            detected_payload_json={"read": "by the run"},
        )
    )
    db.flush()
    # As if the unread set had been computed before the run's write landed.
    monkeypatch.setattr(
        review_anyway_service,
        "unread_objects_for_submission",
        lambda db_, submission_: [(sheet, obj)],
    )

    with pytest.raises(HTTPException) as excinfo:
        _anyway(db, submission, actor)

    _conflict(excinfo, submission_lock_service.SUBMISSION_CHANGED)


def test_review_anyway_by_a_non_member_is_forbidden(db, batch, template_page):
    outsider = User(
        email=f"out-{uuid.uuid4().hex}@example.com", password_hash="x", role=UserRole.USER
    )
    db.add(outsider)
    db.flush()
    submission = _make_submission(db, batch, template_page, attempted_at=LONG_AGO)

    with pytest.raises(HTTPException) as excinfo:
        _anyway(db, submission, outsider)

    assert excinfo.value.status_code == 403


def test_review_anyway_works_on_a_closed_project(db, actor, project, batch, template_page):
    project.status = FormProjectStatus.CLOSED
    submission = _make_submission(db, batch, template_page, attempted_at=LONG_AGO)
    db.flush()

    assert _anyway(db, submission, actor).status is SubmissionStatus.SCANNED
