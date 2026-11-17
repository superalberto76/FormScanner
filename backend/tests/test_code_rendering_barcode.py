# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for the CODE rendering of the PDF export.

A BARCODE printed by the export must decode off the page as the upload sees it:
the PDF rasterised at ``PDF_RENDER_DPI`` (150) by ``rasterize_submission_pdf``.
A Code128 in a wide, low box must not be fitted to the box HEIGHT
(``keep_proportion=True`` on a ~1.16:1 PNG with the text inside it): that
shrinks the bars to ~0.3 pt per module, unreadable below 400 dpi.

The geometry is COLL-MULTI's (tests/synthetic/specs.py): BARCODE "LOT-0001" in
(404, 310, 250, 70) with the human-readable strip, VID QR 80×80 in (674, 40).
The QR case guards that QR codes keep their proportions.

The tests SKIP when an encoder or pyzbar/libzbar is missing, the same contract
as ``test_code_extraction.py``.
"""
from __future__ import annotations

import io

import fitz
import pytest
from PIL import Image

from app.models.enums import CodeEncoding, CodeType, PageObjectRole
from app.services.storage_service import rasterize_submission_pdf
from app.utils import code_rendering
from app.utils.pdf_rendering import PageRenderer, page_metrics

BARCODE_BOX = (404, 310, 250, 70)
QR_BOX = (674, 40, 80, 80)


def _require_toolchain() -> None:
    try:
        import barcode  # noqa: F401
        import qrcode  # noqa: F401
        import pyzbar.pyzbar  # noqa: F401
    except Exception:
        pytest.skip("barcode/qrcode encoder or pyzbar/libzbar not available")


def _require_barcode_encoder() -> None:
    try:
        import barcode  # noqa: F401
    except Exception:
        pytest.skip("python-barcode encoder not available")


def _export_page(*codes) -> tuple[fitz.Document, list]:
    """A one-page A4 PDF with each ``(box, kwargs)`` drawn by ``render_code``."""
    width_pt, height_pt, scale = page_metrics(None, None, None)
    doc = fitz.open()
    page = doc.new_page(width=width_pt, height=height_pt)
    renderer = PageRenderer(page, scale)
    embedded = [_render_at(renderer, box, **kwargs) for box, kwargs in codes]
    return doc, embedded


def _render_at(renderer: PageRenderer, box, **kwargs) -> bool:
    x, y, w, h = box
    return code_rendering.render_code(renderer, x, y, w, h, **kwargs)


def _decoded_at_upload_dpi(doc: fitz.Document, tmp_path) -> set[str]:
    """Payloads read off page 1 exactly as the upload path rasterises it."""
    pdf = tmp_path / "export.pdf"
    doc.save(pdf)
    png = next(iter(rasterize_submission_pdf(pdf)))
    with Image.open(io.BytesIO(png)) as image:
        symbols = code_rendering.decode_symbols_from_pil_image(image.convert("RGB"))
    return {symbol.payload for symbol in symbols}


def _image_bboxes(doc: fitz.Document) -> list[fitz.Rect]:
    return [fitz.Rect(info["bbox"]) for info in doc[0].get_image_info()]


def test_barcode_in_a_wide_low_box_decodes_at_upload_dpi(tmp_path):
    _require_toolchain()
    doc, embedded = _export_page(
        (BARCODE_BOX, dict(code_type=CodeType.BARCODE, value="LOT-0001", human_readable=True)),
    )
    assert embedded == [True]
    assert "LOT-0001" in _decoded_at_upload_dpi(doc, tmp_path)


def test_barcode_fills_the_box_above_the_human_readable_strip():
    _require_barcode_encoder()  # geometry only: nothing is decoded
    doc, _ = _export_page(
        (BARCODE_BOX, dict(code_type=CodeType.BARCODE, value="LOT-0001", human_readable=True)),
    )
    _, _, scale = page_metrics(None, None, None)
    x, y, w, h = BARCODE_BOX
    code_h = h - code_rendering._HUMAN_READABLE_STRIP_PX
    expected = fitz.Rect(x * scale, y * scale, (x + w) * scale, (y + code_h) * scale)
    (bbox,) = _image_bboxes(doc)
    # Same rectangle as the canvas placeholder.
    assert tuple(bbox) == pytest.approx(tuple(expected), abs=0.01)


def test_qr_keeps_its_proportions_and_still_decodes(tmp_path):
    _require_toolchain()
    doc, embedded = _export_page(
        (
            QR_BOX,
            dict(
                code_type=CodeType.QR, value="COLL-MULTI-A", human_readable=False,
                page_index=2, role=PageObjectRole.VARIANT_IDENTIFIER,
            ),
        ),
    )
    assert embedded == [True]
    (bbox,) = _image_bboxes(doc)
    assert bbox.width == pytest.approx(bbox.height, abs=0.01)
    assert "VID:COLL-MULTI-A|2" in _decoded_at_upload_dpi(doc, tmp_path)


# ---------------------------------------------------------------------------
# Barcode values per symbology: the one implementation of
# the rules, shared by the authoring 422 and the export fallback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoding, value",
    [
        (CodeEncoding.CODE128, "LOT-0001"),
        (CodeEncoding.CODE128, "VID:COLL-MULTI-A|2"),
        (CodeEncoding.CODE128, ""),  # an empty value is accepted
        (CodeEncoding.CODE39, "LOT-0001"),
        (CodeEncoding.CODE39, "A B.$/+%"),
        (CodeEncoding.EAN13, "590123412345"),
        (CodeEncoding.EAN13, "5901234123457"),
        (CodeEncoding.EAN8, "9638507"),
        (CodeEncoding.EAN8, "96385074"),
        (CodeEncoding.UPCA, "03600029145"),
        (CodeEncoding.UPCA, "036000291452"),
    ],
)
def test_barcode_value_accepted(encoding, value):
    assert code_rendering.barcode_value_error(encoding, value) is None


@pytest.mark.parametrize(
    "encoding, value",
    [
        (CodeEncoding.CODE128, "LOT-0001 è"),       # not ASCII
        (CodeEncoding.CODE128, "LOT\t0001"),         # control character
        (CodeEncoding.CODE39, "lot-0001"),           # lower case
        (CodeEncoding.CODE39, "VID:A|2"),            # ':' and '|'
        (CodeEncoding.EAN13, "LOT-0001"),            # not digits
        (CodeEncoding.EAN13, "59012341234"),         # 11 digits
        (CodeEncoding.EAN13, "5901234123450"),       # wrong check digit
        (CodeEncoding.EAN13, "５９０１２３４１２３４５"),  # non-ASCII digits
        (CodeEncoding.EAN8, "96385070"),             # wrong check digit
        (CodeEncoding.UPCA, "12345"),
        (CodeEncoding.UPCA, "036000291450"),         # wrong check digit
    ],
)
def test_barcode_value_rejected(encoding, value):
    assert code_rendering.barcode_value_error(encoding, value)


def test_wrong_check_digit_message_names_the_right_one():
    message = code_rendering.barcode_value_error(CodeEncoding.EAN13, "5901234123450")
    assert message.endswith("it should be 7.")
