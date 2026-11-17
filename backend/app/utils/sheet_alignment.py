# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Aligning a scanned page to its template from the four printed marks.

Every exported page prints four alignment marks, one per corner, inside the
reserved bands (positions in :mod:`app.utils.alignment_marks`).
This module finds them on a scan and computes the transformation that brings
the scan back into the template's frame: shift, scale per axis and a small
rotation — an affine map, estimated by least squares on the marks' centres.
A page scanned upside down (180°) is recognised by the distinct mark (the one
with a hole, bottom-right on the template) and aligned too.

Pure: numpy / OpenCV arrays in, numbers out — no session, no file. The service
(``page_alignment_service``) reads the scan, calls :func:`find_alignment`,
writes the straightened image (:func:`warp_to_template`) and the page's fields.

Finding the marks
-----------------
The scan is binarised at ``bubble_sampling.DARK_PIXEL_THRESHOLD`` (the same
notion of "ink" as the bubbles). In a window of :data:`WINDOW_MM` per corner
(sized with the proportional scale scan / template) the connected dark
components are candidates, and a candidate is a mark when:

  * it does not touch the window's edges (shadows along the scanner's border,
    a component cut by the window);
  * the sides of its minimum-area rectangle are within ±:data:`SIZE_TOLERANCE`
    of ``MARK_SIZE`` × the proportional scale (rotation does not change them);
  * it is nearly square (sides within :data:`MAX_ASPECT`) and FILLS that
    rectangle (outline area / rectangle area ≥ :data:`MIN_RECT_FILL`): a full
    disc fills π/4 ≈ 0.79 of its square, so a filled bubble is never a mark;
  * optionally holed: the light pixels inside its outline are between
    :data:`HOLE_RATIO_RANGE` of its filled area and its centre is light — the
    distinct mark. The QR finder pattern, a ring with a hole of half its area,
    stays out of that range.

Per window the candidate nearest to the scan's corner wins: the mark sits
8 mm from both edges, objects start at 15 mm (outside the reserved bands), so
it is always nearer.

Orientation and the transformation
----------------------------------
At least 3 marks of 4. The distinct mark in the scan's bottom-right corner →
upright; in the top-left → upside down (each scan corner then matches the
opposite template corner). With 3 marks and none holed, the missing one is the
distinct one when it is bottom-right or top-left; otherwise, or with the hole
in another corner, or two holes, or four marks with no hole, the orientation is
unknown and the page is not aligned.

Plausibility
------------
From the linear part of the affine map (the 180° of an upside-down page
removed): rotation within ±:data:`MAX_ROTATION_DEG`, each axis' scale within
:data:`SCALE_RANGE` of the proportional one, the two scales within
:data:`MAX_SCALE_MISMATCH` of each other, shear within :data:`MAX_SHEAR_DEG`,
no mirroring; with 4 marks the largest residual within
:data:`MAX_RESIDUAL_MM`. Anything else is NOT aligned, with the reason.

The strip correction
--------------------
The ADF stretches the sheet unevenly along its feed: after the affine map on
the corners a point in the middle of the page can still be off by about 1 mm.
Every exported page also prints a column of side marks on each side
(``alignment_marks.side_mark_rects``). Only on a page the corners ALIGNED,
:func:`find_strip_correction` looks for each side mark within
±:data:`SIDE_SEARCH_MM` of where the affine map puts it (an upside-down page
needs nothing apart: the map already holds the 180°), with the same "ink" as
the corners. A candidate is a solid bar whose sides are within
±:data:`SIDE_SIZE_TOLERANCE` of the printed ones × the scale, its aspect ratio
within :data:`SIDE_ASPECT_RANGE` and its fill at least
:data:`SIDE_MIN_RECT_FILL`; the nearest to the expected centre wins.

Each node of a column (the two corner marks and the side marks between them)
gets its shift from the affine map, in scan px. A side mark whose shift is
off by more than :data:`MAX_SIDE_DEVIATION_MM` from the interpolation of its
valid neighbours is discarded (the worst first, then again); then each column
needs :data:`MIN_SIDE_MARKERS` side marks and no more than
:data:`MAX_SIDE_MISSING_RUN` missing in a row, and every shift must stay
within :data:`MAX_SIDE_SHIFT_MM`. Otherwise the correction is not applied and
the page keeps the affine image, with the reason in the
diagnostics only — never a page NOT aligned, never a review reason.

Applied, the scan point of a template point is the affine map plus a shift
interpolated linearly in y along each column, between the two nodes around
it, and linearly in x between the left column and the right one (constant
outside the columns): the bilinear interpolation of each strip on its four
marks, continuous from one strip to the next. :func:`warp_to_template` then
resamples with ``cv2.remap`` instead of ``cv2.warpAffine``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from app.utils import alignment_marks
from app.utils.bubble_sampling import DARK_PIXEL_THRESHOLD

# Template pixel space: 96 dpi (see ``alignment_marks``).
MM_PX = 96 / 25.4

# --- Finding the marks (values tuned on test scans) ------------------------
WINDOW_MM = 60.0
SIZE_TOLERANCE = 0.35
MAX_ASPECT = 1.25
MIN_RECT_FILL = 0.88
HOLE_RATIO_RANGE = (0.04, 0.30)

# --- Plausibility -----------------------------------------------------------
MAX_ROTATION_DEG = 10.0
SCALE_RANGE = (0.80, 1.15)
MAX_SCALE_MISMATCH = 0.05
MAX_SHEAR_DEG = 2.0
MAX_RESIDUAL_MM = 2.0

# --- Why a page is not aligned (diagnostics in ``alignment_json``) ----------
MARKERS_NOT_FOUND = "MARKERS_NOT_FOUND"
ORIENTATION_UNKNOWN = "ORIENTATION_UNKNOWN"
MIRRORED = "MIRRORED"
ROTATION_OUT_OF_RANGE = "ROTATION_OUT_OF_RANGE"
SCALE_OUT_OF_RANGE = "SCALE_OUT_OF_RANGE"
SCALE_MISMATCH = "SCALE_MISMATCH"
SHEAR_OUT_OF_RANGE = "SHEAR_OUT_OF_RANGE"
RESIDUAL_TOO_LARGE = "RESIDUAL_TOO_LARGE"

# --- Side marks and the strip correction ------------------------------------
SIDE_SEARCH_MM = 4.0
SIDE_SIZE_TOLERANCE = 0.35
SIDE_ASPECT_RANGE = (1.8, 3.2)
SIDE_MIN_RECT_FILL = 0.85
MAX_SIDE_SHIFT_MM = 2.5
MAX_SIDE_DEVIATION_MM = 0.6
MIN_SIDE_MARKERS = 13
MAX_SIDE_MISSING_RUN = 2

# --- Why the strip correction is not applied (``alignment_json.strips``) ----
SIDE_MARKERS_NOT_FOUND = "SIDE_MARKERS_NOT_FOUND"  # e.g. printed without side marks
SIDE_MARKERS_INSUFFICIENT = "SIDE_MARKERS_INSUFFICIENT"
SIDE_SHIFT_OUT_OF_RANGE = "SIDE_SHIFT_OUT_OF_RANGE"
SIDE_ERROR = "SIDE_ERROR"  # an exception, caught by the service

SIDE_COLUMNS = ("L", "R")
# The corner marks at the top and the bottom of each column, on the template.
_COLUMN_CORNERS = {"L": ("TL", "BL"), "R": ("TR", "BR")}

# An upside-down page: each scan corner shows the opposite template corner.
_FLIPPED_CORNER = {"TL": "BR", "TR": "BL", "BL": "TR", "BR": "TL"}


@dataclass(frozen=True)
class Marker:
    """A mark found on the scan: its centre in scan pixels, and whether it has
    the hole of the distinct mark."""

    x: float
    y: float
    holed: bool


@dataclass(frozen=True)
class Alignment:
    """The outcome of :func:`find_alignment`.

    ``matrix`` maps template pixels to scan pixels (2 × 3, ``[u, v] = M ·
    [x, y, 1]``); with ``aligned`` False it may still be set (a transformation
    was computed and found implausible), for the diagnostics only.
    """

    aligned: bool
    reason: str | None
    markers: dict[str, Marker | None]  # by SCAN corner
    flipped: bool | None = None
    matrix: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None
    rotation_deg: float | None = None
    scale_x: float | None = None  # scan px per template px
    scale_y: float | None = None
    shear_deg: float | None = None
    residual_mm: float | None = None  # only with 4 marks

    def output_size(self, template_w: float, template_h: float) -> tuple[int, int]:
        """The straightened image's size: the template's × the estimated scale
        (the mean of the two axes), rounded."""
        scale = self._scale()
        return max(1, round(template_w * scale)), max(1, round(template_h * scale))

    def _scale(self) -> float:
        if self.scale_x is None or self.scale_y is None:
            raise ValueError("no transformation was computed")
        return (self.scale_x + self.scale_y) / 2

    def to_json(self) -> dict:
        """The diagnostics stored in ``SubmissionPage.alignment_json``."""
        return {
            "aligned": self.aligned,
            "reason": self.reason,
            "markers": {
                corner: (
                    {"x": round(m.x, 2), "y": round(m.y, 2), "holed": m.holed}
                    if m is not None
                    else None
                )
                for corner, m in self.markers.items()
            },
            "flipped": self.flipped,
            "matrix": (
                [[round(v, 6) for v in row] for row in self.matrix]
                if self.matrix is not None
                else None
            ),
            "rotation_deg": _rounded(self.rotation_deg, 3),
            "scale_x": _rounded(self.scale_x, 6),
            "scale_y": _rounded(self.scale_y, 6),
            "shear_deg": _rounded(self.shear_deg, 3),
            "residual_mm": _rounded(self.residual_mm, 3),
        }


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


# ---------------------------------------------------------------------------
# Finding the marks
# ---------------------------------------------------------------------------


def _proportional_scale(
    gray: np.ndarray, template_w: float, template_h: float
) -> tuple[float, float]:
    height, width = gray.shape[:2]
    return width / template_w, height / template_h


def find_markers(
    gray: np.ndarray, template_w: float, template_h: float
) -> dict[str, Marker | None]:
    """The mark found in each corner window of the scan, by SCAN corner
    (``TL``, ``TR``, ``BL``, ``BR``), or None (module docstring)."""
    height, width = gray.shape[:2]
    sx, sy = _proportional_scale(gray, template_w, template_h)
    win_w = min(width, max(1, round(WINDOW_MM * MM_PX * sx)))
    win_h = min(height, max(1, round(WINDOW_MM * MM_PX * sy)))
    expected_side = alignment_marks.MARK_SIZE * math.sqrt(sx * sy)
    dark = (gray < DARK_PIXEL_THRESHOLD).astype(np.uint8)

    windows = {
        "TL": (0, 0, (0, 0)),
        "TR": (width - win_w, 0, (width, 0)),
        "BL": (0, height - win_h, (0, height)),
        "BR": (width - win_w, height - win_h, (width, height)),
    }
    found: dict[str, Marker | None] = {}
    for corner, (x0, y0, (cx, cy)) in windows.items():
        window = dark[y0 : y0 + win_h, x0 : x0 + win_w]
        candidates = _candidates(window, expected_side)
        best = min(
            candidates,
            key=lambda m: math.hypot(m.x + x0 - cx, m.y + y0 - cy),
            default=None,
        )
        found[corner] = (
            Marker(x=float(best.x + x0), y=float(best.y + y0), holed=bool(best.holed))
            if best is not None
            else None
        )
    return found


def _candidates(window: np.ndarray, expected_side: float) -> list[Marker]:
    """The components of *window* (a 0/1 dark mask) that look like a mark, in
    window coordinates."""
    win_h, win_w = window.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        window, connectivity=8
    )
    min_side = expected_side * (1 - SIZE_TOLERANCE)
    max_side = expected_side * (1 + SIZE_TOLERANCE)
    # The bounding box of a square rotated by up to ~10° is up to ~1.2 × its side.
    max_box = max_side * 1.25
    out: list[Marker] = []
    for label in range(1, count):
        bx, by, bw, bh, pixels = stats[label]
        if bx == 0 or by == 0 or bx + bw >= win_w or by + bh >= win_h:
            continue  # touches the window: the scanner's border, or cut
        if bw < min_side or bh < min_side or bw > max_box or bh > max_box:
            continue
        mask = (labels[by : by + bh, bx : bx + bw] == label).astype(np.uint8)
        marker = _as_marker(mask, int(pixels), min_side, max_side)
        if marker is not None:
            out.append(Marker(x=marker.x + bx, y=marker.y + by, holed=marker.holed))
    return out


def _as_marker(
    mask: np.ndarray,
    pixels: int,
    min_side: float,
    max_side: float,
) -> Marker | None:
    """*mask* (one component, 0/1) as a mark in its own coordinates, or None."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    outline = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(outline)
    if area <= 0:
        return None
    (_, _), (rw, rh), _ = cv2.minAreaRect(outline)
    # The outline runs through pixel centres: one pixel shorter than the side.
    side_a, side_b = rw + 1, rh + 1
    if min(side_a, side_b) < min_side or max(side_a, side_b) > max_side:
        return None
    if max(side_a, side_b) / min(side_a, side_b) > MAX_ASPECT:
        return None
    if rw <= 0 or rh <= 0 or area / (rw * rh) < MIN_RECT_FILL:
        return None

    filled = np.zeros_like(mask)
    cv2.drawContours(filled, [outline], -1, 1, thickness=cv2.FILLED)
    filled_pixels = int(filled.sum())
    hole_ratio = (filled_pixels - pixels) / filled_pixels if filled_pixels else 0.0
    moments = cv2.moments(outline)
    cx = moments["m10"] / moments["m00"]
    cy = moments["m01"] / moments["m00"]
    low, high = HOLE_RATIO_RANGE
    holed = False
    if low <= hole_ratio <= high:
        px, py = int(round(cx)), int(round(cy))
        holed = (
            0 <= py < mask.shape[0]
            and 0 <= px < mask.shape[1]
            and mask[py, px] == 0
        )
    elif hole_ratio > high:
        return None  # a ring, not a mark (the QR finder pattern)
    return Marker(x=float(cx), y=float(cy), holed=bool(holed))


# ---------------------------------------------------------------------------
# Orientation, transformation, plausibility
# ---------------------------------------------------------------------------


def _orientation(markers: dict[str, Marker | None]) -> bool | None:
    """True = upside down, False = upright, None = unknown (module docstring)."""
    found = [c for c, m in markers.items() if m is not None]
    holed = [c for c in found if markers[c].holed]  # type: ignore[union-attr]
    if len(holed) > 1:
        return None
    if len(holed) == 1:
        return {"BR": False, "TL": True}.get(holed[0])
    if len(found) != 3:
        return None
    missing = next(c for c in alignment_marks.CORNERS if markers[c] is None)
    return {"BR": False, "TL": True}.get(missing)


def find_alignment(
    gray: np.ndarray, template_w: float, template_h: float
) -> Alignment:
    """Find the marks on *gray* (a 2-D uint8 scan) and the transformation from
    a *template_w* × *template_h* template onto it (module docstring)."""
    markers = find_markers(gray, template_w, template_h)
    found = {c: m for c, m in markers.items() if m is not None}
    if len(found) < 3:
        return Alignment(aligned=False, reason=MARKERS_NOT_FOUND, markers=markers)
    flipped = _orientation(markers)
    if flipped is None:
        return Alignment(aligned=False, reason=ORIENTATION_UNKNOWN, markers=markers)

    template_rects = alignment_marks.mark_rects(template_w, template_h)
    half = alignment_marks.MARK_SIZE / 2
    src, dst = [], []
    for scan_corner, marker in found.items():
        template_corner = _FLIPPED_CORNER[scan_corner] if flipped else scan_corner
        x, y, _, _ = template_rects[template_corner]
        src.append((x + half, y + half, 1.0))
        dst.append((marker.x, marker.y))
    src_m, dst_m = np.array(src), np.array(dst)
    solution, *_ = np.linalg.lstsq(src_m, dst_m, rcond=None)
    matrix = solution.T  # 2 × 3, template → scan

    linear = -matrix[:, :2] if flipped else matrix[:, :2]
    col_x, col_y = linear[:, 0], linear[:, 1]
    scale_x = float(np.hypot(*col_x))
    scale_y = float(np.hypot(*col_y))
    rotation = math.degrees(math.atan2(col_x[1], col_x[0]))
    angle_y = math.degrees(math.atan2(col_y[1], col_y[0]))
    shear = _wrap_degrees(angle_y - rotation - 90.0)
    residual_mm = None
    if len(found) == 4:
        errors = np.hypot(*(src_m @ solution - dst_m).T)
        residual_mm = float(errors.max()) / ((scale_x + scale_y) / 2) / MM_PX

    common = dict(
        markers=markers,
        flipped=flipped,
        matrix=tuple(tuple(float(v) for v in row) for row in matrix),
        rotation_deg=rotation,
        scale_x=scale_x,
        scale_y=scale_y,
        shear_deg=shear,
        residual_mm=residual_mm,
    )
    reason = _implausibility(
        linear, rotation, scale_x, scale_y, shear, residual_mm,
        _proportional_scale(gray, template_w, template_h),
    )
    return Alignment(aligned=reason is None, reason=reason, **common)


def _wrap_degrees(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def _implausibility(
    linear: np.ndarray,
    rotation: float,
    scale_x: float,
    scale_y: float,
    shear: float,
    residual_mm: float | None,
    proportional: tuple[float, float],
) -> str | None:
    if np.linalg.det(linear) <= 0:
        return MIRRORED
    if abs(rotation) > MAX_ROTATION_DEG:
        return ROTATION_OUT_OF_RANGE
    low, high = SCALE_RANGE
    if not (low <= scale_x / proportional[0] <= high) or not (
        low <= scale_y / proportional[1] <= high
    ):
        return SCALE_OUT_OF_RANGE
    if abs(scale_x - scale_y) / max(scale_x, scale_y) > MAX_SCALE_MISMATCH:
        return SCALE_MISMATCH
    if abs(shear) > MAX_SHEAR_DEG:
        return SHEAR_OUT_OF_RANGE
    if residual_mm is not None and residual_mm > MAX_RESIDUAL_MM:
        return RESIDUAL_TOO_LARGE
    return None


# ---------------------------------------------------------------------------
# The strip correction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StripNode:
    """One node of a column: a corner mark (first and last) or a side mark.

    ``found`` is its centre on the scan (None: not found, or a corner mark
    missing among three); ``shift`` is found − where the affine map puts it,
    in scan px (a missing corner mark: 0, the affine map passes through the
    other three); ``rejected``: found but discarded as incoherent."""

    template_y: float
    corner: bool
    found: tuple[float, float] | None
    shift: tuple[float, float] | None
    rejected: bool = False

    @property
    def valid(self) -> bool:
        return self.shift is not None and not self.rejected


@dataclass(frozen=True)
class StripCorrection:
    """The outcome of :func:`find_strip_correction` (module docstring).

    ``columns`` holds, by column (``L``, ``R``), the template x of its centre
    and its nodes top to bottom; with ``applied`` False the page keeps the
    affine image and ``reason`` says why."""

    applied: bool
    reason: str | None
    columns: dict[str, tuple[float, tuple[StripNode, ...]]]
    px_per_mm: float  # scan px per mm

    def shifts(self, column: str) -> np.ndarray:
        """The shift of every node of *column* (N × 2, scan px), a node not
        valid interpolated linearly between the nearest valid ones."""
        _, nodes = self.columns[column]
        ys = np.array([n.template_y for n in nodes])
        valid = [i for i, n in enumerate(nodes) if n.valid]
        known = np.array([nodes[i].shift for i in valid])
        return np.column_stack([
            np.interp(ys, ys[valid], known[:, 0]),
            np.interp(ys, ys[valid], known[:, 1]),
        ])

    def max_shift_mm(self) -> float | None:
        values = [
            math.hypot(*n.shift) / self.px_per_mm
            for _, nodes in self.columns.values()
            for n in nodes
            if n.valid
        ]
        return max(values) if values else None

    def to_json(self) -> dict:
        """The diagnostics stored in ``alignment_json["strips"]``."""

        def node_json(n: StripNode) -> dict:
            return {
                "corner": n.corner,
                "found": (
                    {"x": round(n.found[0], 2), "y": round(n.found[1], 2)}
                    if n.found is not None
                    else None
                ),
                "rejected": n.rejected,
                "shift_mm": (
                    [round(v / self.px_per_mm, 3) for v in n.shift]
                    if n.shift is not None
                    else None
                ),
            }

        return {
            "applied": self.applied,
            "reason": self.reason,
            "columns": {
                column: [node_json(n) for n in nodes]
                for column, (_, nodes) in self.columns.items()
            },
            "max_shift_mm": _rounded(self.max_shift_mm(), 3),
        }


def find_strip_correction(
    gray: np.ndarray, alignment: Alignment, template_w: float, template_h: float
) -> StripCorrection:
    """Find the side marks on *gray* (the scan) around where the corners'
    affine map puts them, and decide whether the strip correction holds
    (module docstring). *alignment* must be aligned."""
    if not alignment.aligned or alignment.matrix is None:
        raise ValueError("the page is not aligned")
    matrix = np.array(alignment.matrix)
    scale = alignment._scale()
    px_per_mm = MM_PX * scale
    dark = (gray < DARK_PIXEL_THRESHOLD).astype(np.uint8)
    corner_rects = alignment_marks.mark_rects(template_w, template_h)
    rows = alignment_marks.side_mark_rects(template_w, template_h)
    half = alignment_marks.MARK_SIZE / 2

    def project(x: float, y: float) -> tuple[float, float]:
        u, v = matrix @ np.array([x, y, 1.0])
        return float(u), float(v)

    def corner_node(template_corner: str) -> StripNode:
        x, y, _, _ = corner_rects[template_corner]
        scan_corner = (
            _FLIPPED_CORNER[template_corner] if alignment.flipped else template_corner
        )
        marker = alignment.markers.get(scan_corner)
        if marker is None:
            return StripNode(y + half, True, None, (0.0, 0.0))
        u, v = project(x + half, y + half)
        # Measured like the side marks (the darkness centroid), so that the
        # shifts of a column share one reference: the outline centroid of the
        # corner search sits a fraction of a pixel away from it.
        cx, cy = _corner_centre(gray, dark, marker, scale)
        return StripNode(y + half, True, (cx, cy), (cx - u, cy - v))

    columns: dict[str, tuple[float, tuple[StripNode, ...]]] = {}
    any_found = False
    for column in SIDE_COLUMNS:
        top, bottom = _COLUMN_CORNERS[column]
        nodes = [corner_node(top)]
        for row in rows:
            x, y, w, h = row[column]
            u, v = project(x + w / 2, y + h / 2)
            found = _find_side_marker(gray, dark, u, v, scale, px_per_mm)
            any_found = any_found or found is not None
            nodes.append(StripNode(
                y + h / 2,
                False,
                found,
                (found[0] - u, found[1] - v) if found is not None else None,
            ))
        nodes.append(corner_node(bottom))
        cx = corner_rects[top][0] + half
        columns[column] = (cx, tuple(_reject_incoherent(nodes, px_per_mm)))

    reason = None
    if not any_found:
        reason = SIDE_MARKERS_NOT_FOUND
    elif not all(_enough(nodes) for _, nodes in columns.values()):
        reason = SIDE_MARKERS_INSUFFICIENT
    correction = StripCorrection(
        applied=False, reason=reason, columns=columns, px_per_mm=px_per_mm
    )
    if reason is None:
        largest = correction.max_shift_mm()
        if largest is not None and largest > MAX_SIDE_SHIFT_MM:
            reason = SIDE_SHIFT_OUT_OF_RANGE
    return StripCorrection(
        applied=reason is None, reason=reason, columns=columns, px_per_mm=px_per_mm
    )


def _find_side_marker(
    gray: np.ndarray,
    dark: np.ndarray,
    u: float,
    v: float,
    scale: float,
    px_per_mm: float,
) -> tuple[float, float] | None:
    """The centre of the side mark nearest to (*u*, *v*) within
    ±``SIDE_SEARCH_MM`` (Chebyshev), in scan px, or None. The window around
    the expected centre is that reach plus half a bar, so that a bar shifted
    to the limit does not touch it.

    The centre is the centroid of the bar's darkness (255 − grey) over the
    component grown by 2 px, not of its thresholded outline: the outline
    moves by whole pixels, about 0.17 mm at 150 dpi, as large as the shifts
    to correct between two rows; the grey edges place the bar well below the
    pixel."""
    height, width = dark.shape
    long_side = alignment_marks.SIDE_MARK_W * scale
    short_side = alignment_marks.SIDE_MARK_H * scale
    reach = SIDE_SEARCH_MM * px_per_mm
    margin = reach + long_side * (1 + SIDE_SIZE_TOLERANCE) / 2 + 2
    x0, x1 = max(0, math.floor(u - margin)), min(width, math.ceil(u + margin) + 1)
    y0, y1 = max(0, math.floor(v - margin)), min(height, math.ceil(v + margin) + 1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None  # off the scan
    window = dark[y0:y1, x0:x1]
    win_h, win_w = window.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(window, connectivity=8)
    max_box = long_side * (1 + SIDE_SIZE_TOLERANCE) * 1.25
    best: tuple[float, float] | None = None
    best_distance = math.inf
    for label in range(1, count):
        bx, by, bw, bh, _ = stats[label]
        if bx == 0 or by == 0 or bx + bw >= win_w or by + bh >= win_h:
            continue  # touches the window: cut, or something larger
        if bw > max_box or bh > max_box:
            continue
        mask = (labels[by : by + bh, bx : bx + bw] == label).astype(np.uint8)
        if not _is_side_marker(mask, long_side, short_side):
            continue
        cx, cy = _dark_centroid(gray, labels == label, x0, y0)
        if abs(cx - u) > reach or abs(cy - v) > reach:
            continue
        distance = math.hypot(cx - u, cy - v)
        if distance < best_distance:
            best, best_distance = (cx, cy), distance
    return best


def _is_side_marker(mask: np.ndarray, long_side: float, short_side: float) -> bool:
    """Whether *mask* (one component, 0/1) has the shape of a side mark
    (module docstring)."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return False
    outline = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(outline)
    if area <= 0:
        return False
    (_, _), (rw, rh), _ = cv2.minAreaRect(outline)
    # The outline runs through pixel centres: one pixel shorter than the side.
    a, b = max(rw, rh) + 1, min(rw, rh) + 1
    tol = SIDE_SIZE_TOLERANCE
    low, high = SIDE_ASPECT_RANGE
    return (
        long_side * (1 - tol) <= a <= long_side * (1 + tol)
        and short_side * (1 - tol) <= b <= short_side * (1 + tol)
        and low <= a / b <= high
        and rw > 0
        and rh > 0
        and area / (rw * rh) >= SIDE_MIN_RECT_FILL
    )


def _corner_centre(
    gray: np.ndarray, dark: np.ndarray, marker: Marker, scale: float
) -> tuple[float, float]:
    """The darkness centroid of the corner mark found at *marker*: the largest
    dark component within about a mark of its centre. Falls back to the
    marker's own centre if nothing is there (it cannot happen on the scan the
    corner search looked at)."""
    height, width = dark.shape
    reach = alignment_marks.MARK_SIZE * scale
    x0, x1 = max(0, math.floor(marker.x - reach)), min(width, math.ceil(marker.x + reach) + 1)
    y0, y1 = max(0, math.floor(marker.y - reach)), min(height, math.ceil(marker.y + reach) + 1)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        dark[y0:y1, x0:x1], connectivity=8
    )
    if count < 2:
        return marker.x, marker.y
    label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return _dark_centroid(gray, labels == label, x0, y0)


def _dark_centroid(
    gray: np.ndarray, component: np.ndarray, x0: int, y0: int
) -> tuple[float, float]:
    """The centroid of the darkness (255 − grey) of the scan over
    *component* (a mask of the window at (*x0*, *y0*)) grown by 2 px, in scan
    px (pixel centres at integer coordinates, as the outlines)."""
    grown = cv2.dilate(component.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    h, w = component.shape
    weights = (255.0 - gray[y0 : y0 + h, x0 : x0 + w].astype(np.float64)) * grown
    total = weights.sum()
    ys, xs = np.mgrid[0:h, 0:w]
    return (
        float((weights * xs).sum() / total) + x0,
        float((weights * ys).sum() / total) + y0,
    )


def _reject_incoherent(nodes: list[StripNode], px_per_mm: float) -> list[StripNode]:
    """Discard, one at a time and the worst first, the side marks whose shift
    is off by more than ``MAX_SIDE_DEVIATION_MM`` from the linear
    interpolation of their nearest valid neighbours (the corner nodes are
    always valid: they bound the column)."""
    nodes = list(nodes)
    limit = MAX_SIDE_DEVIATION_MM * px_per_mm
    while True:
        valid = [i for i, n in enumerate(nodes) if n.valid]
        worst, worst_dev = None, limit
        for k in range(1, len(valid) - 1):
            i = valid[k]
            if nodes[i].corner:
                continue
            p, q = nodes[valid[k - 1]], nodes[valid[k + 1]]
            t = (nodes[i].template_y - p.template_y) / (q.template_y - p.template_y)
            ex = p.shift[0] + t * (q.shift[0] - p.shift[0])  # type: ignore[index]
            ey = p.shift[1] + t * (q.shift[1] - p.shift[1])  # type: ignore[index]
            dev = math.hypot(nodes[i].shift[0] - ex, nodes[i].shift[1] - ey)  # type: ignore[index]
            if dev > worst_dev:
                worst, worst_dev = i, dev
        if worst is None:
            return nodes
        n = nodes[worst]
        nodes[worst] = StripNode(n.template_y, n.corner, n.found, n.shift, rejected=True)


def _enough(nodes: tuple[StripNode, ...]) -> bool:
    """At least ``MIN_SIDE_MARKERS`` valid side marks in the column, and no
    more than ``MAX_SIDE_MISSING_RUN`` missing or discarded in a row."""
    side = [n for n in nodes if not n.corner]
    if sum(n.valid for n in side) < MIN_SIDE_MARKERS:
        return False
    run = 0
    for n in side:
        run = 0 if n.valid else run + 1
        if run > MAX_SIDE_MISSING_RUN:
            return False
    return True


def _strip_maps(
    alignment: Alignment,
    strips: StripCorrection,
    out_w: int,
    out_h: int,
) -> tuple[np.ndarray, np.ndarray]:
    """For each output pixel, the scan point to sample (``cv2.remap`` maps):
    output px → template px (÷ scale) → affine map + the interpolated shift."""
    scale = alignment._scale()
    m = np.array(alignment.matrix, dtype=np.float64)
    tx = (np.arange(out_w, dtype=np.float64) / scale)[None, :]
    ty = (np.arange(out_h, dtype=np.float64) / scale)[:, None]

    (xl, nodes_l), (xr, nodes_r) = strips.columns["L"], strips.columns["R"]
    ys_l = np.array([n.template_y for n in nodes_l])
    ys_r = np.array([n.template_y for n in nodes_r])
    shift_l, shift_r = strips.shifts("L"), strips.shifts("R")
    rows = ty[:, 0]
    t = np.clip((tx - xl) / (xr - xl), 0.0, 1.0).astype(np.float32)

    maps = []
    for axis in (0, 1):
        left = np.interp(rows, ys_l, shift_l[:, axis]).astype(np.float32)[:, None]
        right = np.interp(rows, ys_r, shift_r[:, axis]).astype(np.float32)[:, None]
        affine = (m[axis, 0] * tx).astype(np.float32) + (
            m[axis, 1] * ty + m[axis, 2]
        ).astype(np.float32)
        maps.append(affine + left * (1 - t) + right * t)
    return maps[0], maps[1]


# ---------------------------------------------------------------------------
# The straightened image
# ---------------------------------------------------------------------------


def warp_to_template(
    image: np.ndarray,
    alignment: Alignment,
    template_w: float,
    template_h: float,
    strips: StripCorrection | None = None,
) -> np.ndarray:
    """*image* (the scan, grey or RGB) straightened into the template's frame,
    at the scan's resolution (``alignment.output_size``): on it, template
    geometry maps by a single proportional scale. Outside the scan
    is white.

    Nearest-pixel resampling, not bilinear: the extractors count pixels darker
    than a threshold, and averaging a pale, speckled pencil mark with the
    paper between its specks pushes part of it above the threshold — a light
    respondent-ID digit readable on the unaligned scan was lost that way. The
    nearest pixel keeps the scan's grey values as they are.

    With *strips* applied the map is the affine one plus the strip shifts,
    resampled by ``cv2.remap`` (same size, same nearest pixel, same white
    outside); without, the plain ``cv2.warpAffine``."""
    if not alignment.aligned or alignment.matrix is None:
        raise ValueError("the page is not aligned")
    out_w, out_h = alignment.output_size(template_w, template_h)
    white = (255,) * (image.shape[2] if image.ndim == 3 else 1)
    if strips is not None and strips.applied:
        map_x, map_y = _strip_maps(alignment, strips, out_w, out_h)
        return cv2.remap(
            image,
            map_x,
            map_y,
            interpolation=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=white,
        )
    scale = alignment._scale()
    matrix = np.array(alignment.matrix)
    # Output px → template px (÷ scale) → scan px (matrix).
    inverse = np.hstack([matrix[:, :2] / scale, matrix[:, 2:]])
    return cv2.warpAffine(
        image,
        inverse,
        (out_w, out_h),
        flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=white,
    )
