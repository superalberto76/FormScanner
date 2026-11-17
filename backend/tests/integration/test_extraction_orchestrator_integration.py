# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the extraction orchestrator.

The piece that walks batch → Submission → SubmissionPage → PageObject and hands
each object to the extractor for its type: the extractors read one object, and
this is what calls them.

Everything runs on REAL synthetic scans generated with Pillow, drawn at the same
geometry the PDF export prints, so the QUESTION objects genuinely extract rather
than being mocked out. What this suite pins down:

  * **the happy path** — every QUESTION on every eligible sheet is read, and the
    counters add up;
  * **the three kinds of "did nothing", kept apart** — not applicable (TEXT,
    IMAGE, a CODE holding a scanning role), not yet supported (an answer type
    with no extractor — none exists, so the tests simulate one by removing an
    entry from the dispatch table), and already extracted;
  * **dispatch by type AND role** — an ordinary MATRIX is read by the MATRIX
    extractor, while a MATRIX carrying RESPONDENT_IDENTIFIER is a
    respondent-ID grid routed to the respondent-identifier extractor instead —
    the two share one dispatch-table entry but never one extractor. A
    role-less FREE_TEXT_AREA is read by the OCR extractor, and so is one
    holding RESPONDENT_IDENTIFIER (a FREE_TEXT reading, always reviewed). A
    role-less CODE is read by the code decoder;
  * **idempotency** — running twice reads nothing twice, and the first run's
    results are left byte-for-byte alone. This is what makes a new extractor
    a re-press rather than a backfill script;
  * **eligibility** — a MATCH_FAILED submission is skipped, and so is an
    UNASSIGNED one EVEN WHEN its variant resolved, which is the case matching
    deliberately creates and the one a naive filter would get wrong; and so
    are REVIEWED / FINALIZED ones (never add readings to a closed
    submission);
  * **robustness** — an object whose extraction raises is counted and stepped
    over, and every other object of the batch is still processed. This is the
    heart of the design, so it has an explicit test;
  * **writing SCANNED** — a MATCHED submission read without a single
    error moves to SCANNED; one error keeps it MATCHED until a clean re-run;
    not-yet-supported objects do not hold it back; nothing but MATCHED ever
    moves, and matching does not undo it;
  * **the endpoint** — response shape, one audit event per run, no ACTIVE gate,
    and membership enforced exactly as run-matching enforces it.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import io
import uuid

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw
from sqlalchemy import select, update

from app.api.v1.endpoints.submission_batches import run_batch_extraction
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    QuestionType,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
    Validity,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import (
    audit_service,
    code_extraction_service,
    extraction_orchestrator_service as svc,
    free_text_extraction_service,
    respondent_identifier_extraction_service,
    submission_matching_service,
)
from app.utils import question_layout


# The template these tests author on: A4 @ 96 DPI, the export's default.
TEMPLATE_W = 794
TEMPLATE_H = 1123

# QUESTION objects are stacked down the page, each sized to hold three options.
Q_X = 60.0
Q_W = 400.0
Q_H = float(question_layout.HEADER_H + 3 * question_layout.OPTION_H)
Q_FIRST_Y = 100.0
Q_SPACING = 120.0


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    target = tmp_path / "submission_pages"
    target.mkdir()
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    return target


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"orch-{uuid.uuid4().hex}@example.com",
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
        name="Orchestrator project",
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


@pytest.fixture
def project(db, actor) -> FormProject:
    return _make_project(db, actor)


@pytest.fixture
def template_page(db, project, actor) -> FormVariantPage:
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(
        form_variant_id=variant.id,
        page_index=1,
        width_px=TEMPLATE_W,
        height_px=TEMPLATE_H,
    )
    db.add(page)
    db.flush()
    return page


def _add_object(
    db, template_page, object_type, *, x=10.0, y=10.0, width=50.0, height=50.0
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=object_type,
        x=x,
        y=y,
        width=width,
        height=height,
    )
    db.add(obj)
    db.flush()
    return obj


def _add_question(db, template_page, *, slot: int, option_count: int = 3) -> PageObject:
    """A QUESTION object in vertical *slot* of the page, with its options.

    Slots keep the bubbles of different questions from overlapping, so one scan
    image can carry several questions and the reading of each is unambiguous.
    """
    obj = _add_object(
        db,
        template_page,
        PageObjectType.QUESTION,
        x=Q_X,
        y=Q_FIRST_Y + slot * Q_SPACING,
        width=Q_W,
        height=Q_H,
    )
    config = QuestionObjectConfig(
        page_object_id=obj.id,
        question_type=QuestionType.SINGLE_CHOICE,
        question_text=f"Question {slot}",
    )
    db.add(config)
    db.flush()
    for index in range(option_count):
        db.add(
            QuestionOption(
                question_object_config_id=config.id,
                order_index=index,
                option_text=f"Option {index}",
            )
        )
    db.flush()
    db.refresh(obj)
    return obj


def _add_matrix(db, template_page, *, role: PageObjectRole | None = None) -> PageObject:
    """A MATRIX object, optionally holding a scanning role.

    Without a role it is an evaluable question, read by
    ``matrix_extraction_service``. With RESPONDENT_IDENTIFIER it is a
    respondent-ID grid — the same object type, and the orchestrator has to tell
    the two apart, which is why both shapes are
    buildable here.
    """
    obj = _add_object(
        db, template_page, PageObjectType.MATRIX, x=500.0, y=100.0,
        width=200.0, height=200.0,
    )
    db.add(
        MatrixObjectConfig(
            page_object_id=obj.id,
            rows_count=2,
            cols_count=3,
            selection_rule=MatrixSelectionRule.ONE_PER_ROW,
        )
    )
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


def _add_free_text(
    db, template_page, *, role: PageObjectRole | None = None
) -> PageObject:
    """A FREE_TEXT_AREA, optionally holding a scanning role.

    Without a role it is a free-text answer, read by
    ``free_text_extraction_service``. With RESPONDENT_IDENTIFIER it spells an
    identity, and it is read by the same extractor.
    """
    obj = _add_object(
        db, template_page, PageObjectType.FREE_TEXT_AREA,
        x=500.0, y=600.0, width=200.0, height=100.0,
    )
    db.add(FreeTextAreaConfig(page_object_id=obj.id))
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


def _stub_ocr(monkeypatch, text: str = "Mario Rossi") -> list:
    """Replace the free-text OCR with a fixed reading, recording each call.

    This suite is about the WALK, not the reading — and stubbed, it runs
    whether or not the Tesseract binary is installed.
    """
    calls: list = []

    def fake(crop_image):
        calls.append(crop_image)
        return text, 0.9

    monkeypatch.setattr(free_text_extraction_service, "_run_ocr", fake)
    return calls


def _add_code(db, template_page, *, role: PageObjectRole | None) -> PageObject:
    """A CODE object, optionally holding a scanning role.

    With a role it is already consumed in full by matching; without one it is
    printed content read by its own extractor. The orchestrator
    has to tell those two apart, which is why both shapes are buildable here.
    """
    obj = _add_object(
        db, template_page, PageObjectType.CODE, x=500.0, y=400.0,
        width=80.0, height=80.0,
    )
    db.add(
        CodeObjectConfig(
            page_object_id=obj.id, code_type=CodeType.QR, value="ANY-VALUE"
        )
    )
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


def _make_batch(db, project, actor) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        # Permissive, so review_required never depends on the exact ink of a
        # synthetic bubble: this suite is about the WALK, not the reading.
        confidence_threshold=0.3,
    )
    db.add(batch)
    db.flush()
    return batch


def _bubble_center(page_object: PageObject, option_index: int) -> tuple[float, float]:
    band_top = (
        page_object.y
        + question_layout.HEADER_H
        + option_index * question_layout.OPTION_H
    )
    return (
        page_object.x + question_layout.CIRCLE_CX,
        band_top + question_layout.OPTION_H / 2,
    )


def _draw_scan(marks: list[tuple[PageObject, int]], *, scale: float = 4.0) -> bytes:
    """A scan of the template page with the given (question, option) bubbles inked."""
    img = Image.new(
        "L", (int(TEMPLATE_W * scale), int(TEMPLATE_H * scale)), 255
    )
    draw = ImageDraw.Draw(img)
    radius = question_layout.CIRCLE_R * scale
    for page_object, option_index in marks:
        cx, cy = _bubble_center(page_object, option_index)
        cx, cy = cx * scale, cy * scale
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_submission(
    db,
    pages_dir,
    batch,
    template_page: FormVariantPage,
    *,
    scan: bytes,
    status: SubmissionStatus = SubmissionStatus.MATCHED,
    matched: bool = True,
    page_bound: bool = True,
) -> Submission:
    """A one-sheet submission whose page image is *scan*.

    *matched* controls ``Submission.matched_form_variant_id`` and *page_bound*
    the page's ``form_variant_page_id`` — the two independent things the
    orchestrator's eligibility rules turn on.
    """
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id if matched else None,
        status=status,
    )
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    path = pages_dir / f"{page_id}.png"
    path.write_bytes(scan)
    db.add(
        SubmissionPage(
            id=page_id,
            submission_id=submission.id,
            form_variant_page_id=template_page.id if page_bound else None,
            source_file_path=str(path),
            source_filename="scan.png",
            source_page_order=0,
        )
    )
    db.flush()
    return submission


def _results(db, submission: Submission) -> list[SubmissionObjectResult]:
    page_ids = [p.id for p in submission.pages]
    return list(
        db.scalars(
            select(SubmissionObjectResult).where(
                SubmissionObjectResult.submission_page_id.in_(page_ids)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_every_question_on_every_eligible_sheet_is_extracted(
    db, pages_dir, project, actor, template_page
):
    first = _add_question(db, template_page, slot=0)
    second = _add_question(db, template_page, slot=1)
    batch = _make_batch(db, project, actor)
    scan = _draw_scan([(first, 0), (second, 2)])
    _make_submission(db, pages_dir, batch, template_page, scan=scan)
    _make_submission(db, pages_dir, batch, template_page, scan=scan)

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.submissions_processed == 2
    assert summary.pages_processed == 2
    assert summary.results_created == 4  # 2 questions x 2 submissions
    assert summary.errors == 0
    assert summary.skipped_already_extracted == 0
    assert summary.skipped_not_applicable == 0
    assert summary.skipped_not_yet_supported == 0


def test_the_readings_are_real_and_land_on_the_right_objects(
    db, pages_dir, project, actor, template_page
):
    """Not a smoke test of the walk: the orchestrator hands each object to the
    extractor, so the option actually inked has to come back out."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 1)])
    )

    svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    results = _results(db, submission)
    assert len(results) == 1
    payload = results[0].detected_payload_json
    assert results[0].page_object_id == question.id
    assert payload["validity"] == Validity.VALID.value
    options = sorted(question.question_config.options, key=lambda o: o.order_index)
    assert [o["option_id"] for o in payload["selected_options"]] == [str(options[1].id)]


def test_the_orchestrator_does_not_commit(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """The caller owns the transaction, like every other engine in this pipeline
    (submission_matching_service). The run flushes before setting candidates
    from the respondent ID, so the rows reach the transaction — but it never
    commits it."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )
    commits = []
    monkeypatch.setattr(db, "commit", lambda: commits.append(True))

    svc.run_extraction_for_batch(db, batch.id)

    assert commits == []
    assert db.scalars(
        select(SubmissionObjectResult).where(
            SubmissionObjectResult.page_object_id == question.id
        )
    ).all()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_an_object_already_extracted_is_skipped_not_reprocessed(
    db, pages_dir, project, actor, template_page
):
    """The whole point of the design. Note what is NOT asserted anywhere else:
    no IntegrityError. The uniqueness of (submission_page_id, page_object_id)
    would refuse a second reading at flush, so if the skip did not work this
    test would blow up rather than quietly double-count."""
    first = _add_question(db, template_page, slot=0)
    second = _add_question(db, template_page, slot=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(first, 0), (second, 1)]),
    )

    svc.run_extraction_for_batch(db, batch.id)
    db.flush()
    before = {r.page_object_id: (r.id, r.detected_payload_json) for r in _results(db, submission)}

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.skipped_already_extracted == 2
    assert summary.results_created == 0
    assert summary.errors == 0
    after = {r.page_object_id: (r.id, r.detected_payload_json) for r in _results(db, submission)}
    # Same rows, same readings — not re-read, not overwritten.
    assert after == before


def test_a_new_object_is_picked_up_while_the_old_ones_are_left_alone(
    db, pages_dir, project, actor, template_page
):
    """The forward-looking half of idempotency: whatever gains an extractor
    later is picked up by a re-press, and everything already read — human
    review included — is untouched.
    Simulated here by adding an object between two runs, which is the same shape
    of change as adding an extractor for one."""
    first = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(first, 0)])
    )
    svc.run_extraction_for_batch(db, batch.id)
    db.flush()
    original_id = _results(db, submission)[0].id

    second = _add_question(db, template_page, slot=1)
    db.refresh(template_page)

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.results_created == 1
    assert summary.skipped_already_extracted == 1
    by_object = {r.page_object_id: r.id for r in _results(db, submission)}
    assert by_object[first.id] == original_id  # untouched
    assert second.id in by_object              # newly read


# ---------------------------------------------------------------------------
# The three kinds of "did nothing", kept apart
# ---------------------------------------------------------------------------
def test_a_type_with_no_extractor_yet_is_counted_not_yet_supported(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """An answer type with no extractor in this build is counted, never raised,
    never a stop — and the QUESTION beside it still extracts. Every answer type
    has an extractor, so the case is simulated by taking FREE_TEXT_AREA out of
    the dispatch table."""
    monkeypatch.delitem(svc._EXTRACTORS, PageObjectType.FREE_TEXT_AREA)
    question = _add_question(db, template_page, slot=0)
    _add_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.skipped_not_yet_supported == 1
    assert summary.skipped_not_applicable == 0
    assert summary.results_created == 1
    assert summary.errors == 0


def test_a_role_less_matrix_is_read_by_the_matrix_extractor(
    db, pages_dir, project, actor, template_page
):
    """The other half of the dispatch by role, and the reason it is a routing
    and not a veto: an ordinary MATRIX IS extracted. The result's type and
    its per-row payload are what prove ``extract_matrix_result`` actually ran —
    the QUESTION extractor would have raised on an object with no
    question_config."""
    matrix = _add_matrix(db, template_page)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([])
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.results_created == 1
    assert summary.skipped_not_yet_supported == 0
    assert summary.errors == 0
    results = _results(db, submission)
    assert [r.page_object_id for r in results] == [matrix.id]
    assert results[0].result_type is SubmissionResultType.MATRIX
    # Two rows authored, both blank on this scan: read, not guessed.
    assert [r["validity"] for r in results[0].detected_payload_json["rows"]] == [
        Validity.NO_ANSWER.value
    ] * 2


def test_a_respondent_identifier_matrix_is_extracted(
    db, pages_dir, project, actor, template_page
):
    """The heart of the dispatch by role. A MATRIX carrying
    RESPONDENT_IDENTIFIER is a respondent-ID grid: its cells spell a candidate's
    id, they have no Validity, and handing them to the evaluable-MATRIX
    extractor would store a reading of a question nobody asked.

    MATRIX is in the dispatch table, so a plain type lookup WOULD route it to
    ``matrix_extraction_service``. ``_resolve_extractor`` is what reroutes it
    instead — to ``respondent_identifier_extraction_service`` — so a batch
    holding one produces a real result, not a pending count: the object counts
    as ``results_created``, not as ``skipped_not_yet_supported``."""
    identifier = _add_matrix(
        db, template_page, role=PageObjectRole.RESPONDENT_IDENTIFIER
    )
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.skipped_not_yet_supported == 0
    assert summary.skipped_not_applicable == 0
    assert summary.errors == 0
    # Both the QUESTION and the respondent-ID grid were read.
    assert summary.results_created == 2
    results_by_object = {r.page_object_id: r for r in _results(db, submission)}
    assert set(results_by_object) == {question.id, identifier.id}
    assert (
        results_by_object[identifier.id].result_type
        is SubmissionResultType.RESPONDENT_IDENTIFIER
    )


def test_the_role_routes_to_a_different_extractor_not_a_veto(
    db, pages_dir, project, actor, template_page
):
    """Stated directly on the resolver, because the two objects differ by
    nothing else: same type, same config, same geometry — only the role
    decides which extractor reads them, and neither ever gets ``None``."""
    plain = _add_matrix(db, template_page)
    identified = _add_matrix(
        db, template_page, role=PageObjectRole.RESPONDENT_IDENTIFIER
    )

    assert svc._resolve_extractor(plain) is svc._EXTRACTORS[PageObjectType.MATRIX]
    assert (
        svc._resolve_extractor(identified)
        is respondent_identifier_extraction_service.extract_respondent_identifier_result
    )


def test_a_role_less_free_text_area_is_read_by_the_ocr_extractor(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """FREE_TEXT_AREA's line in the dispatch table: an ordinary FREE_TEXT_AREA
    is extracted. The OCR stub being called, and the FREE_TEXT result carrying
    its reading, are what prove ``extract_free_text_result`` actually ran."""
    ocr_calls = _stub_ocr(monkeypatch)
    free_text = _add_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([])
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.results_created == 1
    assert summary.skipped_not_yet_supported == 0
    assert summary.errors == 0
    assert len(ocr_calls) == 1
    results = _results(db, submission)
    assert [r.page_object_id for r in results] == [free_text.id]
    assert results[0].result_type is SubmissionResultType.FREE_TEXT
    assert results[0].detected_payload_json == {"suggested_text": "Mario Rossi"}
    assert results[0].review_required is True


def test_a_respondent_identifier_free_text_area_is_read_as_free_text(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """No veto on the role: a FREE_TEXT_AREA holding
    RESPONDENT_IDENTIFIER is read by the ordinary OCR extractor — a FREE_TEXT
    reading, always in review (ALWAYS_REVIEW), where confirming or correcting
    it sets the candidate. ``is_extractable`` and ``result_type_for`` follow
    from the same routing."""
    ocr_calls = _stub_ocr(monkeypatch, text="123456")
    identifier = _add_free_text(
        db, template_page, role=PageObjectRole.RESPONDENT_IDENTIFIER
    )
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.skipped_not_yet_supported == 0
    assert summary.errors == 0
    assert summary.results_created == 2
    assert len(ocr_calls) == 1
    by_object = {r.page_object_id: r for r in _results(db, submission)}
    reading = by_object[identifier.id]
    assert reading.result_type is SubmissionResultType.FREE_TEXT
    assert reading.detected_payload_json == {"suggested_text": "123456"}
    assert reading.validity_forces_review is True
    assert reading.review_required is True
    assert svc._resolve_extractor(identifier) is free_text_extraction_service.extract_free_text_result
    assert svc.is_extractable(identifier) is True
    assert svc.result_type_for(identifier) is SubmissionResultType.FREE_TEXT
    assert submission.status is SubmissionStatus.SCANNED


def test_a_code_holding_a_scanning_role_is_not_applicable(
    db, pages_dir, project, actor, template_page
):
    """Matching consumed it in full at ingestion; re-reading it here
    would store a second, divergeable copy of an already-answered question."""
    _add_code(db, template_page, role=PageObjectRole.VARIANT_IDENTIFIER)
    batch = _make_batch(db, project, actor)
    _make_submission(db, pages_dir, batch, template_page, scan=_draw_scan([]))

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.skipped_not_applicable == 1
    assert summary.skipped_not_yet_supported == 0
    assert summary.results_created == 0


def test_a_role_less_code_is_read_by_the_code_extractor(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """The distinction the previous test exists to contrast with, and the
    role-less CODE's line in the dispatch table. A CODE with no role is printed CONTENT,
    not identification: it is handed to ``extract_code_result`` rather than
    written off as never evaluable. A spy wrapping the real extractor proves it
    ran; the CODE result proves what it stored.

    The scan carries no QR, so the reading is a failed decode sent to review —
    this suite is about the WALK; the decoding itself is covered by
    test_code_extraction_integration.py."""
    assert (
        svc._EXTRACTORS[PageObjectType.CODE]
        is code_extraction_service.extract_code_result
    )
    calls: list = []

    def spy(db_, page, page_object, batch_):
        calls.append(page_object.id)
        return code_extraction_service.extract_code_result(
            db_, page, page_object, batch_
        )

    monkeypatch.setitem(svc._EXTRACTORS, PageObjectType.CODE, spy)
    code = _add_code(db, template_page, role=None)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([])
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.results_created == 1
    assert summary.skipped_not_yet_supported == 0
    assert summary.skipped_not_applicable == 0
    assert summary.errors == 0
    assert calls == [code.id]
    results = _results(db, submission)
    assert [r.page_object_id for r in results] == [code.id]
    assert results[0].result_type is SubmissionResultType.CODE
    assert results[0].detected_payload_json == {
        "decoded_value": None,
        "decode_successful": False,
    }
    assert results[0].confidence_score is None
    assert results[0].review_required is True


def test_text_and_image_objects_are_never_evaluable(
    db, pages_dir, project, actor, template_page
):
    _add_object(db, template_page, PageObjectType.TEXT)
    _add_object(db, template_page, PageObjectType.IMAGE, x=200.0)
    batch = _make_batch(db, project, actor)
    _make_submission(db, pages_dir, batch, template_page, scan=_draw_scan([]))

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.skipped_not_applicable == 2
    assert summary.skipped_not_yet_supported == 0
    assert summary.errors == 0


# ---------------------------------------------------------------------------
# Which submissions are eligible at all
# ---------------------------------------------------------------------------
def test_an_unassigned_submission_is_skipped_even_with_its_variant_resolved(
    db, pages_dir, project, actor, template_page
):
    """The case matching deliberately creates and a naive filter gets wrong.
    Matching resolves the variant of an ungroupable sheet WITHOUT clearing
    UNASSIGNED, because the open question there is 'whose submission is this?'.
    Reading answers off it would attribute them to a candidate nobody has yet
    decided it belongs to."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(question, 0)]),
        status=SubmissionStatus.UNASSIGNED,   # …but matched_form_variant_id IS set
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.submissions_processed == 0
    assert summary.pages_processed == 0
    assert summary.results_created == 0


def test_a_match_failed_submission_is_skipped(
    db, pages_dir, project, actor, template_page
):
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(question, 0)]),
        status=SubmissionStatus.MATCH_FAILED,
        matched=False,
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.submissions_processed == 0
    assert summary.results_created == 0


def test_an_eligible_submission_is_processed_beside_ineligible_ones(
    db, pages_dir, project, actor, template_page
):
    """The counters describe the run, so an ineligible submission must not even
    be counted as examined — while never getting in the way of its siblings."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    scan = _draw_scan([(question, 0)])
    _make_submission(db, pages_dir, batch, template_page, scan=scan,
                     status=SubmissionStatus.UNASSIGNED)
    _make_submission(db, pages_dir, batch, template_page, scan=scan,
                     status=SubmissionStatus.MATCH_FAILED, matched=False)
    good = _make_submission(db, pages_dir, batch, template_page, scan=scan)

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.submissions_processed == 1
    assert summary.results_created == 1
    assert len(_results(db, good)) == 1


def test_a_sheet_matching_could_not_place_is_stepped_over(
    db, pages_dir, project, actor, template_page
):
    """No template page bound means no geometry to read the sheet against. Not
    counted as processed, and no error."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(question, 0)]), page_bound=False,
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.submissions_processed == 1  # the submission IS eligible…
    assert summary.pages_processed == 0        # …but its only sheet is unplaced
    assert summary.results_created == 0
    assert summary.errors == 0


# ---------------------------------------------------------------------------
# Robustness: one failure never stops the run
# ---------------------------------------------------------------------------
def test_one_failing_object_does_not_stop_the_others(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """The heart of the design. A batch of hundreds of submissions must not be
    lost to one unreadable sheet.

    The dispatch ENTRY is patched, not ``question_extraction_service
    .extract_question_result``: the table captured the function object at import
    time, so patching the module attribute would leave the dict pointing at the
    original.
    """
    doomed = _add_question(db, template_page, slot=0)
    survivor = _add_question(db, template_page, slot=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(doomed, 0), (survivor, 1)]),
    )

    real = svc._EXTRACTORS[PageObjectType.QUESTION]

    def flaky(db_, page, page_object, batch_):
        if page_object.id == doomed.id:
            raise RuntimeError("scanner ate the sheet")
        return real(db_, page, page_object, batch_)

    monkeypatch.setitem(svc._EXTRACTORS, PageObjectType.QUESTION, flaky)

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.errors == 1
    assert summary.results_created == 1
    assert summary.error_details == [
        {
            "submission_page_id": str(submission.pages[0].id),
            "page_object_id": str(doomed.id),
            "error": "RuntimeError: scanner ate the sheet",
        }
    ]
    # The other object of the SAME sheet was still read.
    assert [r.page_object_id for r in _results(db, submission)] == [survivor.id]


def test_a_failure_on_one_submission_does_not_stop_the_next(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """Same contract one level up: the walk continues across submissions, not just
    across the objects of one sheet."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    scan = _draw_scan([(question, 0)])
    doomed = _make_submission(db, pages_dir, batch, template_page, scan=scan)
    later = _make_submission(db, pages_dir, batch, template_page, scan=scan)

    real = svc._EXTRACTORS[PageObjectType.QUESTION]
    doomed_page_id = doomed.pages[0].id

    def flaky(db_, page, page_object, batch_):
        if page.id == doomed_page_id:
            raise OSError("page image is gone")
        return real(db_, page, page_object, batch_)

    monkeypatch.setitem(svc._EXTRACTORS, PageObjectType.QUESTION, flaky)

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.errors == 1
    assert summary.results_created == 1
    assert _results(db, doomed) == []
    assert len(_results(db, later)) == 1


def test_a_missing_page_image_is_an_error_not_a_crash(
    db, pages_dir, project, actor, template_page
):
    """The realistic failure, unmocked: the row points at a file that is not
    there. The extractor raises OSError deep inside storage_service and the run
    absorbs it."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )
    page = submission.pages[0]
    (pages_dir / f"{page.id}.png").unlink()

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.errors == 1
    assert summary.results_created == 0
    assert summary.error_details[0]["page_object_id"] == str(question.id)
    assert "FileNotFoundError" in summary.error_details[0]["error"]


def test_the_named_failures_are_capped_but_the_count_is_not(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """A run over a broken batch could produce thousands of failures, and
    neither an HTTP response nor an audit row is the place for all of them.
    ``errors`` still reports the true total."""
    monkeypatch.setattr(svc, "MAX_ERROR_DETAILS", 2)
    questions = [_add_question(db, template_page, slot=i) for i in range(4)]
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(q, 0) for q in questions]),
    )

    def always_fails(db_, page, page_object, batch_):
        raise ValueError("nope")

    monkeypatch.setitem(svc._EXTRACTORS, PageObjectType.QUESTION, always_fails)

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.errors == 4
    assert len(summary.error_details) == 2


# ---------------------------------------------------------------------------
# Writing SCANNED
# ---------------------------------------------------------------------------
def test_a_fully_extracted_matched_submission_becomes_scanned(
    db, pages_dir, project, actor, template_page
):
    """"Extracted, ready for review": every object read, none raised."""
    first = _add_question(db, template_page, slot=0)
    second = _add_question(db, template_page, slot=1)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(first, 0), (second, 1)]),
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.errors == 0
    assert submission.status is SubmissionStatus.SCANNED
    assert summary.submissions_scanned == 1


def test_an_error_keeps_the_submission_matched_until_a_clean_rerun(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """One error anywhere keeps THAT submission MATCHED — its siblings still
    move — and the next run, retrying only the failed object, moves it."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    scan = _draw_scan([(question, 0)])
    doomed = _make_submission(db, pages_dir, batch, template_page, scan=scan)
    sibling = _make_submission(db, pages_dir, batch, template_page, scan=scan)

    real = svc._EXTRACTORS[PageObjectType.QUESTION]
    doomed_page_id = doomed.pages[0].id

    def flaky(db_, page, page_object, batch_):
        if page.id == doomed_page_id:
            raise OSError("page image is gone")
        return real(db_, page, page_object, batch_)

    monkeypatch.setitem(svc._EXTRACTORS, PageObjectType.QUESTION, flaky)
    first_run = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert first_run.errors == 1
    assert doomed.status is SubmissionStatus.MATCHED
    assert sibling.status is SubmissionStatus.SCANNED
    assert first_run.submissions_scanned == 1

    monkeypatch.setitem(svc._EXTRACTORS, PageObjectType.QUESTION, real)
    second_run = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert second_run.errors == 0
    assert second_run.results_created == 1  # only the object that failed
    assert doomed.status is SubmissionStatus.SCANNED
    assert second_run.submissions_scanned == 1


def test_a_not_yet_supported_object_does_not_hold_the_submission_back(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """An object with no extractor (simulated, as above: every type has one)
    is not a failure, so a submission holding nothing else is still
    SCANNED."""
    monkeypatch.delitem(svc._EXTRACTORS, PageObjectType.FREE_TEXT_AREA)
    _add_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([])
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.skipped_not_yet_supported == 1
    assert summary.results_created == 0
    assert submission.status is SubmissionStatus.SCANNED
    assert summary.submissions_scanned == 1


def test_a_matched_submission_with_no_bound_page_becomes_scanned(
    db, pages_dir, project, actor, template_page
):
    """Vacuously: nothing was placed, so nothing was left unread. Its missing
    pages are the batch detail's business, not a reason to stay MATCHED."""
    _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([]), page_bound=False
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.pages_processed == 0
    assert submission.status is SubmissionStatus.SCANNED
    assert summary.submissions_scanned == 1


def test_a_scanned_submission_is_read_but_its_status_left_alone(
    db, pages_dir, project, actor, template_page
):
    """Only MATCHED moves: a run must not recount a submission already in the
    queue. It is still eligible — a SCANNED submission may lack a reading."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(question, 0)]), status=SubmissionStatus.SCANNED,
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert summary.submissions_processed == 1  # still eligible, still read…
    assert summary.results_created == 1
    assert submission.status is SubmissionStatus.SCANNED  # …status untouched
    assert summary.submissions_scanned == 0


@pytest.mark.parametrize(
    "status", [SubmissionStatus.REVIEWED, SubmissionStatus.FINALIZED]
)
def test_a_closed_submission_is_skipped_entirely(
    db, pages_dir, project, actor, template_page, status
):
    """A re-run never adds readings to a submission a human has closed — not
    examined, not counted, nothing read, its attempt time untouched. Reopening
    brings it back to SCANNED first."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(question, 0)]), status=status,
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.submissions_processed == 0
    assert summary.results_created == 0
    assert _results(db, submission) == []
    assert submission.status is status
    assert submission.extraction_attempted_at is None


# ---------------------------------------------------------------------------
# A status written while the run is going
# ---------------------------------------------------------------------------
def _set_status_behind_the_session(db, submission, status):
    # A commit from another transaction, as the run's session sees it: in the
    # database, not in the copy it loaded at the start.
    db.execute(
        update(Submission)
        .where(Submission.id == submission.id)
        .values(status=status)
        .execution_options(synchronize_session=False)
    )


def _question_extractor_then(monkeypatch, on_page_id, side_effect):
    """Replace the QUESTION extractor with the real one followed, on the sheet
    *on_page_id*, by *side_effect* — what an operator does while the run is
    reading that sheet."""
    real = svc._EXTRACTORS[PageObjectType.QUESTION]

    def extract_then(db_, page, page_object, batch_):
        result = real(db_, page, page_object, batch_)
        if page.id == on_page_id:
            side_effect()
        return result

    monkeypatch.setitem(svc._EXTRACTORS, PageObjectType.QUESTION, extract_then)


@pytest.mark.parametrize(
    "status", [SubmissionStatus.REVIEWED, SubmissionStatus.FINALIZED]
)
def test_a_submission_closed_while_it_is_read_is_not_put_back_to_scanned(
    db, pages_dir, project, actor, template_page, monkeypatch, status
):
    """The run decides MATCHED → SCANNED on the submission as it is in the
    database when it writes, not on the copy loaded at the start."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )
    _question_extractor_then(
        monkeypatch,
        submission.pages[0].id,
        lambda: _set_status_behind_the_session(db, submission, status),
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()
    db.expire_all()

    assert db.get(Submission, submission.id).status is status
    assert summary.submissions_scanned == 0


def test_a_submission_closed_before_the_run_reaches_it_is_skipped_entirely(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """Closed while the run reads the submission before it: as if it had been
    closed when the run started — nothing read, not counted, attempt time
    untouched."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    scan = _draw_scan([(question, 0)])
    submissions = [
        _make_submission(
            db, pages_dir, batch, template_page, scan=scan, status=SubmissionStatus.SCANNED
        )
        for _ in range(2)
    ]
    # Created in one transaction, so the walk's order between them is the id's:
    # whichever is read first, the operator closes the other one meanwhile.
    real = svc._EXTRACTORS[PageObjectType.QUESTION]
    closed: list[Submission] = []

    def extract_then_close_the_other(db_, page, page_object, batch_):
        result = real(db_, page, page_object, batch_)
        if not closed:
            other = next(s for s in submissions if s.id != page.submission_id)
            _set_status_behind_the_session(db, other, SubmissionStatus.REVIEWED)
            closed.append(other)
        return result

    monkeypatch.setitem(
        svc._EXTRACTORS, PageObjectType.QUESTION, extract_then_close_the_other
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()
    db.expire_all()

    assert summary.submissions_processed == 1
    assert summary.results_created == 1
    assert _results(db, closed[0]) == []
    reloaded = db.get(Submission, closed[0].id)
    assert reloaded.status is SubmissionStatus.REVIEWED
    assert reloaded.extraction_attempted_at is None


def test_a_review_anyway_during_the_run_is_not_counted_as_scanned_by_it(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """"Review anyway" moved the submission to SCANNED while the run was reading
    it: SCANNED it stays, but the run did not move it, so it does not count."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )
    _question_extractor_then(
        monkeypatch,
        submission.pages[0].id,
        lambda: _set_status_behind_the_session(db, submission, SubmissionStatus.SCANNED),
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()
    db.expire_all()

    assert db.get(Submission, submission.id).status is SubmissionStatus.SCANNED
    assert summary.submissions_scanned == 0


def test_a_rerun_over_a_settled_batch_scans_nothing(
    db, pages_dir, project, actor, template_page
):
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    scan = _draw_scan([(question, 0)])
    _make_submission(db, pages_dir, batch, template_page, scan=scan)
    _make_submission(db, pages_dir, batch, template_page, scan=scan)

    first_run = svc.run_extraction_for_batch(db, batch.id)
    db.flush()
    second_run = svc.run_extraction_for_batch(db, batch.id)

    assert first_run.submissions_scanned == 2
    assert second_run.submissions_scanned == 0


def test_an_unassigned_submission_with_a_resolved_variant_stays_unassigned(
    db, pages_dir, project, actor, template_page
):
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(question, 0)]),
        status=SubmissionStatus.UNASSIGNED,   # …but matched_form_variant_id IS set
    )

    summary = svc.run_extraction_for_batch(db, batch.id)

    assert submission.status is SubmissionStatus.UNASSIGNED
    assert summary.submissions_scanned == 0


def test_rerunning_matching_leaves_a_scanned_submission_scanned(
    db, pages_dir, project, actor, template_page
):
    """Matching writes a status only when it RESOLVES a variant. The submission
    here has one already and an unplaced sheet, so ``match_batch`` does pick it
    up (to bind the sheet) — and must still not speak over SCANNED."""
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([]),
        status=SubmissionStatus.SCANNED,
        page_bound=False,
    )

    result = submission_matching_service.match_batch(db, batch.id)

    assert result["submissions_processed"] == 1
    assert result["pages_resolved"] == 1
    assert submission.status is SubmissionStatus.SCANNED


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
def _run(db, batch, user):
    return run_batch_extraction(batch_id=batch.id, db=db, user=user)


def test_the_endpoint_returns_the_run_summary(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    # An object with no extractor, simulated (every type has one).
    monkeypatch.delitem(svc._EXTRACTORS, PageObjectType.FREE_TEXT_AREA)
    question = _add_question(db, template_page, slot=0)
    _add_free_text(db, template_page)
    _add_object(db, template_page, PageObjectType.TEXT)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    result = _run(db, batch, actor)

    assert result.submissions_processed == 1
    assert result.pages_processed == 1
    assert result.results_created == 1
    assert result.skipped_not_yet_supported == 1
    assert result.skipped_not_applicable == 1
    assert result.errors == 0
    assert result.error_details == []
    assert result.submissions_scanned == 1


def test_the_endpoint_commits_the_results(
    db, pages_dir, project, actor, template_page
):
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    _run(db, batch, actor)

    # Committed by the wrapper, so visible without a flush of our own.
    assert len(_results(db, submission)) == 1


def test_running_the_endpoint_twice_repeats_nothing(
    db, pages_dir, project, actor, template_page
):
    """Idempotency end to end, through the transaction boundary the wrapper
    owns — the property an operator relies on when they are not sure whether
    the first press went through."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    submission = _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    first = _run(db, batch, actor)
    second = _run(db, batch, actor)

    assert first.results_created == 1
    assert second.results_created == 0
    assert second.skipped_already_extracted == 1
    assert len(_results(db, submission)) == 1


@pytest.mark.parametrize(
    "status", [FormProjectStatus.CLOSED, FormProjectStatus.ARCHIVED]
)
def test_no_active_gate_the_project_need_not_still_be_open(
    db, pages_dir, actor, status
):
    """Same reasoning as run-matching: that gate stops new sheets being
    COLLECTED, and extraction collects nothing. A batch whose project has since
    closed is precisely the one whose answers still need reading."""
    project = _make_project(db, actor, status=status)
    variant = FormVariant(
        form_project_id=project.id, root_key="root", name="v1",
        status=FormVariantStatus.PUBLISHED, version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    template_page = FormVariantPage(
        form_variant_id=variant.id, page_index=1,
        width_px=TEMPLATE_W, height_px=TEMPLATE_H,
    )
    db.add(template_page)
    db.flush()
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    result = _run(db, batch, actor)

    assert result.results_created == 1


def test_a_non_member_may_not_run_extraction(
    db, pages_dir, project, actor, template_page
):
    _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    outsider = User(
        email=f"outsider-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(outsider)
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        _run(db, batch, outsider)
    assert excinfo.value.status_code == 403


def test_an_unknown_batch_is_a_404(db, actor):
    with pytest.raises(HTTPException) as excinfo:
        run_batch_extraction(batch_id=uuid.uuid4(), db=db, user=actor)
    assert excinfo.value.status_code == 404


def test_one_audit_event_per_run_carrying_the_summary(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """One event for the CALL, never one per result: a batch produces thousands
    of readings and a per-object trail would bury every other event in the
    project's history."""
    first = _add_question(db, template_page, slot=0)
    second = _add_question(db, template_page, slot=1)
    monkeypatch.delitem(svc._EXTRACTORS, PageObjectType.FREE_TEXT_AREA)
    _add_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(first, 0), (second, 0)]),
    )

    _run(db, batch, actor)

    events = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_BATCH_EXTRACTION_RUN,
                AuditLog.entity_id == str(batch.id),
            )
        ).all()
    )
    assert len(events) == 1
    metadata = events[0].metadata_json
    assert metadata["results_created"] == 2
    assert metadata["skipped_not_yet_supported"] == 1
    assert metadata["errors"] == 0
    assert metadata["submissions_processed"] == 1
    assert metadata["submissions_scanned"] == 1


def test_a_second_run_logs_its_own_event_even_though_it_changed_nothing(
    db, pages_dir, project, actor, template_page
):
    """A manual run is an operator ACTION, worth recording even when it turns
    out to be a no-op — same rule run-matching already follows."""
    question = _add_question(db, template_page, slot=0)
    batch = _make_batch(db, project, actor)
    _make_submission(
        db, pages_dir, batch, template_page, scan=_draw_scan([(question, 0)])
    )

    _run(db, batch, actor)
    _run(db, batch, actor)

    events = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_BATCH_EXTRACTION_RUN,
                AuditLog.entity_id == str(batch.id),
            )
        ).all()
    )
    assert len(events) == 2
    assert [e.metadata_json["results_created"] for e in events] == [1, 0]
