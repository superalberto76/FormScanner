# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OCR extraction of a FREE_TEXT_AREA.

The first extractor whose reading is never trusted on its own. A QUESTION or a
MATRIX is a set of bubbles, and a confident, valid reading of them can be
accepted as it is. A free-text answer is handwriting: what this module produces
is a SUGGESTION for the reviewer to confirm or correct, never an answer.

Always routed to review, through the shared rule
------------------------------------------------
``validity_forces_review`` is set to True UNCONDITIONALLY — not derived from
the confidence, not from the text, not from anything. It is the explicit design
rule of this extractor. ``review_required`` is then computed by
``confidence_threshold_service.compute_review_required`` exactly as every other
extractor does, which makes it True whatever the batch's threshold, with no
special case in the shared service and no bypass here. A later threshold change
(``recompute_review_required_for_batch``) therefore keeps it True too, for the
same reason, without knowing FREE_TEXT exists.

Tesseract, deliberately simple
------------------------------
The OCR engine is Tesseract via pytesseract, with the language model set in
the deploy configuration (``settings.ocr_language``, ``OCR_LANGUAGE``,
English by default) and no dedicated handwriting model: the output is always
reviewed by a human, so a sophisticated engine buys little.
``confidence_score`` is Tesseract's own per-word confidence, averaged and
normalised to ``[0, 1]`` — informative for the reviewer, never decisive.

No layout to replicate
----------------------
Unlike QUESTION/MATRIX there are no bubbles and no grid inside the object: the
region read is simply the object's whole rectangle, mapped onto the scan by
``question_extraction_service.resolve_scan_region`` — the same proportional
mapping (and the same ``_open_gray_scan``, which opens the straightened image
of a page aligned to its template) the other extractors import.

No crop is persisted. The geometry to regenerate one on demand
(``PageObject.x/y/width/height`` + the page's image) is already stored; review
cuts it when it needs it.

A FREE_TEXT_AREA holding the RESPONDENT_IDENTIFIER role IS read here, exactly
like any other: a FREE_TEXT reading of what is written, always sent to review.
It spells a candidate's identity rather than an answer, but that changes
nothing about HOW it is read — only what review does with it: confirming or
correcting it there sets the submission's candidate
(``review_action_service``). Neither the orchestrator nor this module refuses
it, because always-review already guarantees a handwritten identity is never
attributed unseen.

Transactions: nothing here flushes or commits, the same discipline as the
other extractors — the caller owns the transaction.
"""
from __future__ import annotations

import logging

import pytesseract
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import SubmissionResultType
from app.models.page_object import PageObject
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.repositories import submission_object_result_repo
from app.schemas.submission_result import FreeTextDetectionPayload
from app.services import confidence_threshold_service, question_extraction_service

# Tesseract page segmentation mode 6: "assume a single uniform block of text".
# A free-text field can span several lines; a reasonable default, easy to tune.
OCR_PSM = 6

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configured language
# ---------------------------------------------------------------------------


def check_ocr_language(language: str, installed: set[str]) -> None:
    """Raise ``RuntimeError`` unless every component of *language* is in
    *installed*.

    *language* is ``settings.ocr_language``: one Tesseract code, or several
    joined with ``+``. Each component is checked on its own, so ``ita+deu``
    with only ``ita`` installed fails instead of running on Italian alone.
    The message names the missing language(s) and the installed ones.
    """
    missing = [code for code in language.split("+") if code not in installed]
    if not missing:
        return
    names = ", ".join(f"'{code}'" for code in missing)
    verb = "is" if len(missing) == 1 else "are"
    available = ", ".join(sorted(installed)) or "none"
    raise RuntimeError(
        f"OCR language {names} {verb} not installed (installed: {available}). "
        "Install the Tesseract data package or change OCR_LANGUAGE."
    )


def check_ocr_language_at_startup() -> None:
    """The startup check of the configured OCR language.

    Asks Tesseract which languages it has and calls :func:`check_ocr_language`:
    a configured language that is not installed stops the backend with a clear
    message, at deploy time rather than as one error per FREE_TEXT_AREA once a
    batch is loaded.

    A missing Tesseract BINARY does not: the backend starts as before, with a
    warning in the log, and the extraction of free texts reports the missing
    engine (:func:`_run_ocr`). The check does not make Tesseract mandatory.
    """
    try:
        installed = set(pytesseract.get_languages(config=""))
    except pytesseract.TesseractNotFoundError:
        logger.warning(
            "Tesseract is not installed: the OCR language %r cannot be checked "
            "and free-text areas cannot be read",
            settings.ocr_language,
        )
        return
    check_ocr_language(settings.ocr_language, installed)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


def _run_ocr(crop_image: Image.Image) -> tuple[str, float | None]:
    """Read *crop_image* with Tesseract: ``(text, confidence)``.

    Runs ``pytesseract.image_to_data`` with the configured language
    (``settings.ocr_language``, read at every call) and keeps the non-empty tokens with a
    confidence >= 0 (Tesseract reports -1 for elements that are not words),
    joined with single spaces. The confidence is the MEAN of the kept words'
    confidences, normalised from Tesseract's 0-100 scale to 0-1. No word kept
    reads as ``("", None)``: nothing was read, so no confidence was produced.

    Deliberately does NOT catch pytesseract/Tesseract errors (a missing binary,
    above all). A missing OCR engine is a deployment problem, not "no text
    read": swallowed here it would turn EVERY FREE_TEXT_AREA of the batch into
    an empty result that looks legitimate. Raised, it surfaces as a counted
    error in the orchestrator's ``errors``/``error_details``. This is the
    opposite of ``code_rendering.decode_codes_from_image``'s "never raises",
    on purpose: there one unreadable code is normal, here an absent engine is
    not.
    """
    data = pytesseract.image_to_data(
        crop_image,
        lang=settings.ocr_language,
        config=f"--psm {OCR_PSM}",
        output_type=pytesseract.Output.DICT,
    )

    words: list[str] = []
    confidences: list[float] = []
    for text, conf in zip(data["text"], data["conf"]):
        word = (text or "").strip()
        confidence = float(conf)
        if not word or confidence < 0:
            continue
        words.append(word)
        confidences.append(confidence)

    if not words:
        return "", None
    mean = sum(confidences) / len(confidences) / 100.0
    return " ".join(words), min(max(mean, 0.0), 1.0)


def _crop_box(
    region: question_extraction_service.ScanRegion,
    scan_width_px: int,
    scan_height_px: int,
) -> tuple[int, int, int, int] | None:
    """*region* as an integer Pillow box clamped to the scan, or ``None`` when
    nothing of it is left to read.

    Clamped rather than passed through: Pillow pads an out-of-bounds crop with
    black, which Tesseract would try to read as ink.
    """
    left = max(int(round(region.x)), 0)
    top = max(int(round(region.y)), 0)
    right = min(int(round(region.x + region.width)), scan_width_px)
    bottom = min(int(round(region.y + region.height)), scan_height_px)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def extract_free_text_result(
    db: Session,
    submission_page: SubmissionPage,
    page_object: PageObject,
    batch: SubmissionBatch,
) -> SubmissionObjectResult:
    """Extract the OCR suggestion for one FREE_TEXT_AREA off one scanned sheet.

    Preconditions, both raising rather than guessing: *submission_page* must be
    MATCHED to a template page, and *page_object* must carry a
    ``free_text_config``. Whether it holds the RESPONDENT_IDENTIFIER role does
    not matter here (module docstring).

    Builds and STAGES a ``SubmissionObjectResult`` carrying:

      * ``detected_payload_json`` — a ``FreeTextDetectionPayload`` holding the
        OCR's ``suggested_text``;
      * ``confidence_score`` — :func:`_run_ocr`'s, unchanged (NULL when no
        word was read);
      * ``validity_forces_review`` — ALWAYS True (see the module docstring);
      * ``review_required`` — via ``confidence_threshold_service``, like every
        other extractor, and therefore always True.

    A degenerate region (no width or no height once mapped onto the scan, or
    nothing of it inside the scan) is not an error: an empty
    ``suggested_text``, a NULL confidence, still sent to review.

    Does NOT flush or commit: the caller owns the transaction.
    """
    if submission_page.form_variant_page_id is None:
        raise ValueError(
            f"SubmissionPage {submission_page.id} is not matched to a template "
            "page: extraction needs form_variant_page_id, which matching "
            "must have resolved first"
        )
    if page_object.free_text_config is None:
        raise ValueError(
            f"PageObject {page_object.id} has no free_text_config: "
            "extract_free_text_result only handles FREE_TEXT_AREA objects"
        )
    gray_image = question_extraction_service._open_gray_scan(submission_page)
    region = question_extraction_service.resolve_scan_region(
        page_object, submission_page.form_variant_page, *gray_image.size
    )

    # A degenerate region (width or height <= 0, or wholly off the scan) has no
    # box: nothing to read, and not an error.
    box = _crop_box(region, *gray_image.size)
    if box is None:
        suggested_text, confidence_score = "", None
    else:
        suggested_text, confidence_score = _run_ocr(gray_image.crop(box))

    payload = FreeTextDetectionPayload(suggested_text=suggested_text)
    result = SubmissionObjectResult(
        submission_page_id=submission_page.id,
        page_object_id=page_object.id,
        result_type=SubmissionResultType.FREE_TEXT,
        detected_payload_json=payload.model_dump(mode="json"),
        confidence_score=confidence_score,
        # The design rule of this extractor: a free-text reading always needs a
        # human. Unconditional — not derived from anything.
        validity_forces_review=True,
        sheet_not_aligned=question_extraction_service.sheet_not_aligned(
            submission_page
        ),
    )
    result.review_required = confidence_threshold_service.compute_review_required(
        result, confidence_threshold_service.effective_threshold(batch)
    )
    return submission_object_result_repo.add(db, result)
