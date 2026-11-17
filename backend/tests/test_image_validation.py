# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-free unit tests for content-based image validation.

Raster fixtures are generated in-memory with Pillow so the sniff is exercised
against real magic bytes (never the extension/declared type).
"""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.utils.image_validation import (
    UnsupportedImageError,
    sniff_image,
)


def _raster(fmt: str) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 6), "red").save(buf, format=fmt)
    return buf.getvalue()


def test_png_sniffed_with_dimensions():
    s = sniff_image(_raster("PNG"))
    assert s.mime_type == "image/png"
    assert s.ext == ".png"
    assert s.is_svg is False
    assert (s.width_px, s.height_px) == (4, 6)


def test_jpeg_sniffed():
    s = sniff_image(_raster("JPEG"))
    assert s.mime_type == "image/jpeg"
    assert s.ext == ".jpg"
    assert (s.width_px, s.height_px) == (4, 6)


def test_bmp_sniffed():
    s = sniff_image(_raster("BMP"))
    assert s.mime_type == "image/bmp"
    assert s.ext == ".bmp"


def test_svg_detected():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    s = sniff_image(svg)
    assert s.mime_type == "image/svg+xml"
    assert s.ext == ".svg"
    assert s.is_svg is True
    assert s.width_px is None and s.height_px is None


def test_unsupported_raster_rejected():
    # GIF is a real image but outside the whitelist -> rejected.
    with pytest.raises(UnsupportedImageError):
        sniff_image(_raster("GIF"))


def test_garbage_rejected():
    with pytest.raises(UnsupportedImageError):
        sniff_image(b"definitely not an image")


def test_empty_rejected():
    with pytest.raises(UnsupportedImageError):
        sniff_image(b"")
