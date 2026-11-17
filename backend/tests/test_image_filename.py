# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure unit tests for the image-upload naming convention.

Covers ``parse_image_filename`` — the fallback that tells ingestion which
submission a loose image belongs to, and where in it, when the sheet carries no
readable code. The convention is ``<submission>_page_<n>.<ext>``, with
``_pagina_`` accepted as an equivalent separator.

No DB: the ORM-facing use of this parser is exercised end-to-end in
``tests/integration/test_submission_image_ingestion_integration.py``.
"""
from __future__ import annotations

import pytest

from app.utils.image_filename import parse_image_filename


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_conforming_filename_yields_token_and_zero_based_index():
    # <n> is 1-BASED in the convention, source_page_order is 0-based.
    assert parse_image_filename("smith-john_page_1.jpg") == ("smith-john", 0)
    assert parse_image_filename("smith-john_page_2.jpg") == ("smith-john", 1)


def test_the_pagina_separator_is_equivalent():
    assert parse_image_filename("smith-john_pagina_1.jpg") == ("smith-john", 0)
    assert parse_image_filename("smith-john_pagina_2.jpg") == ("smith-john", 1)


def test_the_extension_is_irrelevant():
    """What a file really IS comes from content sniffing, never from its name."""
    for name in ("x_page_3.jpg", "x_page_3.png", "x_page_3.tiff", "x_page_3"):
        assert parse_image_filename(name) == ("x", 2)


def test_the_grouping_token_may_contain_underscores():
    assert parse_image_filename("ROOM3_DESK12_page_1.png") == ("ROOM3_DESK12", 0)


# ---------------------------------------------------------------------------
# Case handling — insensitive on the literal and the extension, NOT on the token
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "x_page_1.jpg",
        "x_PAGE_1.JPG",
        "x_Page_1.Jpg",
        "x_pAgE_1.jPg",
        "x_pagina_1.jpg",
        "x_PAGINA_1.JPG",
        "x_Pagina_1.Jpg",
        "x_pAgInA_1.jPg",
    ],
)
def test_separator_and_extension_are_case_insensitive(name):
    assert parse_image_filename(name) == ("x", 0)


def test_the_grouping_token_keeps_its_case():
    """Two submissions deliberately named 'A' and 'a' must stay distinct: the
    token is a grouping KEY, so folding its case would silently merge them."""
    assert parse_image_filename("Smith_page_1.jpg") == ("Smith", 0)
    assert parse_image_filename("smith_page_1.jpg") == ("smith", 0)


# ---------------------------------------------------------------------------
# The LAST separator is the split point
# ---------------------------------------------------------------------------
def test_the_last_separator_wins():
    """What makes the grouping token free-form: it may itself contain the
    separator."""
    assert parse_image_filename("test_page_green_page_2.jpg") == (
        "test_page_green",
        1,
    )
    assert parse_image_filename("test_pagina_green_pagina_2.jpg") == (
        "test_pagina_green",
        1,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("test_page_green_pagina_2.jpg", ("test_page_green", 1)),
        ("test_pagina_green_page_2.jpg", ("test_pagina_green", 1)),
        ("x_PAGINA_7_Page_3.jpg", ("x_PAGINA_7", 2)),
    ],
)
def test_the_last_separator_of_either_kind_wins(name, expected):
    assert parse_image_filename(name) == expected


def test_a_token_ending_in_a_number_is_not_confused_with_the_page():
    assert parse_image_filename("room_page_12_page_3.jpg") == ("room_page_12", 2)


# ---------------------------------------------------------------------------
# The page number
# ---------------------------------------------------------------------------
def test_zero_padding_is_accepted():
    """Scanners routinely zero-pad. Deliberately looser than
    code_rendering.parse_payload, which round-trips a payload WE generated."""
    assert parse_image_filename("x_page_01.jpg") == ("x", 0)
    assert parse_image_filename("x_page_007.jpg") == ("x", 6)
    assert parse_image_filename("x_pagina_007.jpg") == ("x", 6)


def test_large_page_numbers_parse():
    assert parse_image_filename("x_page_250.jpg") == ("x", 249)


@pytest.mark.parametrize(
    "name",
    [
        "x_page_0.jpg",         # 1-based by definition: there is no page zero
        "x_page_-1.jpg",        # nor a negative one
        "x_page_+1.jpg",        # a sign is not "a padded number"
        "x_page_ 1.jpg",        # nor is whitespace
        "x_page_1.5.jpg",       # nor a decimal
        "x_page_one.jpg",       # nor a word
        "x_page_.jpg",          # nor nothing at all
        "x_pagina_0.jpg",       # the same rules hold for the other separator
        "x_pagina_-1.jpg",
        "x_pagina_+1.jpg",
        "x_pagina_ 1.jpg",
        "x_pagina_1.5.jpg",
        "x_pagina_one.jpg",
        "x_pagina_.jpg",
    ],
)
def test_non_conforming_page_numbers_are_rejected(name):
    assert parse_image_filename(name) is None


# ---------------------------------------------------------------------------
# Non-conforming names — a normal outcome, never an error
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "scan001.jpg",           # no separator at all
        "x_pag_1.jpg",           # an abbreviation is not the convention
        "x-page-1.jpg",          # hyphens are not underscores
        "xpage1.jpg",            # no underscores at all
        "_page_1.jpg",           # empty grouping token
        "_pagina_1.jpg",
        "page_1.jpg",            # no leading underscore -> no separator match
        "",                      # empty
        "   ",                   # whitespace only
    ],
)
def test_non_conforming_filenames_return_none(name):
    assert parse_image_filename(name) is None


def test_directory_components_are_stripped():
    """A client cannot smuggle a path in — and the name never reaches the
    filesystem anyway (a page image is stored under its SubmissionPage id)."""
    assert parse_image_filename("../../etc/smith_page_1.jpg") == ("smith", 0)
    assert parse_image_filename("/tmp/smith_page_1.jpg") == ("smith", 0)


@pytest.mark.parametrize("value", [None, 42, b"x_page_1.jpg", object()])
def test_non_string_input_is_tolerated(value):
    """Total, like every parser on the ingestion path: a bad name must never be
    able to fail an upload."""
    assert parse_image_filename(value) is None
