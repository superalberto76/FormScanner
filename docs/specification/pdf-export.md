# FormScanner — Printable PDF

This document describes how a variant becomes a printable PDF: the two modes, the coordinate model, how each object type is drawn, the payload of the printed codes, the alignment marks and reserved bands, and the layout constants shared with the editor's canvas. What the user sees and why is in the [functional specification](functional-specification.md) (§2.7, §4); the endpoint in [api.md](api.md#10-downloads).

## Contents

1. [Overview](#1-overview)
2. [Coordinate model](#2-coordinate-model)
3. [Rendering of each object type](#3-rendering-of-each-object-type)
4. [Code payload](#4-code-payload)
5. [Alignment marks and reserved bands](#5-alignment-marks-and-reserved-bands)
6. [Constants shared with the canvas](#6-constants-shared-with-the-canvas)
7. [Known limits](#7-known-limits)

## 1. Overview

`GET /api/v1/form-variants/{variant_id}/export/pdf?mode=STUDENT|SOLUTION` renders the variant with PyMuPDF (`backend/app/services/form_variant_export_service.py`) and returns it as an attachment.

- **Modes.** `STUDENT` (default) leaves every bubble empty: it is the sheet that is printed and filled in. `SOLUTION` fills the bubbles of the correct options and matrix cells. Everything else is identical in the two modes, alignment marks included, so a solution sheet lies exactly over a student sheet. Scores are never printed.
- **Who and when.** Any member of the project, in every project and variant status: a draft can be exported to check it.
- **File name.** `<root_key>_student.pdf` or `<root_key>_solution.pdf`, or the `filename` parameter. Characters other than letters, digits, `.`, `_` and `-` become `_`, and `.pdf` is added if missing.
- **Pages.** One PDF page per variant page, in `page_index` order. A variant with no pages gives one blank A4 page.
- **Objects.** Only visible objects (`visible = true`) are drawn, in order of `z_index`, then `created_at`. `locked` only matters in the editor.
- **Order on the page.** First the objects, then the alignment marks, so a mark is always whole.

`GET /api/v1/form-variants/{variant_id}/export/preview-metadata` returns, without rendering, the page sizes and the count of objects per type: a diagnostic aid.

## 2. Coordinate model

Object geometry (`x`, `y`, `width`, `height`), page size (`width_px`, `height_px`) and font sizes are all in the **pixel space of the page**, with the origin at the top-left corner. New pages are A4 at 96 dpi: 794 × 1123 px. A page without a size counts as A4 at 96 dpi.

The PDF converts pixels to points with `scale = 72 / dpi` (`backend/app/utils/pdf_rendering.py`, `page_metrics`). Font sizes are scaled too, so text keeps its proportion to its box. At 96 dpi, 1 mm ≈ 3.78 px.

**Text** is wrapped inside its box and clipped. If it does not fit at its size, the size is multiplied by 0.9 until it fits, down to 1 px; at that floor it is drawn anyway, so a value never disappears. There is no ellipsis.

**Fonts** are the PDF base fonts, chosen from the object's `font_family` (case-insensitive substring):

| `font_family` contains | Font |
|---|---|
| `cour` or `mono` | Courier |
| `times` or `serif` | Times |
| anything else, or no family | Helvetica |

with the bold, italic and bold italic variants of each.

**Colours:** black, gray (`0.45`), light gray (`0.72`) and white (the hole of the distinct alignment mark only).

## 3. Rendering of each object type

| Type | Drawn as |
|---|---|
| `TEXT` | The text in its box, with its size, bold, italic and font |
| `QUESTION` | The question text in bold on an 18 px header; below it one 18 px row per option in `order_index` order, with an empty circle (radius 5, centre 9 px from the left edge) and the option text from 22 px. Options that do not fit the height of the object are not drawn. In `SOLUTION` the circles of the options with `is_correct` are filled |
| `MATRIX` | The label in bold on a 16 px strip (reserved even without a label), the column labels on a 16 px header, the row labels in a left column a quarter of the object's width wide (at most 120 px), and the grid: rows and columns share the remaining space equally, each cell with a light border and a circle of radius 5 at its centre. A missing row or column label prints its index. In `SOLUTION` the cells with `is_correct` are filled |
| `FREE_TEXT_AREA` | The label in bold on a 14 px strip, if there is one, and below it a black box for the handwriting, with the placeholder in light italic in its top-left corner |
| `CODE` | The QR code or barcode of the payload (section 4); with `human_readable`, the printed value centred in a 14 px strip below the code |
| `IMAGE` | The uploaded picture: `contain` keeps its proportions inside the box, `cover` fills the box. SVG pictures are converted to vector PDF content. Without a picture, or if it cannot be embedded, a gray box with the object's label (or "image") |

**QR codes** keep their proportions inside the box. **Barcodes** are printed in the object's encoding (`CODE128`, `CODE39`, `EAN13`, `EAN8`, `UPCA`), with no text in the image and a quiet zone of 10 modules, and are stretched to fill the whole box above the human-readable strip: wider bars stay readable at the resolution of the scans. For EAN and UPC the printed value includes the check digit, which is what a scanner reads.

Values a barcode can hold (`backend/app/utils/code_rendering.py`, `barcode_value_error`), checked when the object is saved (`422`):

| Encoding | Value |
|---|---|
| `CODE128` | Printable ASCII |
| `CODE39` | Upper-case letters, digits, space and `- . $ / + %`; printed without the optional check character |
| `EAN13`, `EAN8`, `UPCA` | 12, 7, 11 digits (the check digit is computed) or 13, 8, 12 with a correct check digit |

A barcode holding a scanning role must be `CODE128` (`409`), because the role tag cannot be encoded otherwise. A stored barcode whose payload does not fit its encoding is printed as `CODE128`, with a warning in the log. If the encoder fails, a gray box shows the code type and the payload instead of the code.

The same layout functions place the bubbles for printing and for reading: `backend/app/utils/question_layout.py` and `backend/app/utils/matrix_layout.py` are used by the export and by extraction, so a reading samples each bubble exactly where it was printed.

## 4. Code payload

The payload is the exact string encoded in the code; the same string is printed in the human-readable strip. It is built by `build_tagged_payload` and read back by `parse_tagged_payload` (`backend/app/utils/code_rendering.py`).

| Role of the object | `include_page_index` | Payload |
|---|---|---|
| none | — | `<value>` |
| `VARIANT_IDENTIFIER` | false | `VID:<value>` |
| `VARIANT_IDENTIFIER` | true | `VID:<value>\|<page_index>` |
| `RESPONDENT_IDENTIFIER` | — | `RID:<value>` |

- **Role tag.** `VID:` and `RID:` tell the two identifier codes apart on the sheet itself: ingestion has to know which code is which before matching has found the variant.
- **Page index.** Only on the variant identifier with `include_page_index`: the 1-based `page_index` of the page it is printed on. A respondent identifier never carries one.
- **Separators.** `:` after the tag, `|` before the page index. When reading, the last `|` is taken, and only if what follows is a plain integer exactly as written by the export (no sign, spaces or leading zeros), so a value containing `|` survives.
- **Codes without a role** carry the bare value. When read they are "untagged" and take no part in identification; the same holds for codes printed by other software. Parsing never fails: a malformed payload is untagged.
- **Linked copies.** A variant identifier is shown on every page; the role is held by one row of the linked group, and every linked copy is printed with the group's role (`PageObject.effective_role`). An unlinked copy carries only its own role.

## 5. Alignment marks and reserved bands

Every page, in both modes, carries alignment marks that reading uses to straighten the scan ([functional specification, §6](functional-specification.md#6-sheet-alignment)). They are not objects: they cannot be selected and are never read as answers. Their geometry is in `backend/app/utils/alignment_marks.py`, shared by the export, the authoring checks and the reading.

| Constant | Value (px at 96 dpi) | Meaning |
|---|---|---|
| `BAND_W` | 57 (15 mm) | Width of the reserved band along each side |
| `MARK_INSET` | 30 (8 mm) | Distance of the corner marks from the page edges |
| `MARK_SIZE` | 19 (5 mm) | Side of a corner mark |
| `HOLE_SIZE` | 7 | Side of the white hole of the distinct mark |
| `DISTINCT_CORNER` | `BR` | The corner whose mark has the hole, which gives the page orientation |
| `SIDE_MARK_COUNT` | 17 | Side marks on each side |
| `SIDE_MARK_W` × `SIDE_MARK_H` | 19 × 8 (5 × 2 mm) | Size of a side mark |

- **Corner marks** (`mark_rects`): solid black squares, without stroke, at `MARK_INSET` from the edges; the bottom-right one has a centred white square hole (`hole_rect`). On an A4 page: TL (30, 30), TR (745, 30), BL (30, 1074), BR (745, 1074).
- **Side marks** (`side_mark_rects`): on each side, in the same column as the corner marks, 17 solid black horizontal bars. Their centres are equally spaced between the centre of the top corner mark and that of the bottom one, step `(bottom − top) / 18`, not rounded. On an A4 page: x 30 (left) and 745 (right), y `93.5 + 58 · (i − 1)` for `i = 1…17`.
- **Usable area** (`usable_rect`): the page minus the four bands, edges included; on an A4 page x 57…737, y 57…1066. `in_reserved_band` is true for a rectangle not wholly inside it. The server refuses creating, moving or resizing an object into a band (`409 OBJECT_IN_RESERVED_BAND`) and publishing a variant that still has one there (`409 OBJECTS_IN_RESERVED_BANDS`). The editor draws the bands and keeps objects out of them.

The constants assume 96 dpi pages, as every page is created. A page with another `dpi` would get marks of a different physical size.

## 6. Constants shared with the canvas

The editor's canvas draws a preview faithful to the printed PDF, so both read the same numbers. The export is the source; `frontend/src/utils/exportLayout.ts` is the only copy on the frontend. The canvas multiplies every constant by one factor (rendered width of the page ÷ page width in px). The canvas has no code encoder: it always draws the gray placeholder of a code.

**The rule:** when a value changes, change it in the Python source, in `exportLayout.ts` and in this table, together. Two tests freeze the values and fail if the two sides drift apart: `backend/tests/test_export_layout_freeze.py` and `frontend/src/utils/exportLayout.test.ts`. Values written inline in the Python render functions cannot be imported by the Python test: they are frozen on the frontend side only.

| Group | Constant | Value | Python | `exportLayout.ts` |
|---|---|---|---|---|
| Colours | black, gray, light, white | `#000`, `rgb(115,115,115)`, `rgb(184,184,184)`, `#fff` | `pdf_rendering.BLACK`, `GRAY`, `LIGHT`, `WHITE` (`0`, `0.45`, `0.72`, `1`) | `COLORS` |
| Fonts | family buckets | section 2 | `pdf_rendering.fitz_font` | `cssFontStack()` |
| Text fit | shrink factor, floor | `0.9`, `1` px | inline in `PageRenderer.text` | `AUTOFIT.SHRINK`, `AUTOFIT.FLOOR_PX` |
| Question | header height, option row height | `18`, `18` | `question_layout.HEADER_H`, `OPTION_H` | `Q.HEADER_H`, `Q.OPTION_H` |
| Question | circle radius, circle centre x, option text x | `5`, `9`, `22` | `question_layout.CIRCLE_R`, `CIRCLE_CX`, `TEXT_X` | `Q.CIRCLE_R`, `Q.CIRCLE_CX`, `Q.TEXT_X` |
| Question | header size (bold), option size | `12`, `11` | inline in `_render_question` | `Q.HEADER_SIZE`, `Q.OPTION_SIZE` |
| Matrix | label strip, header strip | `16`, `16` | `matrix_layout.LABEL_H`, `HEADER_H` | `M.LABEL_H`, `M.HEADER_H` |
| Matrix | label column ratio, cap | `0.25`, `120` | inline in `matrix_layout.compute_grid_geometry` | `M.LABEL_W_RATIO`, `M.LABEL_W_MAX` |
| Matrix | circle radius | `5` | `question_layout.CIRCLE_R` | `M.CIRCLE_R` |
| Matrix | label size (bold), column header size (centred), row label size | `12`, `9`, `9` | inline in `_render_matrix` | `M.LABEL_SIZE`, `M.HEADER_SIZE`, `M.ROW_LABEL_SIZE` |
| Free-text area | label strip, label size (bold) | `14`, `10` | `_FT_LABEL_H`, inline in `_render_free_text` | `FT.LABEL_H`, `FT.LABEL_SIZE` |
| Free-text area | placeholder size (italic, light), padding x, padding y, max height | `9`, `4`, `3`, `16` | inline in `_render_free_text` | `FT.PLACEHOLDER_SIZE`, `FT.PAD_X`, `FT.PAD_Y`, `FT.PLACEHOLDER_MAX_H` |
| Code | human-readable strip | `14` | `code_rendering._HUMAN_READABLE_STRIP_PX` | `CODE.HUMAN_READABLE_STRIP_PX` |
| Code | placeholder and strip text size, placeholder padding | `8`, `3` | inline in `code_rendering.render_code` | `CODE.TEXT_SIZE`, `CODE.PAD` |
| Image | placeholder label size | `9` | inline in `_render_image_placeholder` | `IMAGE_PLACEHOLDER.LABEL_SIZE` |
| Marks | band, inset, mark side, hole side, distinct corner | `57`, `30`, `19`, `7`, `BR` | `alignment_marks.BAND_W`, `MARK_INSET`, `MARK_SIZE`, `HOLE_SIZE`, `DISTINCT_CORNER` | `MARKS.*`; `markRects`, `holeRect` |
| Side marks | count, width, height | `17`, `19`, `8` | `alignment_marks.SIDE_MARK_COUNT`, `SIDE_MARK_W`, `SIDE_MARK_H` | `SIDE_MARKS.*`; `sideMarkRects` |
| Usable area | — | section 5 | `alignment_marks.usable_rect`, `in_reserved_band` | `usableRect`, `inReservedBand` |

Python paths are under `backend/app/`: `utils/` for `pdf_rendering`, `question_layout`, `matrix_layout`, `code_rendering`, `alignment_marks`; the `_render_*` functions and `_FT_LABEL_H` are in `services/form_variant_export_service.py`. The barcode quiet zone (`code_rendering._BARCODE_QUIET_ZONE_MODULES`, 10) has no canvas counterpart.

## 7. Known limits

- **Barcode density.** A barcode fills the width of its box, so the width of its modules depends on the length of the payload and the width of the box. A long payload in a narrow box may not be readable at the resolution of the scans: for example, a respondent number of 12 digits printed as `RID:<digits>` (16 characters) needs a box about 350 px wide (page pixels, at 96 dpi) to be read from a scan at 150 dpi. The editor gives no warning; a QR code does not have this problem.
- **No reflow.** Positioning is absolute: question options that do not fit the height of the object are not printed, and text that does not fit is shrunk (section 2). Nothing flows to another page.
- **Base fonts only.** Any `font_family` maps to Helvetica, Times or Courier.
- **UPC-A when read back.** The reader reports a UPC-A code in its EAN-13 form, with a leading `0`: a role-less UPC-A code is extracted with that value.
