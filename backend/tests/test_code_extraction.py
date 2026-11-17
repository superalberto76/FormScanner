# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for role-less CODE extraction.

Two things are pinned down here:

  * ``_find_matching_symbol`` — the position matching that attributes one of the
    page's decoded symbols to the object, with no image involved;
  * ``code_rendering``: ``decode_symbols_from_pil_image`` reports where each
    symbol is, and ``decode_codes_from_image`` — a thin wrapper over it, used
    by ingestion — returns the same payloads, in the same order, as a direct
    pyzbar decode, and never raises.

The real-QR tests SKIP when the qrcode encoder or pyzbar/libzbar is missing,
the same contract as the ingestion end-to-end tests. The DB-backed half lives in
``tests/integration/test_code_extraction_integration.py``.
"""
from __future__ import annotations

import builtins

import pytest
from PIL import Image

from app.services import code_extraction_service as svc
from app.services.question_extraction_service import ScanRegion
from app.utils import code_rendering
from app.utils.code_rendering import DecodedSymbol


def _require_qr_toolchain() -> None:
    try:
        import qrcode  # noqa: F401
        import pyzbar.pyzbar  # noqa: F401
    except Exception:
        pytest.skip("qrcode encoder or pyzbar/libzbar not available")


def _page_with_qrs(placements: list[tuple[str, int, int]]) -> Image.Image:
    """A white RGB page with one QR per ``(payload, left, top)``."""
    import qrcode

    page = Image.new("RGB", (1200, 900), "white")
    for payload, left, top in placements:
        page.paste(qrcode.make(payload).convert("RGB"), (left, top))
    return page


# ---------------------------------------------------------------------------
# _find_matching_symbol
# ---------------------------------------------------------------------------
REGION = ScanRegion(x=100.0, y=100.0, width=30.0, height=40.0)  # centre (115, 120), diagonal 50


def test_the_symbol_nearest_to_the_region_centre_wins():
    far = DecodedSymbol("FAR", 140.0, 150.0)
    near = DecodedSymbol("NEAR", 118.0, 122.0)
    # The premise: both qualify, so only the distance tells them apart.
    assert svc._qualifies(far, REGION) and svc._qualifies(near, REGION)

    assert svc._find_matching_symbol([far, near], REGION) is near


def test_no_symbol_is_no_match():
    assert svc._find_matching_symbol([], REGION) is None


def test_a_symbol_outside_the_widened_region_is_never_matched():
    # Margin = 0.5 * size per axis: x within [85, 145], y within [80, 160].
    for inside, outside in [
        ((145.0, 120.0), (145.5, 120.0)),  # right
        ((85.0, 120.0), (84.5, 120.0)),  # left
        ((115.0, 160.0), (115.0, 160.5)),  # bottom
        ((115.0, 80.0), (115.0, 79.5)),  # top
    ]:
        just_inside = DecodedSymbol("IN", *inside)
        just_outside = DecodedSymbol("OUT", *outside)

        assert svc._find_matching_symbol([just_inside], REGION) is just_inside
        assert svc._find_matching_symbol([just_outside], REGION) is None


def test_the_margin_follows_each_axis_not_the_diagonal():
    """A wide, low region (a barcode) tolerates drift along its width, and up
    to half its height vertically, but not a symbol well above it that a
    1.5-diagonal radius would reach."""
    barcode = ScanRegion(x=404.0, y=310.0, width=250.0, height=70.0)
    vid_centre = DecodedSymbol("PLAIN", 714.0, 80.0)  # ~1.24 diagonals away
    along_width = DecodedSymbol("PLAIN", 404.0 + 250.0 + 120.0, 345.0)
    # Vertical margin = 35: y within [275, 415].
    shifted_down = DecodedSymbol("PLAIN", 529.0, 380.0 + 30.0)
    shifted_up = DecodedSymbol("PLAIN", 529.0, 310.0 - 30.0)
    too_low = DecodedSymbol("PLAIN", 529.0, 380.0 + 36.0)

    assert svc._find_matching_symbol([vid_centre], barcode) is None
    assert svc._find_matching_symbol([along_width], barcode) is along_width
    assert svc._find_matching_symbol([shifted_down], barcode) is shifted_down
    assert svc._find_matching_symbol([shifted_up], barcode) is shifted_up
    assert svc._find_matching_symbol([too_low], barcode) is None


def test_the_nearest_qualifying_symbol_wins_over_a_nearer_tagged_one():
    tagged = DecodedSymbol("VID:VAR-A|1", 115.0, 120.0)
    plain = DecodedSymbol("PLAIN", 130.0, 140.0)

    assert svc._find_matching_symbol([tagged, plain], REGION) is plain


@pytest.mark.parametrize("payload", ["VID:VAR-A|1", "VID:VAR-A", "RID:STUDENT-42"])
def test_a_tagged_payload_is_never_matched_even_at_the_centre(payload):
    assert svc._find_matching_symbol([DecodedSymbol(payload, 115.0, 120.0)], REGION) is None


@pytest.mark.parametrize("payload", ["LOT-0001", "vid:lowercase", "XVID:VAR-A"])
def test_an_untagged_payload_is_matched(payload):
    symbol = DecodedSymbol(payload, 115.0, 120.0)

    assert svc._find_matching_symbol([symbol], REGION) is symbol


def test_a_tie_keeps_the_decoders_order():
    first = DecodedSymbol("FIRST", 125.0, 120.0)
    second = DecodedSymbol("SECOND", 105.0, 120.0)

    assert svc._find_matching_symbol([first, second], REGION) is first


# ---------------------------------------------------------------------------
# decode_symbols_from_pil_image
# ---------------------------------------------------------------------------
def test_symbols_carry_their_payload_and_the_centre_of_their_rect():
    _require_qr_toolchain()
    import qrcode

    qr_size = qrcode.make("HELLO").size[0]
    page = _page_with_qrs([("HELLO", 600, 300)])

    symbols = code_rendering.decode_symbols_from_pil_image(page)

    assert [s.payload for s in symbols] == ["HELLO"]
    # The QR image carries a quiet zone, so the symbol's rect is inset from the
    # pasted image — but centred on it.
    assert symbols[0].center_x == pytest.approx(600 + qr_size / 2, abs=2)
    assert symbols[0].center_y == pytest.approx(300 + qr_size / 2, abs=2)


def test_greyscale_decodes_exactly_like_rgb():
    """Why the extractor may reuse ``_open_gray_scan``: pyzbar reduces any
    Pillow image to 'L' itself."""
    _require_qr_toolchain()
    page = _page_with_qrs([("A-1", 50, 50), ("B-2", 700, 400)])

    assert code_rendering.decode_symbols_from_pil_image(
        page.convert("L")
    ) == code_rendering.decode_symbols_from_pil_image(page)


def test_a_failing_decoder_is_nothing_readable_not_an_exception(monkeypatch):
    real_import = builtins.__import__

    def no_pyzbar(name, *args, **kwargs):
        if name.startswith("pyzbar"):
            raise ImportError("libzbar0 missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyzbar)

    assert code_rendering.decode_symbols_from_pil_image(Image.new("L", (10, 10))) == []


# ---------------------------------------------------------------------------
# decode_codes_from_image — non-regression of the path-based API
# ---------------------------------------------------------------------------
def _direct_pyzbar_decode(image_path) -> list[str]:
    """A direct pyzbar decode of the whole image, the reference the wrapper
    must keep matching."""
    from pyzbar.pyzbar import decode as pyzbar_decode

    with Image.open(image_path) as img:
        symbols = pyzbar_decode(img.convert("RGB"))
    payloads = []
    for symbol in symbols:
        payload = (symbol.data or b"").decode("utf-8", errors="replace")
        if payload:
            payloads.append(payload)
    return payloads


def test_decode_codes_from_image_returns_the_same_payloads_in_the_same_order(
    tmp_path,
):
    _require_qr_toolchain()
    path = tmp_path / "page.png"
    _page_with_qrs(
        [("VID:VAR-A|1", 30, 30), ("RID:STUDENT-42", 800, 500), ("plain", 400, 100)]
    ).save(path)

    payloads = code_rendering.decode_codes_from_image(path)

    assert payloads == _direct_pyzbar_decode(path)
    assert sorted(payloads) == ["RID:STUDENT-42", "VID:VAR-A|1", "plain"]


def test_decode_codes_from_image_still_reads_a_bare_qr(tmp_path):
    """The exact round trip the ingestion end-to-end tests use as a probe."""
    _require_qr_toolchain()
    import qrcode

    path = tmp_path / "probe.png"
    qrcode.make("RID:STUDENT-42").save(path)

    assert code_rendering.decode_codes_from_image(path) == ["RID:STUDENT-42"]


def test_decode_codes_from_image_on_a_blank_page_is_empty(tmp_path):
    path = tmp_path / "blank.png"
    Image.new("RGB", (300, 300), "white").save(path)

    assert code_rendering.decode_codes_from_image(path) == []


def test_decode_codes_from_image_on_an_unreadable_file_is_empty(tmp_path):
    missing = tmp_path / "not-an-image.png"
    assert code_rendering.decode_codes_from_image(missing) == []

    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(b"not a png at all")
    assert code_rendering.decode_codes_from_image(garbage) == []


def test_decode_codes_from_image_with_a_failing_decoder_is_empty(
    tmp_path, monkeypatch
):
    path = tmp_path / "blank.png"
    Image.new("RGB", (50, 50), "white").save(path)
    real_import = builtins.__import__

    def no_pyzbar(name, *args, **kwargs):
        if name.startswith("pyzbar"):
            raise ImportError("libzbar0 missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyzbar)

    assert code_rendering.decode_codes_from_image(path) == []
