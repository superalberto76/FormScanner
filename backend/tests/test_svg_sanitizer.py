# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-free unit tests for the SVG sanitizer.

Pure-function tests — no database, no FastAPI. Cover the security-critical
behaviour: scripts / event handlers / foreignObject / remote refs are stripped,
benign markup survives, and XXE / entity attacks are rejected at parse time.
"""
from __future__ import annotations

import pytest

from app.utils.svg_sanitizer import SvgSanitizeError, looks_like_svg, sanitize_svg

_SVG_OPEN = (
    '<svg xmlns="http://www.w3.org/2000/svg"'
    ' xmlns:xlink="http://www.w3.org/1999/xlink">'
)


def _out(svg: str) -> str:
    return sanitize_svg(svg.encode("utf-8")).decode("utf-8").lower()


def test_valid_svg_is_preserved():
    out = _out(f'{_SVG_OPEN}<rect width="10" height="10"/></svg>')
    assert "<svg" in out
    assert "rect" in out


def test_script_element_removed():
    out = _out(f'{_SVG_OPEN}<script>alert(1)</script><rect/></svg>')
    assert "script" not in out
    assert "alert" not in out


def test_event_handler_attributes_removed():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" onload="evil()">'
        '<rect onclick="boom()" onmouseover="x()"/></svg>'
    )
    out = _out(svg)
    assert "onload" not in out
    assert "onclick" not in out
    assert "onmouseover" not in out
    assert "evil" not in out and "boom" not in out


def test_foreign_object_removed():
    svg = (
        f'{_SVG_OPEN}<foreignObject><body xmlns="http://www.w3.org/1999/xhtml">'
        "hi</body></foreignObject><rect/></svg>"
    )
    out = _out(svg)
    assert "foreignobject" not in out


def test_remote_href_stripped_fragment_kept():
    svg = (
        f'{_SVG_OPEN}<a xlink:href="http://evil.example/x"><rect/></a>'
        '<use xlink:href="#safe"/></svg>'
    )
    out = _out(svg)
    assert "evil.example" not in out
    assert "#safe" in out  # local fragment references are allowed


def test_non_svg_rejected():
    assert looks_like_svg(b"<html><body>hi</body></html>") is False
    with pytest.raises(SvgSanitizeError):
        sanitize_svg(b"<html></html>")
    with pytest.raises(SvgSanitizeError):
        sanitize_svg(b"this is not xml at all")


def test_xxe_entity_rejected():
    # External / internal entity definitions must be refused by the safe parser.
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>'
    ).encode("utf-8")
    with pytest.raises(SvgSanitizeError):
        sanitize_svg(payload)
