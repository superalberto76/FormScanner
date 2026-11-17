# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the CODE payload encode/decode contract.

``parse_payload`` is the exact inverse of ``build_payload``, and it is TOTAL: no
input may raise, because a malformed payload read off a scanned page must
degrade to "no page index" rather than abort the ingestion of a whole PDF.

Pure — no DB, no pyzbar — so these run everywhere.
"""
from __future__ import annotations

import pytest

from app.utils.code_rendering import PAGE_INDEX_SEPARATOR, build_payload, parse_payload


# ---------------------------------------------------------------------------
# Round-trip: whatever build_payload emits, parse_payload must recover
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,page_index",
    [
        ("V1", None),
        ("V1", 0),
        ("V1", 1),
        ("V1", 3),
        ("V1", 42),
        ("", 2),
        # Page indices are 1-based; the encoder is not told that, so both
        # bases must round-trip identically.
        ("VARIANT-A", 1),
    ],
)
def test_round_trip(value, page_index):
    assert parse_payload(build_payload(value, page_index)) == (value, page_index)


# ---------------------------------------------------------------------------
# No separator -> the whole string is the value
# ---------------------------------------------------------------------------
def test_payload_without_separator_has_no_page_index():
    assert parse_payload("V1") == ("V1", None)
    assert parse_payload("") == ("", None)


# ---------------------------------------------------------------------------
# A value containing the separator still round-trips (split on the LAST one)
# ---------------------------------------------------------------------------
def test_value_containing_separator_round_trips():
    raw = build_payload("A|B", 7)
    assert raw == "A|B|7"
    assert parse_payload(raw) == ("A|B", 7)


def test_value_containing_separator_without_page_index():
    # No trailing integer -> nothing was appended, so it is all value.
    assert parse_payload("A|B") == ("A|B", None)


# ---------------------------------------------------------------------------
# Malformed tails never raise — they are treated as part of the value
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "V1|",          # separator, empty tail
        "V1|abc",       # not a number
        "V1|1.5",       # not an integer
        "V1| 3",        # padded — not what build_payload emits
        "V1|+3",        # signed — not what build_payload emits
        "V1|007",       # zero-padded — not what build_payload emits
        "V1|3x",        # trailing junk
        "|",            # separator only
        "V1|²",         # digit-like unicode that int() rejects
    ],
)
def test_malformed_tail_is_not_a_page_index(raw):
    value, page_index = parse_payload(raw)
    assert page_index is None
    assert value == raw  # returned untouched, nothing was stripped


def test_separator_constant_is_shared_with_the_encoder():
    """Decoding must key off the SAME constant the encoder appends, so the two
    can never drift apart."""
    assert PAGE_INDEX_SEPARATOR == "|"
    assert build_payload("V", 5) == f"V{PAGE_INDEX_SEPARATOR}5"


def test_unicode_digits_are_rejected():
    """int() accepts non-ASCII decimal digits; the strict round-trip check means
    they are NOT mistaken for a page index."""
    assert parse_payload("V1|٣") == ("V1|٣", None)
