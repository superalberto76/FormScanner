# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decoding of a role-less CODE.

A CODE with no scanning role is printed CONTENT — a QR / barcode that is part of
the form, not an identifier matching consumes (a CODE holding
VARIANT_IDENTIFIER / RESPONDENT_IDENTIFIER never reaches this module: the
orchestrator's ``_is_never_evaluable`` drops it before dispatch). This module
reads it back and records what it says.

Decode the WHOLE page, then match by position
---------------------------------------------
Unlike the QUESTION / MATRIX / FREE_TEXT_AREA extractors, this one does NOT crop
the object's region before reading it. pyzbar already searches the whole image
for a symbol, so the page is decoded in full
(``code_rendering.decode_symbols_from_pil_image``) and, among the symbols that
qualify (below), the one whose centre is nearest to the centre of the object's
EXPECTED region is taken as this object's reading (:func:`_find_matching_symbol`).

The expected region comes from ``question_extraction_service
.resolve_scan_region`` — the same proportional mapping the other extractors
use, on the same image (the straightened one for a page aligned to its
template; the original otherwise). Here it is used only to
CHOOSE between symbols, never to cut the image: on a page that could not be
aligned, a skewed sheet whose mapped rectangle would slice through the code
still decodes, because zbar finds the symbol wherever it really is.

A symbol qualifies only if:

  * its centre falls inside the expected region widened, on each side, by
    :data:`MATCH_MARGIN_FACTOR` times the region's size on that axis — half its
    width horizontally, half its height vertically. Tolerant of the
    proportional mapping's drift on a page not aligned, yet tight on the
    short side of a wide, low barcode, where a radius tied to the diagonal
    reached a VID printed nearby;
  * its payload carries no role tag (``VID:``/``RID:``, read with
    ``code_rendering.parse_tagged_payload``): an identifier is never this
    object's content, even printed right next to it. A role-less CODE whose own
    value starts with such a tag is therefore never read, and goes to review.

A symbol that does not qualify is never attributed to this object, even when it
is the only one on the page: it belongs to another CODE, or to nothing we
authored. With none left, the decode fails and the result goes to review —
never someone else's value accepted in silence.

No confidence, a binary outcome
-------------------------------
A decode passes the format's intrinsic checksum or it fails; there is no
continuous axis. ``confidence_score`` is therefore ALWAYS NULL, and
``validity_forces_review`` is simply ``not decode_successful``.
``review_required`` still goes through
``confidence_threshold_service.compute_review_required`` like every other
extractor — with a NULL confidence it reduces to "the decode failed", whatever
the batch threshold, with no bypass here.

The image is opened with ``question_extraction_service._open_gray_scan``, the
same storage-validated read the other extractors use. Greyscale loses nothing:
pyzbar converts any Pillow image to ``'L'`` itself before decoding.

Transactions: nothing here flushes or commits, the same discipline as the
other extractors — the caller owns the transaction.
"""
from __future__ import annotations

import math

from sqlalchemy.orm import Session

from app.models.enums import SubmissionResultType
from app.models.page_object import PageObject
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.repositories import submission_object_result_repo
from app.schemas.submission_result import CodeDetectionPayload
from app.services import confidence_threshold_service, question_extraction_service
from app.services.question_extraction_service import ScanRegion
from app.utils.code_rendering import (
    TAG_UNTAGGED,
    DecodedSymbol,
    decode_symbols_from_pil_image,
    parse_tagged_payload,
)

# How far outside the expected region a symbol's centre may fall, on each side,
# as a fraction of the region's size on that axis — so a code meant for another
# CODE on the same page is not stolen by mistake.
MATCH_MARGIN_FACTOR = 0.5


def _qualifies(symbol: DecodedSymbol, region: ScanRegion) -> bool:
    """Whether *symbol* may be this object's reading: centre inside the widened
    region, and no role tag in its payload (see the module docstring)."""
    margin_x = MATCH_MARGIN_FACTOR * region.width
    margin_y = MATCH_MARGIN_FACTOR * region.height
    inside = (
        region.x - margin_x <= symbol.center_x <= region.x + region.width + margin_x
        and region.y - margin_y <= symbol.center_y <= region.y + region.height + margin_y
    )
    return inside and parse_tagged_payload(symbol.payload)[0] == TAG_UNTAGGED


def _find_matching_symbol(
    symbols: list[DecodedSymbol], region: ScanRegion
) -> DecodedSymbol | None:
    """The qualifying symbol whose centre is nearest to *region*'s centre, or
    ``None``.

    With no qualifying symbol the decode counts as failed for THIS object, even
    if other codes were read elsewhere on the page. Ties keep the decoder's
    order.
    """
    center_x = region.x + region.width / 2
    center_y = region.y + region.height / 2

    best: DecodedSymbol | None = None
    best_distance = math.inf
    for symbol in symbols:
        if not _qualifies(symbol, region):
            continue
        distance = math.hypot(symbol.center_x - center_x, symbol.center_y - center_y)
        if distance < best_distance:
            best, best_distance = symbol, distance
    return best


def extract_code_result(
    db: Session,
    submission_page: SubmissionPage,
    page_object: PageObject,
    batch: SubmissionBatch,
) -> SubmissionObjectResult:
    """Decode one role-less CODE off one scanned sheet.

    Preconditions, all raising rather than guessing: *submission_page* must be
    MATCHED to a template page, *page_object* must carry a ``code_config``, and
    it must hold NO role — ``_is_never_evaluable`` drops a role-carrying CODE
    before dispatch, so receiving one means dispatch has already gone wrong.

    Builds and STAGES a ``SubmissionObjectResult`` carrying:

      * ``detected_payload_json`` — a ``CodeDetectionPayload``: the matched
        symbol's raw payload, or ``None`` with ``decode_successful=False``;
      * ``confidence_score`` — ALWAYS NULL (see the module docstring);
      * ``validity_forces_review`` — ``not decode_successful``;
      * ``review_required`` — via ``confidence_threshold_service``, like every
        other extractor.

    Does NOT flush or commit: the caller owns the transaction.
    """
    if submission_page.form_variant_page_id is None:
        raise ValueError(
            f"SubmissionPage {submission_page.id} is not matched to a template "
            "page: extraction needs form_variant_page_id, which matching "
            "must have resolved first"
        )
    if page_object.code_config is None:
        raise ValueError(
            f"PageObject {page_object.id} has no code_config: "
            "extract_code_result only handles CODE objects"
        )
    role = page_object.effective_role
    if role is not None:
        raise ValueError(
            f"PageObject {page_object.id} holds the {role.value} role: "
            "extract_code_result must not be called on it — a role-carrying CODE "
            "is consumed by matching, and dispatch never routes it here"
        )

    gray_image = question_extraction_service._open_gray_scan(submission_page)
    symbols = decode_symbols_from_pil_image(gray_image)
    # Used to CHOOSE among the page's symbols, never to crop.
    region = question_extraction_service.resolve_scan_region(
        page_object, submission_page.form_variant_page, *gray_image.size
    )
    match = _find_matching_symbol(symbols, region)

    decode_successful = match is not None
    payload = CodeDetectionPayload(
        decoded_value=match.payload if match is not None else None,
        decode_successful=decode_successful,
    )
    result = SubmissionObjectResult(
        submission_page_id=submission_page.id,
        page_object_id=page_object.id,
        result_type=SubmissionResultType.CODE,
        detected_payload_json=payload.model_dump(mode="json"),
        # A decode has no continuous axis: it passes its checksum or it fails.
        confidence_score=None,
        validity_forces_review=not decode_successful,
        sheet_not_aligned=question_extraction_service.sheet_not_aligned(
            submission_page
        ),
    )
    result.review_required = confidence_threshold_service.compute_review_required(
        result, confidence_threshold_service.effective_threshold(batch)
    )
    return submission_object_result_repo.add(db, result)
