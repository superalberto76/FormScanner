# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for QUESTION extraction.

The full chain, on REAL synthetic scans generated with Pillow (the style of the
ingestion and matching suites): an authored QUESTION is laid out with the very
geometry the PDF export uses, a scan of that page is drawn with some bubbles
blackened, and ``extract_question_result`` has to read back exactly which ones.

What this suite pins down:

  * **detection** — a blackened bubble is found, an empty one is not, and the
    option reported is the right one (``order_index`` order, no off-by-one);
  * **validity** — nothing blackened is NO_ANSWER; one bubble is VALID; two
    bubbles are TOO_MANY_ANSWERS on a SINGLE_CHOICE question and VALID on a
    MULTIPLE_CHOICE one, which is the whole difference between the two types;
  * **confidence** — the fill ratio IS the per-option confidence, and the
    result's aggregated ``confidence_score`` is the MINIMUM among the selected
    options; NULL — not 0.0 — when nothing was selected;
  * **review_required** — OR-ed across the two independent axes, confidence
    and validity: raised by a confidence below the batch's effective threshold,
    and raised by a non-VALID reading however confident it was;
  * **scale independence** — the same sheet scanned at a different resolution
    reads the same, because the mapping uses the observed dimensional ratio and
    nothing about the file's DPI history;
  * **one result per (page, object)** — the DB constraint refuses a second
    extraction of the same object off the same sheet, rather than silently
    appending a second reading of one fact;
  * **preconditions** — an unmatched page raises rather than guessing, and the
    function neither flushes nor commits.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image, ImageDraw
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
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
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import question_extraction_service as svc
from app.utils import bubble_sampling, question_layout


# The template page these tests author on: A4 @ 96 DPI, the export's default.
TEMPLATE_W = 794
TEMPLATE_H = 1123

# The QUESTION object's geometry, sized to hold exactly three option bands.
OBJ_X = 60.0
OBJ_Y = 120.0
OBJ_W = 400.0
OBJ_H = float(question_layout.HEADER_H + 3 * question_layout.OPTION_H)

WHITE = 255
BLACK = 0


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    """Redirect submission-page storage into the test's tmp_path.

    ``read_submission_page_image`` validates every path against this directory,
    so a scan written anywhere else would be refused — which is the point of
    that validation, and the reason it has to be pointed here rather than
    bypassed.
    """
    target = tmp_path / "submission_pages"
    target.mkdir()
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    return target


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"extract-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    p = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Extraction project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(p)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=p.id, user_id=actor.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return p


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


def _make_question(
    db,
    template_page: FormVariantPage,
    *,
    question_type: QuestionType = QuestionType.SINGLE_CHOICE,
    option_count: int = 3,
    height: float = OBJ_H,
) -> PageObject:
    """One authored QUESTION object with *option_count* options."""
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.QUESTION,
        x=OBJ_X,
        y=OBJ_Y,
        width=OBJ_W,
        height=height,
    )
    db.add(obj)
    db.flush()
    config = QuestionObjectConfig(
        page_object_id=obj.id,
        question_type=question_type,
        question_text="Which one?",
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


def _make_batch(db, project, actor, *, threshold: float | None = None):
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        confidence_threshold=threshold,
    )
    db.add(batch)
    db.flush()
    return batch


def _bubble_center(option_index: int) -> tuple[float, float]:
    """Where the export prints option *option_index*'s bubble, template space.

    Recomputed here from the shared constants rather than imported from the
    extractor: a test that reused the extractor's own placement could not catch
    the extractor placing them wrongly.
    """
    band_top = OBJ_Y + question_layout.HEADER_H + option_index * question_layout.OPTION_H
    return (
        OBJ_X + question_layout.CIRCLE_CX,
        band_top + question_layout.OPTION_H / 2,
    )


def _draw_scan(
    *,
    filled: dict[int, float] | None = None,
    outlined: tuple[int, ...] = (),
    scale: float = 4.0,
) -> bytes:
    """A synthetic scan of the template page, as PNG bytes.

    *filled* maps an option index to the fraction of the printed radius to
    blacken — 1.0 is a bubble filled to the edge, a smaller value one filled
    less thoroughly, which is how these tests produce DIFFERENT confidences for
    different options. *outlined* draws the empty bubble the export prints for
    an unanswered option, so "not selected" is tested against a real outline
    and not against blank paper.

    *scale* is the resolution the sheet was scanned at relative to the
    template. Deliberately not 1.0 by default: a 4x scan is both realistic
    (150 DPI vs the template's 96) and enough pixels per bubble that the
    ratios are not dominated by rasterisation noise.
    """
    filled = filled or {}
    img = Image.new("L", (int(TEMPLATE_W * scale), int(TEMPLATE_H * scale)), WHITE)
    draw = ImageDraw.Draw(img)
    printed_radius = question_layout.CIRCLE_R * scale

    for index in outlined:
        cx, cy = _bubble_center(index)
        cx, cy = cx * scale, cy * scale
        draw.ellipse(
            [cx - printed_radius, cy - printed_radius,
             cx + printed_radius, cy + printed_radius],
            outline=BLACK,
            width=max(int(scale / 2), 1),
        )
    for index, fraction in filled.items():
        cx, cy = _bubble_center(index)
        cx, cy = cx * scale, cy * scale
        r = printed_radius * fraction
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_scanned_page(
    db,
    pages_dir,
    batch,
    template_page: FormVariantPage,
    *,
    scan: bytes,
    matched: bool = True,
) -> SubmissionPage:
    """A matched SubmissionPage whose stored image is *scan*."""
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=SubmissionStatus.MATCHED,
    )
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    path = pages_dir / f"{page_id}.png"
    path.write_bytes(scan)
    page = SubmissionPage(
        id=page_id,
        submission_id=submission.id,
        form_variant_page_id=template_page.id if matched else None,
        source_file_path=str(path),
        source_filename="scan.png",
        source_page_order=0,
    )
    db.add(page)
    db.flush()
    return page


def _selected_ids(result) -> list[str]:
    return [o["option_id"] for o in result.detected_payload_json["selected_options"]]


def _confidences(result) -> list[float]:
    return [o["confidence"] for o in result.detected_payload_json["selected_options"]]


def _option_ids(page_object: PageObject) -> list[uuid.UUID]:
    return [
        o.id
        for o in sorted(page_object.question_config.options, key=lambda o: o.order_index)
    ]


# ---------------------------------------------------------------------------
# SINGLE_CHOICE — the three validity outcomes
# ---------------------------------------------------------------------------
def test_no_bubble_blackened_reads_as_no_answer(db, pages_dir, project, actor, template_page):
    """Every bubble printed and none filled — the sheet a candidate skipped."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan(outlined=(0, 1, 2))
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.detected_payload_json["validity"] == Validity.NO_ANSWER.value
    assert result.detected_payload_json["selected_options"] == []
    # NULL, not 0.0: there is no reading to be confident about. NO_ANSWER
    # already carries the fact on the other, independent axis.
    assert result.confidence_score is None
    assert result.validity_forces_review is True


def test_one_bubble_blackened_reads_as_valid(db, pages_dir, project, actor, template_page):
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={1: 1.0}, outlined=(0, 2)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.detected_payload_json["validity"] == Validity.VALID.value
    assert _selected_ids(result) == [str(_option_ids(obj)[1])]
    assert result.validity_forces_review is False


def test_two_bubbles_blackened_break_a_single_choice_question(
    db, pages_dir, project, actor, template_page
):
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0, 2: 1.0}, outlined=(1,)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert (
        result.detected_payload_json["validity"] == Validity.TOO_MANY_ANSWERS.value
    )
    assert len(_selected_ids(result)) == 2
    # Not VALID → the second axis forces a human, whatever the confidence was.
    assert result.validity_forces_review is True
    assert result.review_required is True


# ---------------------------------------------------------------------------
# MULTIPLE_CHOICE — the same two bubbles, the opposite verdict
# ---------------------------------------------------------------------------
def test_two_bubbles_blackened_are_valid_on_a_multiple_choice_question(
    db, pages_dir, project, actor, template_page
):
    """The one thing that separates the two question types. Same sheet, same
    ink, opposite conclusion."""
    obj = _make_question(
        db, template_page, question_type=QuestionType.MULTIPLE_CHOICE
    )
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0, 2: 1.0}, outlined=(1,)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.detected_payload_json["validity"] == Validity.VALID.value
    ids = _option_ids(obj)
    assert _selected_ids(result) == [str(ids[0]), str(ids[2])]
    assert result.validity_forces_review is False
    assert result.review_required is False


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def test_the_aggregated_confidence_is_the_minimum_among_selected_options(
    db, pages_dir, project, actor, template_page
):
    """A question is only as sure as its least sure bubble. Two options filled
    to different depths give two different confidences, and the result carries
    the lower one."""
    obj = _make_question(
        db, template_page, question_type=QuestionType.MULTIPLE_CHOICE
    )
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0, 1: 0.85}, outlined=(2,)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    confidences = _confidences(result)
    assert len(confidences) == 2
    # The thinner mark really is read as the less certain one.
    assert confidences[0] > confidences[1]
    assert result.confidence_score == pytest.approx(min(confidences))


def test_the_confidence_reported_is_the_fill_ratio_itself(
    db, pages_dir, project, actor, template_page
):
    """No second, invented score sits on top of the measurement: the fraction
    of ink that decided the selection IS the confidence of that reading."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    scan = _draw_scan(filled={1: 1.0}, outlined=(0, 2))
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=scan)

    result = svc.extract_question_result(db, page, obj, batch)

    # Re-measure the same window straight off the image and compare.
    with Image.open(io.BytesIO(scan)) as img:
        gray = img.convert("L")
    scale_x = gray.size[0] / TEMPLATE_W
    scale_y = gray.size[1] / TEMPLATE_H
    cx, cy = _bubble_center(1)
    expected = bubble_sampling.sample_circle_fill_ratio(
        gray, cx * scale_x, cy * scale_y,
        question_layout.CIRCLE_R * min(scale_x, scale_y),
    )
    assert _confidences(result) == [pytest.approx(expected)]
    assert result.confidence_score == pytest.approx(expected)


def test_no_confidence_is_recorded_for_options_left_empty(
    db, pages_dir, project, actor, template_page
):
    """The result contract: absence from ``selected_options`` is how 'not
    selected' is expressed, never a zero-confidence entry."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={2: 1.0}, outlined=(0, 1)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert len(result.detected_payload_json["selected_options"]) == 1
    assert _selected_ids(result) == [str(_option_ids(obj)[2])]


# ---------------------------------------------------------------------------
# review_required — both axes, through confidence_threshold_service
# ---------------------------------------------------------------------------
def test_a_confidence_below_the_batch_threshold_sends_a_valid_reading_to_review(
    db, pages_dir, project, actor, template_page
):
    """A perfectly VALID single answer, but read off a HALF-HEARTED mark — a
    bubble ticked rather than filled — that this batch's threshold does not
    accept. It has to be a genuinely poor mark: a cleanly blackened bubble
    reads near 1.0 and no sane threshold would flag it."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.9)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 0.8}, outlined=(1, 2)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.detected_payload_json["validity"] == Validity.VALID.value
    assert result.validity_forces_review is False
    assert bubble_sampling.SELECTION_FILL_THRESHOLD < result.confidence_score < 0.9
    assert result.review_required is True


def test_a_confident_valid_reading_needs_no_human(
    db, pages_dir, project, actor, template_page
):
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0}, outlined=(1, 2)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.confidence_score > 0.3
    assert result.validity_forces_review is False
    assert result.review_required is False


def test_an_invalid_reading_needs_a_human_however_low_the_threshold(
    db, pages_dir, project, actor, template_page
):
    """The two axes are OR-ed and independent: no threshold, however permissive,
    can clear a reading that broke the rules of the question."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.0)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0, 1: 1.0}, outlined=(2,)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.confidence_score > 0.0
    assert result.validity_forces_review is True
    assert result.review_required is True


def test_an_empty_question_reaches_review_on_validity_alone(
    db, pages_dir, project, actor, template_page
):
    """A NULL confidence contributes nothing to the review flag, so NO_ANSWER has to
    be what raises the flag — and it does."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.0)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan(outlined=(0, 1, 2))
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.confidence_score is None
    assert result.review_required is True


def test_a_batch_without_an_override_follows_the_system_default(
    db, pages_dir, project, actor, template_page
):
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=None)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0}, outlined=(1, 2)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.review_required is (
        result.confidence_score < settings.default_confidence_threshold
    )
    # And concretely: a cleanly blackened bubble CLEARS the shipped 0.7. It
    # would not have, by much, if the reading were capped at π/4 ≈ 0.79 by a
    # square sampling window — which is why the mask is a circle.
    assert result.confidence_score > settings.default_confidence_threshold
    assert result.review_required is False


# ---------------------------------------------------------------------------
# End to end: three options, one blackened, on a real synthetic sheet
# ---------------------------------------------------------------------------
def test_end_to_end_a_three_option_question_with_one_bubble_filled(
    db, pages_dir, project, actor, template_page
):
    """The whole issue in one test: an authored QUESTION, a scan drawn at the
    export's own geometry with the MIDDLE bubble blackened, and the row that
    comes back out."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={1: 1.0}, outlined=(0, 2)),
    )

    result = svc.extract_question_result(db, page, obj, batch)
    db.flush()

    assert result.submission_page_id == page.id
    assert result.page_object_id == obj.id
    assert result.result_type is SubmissionResultType.QUESTION
    assert result.detected_payload_json == {
        "selected_options": [
            {
                "option_id": str(_option_ids(obj)[1]),
                "confidence": pytest.approx(result.confidence_score),
            }
        ],
        "validity": Validity.VALID.value,
    }
    assert result.review_required is False
    assert result.reviewed is False


def test_a_second_extraction_of_the_same_object_on_the_same_page_is_refused(
    db, pages_dir, project, actor, template_page
):
    """The uniqueness of (submission_page_id, page_object_id). Extraction is
    re-runnable, and without the constraint a re-run would append a second
    reading of the same bubble — two rows for one fact, with nothing to say
    which one review should believe. Refused by the DATABASE, at the caller's
    flush, because the service does not flush."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={1: 1.0}, outlined=(0, 2)),
    )

    svc.extract_question_result(db, page, obj, batch)
    db.flush()

    svc.extract_question_result(db, page, obj, batch)
    with pytest.raises(IntegrityError):
        db.flush()


def test_the_same_object_on_a_DIFFERENT_page_is_a_different_result(
    db, pages_dir, project, actor, template_page
):
    """The constraint is on the PAIR: two candidates answering the same printed
    question are two readings, and both must be storable."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    scan = _draw_scan(filled={1: 1.0}, outlined=(0, 2))
    first = _make_scanned_page(db, pages_dir, batch, template_page, scan=scan)
    second = _make_scanned_page(db, pages_dir, batch, template_page, scan=scan)

    svc.extract_question_result(db, first, obj, batch)
    svc.extract_question_result(db, second, obj, batch)
    db.flush()  # no IntegrityError


# ---------------------------------------------------------------------------
# Scale independence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scale", [1.0, 2.0, 4.0, 6.0])
def test_the_same_sheet_reads_the_same_at_any_scan_resolution(
    db, pages_dir, project, actor, template_page, scale
):
    """Nothing about the DPI history of the file is assumed — only the observed
    ratio between the scan's real dimensions and the template's expected ones.
    So our own re-scanned export, an external scanner and a phone photo all
    read alike, as long as the sheet is aligned."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={2: 1.0}, outlined=(0, 1), scale=scale),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert _selected_ids(result) == [str(_option_ids(obj)[2])]
    assert result.detected_payload_json["validity"] == Validity.VALID.value


def test_a_page_whose_template_has_no_geometry_falls_back_to_the_a4_defaults(
    db, pages_dir, project, actor, template_page
):
    """A page authored without a backing image (NULL width_px/height_px) is
    rendered at A4 @ 96 DPI by the export, so extraction assumes the same sheet
    — and the bubbles land where they were printed."""
    template_page.width_px = None
    template_page.height_px = None
    db.flush()
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0}, outlined=(1, 2)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert _selected_ids(result) == [str(_option_ids(obj)[0])]


# ---------------------------------------------------------------------------
# The export's clipping, reproduced
# ---------------------------------------------------------------------------
def test_an_option_the_export_clipped_away_is_never_read(
    db, pages_dir, project, actor, template_page
):
    """The export stops drawing options once a band would fall past the
    object's height. Extraction has to stop looking at exactly the same point:
    an option that was never printed has no bubble, and whatever ink happens to
    sit below the object belongs to something else on the page."""
    # Room for two bands only, but three options authored.
    obj = _make_question(
        db,
        template_page,
        height=float(question_layout.HEADER_H + 2 * question_layout.OPTION_H),
    )
    batch = _make_batch(db, project, actor, threshold=0.3)
    # Ink exactly where the THIRD (unprinted) bubble would have been.
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={2: 1.0}, outlined=(0, 1)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result.detected_payload_json["selected_options"] == []
    assert result.detected_payload_json["validity"] == Validity.NO_ANSWER.value


# ---------------------------------------------------------------------------
# Preconditions and transaction discipline
# ---------------------------------------------------------------------------
def test_an_unmatched_page_raises_instead_of_guessing(
    db, pages_dir, project, actor, template_page
):
    """``form_variant_page_id`` is what says which sheet this is. Without it
    there is no template geometry to map from, and inventing one would produce
    a confidently wrong reading. A precondition of the caller (matching runs
    first), not a case handled here."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0}), matched=False,
    )

    with pytest.raises(ValueError, match="not matched"):
        svc.extract_question_result(db, page, obj, batch)


def test_a_non_question_object_raises(db, pages_dir, project, actor, template_page):
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.TEXT,
        x=OBJ_X, y=OBJ_Y, width=OBJ_W, height=OBJ_H,
    )
    db.add(obj)
    db.flush()
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan()
    )

    with pytest.raises(ValueError, match="question_config"):
        svc.extract_question_result(db, page, obj, batch)


def test_a_scan_stored_outside_the_submission_pages_dir_is_refused(
    db, pages_dir, tmp_path, project, actor, template_page
):
    """Reading through ``storage_service`` rather than opening the stored path
    directly: a corrupted or hand-edited row can never make the extractor read
    a file elsewhere on the filesystem."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan()
    )
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(_draw_scan())
    page.source_file_path = str(outside)
    db.flush()

    with pytest.raises(ValueError, match="outside the submission-pages"):
        svc.extract_question_result(db, page, obj, batch)


def test_extraction_neither_flushes_nor_commits(
    db, pages_dir, project, actor, template_page
):
    """The caller owns the transaction, the discipline of the whole scanning
    pipeline (see submission_matching_service). The row is staged, not written:
    its id — a column default applied at INSERT — is still unassigned."""
    obj = _make_question(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(filled={0: 1.0}, outlined=(1, 2)),
    )

    result = svc.extract_question_result(db, page, obj, batch)

    assert result in db.new
    assert result.id is None

    db.flush()
    assert result.id is not None
