# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FormVariant PDF export service.

Renders a FormVariant to a single PDF using PyMuPDF (fitz):
  - one FormVariantPage -> one PDF page
  - PageObjects positioned absolutely, ordered by (z_index, created_at)
  - only ``visible`` objects are drawn; ``locked`` is editor-only and ignored
  - two modes: STUDENT (blank answers) and SOLUTION (correct answers marked)

Authorization: owner or admin (read-only). Any status is exportable, including
DRAFT — publishing is NOT required for export.
"""
from __future__ import annotations

import logging
import re
import uuid
from enum import Enum

import fitz
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import CodeEncoding, CodeType, PageObjectRole, PageObjectType
from app.models.user import User
from app.repositories import form_variant_export_repo
from app.services import storage_service
from app.services.form_project_service import require_project_member
from app.utils import alignment_marks, code_rendering, matrix_layout
from app.utils.pdf_rendering import (
    ALIGN_CENTER,
    BLACK,
    GRAY,
    LIGHT,
    WHITE,
    PageRenderer,
    page_metrics,
)
# QUESTION layout constants (pixel space). Defined in app.utils.question_layout,
# NOT here: extraction has to sample the bubbles exactly where this module
# prints them, and a second copy of these numbers would drift. Imported under
# private ``_Q_*`` aliases, which every use site below — and the freeze test
# that guards them — refers to.
from app.utils.question_layout import (
    CIRCLE_CX as _Q_CIRCLE_CX,
    CIRCLE_R as _Q_CIRCLE_R,
    HEADER_H as _Q_HEADER_H,
    OPTION_H as _Q_OPTION_H,
    TEXT_X as _Q_TEXT_X,
)
# MATRIX layout, same reason: the two band heights AND the grid arithmetic that
# uses them live in app.utils.matrix_layout, imported here under private
# ``_M_*`` aliases.
from app.utils.matrix_layout import (
    HEADER_H as _M_HEADER_H,
    LABEL_H as _M_LABEL_H,
)

logger = logging.getLogger(__name__)


class ExportMode(str, Enum):
    STUDENT = "STUDENT"
    SOLUTION = "SOLUTION"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def export_pdf(
    db: Session,
    variant_id: uuid.UUID,
    user: User,
    mode: ExportMode,
    filename: str | None = None,
) -> tuple[bytes, str]:
    """Render the variant to PDF bytes; returns (pdf_bytes, download_filename)."""
    variant = _load_authorized(db, variant_id, user)

    doc = fitz.open()
    try:
        for page in sorted(variant.pages, key=lambda p: p.page_index):
            _render_page(doc, page, mode)
        if doc.page_count == 0:
            # A valid PDF must have at least one page.
            w_pts, h_pts, _ = page_metrics(None, None, None)
            doc.new_page(width=w_pts, height=h_pts)
        pdf_bytes = doc.tobytes()
    finally:
        doc.close()

    return pdf_bytes, _resolve_filename(variant.root_key, mode, filename)


def get_preview_metadata(
    db: Session, variant_id: uuid.UUID, user: User
) -> dict:
    """Cheap render metadata for debugging (no PDF produced)."""
    variant = _load_authorized(db, variant_id, user)
    pages = []
    for page in sorted(variant.pages, key=lambda p: p.page_index):
        w_pts, h_pts, scale = page_metrics(page.width_px, page.height_px, page.dpi)
        type_counts: dict[str, int] = {}
        visible = 0
        for obj in page.page_objects:
            type_counts[obj.object_type.value] = (
                type_counts.get(obj.object_type.value, 0) + 1
            )
            if obj.visible:
                visible += 1
        pages.append(
            {
                "page_index": page.page_index,
                "width_px": page.width_px,
                "height_px": page.height_px,
                "dpi": page.dpi,
                "pdf_width_pts": round(w_pts, 2),
                "pdf_height_pts": round(h_pts, 2),
                "scale": round(scale, 4),
                "object_count": len(page.page_objects),
                "visible_object_count": visible,
                "object_type_counts": type_counts,
            }
        )
    return {
        "variant_id": str(variant.id),
        "root_key": variant.root_key,
        "name": variant.name,
        "status": variant.status.value,
        "page_count": len(variant.pages),
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_authorized(db: Session, variant_id: uuid.UUID, user: User):
    variant = form_variant_export_repo.get_variant_for_export(db, variant_id)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form variant not found"
        )
    # Member/admin gate via the parent project (raises 404/403). Export of both
    # STUDENT and SOLUTION modes is allowed for any member.
    # NO project-state gate by design: export is available in EVERY project state
    # (DRAFT/ACTIVE/CLOSED/ARCHIVED) — none of the lifecycle asserts apply here.
    require_project_member(db, variant.form_project_id, user)
    return variant


def _resolve_filename(root_key: str, mode: ExportMode, override: str | None) -> str:
    if override:
        base = override
    else:
        base = f"{root_key}_{mode.value.lower()}"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "export"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def _render_page(doc: "fitz.Document", page, mode: ExportMode) -> None:
    w_pts, h_pts, scale = page_metrics(page.width_px, page.height_px, page.dpi)
    fpage = doc.new_page(width=w_pts, height=h_pts)
    renderer = PageRenderer(fpage, scale)

    objects = sorted(
        (o for o in page.page_objects if o.visible),
        key=lambda o: (o.z_index, o.created_at),
    )
    for obj in objects:
        _render_object(renderer, obj, mode, page.page_index)

    # After the objects, so a mark stays whole even when an object sits in the
    # reserved band.
    _render_alignment_marks(renderer, w_pts / scale, h_pts / scale)


def _render_alignment_marks(r: PageRenderer, page_w_px: float, page_h_px: float) -> None:
    """The four alignment marks of the page, in both modes, then the side
    marks, left and right row by row."""
    for corner, (x, y, w, h) in alignment_marks.mark_rects(page_w_px, page_h_px).items():
        r.page.draw_rect(r.rect(x, y, w, h), color=None, fill=BLACK, width=0)
        if corner == alignment_marks.DISTINCT_CORNER:
            hx, hy, hw, hh = alignment_marks.hole_rect((x, y, w, h))
            r.page.draw_rect(r.rect(hx, hy, hw, hh), color=None, fill=WHITE, width=0)
    for row in alignment_marks.side_mark_rects(page_w_px, page_h_px):
        for x, y, w, h in (row["L"], row["R"]):
            r.page.draw_rect(r.rect(x, y, w, h), color=None, fill=BLACK, width=0)


def _render_object(
    r: PageRenderer, obj, mode: ExportMode, page_index: int
) -> None:
    t = obj.object_type
    if t is PageObjectType.TEXT:
        _render_text(r, obj)
    elif t is PageObjectType.CODE:
        _render_code(r, obj, page_index)
    elif t is PageObjectType.FREE_TEXT_AREA:
        _render_free_text(r, obj)
    elif t is PageObjectType.QUESTION:
        _render_question(r, obj, mode)
    elif t is PageObjectType.MATRIX:
        _render_matrix(r, obj, mode)
    elif t is PageObjectType.IMAGE:
        _render_image(r, obj)  # identical in STUDENT and SOLUTION


def _render_text(r: PageRenderer, obj) -> None:
    cfg = obj.text_config
    if cfg is None:
        return
    r.text(
        obj.x, obj.y, obj.width, obj.height,
        cfg.text, cfg.font_size,
        bold=cfg.bold, italic=cfg.italic, family=cfg.font_family,
    )


def _render_code(r: PageRenderer, obj, page_index: int) -> None:
    cfg = obj.code_config
    if cfg is None:
        return
    # Only codes opted into "include page number" carry the page index; every
    # other code renders without it. The renderer reads the flag off this
    # page's local row (kept in sync by resync) — no group query.
    effective_page_index = page_index if cfg.include_page_index else None
    # The object's special role (if any) is encoded as a self-describing tag in
    # the payload, so a scanner can tell VARIANT_IDENTIFIER from
    # RESPONDENT_IDENTIFIER without first knowing which FormVariant it is
    # looking at. A "show on every page" code carries its role on ONE row only,
    # yet every page copy must be tagged — VARIANT_IDENTIFIER is required to be
    # shared precisely so a scanner finds it on whatever sheet it is handed —
    # so the role is the linked group's (``effective_role``, the rule
    # extraction and review apply too). Eager-loaded by the export repo.
    role = obj.effective_role
    code_rendering.render_code(
        r, obj.x, obj.y, obj.width, obj.height,
        code_type=cfg.code_type, value=cfg.value, human_readable=cfg.human_readable,
        page_index=effective_page_index,
        role=role,
        encoding=_barcode_encoding(obj, cfg, role, effective_page_index),
    )


def _barcode_encoding(
    obj, cfg, role: PageObjectRole | None, page_index: int | None
) -> CodeEncoding | None:
    """The symbology a BARCODE is printed in; None for a QR.

    Authoring guarantees that the value fits the chosen encoding (422) and that
    a barcode holding a role is CODE128 (409). A row that breaks those rules —
    an encoding the payload cannot be encoded in, or a free-text encoding hint
    that is not a ``CodeEncoding`` — is printed as Code128 and logged. The
    check runs on the payload actually encoded, so a role tag or page index
    counts.
    """
    if cfg.code_type is not CodeType.BARCODE or cfg.encoding is None:
        return None
    try:
        encoding = CodeEncoding(cfg.encoding)
    except ValueError:
        problem = f"unknown encoding {cfg.encoding!r}"
    else:
        payload = code_rendering.build_tagged_payload(role, cfg.value, page_index)
        problem = code_rendering.barcode_value_error(encoding, payload)
        if problem is None:
            return encoding
    logger.warning(
        "CODE %s cannot be printed in its encoding (%s); falling back to CODE128",
        obj.id, problem,
    )
    return CodeEncoding.CODE128


def _render_image(r: PageRenderer, obj) -> None:
    """Embed an uploaded image at the object's geometry.

    fit="contain" keeps the aspect ratio inside the box (default); fit="cover"
    fills the box (stretching if needed). On any problem — no file yet, missing
    binary, unembeddable content — a neutral placeholder box is drawn so the
    export never fails for one object.
    """
    cfg = obj.image_config
    if cfg is None or not cfg.file_path:
        _render_image_placeholder(r, obj)
        return
    try:
        data = storage_service.read_object_image(cfg.file_path)
    except (ValueError, OSError):
        _render_image_placeholder(r, obj)
        return

    keep = cfg.fit != "cover"  # contain → preserve proportion; cover → fill
    rect = r.rect(obj.x, obj.y, obj.width, obj.height)
    is_svg = (cfg.mime_type == "image/svg+xml") or cfg.file_path.lower().endswith(".svg")
    try:
        if is_svg:
            _embed_svg(r, rect, data, keep)
        else:
            r.page.insert_image(rect, stream=data, keep_proportion=keep)
    except Exception:  # noqa: BLE001 — any embed failure falls back to a box
        _render_image_placeholder(r, obj)


def _embed_svg(r: PageRenderer, rect, data: bytes, keep: bool) -> None:
    """Render an (already-sanitized) SVG into *rect* via a temporary PDF page."""
    svg_doc = fitz.open(stream=data, filetype="svg")
    try:
        pdf_bytes = svg_doc.convert_to_pdf()
    finally:
        svg_doc.close()
    src = fitz.open("pdf", pdf_bytes)
    try:
        r.page.show_pdf_page(rect, src, 0, keep_proportion=keep)
    finally:
        src.close()


def _render_image_placeholder(r: PageRenderer, obj) -> None:
    r.box(obj.x, obj.y, obj.width, obj.height, color=GRAY, width=0.8)
    label = obj.label or "image"
    r.text(
        obj.x + 2, obj.y + 2, max(obj.width - 4, 1), min(obj.height, 16),
        label, 9, color=GRAY,
    )


# FREE_TEXT_AREA layout constants (pixel space)
_FT_LABEL_H = 14


def _render_free_text(r: PageRenderer, obj) -> None:
    cfg = obj.free_text_config
    has_label = bool(obj.label)
    box_y = obj.y + (_FT_LABEL_H if has_label else 0)
    box_h = obj.height - (_FT_LABEL_H if has_label else 0)
    if has_label:
        r.text(obj.x, obj.y, obj.width, _FT_LABEL_H, obj.label, 10, bold=True)
    if box_h <= 0:
        return
    r.box(obj.x, box_y, obj.width, box_h, color=BLACK, width=0.8)
    if cfg is not None and cfg.placeholder:
        r.text(
            obj.x + 4, box_y + 3, max(obj.width - 8, 1), min(box_h, 16),
            cfg.placeholder, 9, italic=True, color=LIGHT,
        )


def _render_question(r: PageRenderer, obj, mode: ExportMode) -> None:
    cfg = obj.question_config
    if cfg is None:
        return
    if not cfg.question_text:
        raise ValueError(
            f"QUESTION object {obj.id}: question_text is empty — fix the data before exporting"
        )
    r.text(obj.x, obj.y, obj.width, _Q_HEADER_H, cfg.question_text, 12, bold=True)

    bottom = obj.y + obj.height
    yy = obj.y + _Q_HEADER_H
    for opt in sorted(cfg.options, key=lambda o: o.order_index):
        if yy + _Q_OPTION_H > bottom:
            break  # clip overflow (no flow/pagination engine)
        if not opt.option_text:
            raise ValueError(
                f"QUESTION object {obj.id}, option {opt.id}: option_text is empty — fix the data before exporting"
            )
        cy = yy + _Q_OPTION_H / 2
        filled = mode is ExportMode.SOLUTION and opt.is_correct
        r.circle(
            obj.x + _Q_CIRCLE_CX, cy, _Q_CIRCLE_R,
            color=BLACK, width=0.8, fill=(BLACK if filled else None),
        )
        r.text(
            obj.x + _Q_TEXT_X, yy, max(obj.width - _Q_TEXT_X, 1), _Q_OPTION_H,
            opt.option_text, 11,
        )
        yy += _Q_OPTION_H


def _render_matrix(r: PageRenderer, obj, mode: ExportMode) -> None:
    cfg = obj.matrix_config
    if cfg is None:
        return
    rows = cfg.rows_count
    cols = cfg.cols_count
    if obj.label:
        r.text(obj.x, obj.y, obj.width, _M_LABEL_H, obj.label, 12, bold=True)
    if rows <= 0 or cols <= 0:
        return

    row_labels = {row.order_index: row.label for row in cfg.rows}
    col_labels = {col.order_index: col.label for col in cfg.cols}
    # Correct cell coordinates — only filled in SOLUTION mode.
    correct_cells = {
        (cell.row_index, cell.col_index) for cell in cfg.cells if cell.is_correct
    }

    # The grid arithmetic itself lives in app.utils.matrix_layout, NOT here:
    # extraction has to sample each cell exactly where this loop prints it, and
    # a second copy of the formula would drift.
    geometry = matrix_layout.compute_grid_geometry(
        obj.x, obj.y, obj.width, obj.height, rows, cols
    )
    label_w = geometry.label_w
    grid_x = geometry.grid_x
    grid_y = geometry.grid_y
    cell_w = geometry.cell_w
    cell_h = geometry.cell_h
    top = obj.y + _M_LABEL_H

    # Column headers
    for c in range(cols):
        r.text(
            grid_x + c * cell_w, top, cell_w, _M_HEADER_H,
            col_labels.get(c, str(c)), 9, align=ALIGN_CENTER,
        )
    # Row labels + cell circles + light grid
    # Fixed radius, matching QUESTION option circles (see _Q_CIRCLE_R).
    radius = _Q_CIRCLE_R
    for rr in range(rows):
        r.text(
            obj.x, grid_y + rr * cell_h, label_w, cell_h,
            row_labels.get(rr, str(rr)), 9,
        )
        for c in range(cols):
            cx, cy = matrix_layout.cell_center(geometry, rr, c)
            r.box(grid_x + c * cell_w, grid_y + rr * cell_h, cell_w, cell_h,
                  color=LIGHT, width=0.4)
            # SOLUTION marks correct cells filled; STUDENT leaves all empty.
            filled = mode is ExportMode.SOLUTION and (rr, c) in correct_cells
            r.circle(cx, cy, radius, color=BLACK, width=0.8,
                     fill=(BLACK if filled else None))
