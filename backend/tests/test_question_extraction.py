# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for QUESTION extraction.

Everything here is either arithmetic or Pillow: the template→scan mapping, the
bubble sampler, and the validity rule. No Postgres, no session, no storage —
the DB-backed half lives in
``tests/integration/test_question_extraction_integration.py``.

Images are REAL images generated with Pillow, in the style of the ingestion
suites: mocking the sampler's input would only test that the mock returns what
it was told to.
"""
from __future__ import annotations

import uuid

import pytest
from PIL import Image, ImageDraw

from app.models.enums import QuestionType, Validity
from app.models.form_variant_page import FormVariantPage
from app.models.page_object import PageObject
from app.services import form_variant_export_service as export_svc
from app.services import question_extraction_service as svc
from app.utils import bubble_sampling as sampling, question_layout
from app.utils.pdf_rendering import DEFAULT_PAGE_HEIGHT_PX, DEFAULT_PAGE_WIDTH_PX


BLACK = 0
WHITE = 255


def _obj(x=100.0, y=200.0, width=300.0, height=90.0) -> PageObject:
    """A detached QUESTION PageObject — geometry only, never persisted."""
    return PageObject(
        id=uuid.uuid4(), x=x, y=y, width=width, height=height
    )


# ---------------------------------------------------------------------------
# The shared layout constants really are shared
# ---------------------------------------------------------------------------
def test_export_and_extraction_read_the_same_layout_constants():
    """The point of ``app.utils.question_layout``. If the export ever went back
    to its own copies, detection would keep sampling the old coordinates and
    read blackened sheets as blank — silently, and only on the numbers that
    moved. Identity, not equality: two modules holding equal-for-now copies is
    exactly the state the shared module exists to prevent."""
    assert export_svc._Q_HEADER_H is question_layout.HEADER_H
    assert export_svc._Q_OPTION_H is question_layout.OPTION_H
    assert export_svc._Q_CIRCLE_R is question_layout.CIRCLE_R
    assert export_svc._Q_CIRCLE_CX is question_layout.CIRCLE_CX
    assert export_svc._Q_TEXT_X is question_layout.TEXT_X


# ---------------------------------------------------------------------------
# resolve_scan_region — template pixel space -> scan pixel space
# ---------------------------------------------------------------------------
def test_scan_region_scales_by_the_observed_ratio_with_explicit_geometry():
    page = FormVariantPage(width_px=800, height_px=1000)
    region = svc.resolve_scan_region(
        _obj(x=100.0, y=200.0, width=300.0, height=90.0),
        page,
        scan_width_px=1600,   # 2x
        scan_height_px=3000,  # 3x
    )
    assert region.x == 200.0
    assert region.y == 600.0
    assert region.width == 600.0
    assert region.height == 270.0


def test_scan_region_falls_back_to_the_a4_defaults_when_the_page_has_no_geometry():
    """A page authored without a backing image has NULL width_px/height_px. The
    export renders it at A4 @ 96 DPI, so extraction must assume the very same
    sheet size — otherwise the two disagree about what a page even is."""
    page = FormVariantPage(width_px=None, height_px=None)
    obj = _obj(x=0.0, y=0.0, width=float(DEFAULT_PAGE_WIDTH_PX), height=float(
        DEFAULT_PAGE_HEIGHT_PX
    ))

    region = svc.resolve_scan_region(
        obj,
        page,
        scan_width_px=DEFAULT_PAGE_WIDTH_PX * 2,
        scan_height_px=DEFAULT_PAGE_HEIGHT_PX * 2,
    )

    # An object covering the whole default page covers the whole scan.
    assert region.x == 0.0
    assert region.y == 0.0
    assert region.width == pytest.approx(DEFAULT_PAGE_WIDTH_PX * 2)
    assert region.height == pytest.approx(DEFAULT_PAGE_HEIGHT_PX * 2)


def test_scan_region_scales_the_two_axes_independently():
    """A scan whose aspect ratio differs from the template's is stretched to
    fit, not letterboxed: with no perspective correction available, honouring
    both observed ratios keeps the bubbles closer to where they really are."""
    page = FormVariantPage(width_px=800, height_px=1000)
    region = svc.resolve_scan_region(
        _obj(x=400.0, y=500.0, width=80.0, height=100.0),
        page,
        scan_width_px=400,   # 0.5x horizontally
        scan_height_px=4000,  # 4x vertically
    )
    assert (region.x, region.width) == (200.0, 40.0)
    assert (region.y, region.height) == (2000.0, 400.0)


def test_scan_region_is_the_identity_when_the_scan_matches_the_template():
    page = FormVariantPage(width_px=794, height_px=1123)
    obj = _obj()
    region = svc.resolve_scan_region(obj, page, 794, 1123)
    assert (region.x, region.y, region.width, region.height) == (
        obj.x, obj.y, obj.width, obj.height
    )


# ---------------------------------------------------------------------------
# bubble_sampling.sample_circle_fill_ratio — the one thing that touches pixels
# (shared with MATRIX extraction, which reads its cells with it too)
# ---------------------------------------------------------------------------
def _gray(size=(100, 100), color=WHITE) -> Image.Image:
    return Image.new("L", size, color)


def test_a_fully_black_area_reads_as_completely_filled():
    img = _gray(color=BLACK)
    assert sampling.sample_circle_fill_ratio(img, 50, 50, 10) == 1.0


def test_a_fully_white_area_reads_as_completely_empty():
    img = _gray(color=WHITE)
    assert sampling.sample_circle_fill_ratio(img, 50, 50, 10) == 0.0


@pytest.mark.parametrize("radius", [5, 10, 20, 30])
def test_a_filled_circle_reads_as_essentially_completely_filled(radius):
    """A perfectly blackened bubble uses the TOP of the confidence range, not
    four fifths of it. Sampling the circumscribed square would cap it at
    π/4 ≈ 0.79 — barely 0.085 above the 0.7 default confidence threshold, so
    an unambiguous mark would sit one rounding away from being sent to review.
    The shortfall left here is pure rasterisation at the disc's edge, and it
    shrinks as the scan resolution grows."""
    img = _gray()
    ImageDraw.Draw(img).ellipse(
        [50 - radius, 50 - radius, 50 + radius, 50 + radius], fill=BLACK
    )

    ratio = sampling.sample_circle_fill_ratio(img, 50, 50, radius)

    assert ratio == pytest.approx(1.0, abs=0.02)
    assert ratio > sampling.SELECTION_FILL_THRESHOLD


def test_ink_outside_the_bubble_is_not_sampled_at_all():
    """The mask, stated directly: the square crop is only how the candidate
    pixels are fetched. Here every corner of that crop is solid black and the
    disc itself is blank — a bubble surrounded by a heavy printed frame, or by
    the neighbouring option's mark bleeding in — and the reading is ~0. The
    same crop sampled as a square would read 1 - π/4 ≈ 0.215 of ink that
    belongs to somebody else."""
    radius = 20
    img = Image.new("L", (100, 100), BLACK)
    ImageDraw.Draw(img).ellipse(
        [50 - radius, 50 - radius, 50 + radius, 50 + radius], fill=WHITE
    )

    # Not exactly 0.0: a handful of pixels on the disc's rim survive the
    # rasterisation of the white ellipse, the mirror of the ~0.99 ceiling on
    # a filled bubble. Two orders of magnitude below the square's 0.215.
    assert sampling.sample_circle_fill_ratio(img, 50, 50, radius) == pytest.approx(
        0.0, abs=0.02
    )


def test_the_ratio_tracks_how_much_of_the_bubble_was_filled():
    """The ratio is a measurement, not a verdict: a mark covering 90% of the
    bubble's radius is read as less certain than one covering all of it, and
    that ordering is what makes the number usable as a confidence."""
    def ratio(fraction: float) -> float:
        img = _gray()
        r = 20 * fraction
        ImageDraw.Draw(img).ellipse([50 - r, 50 - r, 50 + r, 50 + r], fill=BLACK)
        return sampling.sample_circle_fill_ratio(img, 50, 50, 20)

    full, most, some = ratio(1.0), ratio(0.9), ratio(0.8)
    assert full > most > some > sampling.SELECTION_FILL_THRESHOLD
    # Area scales with the square of the radius, and the mask measures area.
    assert most == pytest.approx(0.9**2, abs=0.15)


def test_an_empty_outlined_circle_stays_below_the_selection_threshold():
    """What an UNANSWERED bubble looks like on a real sheet: the export prints
    the outline, and its stroke must not be mistaken for ink."""
    img = _gray()
    radius = 20
    ImageDraw.Draw(img).ellipse(
        [50 - radius, 50 - radius, 50 + radius, 50 + radius],
        outline=BLACK,
        width=2,
    )

    ratio = sampling.sample_circle_fill_ratio(img, 50, 50, radius)

    assert ratio < sampling.SELECTION_FILL_THRESHOLD


def test_mid_grey_on_either_side_of_the_threshold_decides_ink():
    """The threshold is on the 0-255 grey level, strictly below = ink."""
    dark = _gray(color=sampling.DARK_PIXEL_THRESHOLD - 1)
    light = _gray(color=sampling.DARK_PIXEL_THRESHOLD)
    assert sampling.sample_circle_fill_ratio(dark, 50, 50, 10) == 1.0
    assert sampling.sample_circle_fill_ratio(light, 50, 50, 10) == 0.0


def test_a_window_falling_partly_off_the_sheet_is_sampled_on_what_exists():
    """Half the window is off the left edge; the half on the sheet is black."""
    img = _gray(color=BLACK)
    assert sampling.sample_circle_fill_ratio(img, 0, 50, 10) == 1.0


def test_a_window_entirely_off_the_sheet_reads_as_empty():
    """Nothing was read there, which is not the same as 'read and found ink'.
    A scan cropped short of a bubble must never answer the question for the
    candidate."""
    img = _gray(color=BLACK)
    assert sampling.sample_circle_fill_ratio(img, -100, -100, 10) == 0.0
    assert sampling.sample_circle_fill_ratio(img, 500, 500, 10) == 0.0


def test_a_degenerate_radius_reads_as_empty_rather_than_dividing_by_zero():
    img = _gray(color=BLACK)
    assert sampling.sample_circle_fill_ratio(img, 50, 50, 0) == 0.0


# ---------------------------------------------------------------------------
# _compute_validity — the discrete axis
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question_type", [QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE]
)
def test_nothing_blackened_is_no_answer_for_every_question_type(question_type):
    assert svc._compute_validity(question_type, 0) is Validity.NO_ANSWER


def test_one_option_is_valid_for_every_question_type():
    assert svc._compute_validity(QuestionType.SINGLE_CHOICE, 1) is Validity.VALID
    assert svc._compute_validity(QuestionType.MULTIPLE_CHOICE, 1) is Validity.VALID


def test_two_options_break_a_single_choice_question():
    assert (
        svc._compute_validity(QuestionType.SINGLE_CHOICE, 2)
        is Validity.TOO_MANY_ANSWERS
    )


def test_two_options_are_the_point_of_a_multiple_choice_question():
    assert (
        svc._compute_validity(QuestionType.MULTIPLE_CHOICE, 2) is Validity.VALID
    )
    assert (
        svc._compute_validity(QuestionType.MULTIPLE_CHOICE, 7) is Validity.VALID
    )
