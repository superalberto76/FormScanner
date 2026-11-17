# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content-based validation of uploaded IMAGE files.

The authoritative check is the *content*, never the declared Content-Type or the
filename extension (both attacker-controlled). Raster formats are sniffed with
Pillow (magic-byte based); SVG is detected by safely parsing the XML and checking
for an ``<svg>`` root. Anything outside the whitelist (PNG / JPEG / BMP / SVG) is
rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.utils.svg_sanitizer import looks_like_svg

PNG = "image/png"
JPEG = "image/jpeg"
BMP = "image/bmp"
SVG = "image/svg+xml"

ALLOWED_MIME_TYPES = frozenset({PNG, JPEG, BMP, SVG})

# Pillow format name -> (mime, canonical extension).
_RASTER = {
    "PNG": (PNG, ".png"),
    "JPEG": (JPEG, ".jpg"),
    "BMP": (BMP, ".bmp"),
}


class UnsupportedImageError(ValueError):
    """Raised when the uploaded bytes are not a supported image."""


@dataclass(frozen=True)
class SniffedImage:
    mime_type: str
    ext: str
    is_svg: bool
    width_px: int | None
    height_px: int | None


def sniff_image(data: bytes) -> SniffedImage:
    """Determine the real type of *data* or raise ``UnsupportedImageError``.

    Returns the sniffed MIME type, a canonical extension and (for raster images)
    the intrinsic pixel dimensions. SVG carries no raster dimensions.
    """
    if not data:
        raise UnsupportedImageError("Empty file.")

    # 1) Raster via Pillow (PNG / JPEG / BMP). Pillow identifies by content.
    try:
        with Image.open(BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
    except (UnidentifiedImageError, OSError, ValueError):
        fmt = None
        width = height = None

    if fmt is not None:
        if fmt not in _RASTER:
            raise UnsupportedImageError(
                f"Unsupported image format '{fmt}'. Allowed: PNG, JPEG, BMP, SVG."
            )
        mime, ext = _RASTER[fmt]
        return SniffedImage(mime, ext, is_svg=False, width_px=width, height_px=height)

    # 2) SVG (XML) — parsed safely (no entity/XXE expansion).
    if looks_like_svg(data):
        return SniffedImage(SVG, ".svg", is_svg=True, width_px=None, height_px=None)

    raise UnsupportedImageError(
        "Unsupported or invalid image. Allowed: PNG, JPEG, BMP, SVG."
    )
