# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SVG sanitization for untrusted uploads.

An uploaded SVG is an XML document that a browser will execute when rendered
inline. We never trust one verbatim. Two layers of defence:

1. **Safe parse** — ``defusedxml`` parses the bytes with external entities,
   entity expansion (billion laughs) and external DTDs disabled, neutralising
   the XXE / entity-expansion attack surface at parse time.
2. **Allowlist strip** — the parsed tree is walked and anything that can run
   script or pull a remote resource is removed before re-serialization:
     * elements: ``<script>``, ``<foreignObject>`` (+ a few active-content tags)
     * attributes: every ``on*`` event handler
     * ``href`` / ``xlink:href`` pointing anywhere but a local ``#fragment`` or an
       embedded ``data:image/*`` (so remote/``javascript:`` refs are dropped)

The serialized result is what gets stored — raw SVG is NEVER persisted. The
serve endpoint additionally sets ``X-Content-Type-Options: nosniff`` and a
restrictive ``Content-Security-Policy`` and serves the file with
``Content-Disposition`` so a browser cannot be tricked into executing it.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

# Elements removed wholesale — they can execute script or embed active/remote
# content. Compared on the lower-cased local name (namespace stripped).
_FORBIDDEN_TAGS = {
    "script",
    "foreignobject",
    "iframe",
    "embed",
    "object",
    "audio",
    "video",
    "handler",  # SVG Tiny 1.2 scripting element
}

# href schemes we keep. Anything else (http(s), ftp, file, javascript:, raw
# data:, data:image/svg+xml, …) is dropped from the attribute.
_SAFE_DATA_PREFIXES = (
    "data:image/png",
    "data:image/jpeg",
    "data:image/jpg",
    "data:image/gif",
    "data:image/bmp",
    "data:image/webp",
)


class SvgSanitizeError(ValueError):
    """Raised when the payload is not a valid/safe SVG document."""


def _local(tag: object) -> str:
    """Lower-cased local name of an ElementTree tag (drops any ``{ns}``)."""
    if not isinstance(tag, str):  # comments / PIs have callable tags
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _href_is_safe(value: str) -> bool:
    v = value.strip().lower()
    if v.startswith("#"):
        return True
    return any(v.startswith(p) for p in _SAFE_DATA_PREFIXES)


def _clean_attrs(elem: ET.Element) -> None:
    """Strip event-handler and unsafe-href attributes from *elem* in place."""
    for key in list(elem.attrib):
        local = _local(key)
        if local.startswith("on"):  # onload, onclick, onmouseover, …
            del elem.attrib[key]
        elif local == "href":  # plain href or {xlink}href
            if not _href_is_safe(elem.attrib[key]):
                del elem.attrib[key]


def _clean_tree(elem: ET.Element) -> None:
    """Recursively drop forbidden children and sanitise surviving attributes."""
    _clean_attrs(elem)
    for child in list(elem):
        if _local(child.tag) in _FORBIDDEN_TAGS:
            elem.remove(child)
        else:
            _clean_tree(child)


def _parse(data: bytes) -> ET.Element:
    try:
        return _safe_fromstring(data)
    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
        raise SvgSanitizeError(f"Not a parseable/safe SVG: {exc}") from exc


def looks_like_svg(data: bytes) -> bool:
    """Cheap, safe check that *data* is an SVG document (root ``<svg>``)."""
    try:
        return _local(_parse(data).tag) == "svg"
    except SvgSanitizeError:
        return False


def sanitize_svg(data: bytes) -> bytes:
    """Return a sanitised copy of an SVG document.

    Raises ``SvgSanitizeError`` if the bytes are not a parseable, safe SVG.
    """
    root = _parse(data)
    if _local(root.tag) != "svg":
        raise SvgSanitizeError("Root element is not <svg>")

    _clean_tree(root)

    # Emit clean prefixes (xmlns="…/svg", xmlns:xlink) instead of ns0/ns1.
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body).encode("utf-8")
