# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the exported PDF actually printed each bubble and box.

The synthetic-sheet generator must NOT know the export's layout formulas: if it
borrowed the app's QUESTION / MATRIX layout modules (the very constants the
extractor reads with), a drift between export and extraction would be
reproduced on both sides and stay invisible. So positions are read back from
the DRAWING of the exported PDF, with ``page.get_drawings()``, and only
associated with the objects through their DB rectangles.

What the export draws, as verified on a real COLL-MULTI export (PyMuPDF
1.26): every ``draw_circle`` is a SEPARATE path made of four Bézier curves
(``"c"`` items) with a 7.5 pt square bounding rect — 3.75 pt radius, i.e. the
5 px template radius — so no merging has to be undone. A FREE_TEXT_AREA's
answer box is a black ``"re"`` stroke; MATRIX cell borders are grey ``"re"``.

Units: the PDF is in points (72 dpi), object geometry in the DB is in template
pixels (96 dpi). Everything returned here is in POINTS.
"""
from __future__ import annotations

import string
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import fitz
from sqlalchemy.orm import Session

from app.models.enums import PageObjectType
from app.repositories import form_variant_page_repo, page_object_repo

PX_TO_PT = 72 / 96

# A bubble's bounding square: side 7.5 pt (= 2 × 5 px × 72/96), ±1 pt.
BUBBLE_SIDE_PT = 7.5
BUBBLE_SIDE_TOL_PT = 1.0
# Bubble centres closer than this on one axis share a MATRIX row / column.
GRID_TOL_PT = 1.0
# Slack when testing "this drawing lies inside that object's rectangle".
CONTAIN_TOL_PT = 0.5

LETTERS = string.ascii_uppercase

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 in pt
# (label, object_type, (x, y, width, height) in template px, extra) where extra
# is the option count of a QUESTION, (rows, cols) of a MATRIX, else None.
ObjectRow = tuple[str, PageObjectType, tuple[float, float, float, float], object]


@dataclass(frozen=True)
class Bubble:
    cx: float
    cy: float
    r: float


@dataclass(frozen=True)
class ObjectGeometry:
    label: str
    object_type: PageObjectType
    rect: Rect
    # QUESTION: letter → Bubble; MATRIX: (row, col) → Bubble; else empty.
    bubbles: dict[object, Bubble] = field(default_factory=dict)
    # FREE_TEXT_AREA only: the printed answer box.
    text_box: Rect | None = None


@dataclass(frozen=True)
class PageGeometry:
    number: int
    width: float
    height: float
    objects: dict[str, ObjectGeometry]


@dataclass(frozen=True)
class TemplateGeometry:
    variant: str
    pages: dict[int, PageGeometry]

    def find(self, label: str) -> list[tuple[int, ObjectGeometry]]:
        """Every (page_number, geometry) of *label* — several for a shared VID."""
        return [
            (n, page.objects[label])
            for n, page in sorted(self.pages.items())
            if label in page.objects
        ]


# ---------------------------------------------------------------------------
# DB side
# ---------------------------------------------------------------------------


def objects_by_page(db: Session, variant_id: uuid.UUID) -> dict[int, list[ObjectRow]]:
    """The visible objects of each page of the variant, as ``read_template`` wants."""
    result: dict[int, list[ObjectRow]] = {}
    for page in form_variant_page_repo.list_for_variant(db, variant_id):
        rows: list[ObjectRow] = []
        for obj in page_object_repo.list_for_page_with_configs(db, page.id):
            if not obj.visible:
                continue  # the export does not draw it
            extra: object = None
            if obj.object_type is PageObjectType.QUESTION and obj.question_config:
                extra = len(obj.question_config.options)
            elif obj.object_type is PageObjectType.MATRIX and obj.matrix_config:
                extra = (obj.matrix_config.rows_count, obj.matrix_config.cols_count)
            rows.append(
                (obj.label, obj.object_type, (obj.x, obj.y, obj.width, obj.height), extra)
            )
        result[page.page_index] = rows
    return result


# ---------------------------------------------------------------------------
# PDF side
# ---------------------------------------------------------------------------


def read_template(
    pdf_bytes: bytes,
    objects_by_page: Mapping[int, Sequence[ObjectRow]],
    *,
    variant: str = "?",
) -> TemplateGeometry:
    """Bubbles, answer boxes and code rectangles of every page, in pt.

    Raises ``ValueError`` (naming variant, page, label, expected and found) when
    an object does not get exactly the bubbles its config says it prints.
    """
    pages: dict[int, PageGeometry] = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, page in enumerate(doc):
            number = index + 1
            drawings = page.get_drawings()
            bubbles = [_as_bubble(d) for d in drawings]
            bubbles = [b for b in bubbles if b is not None]
            black_rects = [_rect(d["rect"]) for d in drawings if _is_black_rect(d)]

            objects: dict[str, ObjectGeometry] = {}
            for label, otype, rect_px, extra in objects_by_page.get(number, ()):
                rect = _px_rect_to_pt(rect_px)
                where = f"variant {variant}, page {number}, {label}"
                inside = [b for b in bubbles if _contains_point(rect, b.cx, b.cy)]
                if otype is PageObjectType.QUESTION:
                    geom = ObjectGeometry(
                        label, otype, rect, bubbles=_question_bubbles(inside, extra, where)
                    )
                elif otype is PageObjectType.MATRIX:
                    geom = ObjectGeometry(
                        label, otype, rect, bubbles=_matrix_bubbles(inside, extra, where)
                    )
                elif otype is PageObjectType.FREE_TEXT_AREA:
                    boxes = [r for r in black_rects if _contains_rect(rect, r)]
                    if not boxes:
                        raise ValueError(f"{where}: no answer box found")
                    box = max(boxes, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
                    geom = ObjectGeometry(label, otype, rect, text_box=box)
                else:  # CODE (only covered) and anything else: the DB rectangle
                    geom = ObjectGeometry(label, otype, rect)
                objects[label] = geom
            pages[number] = PageGeometry(
                number, page.rect.width, page.rect.height, objects
            )
    return TemplateGeometry(variant, pages)


def _rect(r: fitz.Rect) -> Rect:
    return (r.x0, r.y0, r.x1, r.y1)


def _px_rect_to_pt(rect_px) -> Rect:
    x, y, w, h = rect_px
    return (x * PX_TO_PT, y * PX_TO_PT, (x + w) * PX_TO_PT, (y + h) * PX_TO_PT)


def _as_bubble(drawing: dict) -> Bubble | None:
    items = drawing["items"]
    if not items or any(item[0] != "c" for item in items):
        return None
    r = drawing["rect"]
    if abs(r.width - r.height) > BUBBLE_SIDE_TOL_PT:
        return None
    if abs(r.width - BUBBLE_SIDE_PT) > BUBBLE_SIDE_TOL_PT:
        return None
    return Bubble((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, r.width / 2)


def _is_black_rect(drawing: dict) -> bool:
    items = drawing["items"]
    color = drawing.get("color")
    return (
        bool(items)
        and all(item[0] == "re" for item in items)
        and color is not None
        and all(c < 0.01 for c in color)
    )


def _contains_point(rect: Rect, x: float, y: float) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _contains_rect(outer: Rect, inner: Rect) -> bool:
    t = CONTAIN_TOL_PT
    return (
        inner[0] >= outer[0] - t and inner[1] >= outer[1] - t
        and inner[2] <= outer[2] + t and inner[3] <= outer[3] + t
    )


def _question_bubbles(inside: list[Bubble], option_count, where: str) -> dict:
    if len(inside) != option_count:
        raise ValueError(
            f"{where}: expected {option_count} bubbles, found {len(inside)}"
        )
    ordered = sorted(inside, key=lambda b: b.cy)
    return {LETTERS[i]: b for i, b in enumerate(ordered)}


def _cluster(values: list[float]) -> list[float]:
    """Distinct values, merging those within GRID_TOL_PT of the previous one."""
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= GRID_TOL_PT:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _nearest(centres: list[float], v: float) -> int:
    return min(range(len(centres)), key=lambda i: abs(centres[i] - v))


def _matrix_bubbles(inside: list[Bubble], shape, where: str) -> dict:
    rows, cols = shape
    ys = _cluster([b.cy for b in inside])
    xs = _cluster([b.cx for b in inside])
    cells = {(_nearest(ys, b.cy), _nearest(xs, b.cx)): b for b in inside}
    if len(inside) != rows * cols or len(ys) != rows or len(xs) != cols or len(cells) != rows * cols:
        raise ValueError(
            f"{where}: expected {rows}×{cols} = {rows * cols} bubbles, found "
            f"{len(inside)} in {len(ys)} rows × {len(xs)} columns"
        )
    return cells
