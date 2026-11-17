# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure unit tests for the tagged CODE payload contract.

No DB, no PyMuPDF: these exercise only the encode/decode pair in
``app.utils.code_rendering`` that makes a CODE payload self-describing —
``VID:``/``RID:`` — so ingestion can tell a variant identifier from a respondent
identifier without first knowing the FormVariant.

The untagged inner pair it builds on (``build_payload`` / ``parse_payload``)
is covered by ``tests/test_code_payload_parsing.py``; only the tag layer is
tested here.
"""
from __future__ import annotations

import pytest

from app.models.enums import PageObjectRole
from app.utils import code_rendering
from app.utils.code_rendering import build_tagged_payload, parse_tagged_payload


# ---------------------------------------------------------------------------
# Separators
# ---------------------------------------------------------------------------
def test_separators_are_distinct():
    """The tag separator must never be confusable with the page-index one."""
    assert code_rendering.PAGE_INDEX_SEPARATOR == "|"
    assert code_rendering.ROLE_TAG_SEPARATOR == ":"
    assert code_rendering.ROLE_TAG_SEPARATOR != code_rendering.PAGE_INDEX_SEPARATOR
    assert code_rendering.TAG_VARIANT_IDENTIFIER == "VID"
    assert code_rendering.TAG_RESPONDENT_IDENTIFIER == "RID"
    assert code_rendering.TAG_UNTAGGED == "UNTAGGED"


# ---------------------------------------------------------------------------
# build_tagged_payload — the encode side
# ---------------------------------------------------------------------------
def test_variant_identifier_is_tagged_vid():
    assert (
        build_tagged_payload(PageObjectRole.VARIANT_IDENTIFIER, "V1", None) == "VID:V1"
    )
    assert build_tagged_payload(PageObjectRole.VARIANT_IDENTIFIER, "V1", 0) == "VID:V1|0"
    assert build_tagged_payload(PageObjectRole.VARIANT_IDENTIFIER, "V1", 3) == "VID:V1|3"


def test_respondent_identifier_is_tagged_rid_and_never_carries_a_page_index():
    assert (
        build_tagged_payload(PageObjectRole.RESPONDENT_IDENTIFIER, "R1", None)
        == "RID:R1"
    )
    # Even if a page index were passed in, a respondent identifier drops it.
    assert (
        build_tagged_payload(PageObjectRole.RESPONDENT_IDENTIFIER, "R1", 2) == "RID:R1"
    )


def test_code_without_role_keeps_the_untagged_payload():
    """No role -> byte-for-byte the untagged payload, no tag whatsoever."""
    assert build_tagged_payload(None, "V1", None) == "V1"
    assert build_tagged_payload(None, "V1", 4) == "V1|4"


# ---------------------------------------------------------------------------
# parse_tagged_payload — the decode side
# ---------------------------------------------------------------------------
def test_parse_tagged_payload_reads_the_variant_tag():
    assert parse_tagged_payload("VID:V1") == ("VID", "V1", None)
    assert parse_tagged_payload("VID:V1|0") == ("VID", "V1", 0)
    assert parse_tagged_payload("VID:V1|12") == ("VID", "V1", 12)


def test_parse_tagged_payload_reads_the_respondent_tag():
    assert parse_tagged_payload("RID:R1") == ("RID", "R1", None)
    # A "|" inside a respondent value is part of the value, never a page index.
    assert parse_tagged_payload("RID:R1|3") == ("RID", "R1|3", None)


@pytest.mark.parametrize(
    "role,value,page_index",
    [
        (PageObjectRole.VARIANT_IDENTIFIER, "V1", None),
        (PageObjectRole.VARIANT_IDENTIFIER, "V1", 0),
        (PageObjectRole.VARIANT_IDENTIFIER, "V-2024|A", 5),
        (PageObjectRole.RESPONDENT_IDENTIFIER, "R1", None),
        (None, "plain", None),
    ],
)
def test_tagged_round_trip(role, value, page_index):
    tag, parsed_value, parsed_index = parse_tagged_payload(
        build_tagged_payload(role, value, page_index)
    )
    expected_tag = {
        PageObjectRole.VARIANT_IDENTIFIER: "VID",
        PageObjectRole.RESPONDENT_IDENTIFIER: "RID",
        None: "UNTAGGED",
    }[role]
    assert tag == expected_tag
    assert parsed_value == value
    assert parsed_index == (page_index if role is PageObjectRole.VARIANT_IDENTIFIER else None)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "V1",
        "V1|0",  # untagged payload: readable, but claims no role
        "VID",  # tag without separator
        "vid:V1",  # tags are case-sensitive
        "XID:V1",
        ":V1",
        "|",
        "RID",
        "VID:VID:V1",  # nested-looking garbage: inner text is just the value
        "  VID:V1",  # leading whitespace defeats the prefix match, by design
        "\x00\xff nonsense",
    ],
)
def test_parse_tagged_payload_never_raises_on_malformed_input(raw):
    tag, value, page_index = parse_tagged_payload(raw)
    assert tag in ("VID", "RID", "UNTAGGED")
    assert isinstance(value, str)
    assert page_index is None or isinstance(page_index, int)


def test_parse_tagged_payload_untagged_passes_the_raw_value_through():
    assert parse_tagged_payload("V1") == ("UNTAGGED", "V1", None)
    assert parse_tagged_payload("V1|0") == ("UNTAGGED", "V1|0", None)
    assert parse_tagged_payload("") == ("UNTAGGED", "", None)


def test_parse_tagged_payload_tolerates_a_non_string():
    """Decoders can hand back None/bytes on a failed read — never blow up."""
    assert parse_tagged_payload(None) == ("UNTAGGED", "", None)
    assert parse_tagged_payload(b"VID:V1") == ("UNTAGGED", "", None)
    assert parse_tagged_payload(42) == ("UNTAGGED", "", None)


def test_empty_tagged_payloads_degrade_to_an_empty_value():
    assert parse_tagged_payload("VID:") == ("VID", "", None)
    assert parse_tagged_payload("RID:") == ("RID", "", None)
