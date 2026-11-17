# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Low-level PDF drawing helpers built on PyMuPDF (fitz).

Coordinate model
----------------
PageObject geometry (x, y, width, height) and the page's width_px/height_px are
all interpreted in the SAME pixel space. We convert that pixel space into PDF
points with ``scale = 72 / dpi`` (PDF user space is 72 points / inch). Font
sizes are likewise treated as pixel sizes and scaled, so text stays proportional
to the boxes it sits in. The origin is the top-left corner (matching both the
editor and fitz's default page coordinate system).
"""
from __future__ import annotations

import fitz

# --- defaults --------------------------------------------------------------
DEFAULT_DPI = 96.0
# A4 portrait in pixels @ 96 DPI (8.27in x 11.69in) — matches the default size
# of a newly provisioned blank page.
DEFAULT_PAGE_WIDTH_PX = 794
DEFAULT_PAGE_HEIGHT_PX = 1123

# --- colours (fitz uses 0..1 RGB tuples) -----------------------------------
BLACK = (0.0, 0.0, 0.0)
GRAY = (0.45, 0.45, 0.45)
LIGHT = (0.72, 0.72, 0.72)
# Only the hole of the distinct alignment mark (app.utils.alignment_marks).
WHITE = (1.0, 1.0, 1.0)

# fitz text alignment constants (re-exported for callers)
ALIGN_LEFT = fitz.TEXT_ALIGN_LEFT
ALIGN_CENTER = fitz.TEXT_ALIGN_CENTER
ALIGN_RIGHT = fitz.TEXT_ALIGN_RIGHT


def page_metrics(width_px, height_px, dpi) -> tuple[float, float, float]:
    """Return (page_width_pts, page_height_pts, scale) for a FormVariantPage.

    Falls back to A4 @ 96 DPI when the page lacks geometry.
    """
    eff_dpi = float(dpi) if dpi else DEFAULT_DPI
    scale = 72.0 / eff_dpi
    w_px = width_px or DEFAULT_PAGE_WIDTH_PX
    h_px = height_px or DEFAULT_PAGE_HEIGHT_PX
    return w_px * scale, h_px * scale, scale


def fitz_font(bold: bool, italic: bool, family: str | None) -> str:
    """Map (bold, italic, family) to a PyMuPDF Base-14 font shortcut."""
    fam = (family or "").lower()
    if "cour" in fam or "mono" in fam:
        table = {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"}
    elif "times" in fam or "serif" in fam:
        table = {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"}
    else:  # default Helvetica family
        table = {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"}
    return table[(1 if bold else 0, 1 if italic else 0)]


class PageRenderer:
    """Thin wrapper around one fitz.Page that works in pixel coordinates."""

    def __init__(self, page: "fitz.Page", scale: float) -> None:
        self.page = page
        self.s = scale

    # -- geometry --
    def rect(self, x: float, y: float, w: float, h: float) -> "fitz.Rect":
        s = self.s
        return fitz.Rect(x * s, y * s, (x + w) * s, (y + h) * s)

    def point(self, x: float, y: float) -> "fitz.Point":
        return fitz.Point(x * self.s, y * self.s)

    # -- primitives (inputs in pixel space) --
    def box(self, x, y, w, h, *, color=BLACK, width=0.8, fill=None) -> None:
        self.page.draw_rect(self.rect(x, y, w, h), color=color, width=width, fill=fill)

    def line(self, x1, y1, x2, y2, *, color=LIGHT, width=0.6) -> None:
        self.page.draw_line(self.point(x1, y1), self.point(x2, y2), color=color, width=width)

    def circle(self, cx, cy, r, *, color=BLACK, width=0.8, fill=None) -> None:
        self.page.draw_circle(self.point(cx, cy), r * self.s, color=color, width=width, fill=fill)

    def text(
        self, x, y, w, h, text, px_size, *,
        bold=False, italic=False, family=None, color=BLACK, align=ALIGN_LEFT,
    ) -> None:
        """Insert wrapped, clipped text inside a pixel-space box.

        ``insert_textbox`` is all-or-nothing: if the text does not fit the box
        it writes NOTHING and returns a negative overflow value. A box only
        slightly shorter than one line height (e.g. an 18px header at 96 DPI)
        therefore makes the text silently disappear. To avoid that, shrink the
        font until the text fits; if it still cannot fit at the floor size,
        draw it anyway so the value is never invisible.
        """
        if not text:
            return
        rect = self.rect(x, y, w, h)
        fontname = fitz_font(bold, italic, family)
        fontsize = max(px_size * self.s, 1.0)
        _FLOOR = 1.0
        while fontsize > _FLOOR:
            overflow = self.page.insert_textbox(
                rect, str(text), fontsize=fontsize, fontname=fontname,
                color=color, align=align,
            )
            if overflow >= 0:
                return
            fontsize = max(fontsize * 0.9, _FLOOR)
        # Floor reached: draw at the minimum size even if it overflows.
        self.page.insert_textbox(
            rect, str(text), fontsize=_FLOOR, fontname=fontname,
            color=color, align=align,
        )
