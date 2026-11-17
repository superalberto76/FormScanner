# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the matching engine.

The step after ingestion: which FormVariant is this submission (Level 1), and
which FormVariantPage is each physical sheet (Level 2). What this suite pins
down, in the order the engine decides it:

  * one published variant → matched with NO code read at all;
  * several published variants → the first page whose VID value belongs to a
    published variant of the project decides, wherever it sits in the
    submission;
  * nothing readable → the WHOLE submission goes to MATCH_FAILED, pages
    unbound;
  * a page whose code carries an index uses it VERBATIM (both sides 1-based),
    confidence 1.0;
  * a page without one falls back to ``source_page_order + 1`` — a 0-based to
    1-based CONVERSION, tested explicitly because getting it wrong is the single
    most likely bug in this engine — confidence 0.5;
  * an index the variant has no page for leaves the binding NULL with confidence
    0.0 and never blocks the sibling sheets;
  * everything is idempotent: a second run neither repeats nor overwrites;
  * both upload paths already match what they ingest, in the same transaction;
  * the manual re-run walks a mixed batch and only touches what is unresolved.

Submissions are mostly built DIRECTLY (Submission + SubmissionPage rows with the
``identified_code_value`` ingestion would have stored) rather than through an
upload: the engine's contract is about what it does with stored identification,
and building it directly exercises every branch without rasterising a PDF. The
two ``upload_*`` tests at the end close that loop against the real ingestion
path. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import fitz  # PyMuPDF
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from app.api.v1.endpoints.submission_batches import run_batch_matching
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    MatchFailureReason,
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
from app.schemas.submission import SubmissionBatchMatchingResult
from app.services import (
    audit_service,
    submission_batch_service,
    submission_matching_service,
)
from app.utils import code_rendering


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _submission_storage(tmp_path, monkeypatch):
    """Redirect both submission storage dirs into a throwaway tmp_path."""
    monkeypatch.setattr(
        settings, "submission_originals_dir", str(tmp_path / "submission_originals")
    )
    monkeypatch.setattr(
        settings, "submission_pages_dir", str(tmp_path / "submission_pages")
    )


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"match-{uuid.uuid4().hex}@example.com",
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
        name="Matching project",
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
    identifier_value: str | None = None,
) -> FormVariant:
    """A variant with ``page_count`` pages indexed 1..n, optionally identified.

    Built directly (variant → pages → CODE object → binding) rather than through
    the authoring services, which require a DRAFT variant and a shared object:
    here the PUBLISHED end state is what matching reads, so that is what is set
    up. Page indices are 1-BASED, as ``form_variant_page_repo`` provisions them.
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
    pages = []
    for index in range(1, page_count + 1):
        page = FormVariantPage(form_variant_id=variant.id, page_index=index)
        db.add(page)
        pages.append(page)
    db.flush()

    if identifier_value is not None:
        obj = PageObject(
            form_variant_page_id=pages[0].id,
            object_type=PageObjectType.CODE,
            x=0, y=0, width=50, height=50, label="ident",
        )
        db.add(obj)
        db.flush()
        db.add(
            CodeObjectConfig(
                page_object_id=obj.id,
                code_type=CodeType.QR,
                value=identifier_value,
                include_page_index=True,
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
    status=SubmissionStatus.UPLOADED,
    page_orders: list[int] | None = None,
) -> Submission:
    """A submission whose pages already carry what ingestion would have stored.

    ``codes[i]`` is the RAW ``identified_code_value`` of the i-th page (None for
    a page with no readable VARIANT_IDENTIFIER); ``page_orders`` overrides the
    0-based physical positions, which default to 0..n-1.
    """
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=None,
        status=status,
    )
    db.add(submission)
    db.flush()
    orders = page_orders if page_orders is not None else list(range(len(codes)))
    for order, raw in zip(orders, codes):
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


def _match_event(db, submission) -> AuditLog:
    """The ``submission.matched`` audit event of one submission.

    Where the non-blocking matching anomalies are reported — the same role the
    per-CALL ingest event plays for conflicting codes and duplicate positions.
    """
    db.flush()
    return db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == audit_service.SUBMISSION_MATCHED,
            AuditLog.entity_id == str(submission.id),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    ).first()


def _bound_indices(db, submission) -> list[int | None]:
    """The 1-based template page_index each sheet ended up bound to."""
    result = []
    for page in _pages_in_order(submission):
        if page.form_variant_page_id is None:
            result.append(None)
        else:
            result.append(db.get(FormVariantPage, page.form_variant_page_id).page_index)
    return result


# ---------------------------------------------------------------------------
# LEVEL 1 — a single published variant needs no code at all
# ---------------------------------------------------------------------------
def test_single_published_variant_matches_without_reading_any_code(db, actor):
    """The unambiguous case: there is exactly one variant the sheets can belong
    to, so nothing has to be read off them — and a submission with no readable
    code on ANY page still matches."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=3)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None, None])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant.id
    assert submission.status is SubmissionStatus.MATCHED


def test_single_published_variant_wins_over_draft_siblings(db, actor):
    """DRAFT and ARCHIVED variants are not scannable, so they never make the
    project ambiguous — one PUBLISHED variant beside them is still 'exactly one'."""
    project = _make_project(db, actor)
    published = _make_variant(db, project, actor, version_number=1)
    _make_variant(
        db, project, actor, version_number=2, status=FormVariantStatus.DRAFT
    )
    _make_variant(
        db, project, actor, version_number=3, status=FormVariantStatus.ARCHIVED
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == published.id


# ---------------------------------------------------------------------------
# LEVEL 1 — several published variants: any readable page decides
# ---------------------------------------------------------------------------
def test_vid_on_any_page_resolves_the_right_variant(db, actor):
    """The VARIANT_IDENTIFIER is shared across every page of a variant, so ONE
    readable page anywhere in the submission is enough — here the only
    readable one is the LAST sheet."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None, "VID:VAR-B|3"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant_b.id
    assert submission.status is SubmissionStatus.MATCHED


def test_first_readable_page_in_physical_order_decides(db, actor):
    """Deterministic when sheets disagree (a mixed-up pile): the submission is
    scanned in source_page_order and the first page that resolves wins."""
    project = _make_project(db, actor)
    variant_a = _make_variant(
        db, project, actor, version_number=1, identifier_value="VAR-A"
    )
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-A", "VID:VAR-B"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant_a.id


def test_vid_without_page_index_still_resolves_the_variant(db, actor):
    """include_page_index=False is the default: the code identifies the sheet but
    says nothing about its position. Level 1 only needs the VALUE."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-B"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant_b.id


def test_identifier_on_a_draft_variant_is_not_a_match(db, actor):
    """Only PUBLISHED variants are scannable; a value that only exists on a DRAFT
    one resolves nothing, exactly like the scan-readiness rule."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    _make_variant(
        db,
        project,
        actor,
        version_number=3,
        status=FormVariantStatus.DRAFT,
        identifier_value="VAR-DRAFT",
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-DRAFT|1"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id is None
    assert submission.status is SubmissionStatus.MATCH_FAILED


# ---------------------------------------------------------------------------
# LEVEL 1 — two published variants sharing one identifier value
# ---------------------------------------------------------------------------
def test_ambiguous_identifier_value_still_resolves_but_is_reported(db, actor):
    """A misconfiguration with no correct answer: two PUBLISHED variants printed
    with the SAME VARIANT_IDENTIFIER value. The sheets are indistinguishable on
    paper, so no rule can pick the right one — roughly half the submissions
    will be attributed to the wrong variant, and NOTHING else in the data
    betrays it.

    So the engine does what the rest of this pipeline does with an unresolvable
    clash (conflicting codes, group-key mismatch, duplicate positions): it
    resolves DETERMINISTICALLY and REPORTS. The lowest version_number wins —
    unchanged behaviour, no blocking — and the audit event carries the anomaly.
    """
    project = _make_project(db, actor)
    first = _make_variant(
        db, project, actor, version_number=1, page_count=2, identifier_value="SAME"
    )
    second = _make_variant(
        db, project, actor, version_number=2, page_count=2, identifier_value="SAME"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:SAME|1", "VID:SAME|2"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    # Deterministic: the lowest version_number wins, and the submission is
    # matched and bound exactly as it would have been without the ambiguity.
    assert submission.matched_form_variant_id == first.id
    assert submission.status is SubmissionStatus.MATCHED
    assert _bound_indices(db, submission) == [1, 2]

    # …but the clash is on the record, with EVERY candidate, winner included.
    event = _match_event(db, submission)
    assert event is not None
    assert event.metadata_json["identifier_value_ambiguous"] is True
    assert set(event.metadata_json["ambiguous_variant_ids"]) == {
        str(first.id), str(second.id)
    }
    # The chosen variant is still reported normally alongside it.
    assert event.metadata_json["variant_id"] == str(first.id)


def test_an_unambiguous_match_carries_no_ambiguity_flag(db, actor):
    """The flag is an ANOMALY marker, not a field that is always present: an
    ordinary match must not carry it, or it would be noise instead of a signal."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, page_count=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-B|1"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant_b.id
    event = _match_event(db, submission)
    assert "identifier_value_ambiguous" not in event.metadata_json
    assert "ambiguous_variant_ids" not in event.metadata_json


def test_a_value_shared_with_a_draft_variant_is_not_ambiguous(db, actor):
    """Only PUBLISHED variants can be scanned, so a DRAFT sibling reusing the
    value creates no ambiguity on paper and must not be flagged as one."""
    project = _make_project(db, actor)
    published = _make_variant(
        db, project, actor, version_number=1, page_count=1, identifier_value="SAME"
    )
    _make_variant(db, project, actor, version_number=2, identifier_value="OTHER")
    _make_variant(
        db,
        project,
        actor,
        version_number=3,
        status=FormVariantStatus.DRAFT,
        identifier_value="SAME",
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:SAME|1"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == published.id
    assert "identifier_value_ambiguous" not in _match_event(db, submission).metadata_json


# ---------------------------------------------------------------------------
# LEVEL 1 — failure: the whole submission goes to MATCH_FAILED
# ---------------------------------------------------------------------------
def test_no_resolvable_page_fails_the_whole_submission(db, actor):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.matched_form_variant_id is None
    # Level 2 never runs without a variant: no page is bound, and none is even
    # scored — NULL confidence still means "never examined".
    assert all(p.form_variant_page_id is None for p in submission.pages)
    assert all(p.match_confidence is None for p in submission.pages)
    # The reason is on the submission, not only in the audit trail.
    assert submission.match_failure_reason is MatchFailureReason.NO_VID_FOUND
    assert submission.match_failure_code_value is None


def test_unknown_vid_value_fails_with_its_own_reason(db, actor):
    """A value that WAS read but belongs to no published variant (a sheet from
    another campaign) is a different failure from 'nothing was readable', and the
    audit trail tells them apart."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-FROM-ANOTHER-PROJECT|1"])

    outcome = submission_matching_service._match_submission(
        db, submission, actor_user_id=actor.id
    )

    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert outcome.reason == submission_matching_service.REASON_VID_NOT_RECOGNISED
    # The reason AND the value read, on the submission and in the event.
    assert submission.match_failure_reason is MatchFailureReason.VID_NOT_RECOGNISED
    assert submission.match_failure_code_value == "VAR-FROM-ANOTHER-PROJECT"
    db.flush()
    event = db.scalars(
        select(AuditLog).where(
            AuditLog.action == audit_service.SUBMISSION_MATCH_FAILED,
            AuditLog.entity_id == str(submission.id),
        )
    ).one()
    assert event.metadata_json["code_value"] == "VAR-FROM-ANOTHER-PROJECT"


def test_untagged_and_respondent_codes_identify_no_variant(db, actor):
    """Only a VID tag speaks for the variant: an untagged third-party code and a
    RID are both 'no signal' here."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VAR-A|1", "RID:STUDENT-7"])

    outcome = submission_matching_service._match_submission(
        db, submission, actor_user_id=actor.id
    )

    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert outcome.reason == submission_matching_service.REASON_NO_VID_FOUND
    assert submission.match_failure_reason is MatchFailureReason.NO_VID_FOUND


def test_no_published_variant_at_all_fails_with_a_configuration_reason(db, actor):
    project = _make_project(db, actor)
    _make_variant(
        db, project, actor, version_number=1, status=FormVariantStatus.DRAFT
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-A|1"])

    outcome = submission_matching_service._match_submission(
        db, submission, actor_user_id=actor.id
    )

    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert outcome.reason == submission_matching_service.REASON_NO_PUBLISHED_VARIANT
    assert submission.match_failure_reason is MatchFailureReason.NO_PUBLISHED_VARIANT
    # A value was read, but with nothing published it is not the reason.
    assert submission.match_failure_code_value is None


def test_match_failed_submission_resolves_on_a_later_run(db, actor):
    """MATCH_FAILED is not terminal: it records that a run found nothing, and a
    re-run after the missing signal appears resolves it normally (this is what
    the manual endpoint exists for)."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])
    submission_matching_service.match_submission(db, submission)
    assert submission.status is SubmissionStatus.MATCH_FAILED

    # The sheet is rescanned and its code is now readable.
    _pages_in_order(submission)[0].identified_code_value = "VID:VAR-B|1"
    db.flush()
    submission_matching_service.match_submission(db, submission)

    assert submission.matched_form_variant_id == variant_b.id
    assert submission.status is SubmissionStatus.MATCHED
    # Resolved: the reason it had failed is cleared.
    assert submission.match_failure_reason is None
    assert submission.match_failure_code_value is None


def test_the_value_kept_is_the_first_unrecognised_one_in_physical_order(db, actor):
    """With several unrecognised values the submission keeps the first in
    physical order — the same order Level 1 reads in — and a later run that
    reads a different one replaces it."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, batch, ["VID:SECOND|2", None, "VID:FIRST|1"], page_orders=[1, 2, 0]
    )

    submission_matching_service.match_submission(db, submission)

    assert submission.match_failure_code_value == "FIRST"

    for page in submission.pages:
        page.identified_code_value = None
    db.flush()
    submission_matching_service.match_submission(db, submission)

    assert submission.match_failure_reason is MatchFailureReason.NO_VID_FOUND
    assert submission.match_failure_code_value is None


# ---------------------------------------------------------------------------
# UNRESOLVABLE — an operator's verdict matching never touches
# ---------------------------------------------------------------------------
def test_matching_never_touches_an_unresolvable_submission(db, actor):
    """One published variant would match ANY submission without reading a code,
    so this is the strongest case: still nothing is written — no variant, no
    binding, no status, no event — and the reason it had failed stays."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, batch, [None, None], status=SubmissionStatus.UNRESOLVABLE
    )
    submission.match_failure_reason = MatchFailureReason.NO_VID_FOUND
    db.flush()

    outcome = submission_matching_service._match_submission(
        db, submission, actor_user_id=actor.id
    )

    assert outcome.matched is False
    assert submission.status is SubmissionStatus.UNRESOLVABLE
    assert submission.matched_form_variant_id is None
    assert submission.match_failure_reason is MatchFailureReason.NO_VID_FOUND
    assert all(p.form_variant_page_id is None for p in submission.pages)
    assert all(p.match_confidence is None for p in submission.pages)
    db.flush()
    events = db.scalars(
        select(AuditLog).where(AuditLog.entity_id == str(submission.id))
    ).all()
    assert events == []


def test_the_batch_rerun_does_not_list_an_unresolvable_submission(db, actor):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    unresolvable = _make_submission(
        db, batch, [None], status=SubmissionStatus.UNRESOLVABLE
    )
    pending = _make_submission(db, batch, [None])

    result = run_batch_matching(batch_id=batch.id, db=db, user=actor)

    assert result.submissions_processed == 1
    assert result.matched == 1
    assert pending.status is SubmissionStatus.MATCHED
    db.refresh(unresolvable)
    assert unresolvable.status is SubmissionStatus.UNRESOLVABLE
    assert unresolvable.matched_form_variant_id is None


def test_the_batch_rerun_reads_the_submissions_it_matches_for_update(db, actor):
    """An operator marking a submission unresolvable while a re-run is
    matching it must wait for the run instead of being overwritten by it — so
    the run locks the rows it lists."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    _make_submission(db, batch, [None])
    db.flush()
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        run_batch_matching(batch_id=batch.id, db=db, user=actor)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    listing = [
        s for s in statements
        if s.lstrip().upper().startswith("SELECT")
        and "FROM submissions" in s
        and "submissions.status !=" in s
    ]
    assert len(listing) == 1
    assert listing[0].rstrip().endswith("FOR UPDATE")


# ---------------------------------------------------------------------------
# LEVEL 2 — position declared by the code: 1-based, used VERBATIM
# ---------------------------------------------------------------------------
def test_page_index_from_code_is_used_without_conversion(db, actor):
    """Both sides are 1-BASED — the export encodes FormVariantPage.page_index
    verbatim into the payload — so the decoded index is used as-is."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, page_count=3, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, batch, ["VID:VAR-B|1", "VID:VAR-B|2", "VID:VAR-B|3"]
    )

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant_b.id
    assert _bound_indices(db, submission) == [1, 2, 3]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [1.0, 1.0, 1.0]


def test_declared_index_wins_over_the_physical_position(db, actor):
    """A shuffled pile: the sheets arrived in the wrong order, and the codes say
    so. The DECLARED index decides, so the physical order is overruled — which is
    only visible when the two disagree."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=3)
    batch = _make_batch(db, project, actor)
    # Physical order 0,1,2 but the codes claim pages 3, 1, 2.
    submission = _make_submission(
        db, batch, ["VID:X|3", "VID:X|1", "VID:X|2"]
    )

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant.id
    assert _bound_indices(db, submission) == [3, 1, 2]
    assert all(p.match_confidence == 1.0 for p in submission.pages)


# ---------------------------------------------------------------------------
# LEVEL 2 — fallback: source_page_order + 1  (the 0-based -> 1-based conversion)
# ---------------------------------------------------------------------------
def test_fallback_converts_source_page_order_to_a_1_based_page_index(db, actor):
    """THE conversion test. ``SubmissionPage.source_page_order`` is 0-based (the
    physical position in the file/request) and ``FormVariantPage.page_index`` is
    1-based; they are deliberately different concepts, so the fallback adds 1.

    Written to fail loudly if the +1 is ever dropped: with the raw
    source_page_order the first sheet would look for page_index 0, which no
    variant has, and every binding would shift by one page.
    """
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=3)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None, None])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant.id
    pages = _pages_in_order(submission)
    assert [p.source_page_order for p in pages] == [0, 1, 2]
    # 0 -> 1, 1 -> 2, 2 -> 3. Exactly source_page_order + 1, page by page.
    assert _bound_indices(db, submission) == [1, 2, 3]
    for page in pages:
        bound = db.get(FormVariantPage, page.form_variant_page_id)
        assert bound.page_index == page.source_page_order + 1
    # Deduced, not declared: the sheets never said where they belong.
    assert [p.match_confidence for p in pages] == [0.5, 0.5, 0.5]


def test_fallback_applies_per_page_beside_declared_ones(db, actor):
    """The two ladders coexist inside one submission: the sheet whose code
    carries an index is placed by it (1.0), its unreadable neighbour by the
    physical order (0.5)."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=3)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:X|1", None, "VID:X|3"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant.id
    assert _bound_indices(db, submission) == [1, 2, 3]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [1.0, 0.5, 1.0]


def test_vid_without_index_falls_back_to_the_physical_position(db, actor):
    """A readable VID that carries no page index identifies the VARIANT but not
    the POSITION, so Level 2 still deduces (0.5) — the value alone says nothing
    about where the sheet sits."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(
        db, project, actor, version_number=2, page_count=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-B", "VID:VAR-B"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert _bound_indices(db, submission) == [1, 2]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [0.5, 0.5]


# ---------------------------------------------------------------------------
# LEVEL 2 — an index the variant has no page for
# ---------------------------------------------------------------------------
def test_page_index_out_of_range_leaves_the_binding_null(db, actor):
    """A submission with more physical sheets than the variant has pages: the
    extra sheet is scored 0.0 and left unbound, and — the point of the test —
    the sheets around it are bound normally."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=2)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None, None])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant.id
    # The third sheet would need page_index 3, which the 2-page variant lacks.
    assert _bound_indices(db, submission) == [1, 2, None]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [0.5, 0.5, 0.0]
    # The submission is still MATCHED: an unplaceable sheet is not a failed
    # match.
    assert submission.status is SubmissionStatus.MATCHED


def test_declared_index_out_of_range_leaves_the_binding_null(db, actor):
    """Same outcome from the other direction: the code claims a page the variant
    does not have (a misprint, or a sheet from a longer variant)."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=2)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:X|1", "VID:X|9"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.matched_form_variant_id == variant.id
    assert _bound_indices(db, submission) == [1, None]
    assert [p.match_confidence for p in _pages_in_order(submission)] == [1.0, 0.0]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_running_twice_changes_nothing(db, actor):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(
        db, project, actor, version_number=2, page_count=3, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, ["VID:VAR-B|1", None, "VID:VAR-B|3"])

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)
    first = {
        p.id: (p.form_variant_page_id, p.match_confidence) for p in submission.pages
    }
    first_variant = submission.matched_form_variant_id

    second = submission_matching_service._match_submission(
        db, submission, actor_user_id=actor.id
    )

    assert submission.matched_form_variant_id == first_variant
    assert {
        p.id: (p.form_variant_page_id, p.match_confidence) for p in submission.pages
    } == first
    # Nothing was re-decided: Level 1 was skipped and no page was re-bound.
    assert second.resolved_now is False
    assert second.reason is None
    assert second.pages_resolved == 0


def test_a_second_run_never_overwrites_an_existing_binding(db, actor):
    """An already-bound page is not re-examined even when the engine would now
    decide differently — a manual correction must survive a re-run."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=3)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None, None])
    submission_matching_service.match_submission(db, submission)

    # Someone re-places the first sheet on page 3 and the code now claims page 1.
    first_page = _pages_in_order(submission)[0]
    third_template = next(
        p for p in db.get(FormVariant, variant.id).pages if p.page_index == 3
    )
    first_page.form_variant_page_id = third_template.id
    first_page.identified_code_value = "VID:X|1"
    db.flush()

    submission_matching_service.match_submission(db, submission)

    assert first_page.form_variant_page_id == third_template.id


def test_new_pages_are_bound_without_disturbing_the_old_ones(db, actor):
    """A submission extended by a later upload: only the new sheet is
    examined."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, page_count=3)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None])
    submission_matching_service.match_submission(db, submission)
    assert _bound_indices(db, submission) == [1, 2]

    db.add(
        SubmissionPage(
            submission_id=submission.id,
            source_file_path="/tmp/late.png",
            source_filename="late.png",
            source_page_order=2,
            identified_code_value=None,
        )
    )
    db.flush()
    db.refresh(submission)

    outcome = submission_matching_service._match_submission(db, submission)

    assert _bound_indices(db, submission) == [1, 2, 3]
    assert outcome.pages_resolved == 1  # strictly what THIS run bound


# ---------------------------------------------------------------------------
# UNASSIGNED is a statement about GROUPING, and matching must not speak over it
# ---------------------------------------------------------------------------
def test_unassigned_keeps_its_status_but_still_gets_matched(db, actor):
    """An ungroupable sheet is still awaiting a human decision about WHICH
    submission it belongs to. Matching resolves its variant and its page —
    useful evidence for that decision — but never rewrites the status, which
    would claim the sheet had been sorted out."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=3)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, batch, ["VID:X|2"], status=SubmissionStatus.UNASSIGNED
    )

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.status is SubmissionStatus.UNASSIGNED
    assert submission.matched_form_variant_id == variant.id
    assert _bound_indices(db, submission) == [2]


def test_unassigned_keeps_its_status_when_matching_fails(db, actor):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, batch, [None], status=SubmissionStatus.UNASSIGNED
    )

    submission_matching_service.match_submission(db, submission, actor_user_id=actor.id)

    assert submission.status is SubmissionStatus.UNASSIGNED
    assert submission.matched_form_variant_id is None


# ---------------------------------------------------------------------------
# Automatic trigger — both upload paths match what they ingest
# ---------------------------------------------------------------------------
def _pdf_bytes(page_count: int) -> bytes:
    doc = fitz.open()
    try:
        for i in range(page_count):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {i}")
        return doc.tobytes()
    finally:
        doc.close()


def _stub_decoder(monkeypatch, payloads: list[list[str]]) -> None:
    calls = {"n": 0}

    def fake_decode(image_path):
        index = calls["n"]
        calls["n"] += 1
        return payloads[index] if index < len(payloads) else []

    monkeypatch.setattr(code_rendering, "decode_codes_from_image", fake_decode)


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_upload_pdf_matches_in_the_same_call(db, actor, monkeypatch):
    """Ingestion and matching are one atomic operation: the Submission comes back
    from upload_pdf already matched and bound."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, page_count=3, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["VID:VAR-B|1"], ["VID:VAR-B|2"], ["VID:VAR-B|3"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(3), actor
    )

    assert submission.matched_form_variant_id == variant_b.id
    assert submission.status is SubmissionStatus.MATCHED
    assert _bound_indices(db, submission) == [1, 2, 3]
    assert all(p.match_confidence == 1.0 for p in submission.pages)


def test_upload_pdf_records_match_failed_in_the_same_call(db, actor, monkeypatch):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(db, project, actor, version_number=2, identifier_value="VAR-B")
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(2), actor
    )

    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.matched_form_variant_id is None


def test_upload_pdf_matches_a_single_variant_project_with_no_codes(
    db, actor, monkeypatch
):
    """The end-to-end shape of the commonest deployment: one published variant,
    plain sheets, no codes at all — and every page still lands on its template
    page through the source_page_order + 1 fallback."""
    project = _make_project(db, actor)
    variant = _make_variant(db, project, actor, version_number=1, page_count=2)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(2), actor
    )

    assert submission.matched_form_variant_id == variant.id
    assert _bound_indices(db, submission) == [1, 2]
    assert all(p.match_confidence == 0.5 for p in submission.pages)


def test_upload_images_matches_every_submission_it_touches(db, actor, monkeypatch):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, page_count=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    png = _png_bytes()
    _stub_decoder(
        monkeypatch,
        [["VID:VAR-B|1", "RID:STUD-1"], ["VID:VAR-B|2", "RID:STUD-1"]],
    )

    result = submission_batch_service.upload_images(
        db, batch.id, [("a.png", png), ("b.png", png)], actor
    )

    assert len(result.submissions_created) == 1
    submission = db.get(Submission, result.submissions_created[0])
    assert submission.matched_form_variant_id == variant_b.id
    assert submission.status is SubmissionStatus.MATCHED
    assert _bound_indices(db, submission) == [1, 2]


def test_a_later_image_upload_resolves_a_previously_failed_submission(
    db, actor, monkeypatch
):
    """The reason ``resolver.updated`` is matched too: the first upload could
    not identify the submission, and the page carrying the readable
    VARIANT_IDENTIFIER only arrives with the second call."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, page_count=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    png = _png_bytes()

    # First call: grouped by its RID, but no readable variant identifier.
    _stub_decoder(monkeypatch, [["RID:STUD-9"]])
    first = submission_batch_service.upload_images(
        db, batch.id, [("s.png", png)], actor
    )
    submission = db.get(Submission, first.submissions_created[0])
    assert submission.status is SubmissionStatus.MATCH_FAILED

    # Second call: same submission (same RID), and this sheet's VID is readable.
    _stub_decoder(monkeypatch, [["RID:STUD-9", "VID:VAR-B|2"]])
    second = submission_batch_service.upload_images(
        db, batch.id, [("s2.png", png)], actor
    )

    assert second.submissions_updated == [submission.id]
    db.refresh(submission)
    assert submission.matched_form_variant_id == variant_b.id
    assert submission.status is SubmissionStatus.MATCHED
    assert _bound_indices(db, submission) == [1, 2]


def test_a_later_image_upload_does_not_match_an_unresolvable_submission(
    db, actor, monkeypatch
):
    """The same two calls as above, with the submission marked unresolvable in
    between (through the real upload path): the second sheet is
    added, but nothing is matched — no variant, no binding, no status change,
    and the reason it had failed stays."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    _make_variant(
        db, project, actor, version_number=2, page_count=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)
    png = _png_bytes()

    _stub_decoder(monkeypatch, [["RID:STUD-9"]])
    first = submission_batch_service.upload_images(
        db, batch.id, [("s.png", png)], actor
    )
    submission = db.get(Submission, first.submissions_created[0])
    assert submission.match_failure_reason is MatchFailureReason.NO_VID_FOUND
    submission_batch_service.mark_unresolvable(db, submission.id, actor)

    _stub_decoder(monkeypatch, [["RID:STUD-9", "VID:VAR-B|2"]])
    second = submission_batch_service.upload_images(
        db, batch.id, [("s2.png", png)], actor
    )

    assert second.submissions_updated == [submission.id]
    db.refresh(submission)
    assert len(submission.pages) == 2
    assert submission.status is SubmissionStatus.UNRESOLVABLE
    assert submission.matched_form_variant_id is None
    assert submission.match_failure_reason is MatchFailureReason.NO_VID_FOUND
    assert _bound_indices(db, submission) == [None, None]


# ---------------------------------------------------------------------------
# Manual re-run over a batch
# ---------------------------------------------------------------------------
def test_run_matching_over_a_mixed_batch(db, actor):
    """The realistic re-run: some submissions already resolved, some not, one
    that still cannot be. Only the unresolved ones are examined."""
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, identifier_value="VAR-A")
    variant_b = _make_variant(
        db, project, actor, version_number=2, page_count=2, identifier_value="VAR-B"
    )
    batch = _make_batch(db, project, actor)

    already = _make_submission(db, batch, ["VID:VAR-B|1", "VID:VAR-B|2"])
    submission_matching_service.match_submission(db, already)
    pending = _make_submission(db, batch, ["VID:VAR-B|1", "VID:VAR-B|2"])
    hopeless = _make_submission(db, batch, [None])

    result = run_batch_matching(batch_id=batch.id, db=db, user=actor)

    assert isinstance(result, SubmissionBatchMatchingResult)
    # `already` is fully resolved and is not even examined.
    assert result.submissions_processed == 2
    assert result.matched == 1
    assert result.match_failed == 1
    assert result.pages_resolved == 2
    assert pending.matched_form_variant_id == variant_b.id
    assert _bound_indices(db, pending) == [1, 2]
    assert hopeless.status is SubmissionStatus.MATCH_FAILED


def test_re_running_a_settled_batch_is_a_no_op(db, actor):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1, page_count=2)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None])

    first = run_batch_matching(batch_id=batch.id, db=db, user=actor)
    second = run_batch_matching(batch_id=batch.id, db=db, user=actor)

    assert first.submissions_processed == 1
    assert first.pages_resolved == 2
    # Everything is resolved, so the second run finds nothing to examine.
    assert second.submissions_processed == 0
    assert second.pages_resolved == 0
    assert _bound_indices(db, submission) == [1, 2]


def test_run_matching_picks_up_a_variant_published_after_the_upload(db, actor):
    """Why the manual re-run exists: matching depends on state that changes after
    ingestion. A submission uploaded into a project with no published variant
    fails, and resolves once one is published."""
    project = _make_project(db, actor)
    draft = _make_variant(
        db, project, actor, version_number=1, page_count=2,
        status=FormVariantStatus.DRAFT,
    )
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None, None])
    submission_matching_service.match_submission(db, submission)
    assert submission.status is SubmissionStatus.MATCH_FAILED

    draft.status = FormVariantStatus.PUBLISHED
    db.flush()

    result = run_batch_matching(batch_id=batch.id, db=db, user=actor)

    assert result.matched == 1
    assert submission.matched_form_variant_id == draft.id
    assert submission.status is SubmissionStatus.MATCHED


def test_run_matching_does_not_require_an_active_project(db, actor):
    """Deliberately NOT gated on ACTIVE, unlike the upload routes: re-matching
    stores no new sheet, and a batch found to be mismatched after collection
    closed must still be repairable."""
    project = _make_project(db, actor, status=FormProjectStatus.CLOSED)
    _make_variant(db, project, actor, version_number=1, page_count=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(db, batch, [None])

    result = run_batch_matching(batch_id=batch.id, db=db, user=actor)

    assert result.matched == 1
    assert submission.status is SubmissionStatus.MATCHED


def test_run_matching_is_denied_to_a_non_member(db, actor):
    project = _make_project(db, actor)
    _make_variant(db, project, actor, version_number=1)
    batch = _make_batch(db, project, actor)
    stranger = User(
        email=f"stranger-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(stranger)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        run_batch_matching(batch_id=batch.id, db=db, user=stranger)

    assert exc.value.status_code == 403


def test_run_matching_on_an_unknown_batch_is_404(db, actor):
    with pytest.raises(HTTPException) as exc:
        run_batch_matching(batch_id=uuid.uuid4(), db=db, user=actor)

    assert exc.value.status_code == 404
