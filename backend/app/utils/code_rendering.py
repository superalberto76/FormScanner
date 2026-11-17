# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CODE object rendering (QR / barcode) for the PDF export.

Encoder libraries are probed at runtime. ``qrcode`` and ``python-barcode`` are
listed in ``requirements.txt``, but a bare checkout may still lack them, so:

  - if available, it rasterises a real QR / barcode (PNG) and embeds it;
  - otherwise it draws a labelled placeholder box that visibly shows the encoded
    value, so the layout/position is still correct and the value is legible.

This keeps the export robust regardless of which optional libs are installed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from app.models.enums import CodeEncoding, CodeType, PageObjectRole
from app.utils.pdf_rendering import BLACK, GRAY, PageRenderer

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)

# Reserve this many pixels at the bottom of the object for human-readable text.
_HUMAN_READABLE_STRIP_PX = 14

# Barcode PNG options. The module width is python-barcode's own
# default, stated here because the quiet zone is derived from it: 10 modules on
# each side, the Code128 minimum. python-barcode's default quiet zone (6.5 mm)
# would waste the box width that the bars need once the image is stretched to it.
_BARCODE_MODULE_WIDTH_MM = 0.2
_BARCODE_QUIET_ZONE_MODULES = 10

# python-barcode symbology for each authoring encoding. A BARCODE with no
# encoding (a row stored before the encoding existed: authoring now requires
# one) is printed as Code128.
_BARCODE_SYMBOLOGIES: dict[CodeEncoding, str] = {
    CodeEncoding.CODE128: "code128",
    CodeEncoding.CODE39: "code39",
    CodeEncoding.EAN13: "ean13",
    CodeEncoding.EAN8: "ean8",
    CodeEncoding.UPCA: "upca",
}

# Separator between the encoded value and the appended page number.
PAGE_INDEX_SEPARATOR = "|"

# Separator between the role tag and the rest of the payload.
# Deliberately different from PAGE_INDEX_SEPARATOR so the two delimiters can
# never be confused when parsing "VID:<value>|<page_index>".
ROLE_TAG_SEPARATOR = ":"

TAG_VARIANT_IDENTIFIER = "VID"
TAG_RESPONDENT_IDENTIFIER = "RID"
# Returned by parse_tagged_payload for anything without a recognised role tag.
TAG_UNTAGGED = "UNTAGGED"


def build_payload(value: str, page_index: int | None) -> str:
    """The *untagged* part of the string encoded into a CODE.

    Returns *value* unchanged when *page_index* is None (the default, and the
    behaviour for every code without the "include page number" option). Otherwise
    appends the page index as ``value|<page_index>`` so a VARIANT_IDENTIFIER code
    also carries which page it was printed on.

    This is the inner helper of :func:`build_tagged_payload` —
    a code with a special role gets a ``VID:``/``RID:`` prefix on top of it.
    """
    if page_index is None:
        return value
    return f"{value}{PAGE_INDEX_SEPARATOR}{page_index}"


def build_tagged_payload(
    role: PageObjectRole | None, value: str, page_index: int | None = None
) -> str:
    """The exact string encoded into a CODE, prefixed with its role tag.

    The tag makes the payload self-describing, so a scanner can tell a variant
    identifier from a respondent identifier *without* knowing the FormVariant —
    which is exactly what ingestion cannot know, since resolving the variant is
    the job of matching, which runs after it:

      * VARIANT_IDENTIFIER    -> ``VID:<value>`` (or ``VID:<value>|<page_index>``)
      * RESPONDENT_IDENTIFIER -> ``RID:<value>`` (never carries a page index)
      * no role               -> ``<value>``, the bare value unchanged (no tag)
    """
    if role is PageObjectRole.VARIANT_IDENTIFIER:
        return (
            f"{TAG_VARIANT_IDENTIFIER}{ROLE_TAG_SEPARATOR}"
            f"{build_payload(value, page_index)}"
        )
    if role is PageObjectRole.RESPONDENT_IDENTIFIER:
        # A respondent identifier identifies a person, not a sheet position, so
        # the page index is meaningless here and is deliberately dropped.
        return f"{TAG_RESPONDENT_IDENTIFIER}{ROLE_TAG_SEPARATOR}{value}"
    return build_payload(value, page_index)


# ---------------------------------------------------------------------------
# Barcode values — which values each symbology can encode. The ONE
# implementation of the rules: authoring rejects an invalid value with a 422
# (code_object_service) and the export falls back to Code128 for a stored value
# that breaks them (form_variant_export_service).
# ---------------------------------------------------------------------------

# Code 39's character set, upper case only: python-barcode would silently
# upper-case a lower-case value, so the printed code would not match it.
_CODE39_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.$/+%")

# GTIN symbologies: digits without the check digit (the export computes it); one
# more digit means the check digit is included and must be right, because
# python-barcode would otherwise silently replace a wrong one.
_GTIN_DATA_DIGITS: dict[CodeEncoding, int] = {
    CodeEncoding.EAN13: 12,
    CodeEncoding.EAN8: 7,
    CodeEncoding.UPCA: 11,
}


def _gtin_check_digit(data_digits: str) -> str:
    """The GS1 check digit: weights 3, 1, 3, … from the rightmost data digit."""
    total = sum(
        int(digit) * (3 if position % 2 == 0 else 1)
        for position, digit in enumerate(reversed(data_digits))
    )
    return str((10 - total % 10) % 10)


def barcode_value_error(encoding: CodeEncoding, value: str) -> str | None:
    """Why *value* cannot be printed in *encoding*, or None when it can.

    Rules: CODE128 printable ASCII; CODE39 upper-case letters,
    digits, space and ``- . $ / + %``; EAN13 / EAN8 / UPCA 12 / 7 / 11 digits,
    or 13 / 8 / 12 with a correct check digit. The message is shown to the user
    as is (UI strings in English).
    """
    if encoding is CodeEncoding.CODE128:
        if any(not " " <= char <= "~" for char in value):
            return "A CODE128 barcode can only contain printable ASCII characters."
        return None
    if encoding is CodeEncoding.CODE39:
        if any(char not in _CODE39_CHARACTERS for char in value):
            return (
                "A CODE39 barcode can only contain upper-case letters, digits, "
                "spaces and - . $ / + %."
            )
        return None
    data_digits = _GTIN_DATA_DIGITS[encoding]
    if not value.isascii() or not value.isdigit() or len(value) not in (
        data_digits, data_digits + 1
    ):
        return (
            f"An {encoding.value} barcode needs {data_digits} digits, or "
            f"{data_digits + 1} including the check digit."
        )
    if len(value) == data_digits + 1 and value[-1] != _gtin_check_digit(value[:-1]):
        return (
            f"The check digit of this {encoding.value} barcode is wrong: "
            f"it should be {_gtin_check_digit(value[:-1])}."
        )
    return None


# ---------------------------------------------------------------------------
# Decoding — the exact inverse of build_payload, plus reading a code back off a
# scanned page image (scanning pipeline).
#
# Everything below is deliberately TOTAL: a malformed payload or a failing
# decoder library is a "not readable" outcome, never an exception. Ingestion of a
# whole PDF must never be aborted by one bad page.
# ---------------------------------------------------------------------------


def parse_payload(raw: str) -> tuple[str, int | None]:
    """Inverse of :func:`build_payload`: split a payload into (value, page_index).

    Returns ``(value, page_index)`` when *raw* ends with
    ``PAGE_INDEX_SEPARATOR`` + an integer, else ``(raw, None)``. The LAST
    separator is the split point, so a value that itself contains ``|`` still
    round-trips.

    Note the page index is reported exactly as it was encoded — this function
    applies no base conversion. ``FormVariantPage.page_index`` is 1-BASED and
    the export encodes it verbatim (see the model docstring), so a decoded
    index is 1-based and directly comparable to it.

    Never raises: if the part after the separator is not an integer it is treated
    as part of the value (i.e. no separator was found *at that point*) and
    ``(raw, None)`` is returned, because a malformed payload must never fail
    ingestion.
    """
    value, separator, tail = raw.rpartition(PAGE_INDEX_SEPARATOR)
    if not separator:
        return raw, None
    try:
        page_index = int(tail)
    except ValueError:
        return raw, None
    # Strict symmetry: accept only what build_payload could have emitted, so
    # " 3" / "+3" / "007" stay part of the value instead of silently normalising.
    if str(page_index) != tail:
        return raw, None
    return value, page_index


def parse_tagged_payload(raw: str) -> tuple[str, str, int | None]:
    """Inverse of :func:`build_tagged_payload`.

    Returns ``(role_tag, value, page_index)`` where *role_tag* is one of
    ``'VID'``, ``'RID'`` or ``'UNTAGGED'``:

      * ``VID:`` — prefix stripped, the remainder split by :func:`parse_payload`;
      * ``RID:`` — prefix stripped, *page_index* always None;
      * anything else — ``('UNTAGGED', raw, None)``, the raw value passed through.

    NEVER raises, like everything else in this section. Codes printed without
    a role tag, third-party codes and plain garbage all land in the UNTAGGED
    bucket rather than blowing up the ingestion of a whole PDF.
    """
    if not isinstance(raw, str):
        return TAG_UNTAGGED, "", None
    variant_prefix = f"{TAG_VARIANT_IDENTIFIER}{ROLE_TAG_SEPARATOR}"
    if raw.startswith(variant_prefix):
        value, page_index = parse_payload(raw[len(variant_prefix):])
        return TAG_VARIANT_IDENTIFIER, value, page_index
    respondent_prefix = f"{TAG_RESPONDENT_IDENTIFIER}{ROLE_TAG_SEPARATOR}"
    if raw.startswith(respondent_prefix):
        return TAG_RESPONDENT_IDENTIFIER, raw[len(respondent_prefix):], None
    return TAG_UNTAGGED, raw, None


@dataclass(frozen=True)
class DecodedSymbol:
    """One QR / barcode read off an image, with WHERE it was found.

    ``center_x`` / ``center_y`` are the centre of pyzbar's bounding ``rect``
    (a ``pyzbar.locations.Rect(left, top, width, height)`` namedtuple), in the
    pixel space of the image that was decoded. Position is what lets a caller
    tell apart several codes printed on the same page.
    """

    payload: str
    center_x: float
    center_y: float


def decode_symbols_from_pil_image(image: "Image.Image") -> list[DecodedSymbol]:
    """Decode EVERY QR / barcode found in *image* (an already-open Pillow image).

    Returns one :class:`DecodedSymbol` per non-empty symbol, in the decoder's own
    order, or an empty list when nothing readable is found. Any mode is
    accepted: pyzbar reduces a Pillow image to 8-bit greyscale (``'L'``) itself
    before handing it to zbar, so an RGB image and its greyscale conversion
    decode identically.

    Same contract as :func:`decode_codes_from_image`, of which this is the core:
    NEVER raises. A missing ``libzbar0`` or any decoder failure is logged and
    reported as "nothing readable".
    """
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode

        raw_symbols = pyzbar_decode(image)
    except Exception:
        logger.warning("Code decoding failed on an in-memory image", exc_info=True)
        return []

    symbols: list[DecodedSymbol] = []
    for symbol in raw_symbols:
        payload = (symbol.data or b"").decode("utf-8", errors="replace")
        if not payload:
            continue
        left, top, width, height = symbol.rect
        symbols.append(
            DecodedSymbol(
                payload=payload,
                center_x=left + width / 2,
                center_y=top + height / 2,
            )
        )
    return symbols


def decode_codes_from_image(image_path: Path) -> list[str]:
    """Decode EVERY QR / barcode found in the image at *image_path*.

    Returns the RAW payloads in the decoder's own order (still to be fed to
    :func:`parse_tagged_payload`), or an empty list when nothing readable is
    found. Decodes with pyzbar on a PIL RGB image; pyzbar is imported lazily
    because it needs the system ``libzbar0`` shared library (installed in the
    Dockerfile) that a bare dev checkout may lack.

    ALL symbols are returned, deliberately: a page routinely carries several
    codes (a VARIANT_IDENTIFIER next to a RESPONDENT_IDENTIFIER), and it is
    the role TAG in each payload — not a guess made here — that says which is
    which. Picking a winner is the caller's job
    (``submission_identification_service``), not the decoder's.

    A thin path-based wrapper over :func:`decode_symbols_from_pil_image`,
    keeping the payloads and dropping the positions.

    Never raises: any decoding/library failure is logged and reported as "nothing
    readable" (empty list) so the caller can fall back to the physical page order.
    """
    try:
        from PIL import Image

        with Image.open(image_path) as img:
            rgb = img.convert("RGB")
    except Exception:
        logger.warning("Code decoding failed for %s", image_path, exc_info=True)
        return []

    return [symbol.payload for symbol in decode_symbols_from_pil_image(rgb)]


def _make_qr_png(value: str) -> bytes | None:
    try:
        import qrcode  # type: ignore
    except Exception:
        return None
    try:
        img = qrcode.make(value or "")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _make_barcode_png(value: str, encoding: CodeEncoding) -> tuple[bytes, str] | None:
    """The barcode PNG and the text a scanner reads off it, or None.

    The text differs from *value* only for EAN / UPC given without their check
    digit: python-barcode appends it, and the scanner reads it.
    """
    try:
        import barcode  # type: ignore
        from barcode.writer import ImageWriter  # type: ignore
    except Exception:
        return None
    # Code 39's check character is optional and zbar does not strip it: without
    # it the scanner reads exactly the printed value.
    symbology_options = {"add_checksum": False} if encoding is CodeEncoding.CODE39 else {}
    try:
        buf = BytesIO()
        code = barcode.get(
            _BARCODE_SYMBOLOGIES[encoding], value or "0",
            writer=ImageWriter(), options=symbology_options,
        )
        # No text inside the image: the human-readable strip of render_code is
        # the only printed text, and it is drawn only when the object asks for it.
        code.write(
            buf,
            options={
                "write_text": False,
                "module_width": _BARCODE_MODULE_WIDTH_MM,
                "quiet_zone": _BARCODE_QUIET_ZONE_MODULES * _BARCODE_MODULE_WIDTH_MM,
            },
        )
        return buf.getvalue(), code.get_fullcode()
    except Exception:
        return None


def _make_code_png(
    code_type: CodeType, value: str, encoding: CodeEncoding | None
) -> tuple[bytes, str] | None:
    """The code PNG and the text a scanner reads off it, or None."""
    if code_type is CodeType.QR:
        png = _make_qr_png(value)
        return None if png is None else (png, value)
    return _make_barcode_png(value, encoding or CodeEncoding.CODE128)


def render_code(
    renderer: PageRenderer,
    x: float, y: float, w: float, h: float,
    *,
    code_type: CodeType,
    value: str,
    human_readable: bool,
    page_index: int | None = None,
    role: PageObjectRole | None = None,
    encoding: CodeEncoding | None = None,
) -> bool:
    """Render a CODE object inside (x, y, w, h). Returns True if a real code
    image was embedded, False if the placeholder fallback was used.

    When *page_index* is not None the page number is appended to the encoded value
    (``value|<page_index>``), used by the VARIANT_IDENTIFIER "include page number"
    option. When *role* is set the payload is prefixed with the
    self-describing role tag (``VID:``/``RID:``). Both default to
    None, which keeps every other caller's behaviour (payload = value only). The
    same payload feeds the QR / barcode image, the placeholder fallback and the
    human-readable strip so a scanner and a human read the identical string.

    A barcode is stretched to fill the whole ``w × code_h`` rectangle — the same
    one the canvas placeholder occupies — because widening the bars only widens
    every module in proportion and keeps it readable; fitted to a wide, low box
    with its proportions kept it would be too narrow to decode at the upload's
    150 dpi. A QR keeps its proportions: a distorted QR does not
    decode.

    A barcode is printed in its *encoding* (None → Code128). The value
    is trusted to fit it — authoring validates it with
    :func:`barcode_value_error` and the export falls back to Code128 for a
    stored value that breaks the rules; should the encoder still refuse it,
    the placeholder is drawn. For EAN / UPC the human-readable strip shows the
    code with its check digit, i.e. what the scanner reads."""
    payload = build_tagged_payload(role, value, page_index)
    hr_h = _HUMAN_READABLE_STRIP_PX if human_readable else 0
    code_h = max(h - hr_h, 1.0)

    image = _make_code_png(code_type, payload, encoding)
    embedded = False
    printed = payload
    if image is not None:
        png, scanned = image
        try:
            renderer.page.insert_image(
                renderer.rect(x, y, w, code_h),
                stream=png,
                keep_proportion=code_type is not CodeType.BARCODE,
            )
            embedded = True
            printed = scanned
        except Exception:
            embedded = False

    if not embedded:
        # Placeholder: bordered box with the type + payload clearly shown.
        renderer.box(x, y, w, code_h, color=GRAY, width=0.8)
        renderer.text(
            x + 3, y + 3, max(w - 6, 1), max(code_h - 6, 1),
            f"[{code_type.value}]\n{payload}", 8, color=GRAY,
        )

    if human_readable:
        renderer.text(
            x, y + code_h, w, hr_h, printed, 8, color=BLACK,
            align=1,  # centered
        )
    return embedded
