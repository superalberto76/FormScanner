# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests of ``app.utils.sheet_alignment``.

The page is drawn by the export itself: ``form_variant_export_service
._render_alignment_marks`` on an A4 page at 96 dpi, plus a few objects near
the corners (a filled bubble, a QR-like ring, a filled square smaller than a
mark), rasterised at 150 dpi like an uploaded PDF and then transformed like a
scanner would. No database: the module is pure, and the marks are the only
thing it looks for. The same transformations on a whole COLL export are in
``tests/integration/test_page_alignment_integration.py``.
"""
from __future__ import annotations

import math

import cv2
import fitz
import numpy as np
import pytest

from app.services import form_variant_export_service as export_svc
from app.utils import alignment_marks, sheet_alignment as sa
from app.utils.pdf_rendering import (
    BLACK,
    DEFAULT_PAGE_HEIGHT_PX as TW_H,
    DEFAULT_PAGE_WIDTH_PX as TW_W,
    PageRenderer,
    page_metrics,
)

DPI = 150
K = DPI / 96  # template px → scan px
MM = DPI / 25.4  # scan px per mm


# Dots drawn on purpose in the usable area, to measure where the content lands
# (template px): mid-page, where the ADF stretch is largest.
DOTS = ((150, 300), (400, 300), (650, 300), (150, 561), (400, 561), (650, 561), (400, 820))
DOT_R = 6


def _page(*, marks: bool = True, dpi: int = DPI, dots: bool = False) -> np.ndarray:
    w_pts, h_pts, scale = page_metrics(TW_W, TW_H, 96)
    doc = fitz.open()
    try:
        page = doc.new_page(width=w_pts, height=h_pts)
        r = PageRenderer(page, scale)
        # Objects in the usable area, near the top-left mark: a filled bubble
        # of the mark's size, a ring like a QR finder, a small filled square.
        r.circle(70, 70, 9.5, color=BLACK, fill=BLACK, width=0)
        r.box(95, 62, 19, 19, color=BLACK, width=4)
        r.box(62, 95, 9, 9, color=None, width=0, fill=BLACK)
        r.text(62, 120, 200, 30, "Name and surname", 14)
        if dots:
            for x, y in DOTS:
                r.circle(x, y, DOT_R, color=BLACK, fill=BLACK, width=0)
        if marks:
            export_svc._render_alignment_marks(r, TW_W, TW_H)
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csGRAY)
        return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    finally:
        doc.close()


def _affine(img: np.ndarray, matrix: np.ndarray, size: tuple[int, int] | None = None) -> np.ndarray:
    h, w = img.shape
    return cv2.warpAffine(
        img, matrix, size or (w, h), flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )


def _shift(img, dx, dy):
    return _affine(img, np.float32([[1, 0, dx], [0, 1, dy]]))


def _scale(img, factor):
    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), 0, factor)
    return _affine(img, m)


def _pad(img, mm):
    p = round(mm * MM)
    return cv2.copyMakeBorder(img, p, p, p, p, cv2.BORDER_CONSTANT, value=255)


def _rotate(img, degrees):
    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return _affine(img, m)


def _cover(img, corner):
    """Paint the mark of *corner* (template corner, upright page) white."""
    x, y, w, h = alignment_marks.mark_rects(TW_W, TW_H)[corner]
    out = img.copy()
    out[round((y - 3) * K) : round((y + h + 3) * K), round((x - 3) * K) : round((x + w + 3) * K)] = 255
    return out


def _expected_center(corner, k=K):
    x, y, w, h = alignment_marks.mark_rects(TW_W, TW_H)[corner]
    return (x + w / 2) * k, (y + h / 2) * k


def _map(alignment, x, y):
    m = np.array(alignment.matrix)
    return m @ np.array([x, y, 1.0])


# ---------------------------------------------------------------------------
# Finding the marks
# ---------------------------------------------------------------------------


def test_identity_finds_the_four_marks_and_the_hole():
    gray = _page()
    markers = sa.find_markers(gray, TW_W, TW_H)
    for corner in alignment_marks.CORNERS:
        m = markers[corner]
        assert m is not None, corner
        ex, ey = _expected_center(corner)
        assert abs(m.x - ex) < 1.0 and abs(m.y - ey) < 1.0, (corner, m)
        assert m.holed is (corner == alignment_marks.DISTINCT_CORNER)


def test_a_filled_bubble_or_a_ring_near_the_corner_is_not_a_mark():
    """With the top-left mark gone, nothing else in the window is taken."""
    gray = _cover(_page(), "TL")
    assert sa.find_markers(gray, TW_W, TW_H)["TL"] is None


def test_no_marks_no_alignment():
    alignment = sa.find_alignment(_page(marks=False), TW_W, TW_H)
    assert not alignment.aligned
    assert alignment.reason == sa.MARKERS_NOT_FOUND
    assert all(m is None for m in alignment.markers.values())


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def test_identity():
    a = sa.find_alignment(_page(), TW_W, TW_H)
    assert a.aligned and a.reason is None and a.flipped is False
    assert a.scale_x == pytest.approx(K, rel=0.005)
    assert a.scale_y == pytest.approx(K, rel=0.005)
    assert abs(a.rotation_deg) < 0.1 and abs(a.shear_deg) < 0.1
    assert a.residual_mm < 0.2
    assert a.output_size(TW_W, TW_H) == (round(TW_W * (a.scale_x + a.scale_y) / 2), round(TW_H * (a.scale_x + a.scale_y) / 2))


def test_shift():
    a = sa.find_alignment(_shift(_page(), 12, -9), TW_W, TW_H)
    assert a.aligned
    u, v = _map(a, 0, 0)
    assert u == pytest.approx(12, abs=1.0) and v == pytest.approx(-9, abs=1.0)


def test_printed_at_92_percent():
    a = sa.find_alignment(_scale(_page(), 0.92), TW_W, TW_H)
    assert a.aligned
    assert a.scale_x == pytest.approx(0.92 * K, rel=0.01)
    assert a.scale_y == pytest.approx(0.92 * K, rel=0.01)


def test_rotation_of_4_degrees_on_a_bed_larger_than_the_sheet():
    """The scanner bed exceeds the sheet by 10 mm per side, as a real one
    does: rotated about its centre by 4° the sheet keeps its corners."""
    a = sa.find_alignment(_rotate(_pad(_page(), 10), 4), TW_W, TW_H)
    assert a.aligned, a.to_json()
    # OpenCV's positive angle is counter-clockwise on screen (y down).
    assert a.rotation_deg == pytest.approx(-4, abs=0.2)
    assert a.flipped is False


def test_upside_down_page_is_aligned_and_flipped():
    a = sa.find_alignment(np.rot90(_page(), 2).copy(), TW_W, TW_H)
    assert a.aligned and a.flipped is True
    assert abs(a.rotation_deg) < 0.2
    # The template's top-left corner is the scan's bottom-right.
    h, w = _page().shape
    u, v = _map(a, 0, 0)
    assert u == pytest.approx(w, abs=1.5) and v == pytest.approx(h, abs=1.5)


def test_seven_mm_lost_at_the_top():
    gray = _page()[round(7 * MM) :]
    a = sa.find_alignment(gray, TW_W, TW_H)
    assert a.aligned, a.to_json()
    u, v = _map(a, 0, 0)
    assert v == pytest.approx(-7 * MM, abs=1.5)
    assert a.scale_x == pytest.approx(a.scale_y, rel=0.005)


@pytest.mark.parametrize("corner", alignment_marks.CORNERS)
def test_one_mark_covered_still_aligns(corner):
    a = sa.find_alignment(_cover(_page(), corner), TW_W, TW_H)
    assert a.aligned, (corner, a.to_json())
    assert a.flipped is False
    assert a.residual_mm is None  # 3 marks: no redundancy


def test_one_mark_covered_on_an_upside_down_page():
    a = sa.find_alignment(np.rot90(_cover(_page(), "TR"), 2).copy(), TW_W, TW_H)
    assert a.aligned and a.flipped is True


def test_two_marks_covered_does_not_align():
    a = sa.find_alignment(_cover(_cover(_page(), "TL"), "BR"), TW_W, TW_H)
    assert not a.aligned and a.reason == sa.MARKERS_NOT_FOUND


def test_rotation_of_8_degrees_on_a_wide_bed_aligns():
    """A sheet well inside a larger bed (25 mm per side), turned by 8°: the
    marks move by about 20 mm and stay inside the corner windows."""
    a = sa.find_alignment(_rotate(_pad(_page(), 25), 8), TW_W, TW_H)
    assert a.aligned, a.to_json()
    assert a.rotation_deg == pytest.approx(-8, abs=0.2)


def test_rotation_of_12_degrees_is_not_plausible():
    a = sa.find_alignment(_rotate(_pad(_page(), 25), 12), TW_W, TW_H)
    assert not a.aligned and a.reason == sa.ROTATION_OUT_OF_RANGE


def test_rotation_of_15_degrees_is_not_aligned():
    a = sa.find_alignment(_rotate(_pad(_page(), 25), 15), TW_W, TW_H)
    assert not a.aligned
    assert a.reason in (sa.ROTATION_OUT_OF_RANGE, sa.MARKERS_NOT_FOUND)


def test_rotation_of_15_degrees_with_marks_found_is_rejected_by_the_limit():
    """The limit itself, on marks moved exactly where a 15° turn puts them."""
    gray = _page()
    h, w = gray.shape
    rotation = cv2.getRotationMatrix2D((w / 2, h / 2), 15, 1.0)
    markers = {}
    for corner in alignment_marks.CORNERS:
        x, y = _expected_center(corner)
        u, v = rotation @ np.array([x, y, 1.0])
        markers[corner] = sa.Marker(u, v, corner == "BR")
    a = _alignment_from(markers, gray.shape)
    assert not a.aligned and a.reason == sa.ROTATION_OUT_OF_RANGE


def _alignment_from(markers, shape):
    """find_alignment with find_markers replaced (for the plausibility rules)."""
    original = sa.find_markers
    sa.find_markers = lambda *args: markers
    try:
        return sa.find_alignment(np.full(shape, 255, np.uint8), TW_W, TW_H)
    finally:
        sa.find_markers = original


def test_hole_in_the_wrong_corner_is_unknown_orientation():
    markers = {c: sa.Marker(*_expected_center(c), c == "TR") for c in alignment_marks.CORNERS}
    a = _alignment_from(markers, _page().shape)
    assert not a.aligned and a.reason == sa.ORIENTATION_UNKNOWN


def test_two_holes_is_unknown_orientation():
    markers = {c: sa.Marker(*_expected_center(c), c in ("BR", "TL")) for c in alignment_marks.CORNERS}
    assert _alignment_from(markers, _page().shape).reason == sa.ORIENTATION_UNKNOWN


def test_four_marks_and_no_hole_is_unknown_orientation():
    markers = {c: sa.Marker(*_expected_center(c), False) for c in alignment_marks.CORNERS}
    assert _alignment_from(markers, _page().shape).reason == sa.ORIENTATION_UNKNOWN


def test_three_marks_no_hole_missing_corner_decides():
    centres = {c: _expected_center(c) for c in alignment_marks.CORNERS}
    shape = _page().shape
    # Missing BR: upright. Missing TR (BR and TL both found, neither holed): unknown.
    upright = {c: (sa.Marker(*centres[c], False) if c != "BR" else None) for c in centres}
    assert _alignment_from(upright, shape).aligned
    unknown = {c: (sa.Marker(*centres[c], False) if c != "TR" else None) for c in centres}
    assert _alignment_from(unknown, shape).reason == sa.ORIENTATION_UNKNOWN


def test_one_misplaced_mark_of_four_is_not_aligned():
    """A mark 8 mm off (another component taken for it): with 4 marks the
    least squares spread the error, and the residual or the shear catches it."""
    centres = {c: _expected_center(c) for c in alignment_marks.CORNERS}
    markers = {c: sa.Marker(*centres[c], c == "BR") for c in centres}
    x, y = centres["TR"]
    markers["TR"] = sa.Marker(x - 8 * MM, y + 8 * MM, False)
    a = _alignment_from(markers, _page().shape)
    assert not a.aligned
    assert a.reason in (sa.RESIDUAL_TOO_LARGE, sa.SHEAR_OUT_OF_RANGE)
    assert a.residual_mm > sa.MAX_RESIDUAL_MM


def test_scale_out_of_range_and_mismatch():
    shape = _page().shape
    centres = {c: _expected_center(c) for c in alignment_marks.CORNERS}
    cx, cy = shape[1] / 2, shape[0] / 2

    def scaled(fx, fy):
        return {
            c: sa.Marker(cx + (x - cx) * fx, cy + (y - cy) * fy, c == "BR")
            for c, (x, y) in centres.items()
        }

    assert _alignment_from(scaled(0.7, 0.7), shape).reason == sa.SCALE_OUT_OF_RANGE
    assert _alignment_from(scaled(1.0, 0.92), shape).reason == sa.SCALE_MISMATCH
    assert _alignment_from(scaled(0.97, 0.95), shape).aligned


def test_mirrored_page_is_not_aligned():
    shape = _page().shape
    w = shape[1]
    markers = {
        c: sa.Marker(w - x, y, c == "BR")
        for c, (x, y) in ((c, _expected_center(c)) for c in alignment_marks.CORNERS)
    }
    assert _alignment_from(markers, shape).reason in (sa.MIRRORED, sa.ORIENTATION_UNKNOWN)


# ---------------------------------------------------------------------------
# The straightened image
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transform",
    [
        lambda g: _shift(g, 9, 14),
        lambda g: _scale(g, 0.92),
        lambda g: _rotate(_pad(g, 10), 3),
        lambda g: np.rot90(g, 2).copy(),
    ],
    ids=["shift", "92%", "rotated", "upside-down"],
)
def test_warp_puts_the_marks_back_where_the_template_has_them(transform):
    scan = transform(_page())
    a = sa.find_alignment(scan, TW_W, TW_H)
    assert a.aligned
    straight = sa.warp_to_template(scan, a, TW_W, TW_H)
    out_w, out_h = a.output_size(TW_W, TW_H)
    assert straight.shape == (out_h, out_w)
    k = out_w / TW_W
    markers = sa.find_markers(straight, TW_W, TW_H)
    for corner in alignment_marks.CORNERS:
        ex, ey = _expected_center(corner, k)
        m = markers[corner]
        assert m is not None and math.hypot(m.x - ex, m.y - ey) < 1.5, (corner, m)
        assert m.holed is (corner == "BR")


def test_warp_keeps_rgb_channels():
    gray = _shift(_page(), 5, 5)
    a = sa.find_alignment(gray, TW_W, TW_H)
    rgb = np.dstack([gray] * 3)
    out = sa.warp_to_template(rgb, a, TW_W, TW_H)
    assert out.ndim == 3 and out.shape[2] == 3


def test_warp_refuses_a_page_not_aligned():
    a = sa.find_alignment(_page(marks=False), TW_W, TW_H)
    with pytest.raises(ValueError):
        sa.warp_to_template(_page(marks=False), a, TW_W, TW_H)


def test_to_json_is_serialisable():
    import json

    a = sa.find_alignment(_page(), TW_W, TW_H)
    data = json.loads(json.dumps(a.to_json()))
    assert data["aligned"] is True and data["flipped"] is False
    assert set(data["markers"]) == set(alignment_marks.CORNERS)
    assert data["markers"]["BR"]["holed"] is True


# ---------------------------------------------------------------------------
# Side marks and the strip correction
# ---------------------------------------------------------------------------


def _stretch(img: np.ndarray, peak_mm: float = 1.0) -> np.ndarray:
    """An ADF-like uneven stretch along the feed, the profile measured on the
    real scans: nothing at the corner marks, *peak_mm* pushed
    down halfway between them (a half sine)."""
    h, w = img.shape
    half = alignment_marks.MARK_SIZE / 2
    top = (alignment_marks.MARK_INSET + half) * K
    bottom = (TW_H - alignment_marks.MARK_INSET - half) * K
    ys = np.arange(h, dtype=np.float32)
    s = np.clip((ys - top) / (bottom - top), 0, 1)
    source_y = ys - peak_mm * MM * np.sin(np.pi * s)
    map_y = np.repeat(source_y[:, None], w, axis=1).astype(np.float32)
    map_x = np.repeat(np.arange(w, dtype=np.float32)[None, :], h, axis=0)
    return cv2.remap(
        img, map_x, map_y, cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )


def _side_centre(row: int, column: str, k: float = K) -> tuple[float, float]:
    """Expected centre of side mark *row* (1-based) of *column*, scan px."""
    x, y, w, h = alignment_marks.side_mark_rects(TW_W, TW_H)[row - 1][column]
    return (x + w / 2) * k, (y + h / 2) * k


def _cover_side(img: np.ndarray, column: str, rows) -> np.ndarray:
    """Paint white the side marks *rows* (1-based) of *column* (upright page)."""
    out = img.copy()
    for row in rows:
        x, y, w, h = alignment_marks.side_mark_rects(TW_W, TW_H)[row - 1][column]
        out[round((y - 3) * K) : round((y + h + 3) * K), round((x - 3) * K) : round((x + w + 3) * K)] = 255
    return out


ALL_ROWS = range(1, alignment_marks.SIDE_MARK_COUNT + 1)


def _no_side_marks(img: np.ndarray) -> np.ndarray:
    return _cover_side(_cover_side(img, "L", ALL_ROWS), "R", ALL_ROWS)


def _straighten(scan: np.ndarray):
    a = sa.find_alignment(scan, TW_W, TW_H)
    assert a.aligned, a.to_json()
    strips = sa.find_strip_correction(scan, a, TW_W, TW_H)
    return a, strips


def _dot_error_mm(straight: np.ndarray) -> float:
    """The largest distance (mm) of the drawn dots from where the template has
    them, on a straightened image: each dot's centre is the centroid of its
    darkness (255 − grey), finer than the centroid of its thresholded pixels."""
    k = straight.shape[1] / TW_W
    reach = round(4 * 96 / 25.4 * k)
    ys, xs = np.mgrid[0 : 2 * reach, 0 : 2 * reach]
    worst = 0.0
    for x, y in DOTS:
        ex, ey = x * k, y * k
        x0, y0 = round(ex) - reach, round(ey) - reach
        weights = 255.0 - straight[y0 : y0 + 2 * reach, x0 : x0 + 2 * reach]
        assert weights.sum() > 0, (x, y)
        cx = (weights * xs).sum() / weights.sum() + x0
        cy = (weights * ys).sum() / weights.sum() + y0
        worst = max(worst, math.hypot(cx - ex, cy - ey) / (96 / 25.4 * k))
    return worst


def test_the_side_marks_are_found_where_the_template_has_them():
    a, strips = _straighten(_page())
    assert strips.applied and strips.reason is None
    for column in sa.SIDE_COLUMNS:
        _, nodes = strips.columns[column]
        assert len(nodes) == alignment_marks.SIDE_MARK_COUNT + 2
        assert nodes[0].corner and nodes[-1].corner
        for row, node in zip(ALL_ROWS, nodes[1:-1]):
            ex, ey = _side_centre(row, column)
            assert node.found is not None, (column, row)
            assert math.hypot(node.found[0] - ex, node.found[1] - ey) < 1.0, (column, row, node)
            assert not node.rejected
    assert strips.max_shift_mm() < 0.2


def test_a_corner_window_never_takes_a_side_mark():
    """The top-left mark covered, the side marks in its window stay out (size
    and aspect ratio): three corners, the page aligned, and the strips hold."""
    gray = _cover(_page(), "TL")
    assert sa.find_markers(gray, TW_W, TW_H)["TL"] is None
    a, strips = _straighten(gray)
    assert a.flipped is False and a.markers["TL"] is None
    assert strips.applied
    top_left = strips.columns["L"][1][0]
    assert top_left.found is None and top_left.shift == (0.0, 0.0)


def test_an_uneven_stretch_is_corrected():
    scan = _stretch(_page(dots=True))
    a, strips = _straighten(scan)
    assert strips.applied, strips.to_json()
    assert 0.8 < strips.max_shift_mm() < 1.2
    affine_only = sa.warp_to_template(scan, a, TW_W, TW_H)
    corrected = sa.warp_to_template(scan, a, TW_W, TW_H, strips)
    assert corrected.shape == affine_only.shape
    # Not an empty test: with the corners alone the middle stays off.
    assert _dot_error_mm(affine_only) > 0.6
    assert _dot_error_mm(corrected) < 0.15


# The precision floor of these tests: the straightened image takes the
# nearest pixel, so at the app's 150 dpi for PDFs
# (``storage_service.PDF_RENDER_DPI``) a whole dot can land up to half a
# pixel off, 0.085 mm. The dots of the middle row happen to fall right at half a
# pixel on the stretched page: about 0.10 mm of the error below is that
# rounding, already there with the affine map alone, not the correction.


@pytest.mark.parametrize("rows", [[5], [5, 13]], ids=["one", "one-per-column"])
def test_a_covered_side_mark_is_bridged_by_its_neighbours(rows):
    scan = _cover_side(_stretch(_page(dots=True)), "L", rows[:1])
    if len(rows) > 1:
        scan = _cover_side(scan, "R", rows[1:])
    a, strips = _straighten(scan)
    assert strips.applied, strips.to_json()
    assert strips.columns["L"][1][rows[0]].found is None
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H, strips)) < 0.15


def test_the_side_mark_at_the_peak_of_the_stretch_covered_is_bridged_too():
    """Covered where the stretch is largest: its neighbours still carry it,
    the dots on its row within 0.15 mm (measured 0.146: the half pixel of
    rounding above plus ≈ 0.015 mm of chord between the two neighbours on the
    half sine — tight by construction)."""
    scan = _cover_side(_stretch(_page(dots=True)), "L", [9])
    a, strips = _straighten(scan)
    assert strips.applied, strips.to_json()
    assert strips.columns["L"][1][9].found is None
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H, strips)) < 0.15


@pytest.mark.parametrize("corner", ["TL", "BL"])
def test_a_corner_mark_covered_on_a_stretched_page(corner):
    """With three corners the missing one is a node with a zero shift. On a
    stretched page its side marks are still taken,
    none discarded in a cascade, and the dots come back within 0.15 mm."""
    scan = _cover(_stretch(_page(dots=True)), corner)
    a, strips = _straighten(scan)
    assert a.markers[corner] is None
    assert strips.applied, strips.to_json()
    nodes = [n for _, column in strips.columns.values() for n in column]
    assert not any(n.rejected for n in nodes)
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H)) > 0.6
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H, strips)) < 0.15


def test_a_pen_stroke_touching_a_side_mark_makes_it_missing():
    """A stroke drawn from a side mark into the page merges with it: the
    shape is no longer a bar, the mark counts as missing and its neighbours
    carry the strip."""
    scan = _stretch(_page(dots=True))
    ex, ey = _side_centre(6, "L")
    half_w = alignment_marks.SIDE_MARK_W * K / 2
    y = round(ey + 0.77 * MM)  # the stretch near row 6 is about 0.77 mm
    scan[y - 1 : y + 2, round(ex + half_w - 2) : round(ex + half_w + 40)] = 0
    a, strips = _straighten(scan)
    assert strips.columns["L"][1][6].found is None
    assert strips.applied, strips.to_json()
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H, strips)) < 0.15


def test_three_side_marks_covered_in_a_row_fall_back_to_the_affine_map():
    scan = _cover_side(_stretch(_page()), "R", [7, 8, 9])
    a, strips = _straighten(scan)
    assert not strips.applied and strips.reason == sa.SIDE_MARKERS_INSUFFICIENT
    # The image is the affine one, bit for bit.
    assert np.array_equal(
        sa.warp_to_template(scan, a, TW_W, TW_H, strips),
        sa.warp_to_template(scan, a, TW_W, TW_H),
    )


def test_too_few_side_marks_fall_back_to_the_affine_map():
    scan = _cover_side(_page(), "L", [2, 5, 8, 11, 14])  # 12 of 17 left
    _, strips = _straighten(scan)
    assert not strips.applied and strips.reason == sa.SIDE_MARKERS_INSUFFICIENT


def test_a_pen_bar_next_to_a_covered_side_mark_is_discarded():
    """A mark of the same shape 2.5 mm below the place of a covered side mark:
    found, off its neighbours by far more than the limit, discarded; the
    correction holds on the others."""
    scan = _cover_side(_stretch(_page(dots=True)), "L", [8])
    ex, ey = _side_centre(8, "L")
    w, h = alignment_marks.SIDE_MARK_W * K, alignment_marks.SIDE_MARK_H * K
    cy = ey + 2.5 * MM + 1.0 * MM  # the stretch near row 8 is about 1 mm
    scan[round(cy - h / 2) : round(cy + h / 2), round(ex - w / 2) : round(ex + w / 2)] = 0
    a, strips = _straighten(scan)
    node = strips.columns["L"][1][8]
    assert node.found is not None and node.rejected, strips.to_json()["columns"]["L"][8]
    assert strips.applied
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H, strips)) < 0.15


def test_a_shift_beyond_the_limit_falls_back_to_the_affine_map():
    _, strips = _straighten(_stretch(_page(), peak_mm=3.2))
    assert not strips.applied and strips.reason == sa.SIDE_SHIFT_OUT_OF_RANGE
    assert strips.max_shift_mm() > sa.MAX_SIDE_SHIFT_MM


def test_a_page_printed_without_side_marks_keeps_the_affine_image():
    scan = _no_side_marks(_shift(_page(), 7, -4))
    a, strips = _straighten(scan)
    assert not strips.applied and strips.reason == sa.SIDE_MARKERS_NOT_FOUND
    assert all(
        n.found is None for _, nodes in strips.columns.values() for n in nodes if not n.corner
    )
    assert np.array_equal(
        sa.warp_to_template(scan, a, TW_W, TW_H, strips),
        sa.warp_to_template(scan, a, TW_W, TW_H),
    )


def test_an_upside_down_stretched_page_is_corrected():
    scan = np.rot90(_stretch(_page(dots=True)), 2).copy()
    a, strips = _straighten(scan)
    assert a.flipped is True
    assert strips.applied, strips.to_json()
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H)) > 0.6
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H, strips)) < 0.15


def test_a_stretched_page_turned_by_2_degrees_is_corrected():
    scan = _rotate(_pad(_stretch(_page(dots=True)), 10), 2)
    a, strips = _straighten(scan)
    assert a.rotation_deg == pytest.approx(-2, abs=0.2)
    assert strips.applied, strips.to_json()
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H)) > 0.6
    assert _dot_error_mm(sa.warp_to_template(scan, a, TW_W, TW_H, strips)) < 0.15


def test_the_strip_correction_needs_an_aligned_page():
    a = sa.find_alignment(_page(marks=False), TW_W, TW_H)
    with pytest.raises(ValueError):
        sa.find_strip_correction(_page(marks=False), a, TW_W, TW_H)


def test_strips_to_json_is_serialisable():
    import json

    _, strips = _straighten(_cover_side(_page(), "R", [3]))
    data = json.loads(json.dumps(strips.to_json()))
    assert data["applied"] is True and data["reason"] is None
    assert set(data["columns"]) == {"L", "R"}
    assert len(data["columns"]["R"]) == alignment_marks.SIDE_MARK_COUNT + 2
    assert data["columns"]["R"][3]["found"] is None
    assert data["columns"]["R"][3]["shift_mm"] is None
    assert data["columns"]["L"][0]["corner"] is True
    assert isinstance(data["max_shift_mm"], float)
