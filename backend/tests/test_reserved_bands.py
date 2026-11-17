# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``alignment_marks.in_reserved_band``: the rule shared by saving, publishing
and (through ``exportLayout.inReservedBand``) the editor.

The usable area of an A4 page at 96 dpi is x 57..737, y 57..1066, edges
included; anything partly outside it — in a band or off the page — is refused.
"""
from __future__ import annotations

import pytest

from app.utils.alignment_marks import BAND_W, in_reserved_band, usable_rect

W, H = 794, 1123


def test_usable_rect_is_the_page_minus_the_bands():
    assert usable_rect(W, H) == (BAND_W, BAND_W, W - 2 * BAND_W, H - 2 * BAND_W)


def test_an_object_on_the_edges_of_the_area_is_valid():
    x, y, w, h = usable_rect(W, H)
    assert not in_reserved_band(x, y, w, h, W, H)
    assert not in_reserved_band(57, 57, 10, 10, W, H)
    assert not in_reserved_band(727, 1056, 10, 10, W, H)


@pytest.mark.parametrize(
    "side, rect",
    [
        ("left", (56.5, 100, 50, 50)),
        ("top", (100, 56, 50, 50)),
        ("right", (688, 100, 50, 50)),   # ends at 738 > 737
        ("bottom", (100, 1017, 50, 50)),  # ends at 1067 > 1066
    ],
)
def test_an_object_crossing_one_side_is_in_the_band(side, rect):
    assert in_reserved_band(*rect, W, H), side


@pytest.mark.parametrize(
    "rect",
    [
        (999, 100, 50, 50),   # wholly off the page to the right
        (100, 2000, 50, 50),  # below the page
        (0, 0, W, H),         # the whole page
        (60, 60, 700, 50),    # wider than the area
    ],
)
def test_an_object_off_the_page_or_larger_than_the_area_is_in_the_band(rect):
    assert in_reserved_band(*rect, W, H)


def test_other_page_sizes_use_their_own_edges():
    # A5 landscape-ish page: the right band starts at 600 - 57 = 543.
    assert not in_reserved_band(400, 100, 143, 50, 600, 400)
    assert in_reserved_band(400, 100, 144, 50, 600, 400)
