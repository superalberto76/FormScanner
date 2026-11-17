# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alignment marks and reserved bands, in the pixel space of a FormVariantPage.

Every exported page (STUDENT and SOLUTION) prints four alignment marks, one
per corner, inside four reserved bands along the sides of the sheet. Reading
finds them on the scan and aligns the page to the template; authoring keeps
objects out of the bands. All three must agree on the SAME numbers, so they
live here, in one place, like ``question_layout`` — frozen by
``tests/test_export_layout_freeze.py`` and mirrored on the authoring canvas by
``frontend/src/utils/exportLayout.ts`` (``MARKS``).

Coordinates are in the same pixel space as ``PageObject.x/y/width/height`` and
``FormVariantPage.width_px/height_px`` (96 dpi: 1 mm = 96 / 25.4 ≈ 3.78 px)::

    page edge
    ┌──────────────────────────────────────────────┐
    │  ┌──┐                                  ┌──┐  │  ← MARK_INSET from the edges
    │  └──┘                                  └──┘  │
    │      ┌──────────────────────────────┐        │  ← BAND_W from the edges:
    │      │   area where objects go      │        │    objects stay inside
    │      └──────────────────────────────┘        │
    │  ┌──┐                                  ┌▫─┐  │
    │  └──┘                                  └──┘  │  ← BR: the distinct mark
    └──────────────────────────────────────────────┘

The constants assume pages at 96 dpi, as ``add_blank_page`` always creates them
(A4, 794 × 1123 px). The API still accepts other ``dpi`` / ``width_px`` /
``height_px`` values: on such a page the marks would have a different physical
size (19 px at 300 dpi ≈ 1.6 mm). The reserved-band check of authoring and
the alignment of the scan inherit this assumption.

Three marks are solid black squares; the bottom-right one has a white square
hole in its centre, so reading can tell the page's orientation.
Between the corner marks, in their same column, each side prints a column of
:data:`SIDE_MARK_COUNT` side marks — solid black horizontal bars, equally
spaced: reading straightens the page strip by strip on them
(``sheet_alignment.find_strip_correction``). Mirrored by ``SIDE_MARKS`` /
``sideMarkRects`` in ``exportLayout.ts``.
Values chosen from the first real scans: printing at "100%" shrinks the
content by about 3.5% and the scanner loses 6–7 mm of height, so the marks
sit 8–13 mm from the edge.
"""
from __future__ import annotations

# Width of each reserved band along the four sides: 15 mm (56.7 px).
BAND_W = 57
# Distance of each mark from the two page edges of its corner: 8 mm (30.2 px).
MARK_INSET = 30
# Side of a mark (square): 5 mm (18.9 px).
MARK_SIZE = 19
# Side of the white hole in the distinct mark, centred: about 1/3 of MARK_SIZE.
HOLE_SIZE = 7

# Side marks: a column of horizontal bars along
# the left and the right side, between the corner marks, so that reading can
# straighten a sheet the ADF stretched unevenly along its feed.
# Markers per side, between the two corner marks (excluded).
SIDE_MARK_COUNT = 17
# A bar 5 × 2 mm: as wide as a corner mark, and far below its size in height —
# the corner search discards it for its size and its aspect ratio.
SIDE_MARK_W = MARK_SIZE
SIDE_MARK_H = 8

CORNERS = ("TL", "TR", "BL", "BR")
# The mark that differs from the others (bottom-right).
DISTINCT_CORNER = "BR"

Rect = tuple[float, float, float, float]  # x, y, width, height in page px


def mark_rects(page_w_px: float, page_h_px: float) -> dict[str, Rect]:
    """The four marks of a page, by corner (``TL``, ``TR``, ``BL``, ``BR``)."""
    left = MARK_INSET
    top = MARK_INSET
    right = page_w_px - MARK_INSET - MARK_SIZE
    bottom = page_h_px - MARK_INSET - MARK_SIZE
    return {
        "TL": (left, top, MARK_SIZE, MARK_SIZE),
        "TR": (right, top, MARK_SIZE, MARK_SIZE),
        "BL": (left, bottom, MARK_SIZE, MARK_SIZE),
        "BR": (right, bottom, MARK_SIZE, MARK_SIZE),
    }


def hole_rect(mark: Rect) -> Rect:
    """The white hole of the distinct mark, centred in *mark*."""
    x, y, w, h = mark
    offset = (MARK_SIZE - HOLE_SIZE) / 2
    return (x + offset, y + offset, HOLE_SIZE, HOLE_SIZE)


def side_mark_rects(page_w_px: float, page_h_px: float) -> list[dict[str, Rect]]:
    """The side marks of a page: one row per marker, top to bottom (rows
    ``1 … SIDE_MARK_COUNT``), each with the bar of the left (``L``) and the
    right (``R``) column.

    The bars sit in the column of the corner marks (same x and width), centred
    on rows equally spaced between the centre of the top corner mark and that
    of the bottom one: the corner marks are the first and the last node of each
    column. On the default A4 page the step is exactly 58 px (≈ 15.3 mm); on
    other heights the positions are not rounded to the pixel."""
    top = MARK_INSET + MARK_SIZE / 2
    bottom = page_h_px - MARK_INSET - MARK_SIZE / 2
    step = (bottom - top) / (SIDE_MARK_COUNT + 1)
    left = MARK_INSET
    right = page_w_px - MARK_INSET - SIDE_MARK_W
    rows = []
    for i in range(1, SIDE_MARK_COUNT + 1):
        y = top + i * step - SIDE_MARK_H / 2
        rows.append({
            "L": (left, y, SIDE_MARK_W, SIDE_MARK_H),
            "R": (right, y, SIDE_MARK_W, SIDE_MARK_H),
        })
    return rows


def usable_rect(page_w_px: float, page_h_px: float) -> Rect:
    """The area where objects go: the page minus the four reserved bands.
    Mirrored by ``usableRect`` in ``exportLayout.ts``."""
    return (BAND_W, BAND_W, page_w_px - 2 * BAND_W, page_h_px - 2 * BAND_W)


def in_reserved_band(
    x: float, y: float, w: float, h: float, page_w_px: float, page_h_px: float
) -> bool:
    """Whether the rectangle is not wholly inside :func:`usable_rect`, edges
    included: an object at ``x = BAND_W`` is valid. An object
    partly or wholly outside the page counts as in a band too, so "off the
    page" needs no check of its own. Mirrored by ``inReservedBand`` in
    ``exportLayout.ts``."""
    ux, uy, uw, uh = usable_rect(page_w_px, page_h_px)
    return x < ux or y < uy or x + w > ux + uw or y + h > uy + uh
