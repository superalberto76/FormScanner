# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MATRIX grid geometry, in the pixel space of a FormVariantPage.

The MATRIX twin of :mod:`app.utils.question_layout`, and it exists for exactly
the same reason: extraction has to look for a blackened circle precisely where
the export printed an empty one. Where a QUESTION needed only five constants,
a MATRIX needs an ARITHMETIC — the label column, the header band and the cell
size all depend on the object's own width and height — so what is shared here
is not just the two numbers but the computation that turns them into a grid.

Reproducing that computation by hand in the extractor would be the classic way
to read blank sheets as answered forever after somebody nudged the label
column: the export (``form_variant_export_service._render_matrix``) and the
extractor both call :func:`compute_grid_geometry` and can only move together.

Coordinates are relative to nothing — they are absolute in the page's pixel
space, the same space as ``PageObject.x/y/width/height`` and
``FormVariantPage.width_px/height_px`` (see ``app.utils.pdf_rendering``).
Layout of one MATRIX, top to bottom::

    obj.y      ┌──────────┬─────────┬─────────┐  ┐ LABEL_H
               │ label (bold, 12px) │         │  ┘
               ├──────────┼─────────┼─────────┤  ┐ HEADER_H
               │          │  col 0  │  col 1  │  ┘
    grid_y     ├──────────┼─────────┼─────────┤  ┐
               │  row 0   │   (o)   │   (o)   │  │ cell_h
               ├──────────┼─────────┼─────────┤  ┘
               │  row 1   │   (o)   │   (o)   │
               └──────────┴─────────┴─────────┘
               ↑          ↑
               obj.x      grid_x = obj.x + label_w
                          └─ cell_w ─┘

The bubble drawn in each cell reuses ``question_layout.CIRCLE_R``: it IS the
QUESTION option circle, printed by the same call, and a MATRIX-specific radius
would be a new number saying the same thing twice.

The values are frozen by ``tests/test_export_layout_freeze.py`` and mirrored on
the authoring canvas by ``frontend/src/utils/exportLayout.ts`` — see that
test's header before changing any of them.
"""
from __future__ import annotations

from dataclasses import dataclass

# Height of the object-label band above the column headers.
LABEL_H = 16
# Height of the column-header band above the first row of cells.
HEADER_H = 16


@dataclass(frozen=True)
class MatrixGridGeometry:
    """Where one MATRIX' grid and cells are, in page pixel space."""

    # Width of the left column carrying the row labels.
    label_w: float
    # Top-left corner of the CELL area — past the labels and the headers.
    grid_x: float
    grid_y: float
    # Size of one cell. Every cell of a grid has the same one.
    cell_w: float
    cell_h: float


def compute_grid_geometry(
    obj_x: float,
    obj_y: float,
    obj_width: float,
    obj_height: float,
    rows: int,
    cols: int,
) -> MatrixGridGeometry:
    """The grid of a MATRIX object of the given geometry.

    The same arithmetic ``_render_matrix`` has always used, moved here because
    extraction has to find the cells exactly where the rendering printed them.

    The label column takes a quarter of the object's width, capped at 120px so
    a wide matrix does not spend half the sheet on labels. The remaining width
    and the height left under the header band are divided EQUALLY among the
    columns and rows — both floored at 1.0px, which is what keeps a matrix
    authored smaller than its own chrome from producing a negative cell size
    instead of a squashed one.

    Pure arithmetic: *rows* and *cols* must both be positive (the caller
    handles the degenerate matrix — the export draws nothing, the extractor
    reads nothing).
    """
    label_w = min(obj_width * 0.25, 120.0)
    grid_x = obj_x + label_w
    grid_y = obj_y + LABEL_H + HEADER_H
    grid_w = max(obj_width - label_w, 1.0)
    grid_h = max((obj_y + obj_height) - grid_y, 1.0)
    return MatrixGridGeometry(
        label_w=label_w,
        grid_x=grid_x,
        grid_y=grid_y,
        cell_w=grid_w / cols,
        cell_h=grid_h / rows,
    )


def cell_center(
    geometry: MatrixGridGeometry, row: int, col: int
) -> tuple[float, float]:
    """Centre of the (*row*, *col*) cell — where its bubble is printed."""
    return (
        geometry.grid_x + col * geometry.cell_w + geometry.cell_w / 2,
        geometry.grid_y + row * geometry.cell_h + geometry.cell_h / 2,
    )
