# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure unit tests for the tag-driven identification rules.

These cover ``identify_page_codes`` — the decision logic that reads the
``VID:``/``RID:`` tag explicitly instead of a "whichever code carries a page
index must be the variant identifier" heuristic. That heuristic could not tell
a VARIANT_IDENTIFIER from a RESPONDENT_IDENTIFIER without already knowing the
FormVariant, which ingestion does not: resolving it is matching's job, which
runs later.

No DB here; the ORM-facing helpers (``apply_page_codes`` /
``apply_submission_codes``) are exercised end-to-end through the real ingestion
path in ``tests/integration/test_submission_ingestion_integration.py``.

The loose-image priority ladders (``resolve_image_page``) are unit-tested at
the bottom of this module, for the same reason: they are pure policy over the
same two signals, and deserve to be pinned down without a DB.
"""
from __future__ import annotations

import pytest

from app.models.enums import PageOrderSource
from app.services.submission_identification_service import (
    identify_page_codes,
    resolve_image_page,
)


def test_no_codes_identifies_nothing():
    ident = identify_page_codes([])
    assert ident.variant_identifier is None
    assert ident.variant_identifier_raw is None
    assert ident.page_index is None
    assert ident.respondent_identifier is None
    assert ident.has_conflicting_codes is False
    assert ident.page_order_source is PageOrderSource.PDF_ORDER


def test_variant_tag_is_read_explicitly():
    ident = identify_page_codes(["VID:V1"])
    assert ident.variant_identifier == "V1"
    # The RAW payload is preserved for persistence — matching re-parses it.
    assert ident.variant_identifier_raw == "VID:V1"
    assert ident.page_index is None
    assert ident.respondent_identifier is None
    # A code with no page index says nothing about the ORDER.
    assert ident.page_order_source is PageOrderSource.PDF_ORDER


def test_variant_tag_carries_the_printed_page_index():
    ident = identify_page_codes(["VID:V1|2"])
    assert ident.variant_identifier == "V1"
    assert ident.variant_identifier_raw == "VID:V1|2"
    assert ident.page_index == 2
    assert ident.page_order_source is PageOrderSource.CODE


def test_respondent_tag_is_read_explicitly():
    ident = identify_page_codes(["RID:R1"])
    assert ident.respondent_identifier == "R1"
    assert ident.variant_identifier is None
    assert ident.variant_identifier_raw is None
    assert ident.page_index is None
    assert ident.page_order_source is PageOrderSource.PDF_ORDER


def test_both_roles_on_one_page_are_kept_apart():
    """A page-index heuristic could not do this without knowing the
    FormVariant."""
    ident = identify_page_codes(["RID:R1", "VID:V1|3"])
    assert ident.variant_identifier == "V1"
    assert ident.page_index == 3
    assert ident.respondent_identifier == "R1"
    assert ident.has_conflicting_codes is False


def test_a_respondent_code_is_never_mistaken_for_the_variant_identifier():
    """Even when it is the ONLY code on the page."""
    ident = identify_page_codes(["RID:R1"])
    assert ident.variant_identifier is None


def test_untagged_codes_are_ignored_for_identification():
    """Same treatment as "no readable code" — untagged sheets, foreign codes."""
    ident = identify_page_codes(["V1", "V1|1", "some-third-party-qr"])
    assert ident.variant_identifier is None
    assert ident.variant_identifier_raw is None
    assert ident.page_index is None
    assert ident.respondent_identifier is None
    assert ident.has_conflicting_codes is False
    assert ident.page_order_source is PageOrderSource.PDF_ORDER


def test_untagged_codes_do_not_shadow_a_tagged_one():
    ident = identify_page_codes(["noise", "VID:V1|1", "more-noise"])
    assert ident.variant_identifier == "V1"
    assert ident.page_index == 1


def test_multiple_variant_codes_are_resolved_deterministically():
    """Anomalous sheet: the FIRST VID in decode order wins, rest are reported."""
    ident = identify_page_codes(["VID:A|1", "VID:B|2", "VID:C"])
    assert ident.variant_identifier == "A"
    assert ident.variant_identifier_raw == "VID:A|1"
    assert ident.page_index == 1
    assert ident.extra_variant_identifiers == ("B", "C")
    assert ident.has_conflicting_codes is True


def test_multiple_respondent_codes_are_resolved_deterministically():
    ident = identify_page_codes(["RID:R1", "RID:R2"])
    assert ident.respondent_identifier == "R1"
    assert ident.extra_respondent_identifiers == ("R2",)
    assert ident.has_conflicting_codes is True


def test_malformed_codes_never_break_identification():
    ident = identify_page_codes([None, b"VID:V1", "", "VID:", "VID:V1"])
    # "VID:" decodes to an empty value and is a legitimate (if useless) VID, so
    # it wins as the first one seen; the point is that nothing raises.
    assert ident.variant_identifier == ""
    assert ident.extra_variant_identifiers == ("V1",)


def test_none_iterable_is_tolerated():
    assert identify_page_codes(None).variant_identifier is None


# ---------------------------------------------------------------------------
# Loose-image resolution: the two priority ladders
#
# Grouping: RID > filename token > ungroupable.
# Position: VID page index > filename <n> > None (caller uses arrival order).
# Both read the same two signals and rank them differently, on purpose.
# ---------------------------------------------------------------------------


def test_a_rid_groups_and_says_nothing_about_position():
    res = resolve_image_page("scan001.jpg", ["RID:STUDENT-42"])
    assert res.group_key == "STUDENT-42"
    assert res.group_key_from_code is True
    # A RID identifies a person, not a sheet position.
    assert res.declared_page_index is None
    assert res.page_order_source is PageOrderSource.PDF_ORDER


def test_a_conforming_filename_groups_and_positions_when_no_code_reads():
    res = resolve_image_page("rossi_pagina_3.jpg", [])
    assert res.group_key == "rossi"
    assert res.group_key_from_code is False
    assert res.filename_token == "rossi"
    # 1-based <n> -> 0-based source_page_order.
    assert res.declared_page_index == 2
    assert res.page_order_source is PageOrderSource.FILENAME


def test_neither_signal_leaves_the_image_ungroupable():
    res = resolve_image_page("scan001.jpg", [])
    assert res.group_key is None
    assert res.filename_token is None
    assert res.declared_page_index is None
    assert res.page_order_source is PageOrderSource.PDF_ORDER


def test_the_rid_outranks_the_filename_for_grouping():
    """The RID is printed on the sheet; the filename is typed by an operator."""
    res = resolve_image_page("rossi_pagina_2.jpg", ["RID:STUDENT-42"])
    assert res.group_key == "STUDENT-42"
    assert res.group_key_from_code is True
    # …but the filename still supplies the POSITION, since no VID index did.
    assert res.declared_page_index == 1
    assert res.page_order_source is PageOrderSource.FILENAME


def test_the_vid_index_outranks_the_filename_for_position():
    res = resolve_image_page("rossi_pagina_9.jpg", ["VID:VAR-A|2"])
    assert res.declared_page_index == 1  # from the code (1-based -> 0-based)
    assert res.page_order_source is PageOrderSource.CODE
    # No RID, so the FILENAME still decided the grouping.
    assert res.group_key == "rossi"
    assert res.group_key_from_code is False


def test_both_tags_on_one_image_apply_to_their_own_question():
    """The scenario the whole tag design exists for: group from the RID,
    position from the VID, on the same sheet."""
    res = resolve_image_page("whatever.jpg", ["RID:STUDENT-42", "VID:VAR-A|3"])
    assert res.group_key == "STUDENT-42"
    assert res.group_key_from_code is True
    assert res.declared_page_index == 2
    assert res.page_order_source is PageOrderSource.CODE
    assert res.identification.variant_identifier_raw == "VID:VAR-A|3"


def test_a_vid_without_a_page_index_declares_no_position():
    """include_page_index=False is the default, so this is the common case."""
    res = resolve_image_page("scan.jpg", ["VID:VAR-A"])
    assert res.declared_page_index is None
    assert res.page_order_source is PageOrderSource.PDF_ORDER
    # The value is still captured for the matching engine.
    assert res.identification.variant_identifier_raw == "VID:VAR-A"
    # A VID does NOT group: it names the variant, not the candidate.
    assert res.group_key is None


def test_an_untagged_code_neither_groups_nor_positions():
    res = resolve_image_page("scan.jpg", ["STUDENT-42", "VAR-A|1"])
    assert res.group_key is None
    assert res.declared_page_index is None
    assert res.page_order_source is PageOrderSource.PDF_ORDER


@pytest.mark.parametrize("bad_index", ["VID:VAR-A|0", "VID:VAR-A|-2"])
def test_a_code_page_index_below_one_is_treated_as_no_signal(bad_index):
    """page_index is FormVariantPage.page_index, which is 1-based, so 0 or a
    negative cannot be real — and must not become a negative position."""
    res = resolve_image_page("rossi_pagina_4.jpg", [bad_index])
    # Falls through to the filename rather than yielding -1.
    assert res.declared_page_index == 3
    assert res.page_order_source is PageOrderSource.FILENAME


def test_a_missing_filename_is_tolerated():
    res = resolve_image_page(None, ["RID:R1"])
    assert res.group_key == "R1"
    assert res.filename_token is None


# --- the naming-slip anomaly ------------------------------------------------
def test_a_rid_disagreeing_with_the_filename_is_flagged_not_blocked():
    res = resolve_image_page("bianchi_pagina_1.jpg", ["RID:ROSSI"])
    assert res.group_key == "ROSSI"          # the RID wins
    assert res.filename_token == "bianchi"   # …and the disagreement is kept
    assert res.has_group_key_mismatch is True


def test_a_rid_agreeing_with_the_filename_is_not_a_mismatch():
    res = resolve_image_page("ROSSI_pagina_1.jpg", ["RID:ROSSI"])
    assert res.group_key == "ROSSI"
    assert res.has_group_key_mismatch is False


def test_a_filename_alone_can_never_be_a_mismatch():
    """Nothing to disagree with: the filename IS the grouping key."""
    res = resolve_image_page("rossi_pagina_1.jpg", [])
    assert res.has_group_key_mismatch is False


def test_a_non_conforming_filename_alongside_a_rid_is_not_a_mismatch():
    """A name that does not follow the convention makes no claim at all."""
    res = resolve_image_page("scan001.jpg", ["RID:ROSSI"])
    assert res.filename_token is None
    assert res.has_group_key_mismatch is False
