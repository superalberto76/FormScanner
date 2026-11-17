# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FREEZE TEST — Python side.

Imports the REAL constants from the PDF-export modules and asserts each equals the
value frozen here. The TS twin (frontend/src/utils/exportLayout.test.ts) freezes
the SAME numbers from frontend/src/utils/exportLayout.ts, which the authoring
canvas uses to draw the "faithful" preview. If the export and the canvas drift,
exactly one side's freeze test fails — catching silent divergence.

⚠️  When a value changes, change BOTH sides together: the Python symbol and
    its exportLayout.ts twin.

Intentionally DB-free: it only imports module-level constants + the pure
``fitz_font`` helper (no Postgres, no app wiring). It does require PyMuPDF, like
the export code it guards.
"""
from app.utils import alignment_marks, code_rendering, pdf_rendering
from app.services import form_variant_export_service as export_svc


# --- colours (pdf_rendering) → exportLayout.COLORS ---------------------------
def test_colors_frozen():
    # 0.45 * 255 ≈ 115 → rgb(115,115,115); 0.72 * 255 ≈ 184 → rgb(184,184,184)
    assert pdf_rendering.BLACK == (0.0, 0.0, 0.0)   # COLORS.BLACK
    assert pdf_rendering.GRAY == (0.45, 0.45, 0.45)  # COLORS.GRAY
    assert pdf_rendering.LIGHT == (0.72, 0.72, 0.72)  # COLORS.LIGHT
    assert pdf_rendering.WHITE == (1.0, 1.0, 1.0)  # COLORS.WHITE (hole of the distinct mark)


# --- page metrics defaults (pdf_rendering) -----------------------------------
def test_page_defaults_frozen():
    assert pdf_rendering.DEFAULT_DPI == 96.0
    assert pdf_rendering.DEFAULT_PAGE_WIDTH_PX == 794
    assert pdf_rendering.DEFAULT_PAGE_HEIGHT_PX == 1123


# --- font detection (pdf_rendering.fitz_font) → exportLayout.cssFontStack -----
def test_fitz_font_buckets_frozen():
    f = pdf_rendering.fitz_font
    # "cour"/"mono" → Courier (cssFontStack → '"Courier New", monospace')
    assert f(False, False, "Courier New") == "cour"
    assert f(False, False, "Roboto Mono") == "cour"
    # "times"/"serif" → Times (cssFontStack → '"Times New Roman", serif')
    assert f(False, False, "Times New Roman") == "tiro"
    assert f(False, False, "Noto Serif") == "tiro"
    # default → Helvetica (cssFontStack → 'Arial, "Liberation Sans", sans-serif')
    assert f(False, False, "Arial") == "helv"
    assert f(False, False, None) == "helv"
    # bold/italic variants of the default bucket (freezes the style table)
    assert f(True, False, None) == "hebo"
    assert f(False, True, None) == "heit"
    assert f(True, True, None) == "hebi"


# --- QUESTION (form_variant_export_service) → exportLayout.Q ------------------
def test_question_constants_frozen():
    assert export_svc._Q_HEADER_H == 18   # Q.HEADER_H
    assert export_svc._Q_OPTION_H == 18   # Q.OPTION_H
    assert export_svc._Q_CIRCLE_R == 5    # Q.CIRCLE_R
    assert export_svc._Q_CIRCLE_CX == 9   # Q.CIRCLE_CX
    assert export_svc._Q_TEXT_X == 22     # Q.TEXT_X
    # header size 12 bold / option size 11 are inline literals in _render_question;
    # see exportLayout Q.HEADER_SIZE / Q.OPTION_SIZE.


# --- MATRIX (form_variant_export_service) → exportLayout.M --------------------
def test_matrix_constants_frozen():
    assert export_svc._M_LABEL_H == 16    # M.LABEL_H
    assert export_svc._M_HEADER_H == 16   # M.HEADER_H
    # MATRIX cell radius reuses the QUESTION option radius (M.CIRCLE_R == Q.CIRCLE_R).
    assert export_svc._Q_CIRCLE_R == 5    # M.CIRCLE_R
    # label_w = min(width*0.25, 120); sizes 12/9/9 are inline literals in
    # _render_matrix; see exportLayout M.*.


# --- FREE_TEXT_AREA (form_variant_export_service) → exportLayout.FT -----------
def test_free_text_constants_frozen():
    assert export_svc._FT_LABEL_H == 14   # FT.LABEL_H
    # label size 10 bold / placeholder size 9 italic / pad (x+4, y+3) / min(box_h,16)
    # are inline literals in _render_free_text; see exportLayout FT.*.


# --- CODE (code_rendering) → exportLayout.CODE -------------------------------
def test_code_constants_frozen():
    assert code_rendering._HUMAN_READABLE_STRIP_PX == 14  # CODE.HUMAN_READABLE_STRIP_PX
    # placeholder text size 8 at (x+3, y+3) is an inline literal in render_code;
    # see exportLayout CODE.TEXT_SIZE / CODE.PAD.


# --- alignment marks (alignment_marks) → exportLayout.MARKS -----------------
def test_alignment_marks_frozen():
    assert alignment_marks.BAND_W == 57      # MARKS.BAND_W      (15 mm)
    assert alignment_marks.MARK_INSET == 30  # MARKS.INSET       (8 mm)
    assert alignment_marks.MARK_SIZE == 19   # MARKS.SIZE        (5 mm)
    assert alignment_marks.HOLE_SIZE == 7    # MARKS.HOLE_SIZE
    assert alignment_marks.DISTINCT_CORNER == "BR"  # MARKS.DISTINCT_CORNER
    # positions on the default A4 page (markRects(794, 1123) on the TS side)
    assert alignment_marks.mark_rects(794, 1123) == {
        "TL": (30, 30, 19, 19),
        "TR": (745, 30, 19, 19),
        "BL": (30, 1074, 19, 19),
        "BR": (745, 1074, 19, 19),
    }
    assert alignment_marks.hole_rect((745, 1074, 19, 19)) == (751.0, 1080.0, 7, 7)

    # area outside the reserved bands (usableRect(794, 1123) on the TS side)
    assert alignment_marks.usable_rect(794, 1123) == (57, 57, 680, 1009)


# --- side marks (alignment_marks) → exportLayout.SIDE_MARKS -----------------
def test_side_marks_frozen():
    assert alignment_marks.SIDE_MARK_COUNT == 17  # SIDE_MARKS.COUNT
    assert alignment_marks.SIDE_MARK_W == 19      # SIDE_MARKS.W  (5 mm)
    assert alignment_marks.SIDE_MARK_H == 8       # SIDE_MARKS.H  (2 mm)
    # positions on the default A4 page (sideMarkRects(794, 1123) on the TS side):
    # centres from 39.5 + 58 to 1083.5 - 58, step 58 px
    rows = alignment_marks.side_mark_rects(794, 1123)
    assert len(rows) == 17
    assert rows[0] == {"L": (30, 93.5, 19, 8), "R": (745, 93.5, 19, 8)}
    assert rows[8] == {"L": (30, 557.5, 19, 8), "R": (745, 557.5, 19, 8)}
    assert rows[16] == {"L": (30, 1021.5, 19, 8), "R": (745, 1021.5, 19, 8)}
    assert [r["L"][1] for r in rows] == [93.5 + 58 * i for i in range(17)]
