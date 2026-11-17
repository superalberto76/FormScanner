# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alignment marks in the PDF export.

Every page of the STUDENT and SOLUTION exports prints four marks at the
positions of ``alignment_marks.mark_rects``, the bottom-right one with a white
hole, and the side marks of ``alignment_marks.side_mark_rects``, all drawn
AFTER the objects; the objects themselves are drawn exactly as without the
marks. Nothing can be saved in the 15 mm bands, so the fixture adds to
COLL-MULTI A an object under the top-left mark straight through the ORM, as a
row written without that rule would have it: the export still succeeds and
the marks are whole.
"""
from __future__ import annotations

import fitz
import pytest

from app.models.enums import PageObjectType
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.page_object import PageObject
from app.models.user import User
from app.services import form_variant_export_service as export_svc
from app.services.form_variant_export_service import ExportMode, export_pdf
from app.utils import alignment_marks
from app.utils.pdf_rendering import DEFAULT_PAGE_HEIGHT_PX, DEFAULT_PAGE_WIDTH_PX
from tests.synthetic.builders import build_coll_multi

PX_TO_PT = 72 / 96
TOL_PT = 0.01
# Drawings of the marks per page: four corner marks, the hole, the side marks.
SIDE_COUNT = 2 * alignment_marks.SIDE_MARK_COUNT
MARK_DRAWINGS = 5 + SIDE_COUNT


def _filled_rects(page: fitz.Page) -> list[tuple[fitz.Rect, tuple]]:
    """Filled rectangles without a stroke: the marks and their hole."""
    out = []
    for d in page.get_drawings():
        items = d["items"]
        if d.get("fill") is not None and d.get("color") is None and items and all(
            i[0] == "re" for i in items
        ):
            out.append((d["rect"], tuple(round(c, 3) for c in d["fill"])))
    return out


def _expected_pt(rect_px) -> fitz.Rect:
    x, y, w, h = rect_px
    return fitz.Rect(x, y, x + w, y + h) * PX_TO_PT


def _same(a: fitz.Rect, b: fitz.Rect) -> bool:
    return all(abs(p - q) <= TOL_PT for p, q in zip(a, b))


def _drawing_key(d: dict) -> tuple:
    return (
        tuple((i[0], *(tuple(round(v, 3) for v in p) for p in i[1:] if hasattr(p, "__iter__"))) for i in d["items"]),
        d.get("color"),
        d.get("fill"),
        d.get("width"),
    )


# Under the top-left mark (30..49 px on both axes) and partly outside the band.
BAND_OBJECT_RECT = (20.0, 20.0, 150.0, 60.0)


@pytest.fixture
def project(db):
    """COLL-MULTI with a visible FREE_TEXT_AREA in the band of page 1 of A,
    written through the ORM: the services refuse it."""
    coll = build_coll_multi(db)
    x, y, w, h = BAND_OBJECT_RECT
    obj = PageObject(
        form_variant_page_id=coll.variants["A"].page_ids[0],
        object_type=PageObjectType.FREE_TEXT_AREA,
        x=x, y=y, width=w, height=h, visible=True,  # no label: the frame is the rect
    )
    db.add(obj)
    db.flush()
    db.add(FreeTextAreaConfig(page_object_id=obj.id))
    db.flush()
    return coll


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def test_fixture_has_an_object_under_a_mark():
    """Precondition of the "object in the band" case: the object the fixture
    adds sits under a mark and in the reserved band."""
    rects = alignment_marks.mark_rects(DEFAULT_PAGE_WIDTH_PX, DEFAULT_PAGE_HEIGHT_PX)
    assert any(_overlaps(BAND_OBJECT_RECT, r) for r in rects.values())
    assert alignment_marks.in_reserved_band(
        *BAND_OBJECT_RECT, DEFAULT_PAGE_WIDTH_PX, DEFAULT_PAGE_HEIGHT_PX
    )


def test_band_object_is_drawn_under_the_marks(db, project):
    """The object in the band is printed (its frame is among page 1's drawings)
    and the drawings of the marks come after it, whole."""
    owner = db.get(User, project.owner_id)
    pdf, _ = export_pdf(db, project.variants["A"].variant_id, owner, ExportMode.STUDENT)
    x, y, w, h = BAND_OBJECT_RECT
    frame = _expected_pt((x, y, w, h))
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        drawings = doc[0].get_drawings()
        first_mark = len(drawings) - MARK_DRAWINGS
        assert any(
            _same(d["rect"], frame) for d in drawings[:first_mark]
        ), "the band object's frame is not drawn"


@pytest.mark.parametrize("mode", [ExportMode.STUDENT, ExportMode.SOLUTION])
def test_every_page_has_the_four_marks(db, project, mode):
    owner = db.get(User, project.owner_id)
    pdf, _ = export_pdf(db, project.variants["A"].variant_id, owner, mode)
    rects = alignment_marks.mark_rects(DEFAULT_PAGE_WIDTH_PX, DEFAULT_PAGE_HEIGHT_PX)
    hole = _expected_pt(alignment_marks.hole_rect(rects[alignment_marks.DISTINCT_CORNER]))
    side = [
        _expected_pt(rect)
        for row in alignment_marks.side_mark_rects(DEFAULT_PAGE_WIDTH_PX, DEFAULT_PAGE_HEIGHT_PX)
        for rect in (row["L"], row["R"])
    ]

    with fitz.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count == 3
        for page in doc:
            filled = _filled_rects(page)
            black = [r for r, fill in filled if fill == (0.0, 0.0, 0.0)]
            white = [r for r, fill in filled if fill == (1.0, 1.0, 1.0)]
            assert len(black) == 4 + SIDE_COUNT, f"page {page.number + 1}: {black}"
            for corner, rect in rects.items():
                assert any(_same(b, _expected_pt(rect)) for b in black), (
                    f"page {page.number + 1}: no mark at {corner}"
                )
            # every side mark, left and right
            for rect in side:
                assert any(_same(b, rect) for b in black), (
                    f"page {page.number + 1}: no side mark at {rect}"
                )
            # Only the bottom-right mark has the hole.
            assert len(white) == 1 and _same(white[0], hole)


def test_marks_sit_inside_the_band_and_the_page():
    w, h = DEFAULT_PAGE_WIDTH_PX, DEFAULT_PAGE_HEIGHT_PX
    band = alignment_marks.BAND_W
    for corner, (x, y, mw, mh) in alignment_marks.mark_rects(w, h).items():
        assert mw == mh == alignment_marks.MARK_SIZE
        # within the band on both axes of its corner, never on the page edge
        assert x >= alignment_marks.MARK_INSET and x + mw <= w - alignment_marks.MARK_INSET
        assert y >= alignment_marks.MARK_INSET and y + mh <= h - alignment_marks.MARK_INSET
        assert x + mw <= band or x >= w - band, corner
        assert y + mh <= band or y >= h - band, corner


def test_side_marks_sit_in_the_column_of_the_corner_marks_inside_the_band():
    """In the corner marks' column, inside the band, between
    the corner marks, never touching them nor each other."""
    w, h = DEFAULT_PAGE_WIDTH_PX, DEFAULT_PAGE_HEIGHT_PX
    corners = alignment_marks.mark_rects(w, h)
    rows = alignment_marks.side_mark_rects(w, h)
    for column, (top, bottom) in (("L", ("TL", "BL")), ("R", ("TR", "BR"))):
        rects = [row[column] for row in rows]
        for x, y, mw, mh in rects:
            assert (x, mw) == (corners[top][0], corners[top][2])
            assert x + mw <= alignment_marks.BAND_W or x >= w - alignment_marks.BAND_W
            assert alignment_marks.in_reserved_band(x, y, mw, mh, w, h)
        tops = [r[1] for r in rects]
        assert tops[0] > corners[top][1] + corners[top][3]
        assert tops[-1] + alignment_marks.SIDE_MARK_H < corners[bottom][1]
        assert all(b - a > alignment_marks.SIDE_MARK_H for a, b in zip(tops, tops[1:]))


def test_objects_are_drawn_exactly_as_without_the_marks(db, project, monkeypatch):
    owner = db.get(User, project.owner_id)
    variant_id = project.variants["A"].variant_id
    with_marks, _ = export_pdf(db, variant_id, owner, ExportMode.SOLUTION)
    monkeypatch.setattr(export_svc, "_render_alignment_marks", lambda *a, **k: None)
    without, _ = export_pdf(db, variant_id, owner, ExportMode.SOLUTION)

    with fitz.open(stream=with_marks, filetype="pdf") as a, fitz.open(stream=without, filetype="pdf") as b:
        for pa, pb in zip(a, b):
            da, db_ = pa.get_drawings(), pb.get_drawings()
            # The marks are drawn last: the objects' drawings come first, unchanged.
            assert [_drawing_key(d) for d in da[: len(db_)]] == [_drawing_key(d) for d in db_]
            assert len(da) - len(db_) == MARK_DRAWINGS  # marks, hole, side marks
            assert pa.get_text("words") == pb.get_text("words")
            assert len(pa.get_images()) == len(pb.get_images())
