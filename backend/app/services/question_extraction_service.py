# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optical extraction of a QUESTION answer.

The first thing in this codebase that actually LOOKS at a scanned sheet. It
answers one question about one authored QUESTION object on one physical page:
which of its bubbles were blackened, how sure are we of each, and does the
combination respect the rules of the question.

The whole chain, and why each link is what it is
------------------------------------------------
**1. Where is the object on the scan?**  The authored geometry
(``PageObject.x/y/width/height``) lives in the template's pixel space; the scan
lives in its own, whatever the scanner or the phone produced. The two are
related by a scale factor per axis, derived from the ratio between the scan's
REAL dimensions and the template page's EXPECTED ones — see
:func:`resolve_scan_region`. Nothing about the DPI history of the file is
assumed, only the observed ratio, which is what makes this work for our own
re-scanned export, an external scanner and a phone photo alike.

The mapping itself is linear, per axis, with no rotation term — and it does
not need one. The extraction run first aligns each page to its template from
the four printed marks
(``page_alignment_service``): the scan is straightened into the template's
frame (shift, scale per axis, small rotation, a page upside down) and stored
next to the original, and :func:`_open_gray_scan` opens THAT image for a page
ALIGNED. On it the proportion is exact by construction. A page NOT_ALIGNED
(marks not found, implausible transformation) and a page never aligned (an
extractor called directly) are read on the original in proportion; the
former's readings carry ``sheet_not_aligned`` and go to review
(:func:`sheet_not_aligned`). Perspective (a photo taken at an angle) is not
corrected.

**2. Where are the bubbles inside the object?**  Exactly where the PDF export
printed them — same order, same formula, same clipping — because
:mod:`app.utils.question_layout` holds those five numbers once and both
sides import them. Reproducing the export's geometry by hand here would be the
classic way to read blank sheets as answered forever after somebody nudged a
constant.

**3. Is a bubble blackened?**  :func:`app.utils.bubble_sampling
.sample_circle_fill_ratio`: the fraction of pixels darker than
``DARK_PIXEL_THRESHOLD`` inside the DISC of the bubble. Above
``SELECTION_FILL_THRESHOLD`` it counts as selected. That measurement and its
two thresholds live in ``app.utils.bubble_sampling`` rather than here: a
MATRIX cell is the same printed circle, and reading it with a second copy of
this code would be a second chance to disagree about what "ink" means.

A fixed grey threshold and no dedicated OMR library: both are CHOICES, not
limitations of the approach. They are what "no overengineering"
means here — the simplest thing that reads a real sheet correctly, with
adaptive thresholding left for the day a real batch of scans proves it needed.
The circular mask is NOT in that category and is not an optimisation either:
because the ratio doubles as the confidence, sampling the circumscribed square
would have capped a perfectly filled bubble at π/4 ≈ 0.79 and squeezed the
whole axis into the bottom four fifths of its range.

**4. How sure are we?**  The same fill ratio, unchanged. It IS the confidence
reported for that reading — a bubble filled 95% is a more confident YES than
one filled 55%. No second, invented score sits on top of it. Confidence is
computed only for options read as SELECTED, which is the contract of
``QuestionDetectionPayload``: the absence of an option from
``selected_options`` is how "not selected" is expressed, not a zero-confidence
entry.

The aggregated ``confidence_score`` of the whole reading is the MINIMUM among
the selected options — a question is only as sure as its least sure bubble —
and NULL when nothing was selected, because there is then no reading to be
confident about. NULL is not 0.0: an empty question is handled by ``Validity``
(NO_ANSWER, which forces review on its own axis), and claiming zero confidence
would double-count the same fact on the axis meant to stay independent of it.

**5. Does it need a human?**  Two independent axes, OR-ed, exactly as
``confidence_threshold_service`` defines them: confidence below the batch's
effective threshold, or ``validity_forces_review``. This module writes all
three columns — the two facts of the optical reading, and the
``review_required`` they imply UNDER TODAY'S THRESHOLD. Moving the threshold
later re-derives the third from the first two; ``confidence_threshold_service``
owns that and never rewrites the facts.

Scope
-----
A pure service function. No endpoint, no audit event per result: a batch
produces thousands of readings and one audit row each would drown the trail
that matters, so the auditable act belongs to the orchestrator that runs
extraction over a batch (``extraction_orchestrator_service``).

Transactions: nothing here flushes or commits, the same discipline as
``submission_matching_service``. Note the consequence for the uniqueness of
``(submission_page_id, page_object_id)``: a second extraction of the same
object on the same page is refused by the DATABASE, and the ``IntegrityError``
surfaces at the caller's flush, not at the call.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image
from sqlalchemy.orm import Session

from app.models.enums import (
    PageAlignmentStatus,
    QuestionType,
    SubmissionResultType,
    Validity,
)
from app.models.form_variant_page import FormVariantPage
from app.models.page_object import PageObject
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.repositories import submission_object_result_repo
from app.schemas.submission_result import OptionDetection, QuestionDetectionPayload
from app.services import confidence_threshold_service, storage_service
from app.utils import bubble_sampling, question_layout
from app.utils.pdf_rendering import DEFAULT_PAGE_HEIGHT_PX, DEFAULT_PAGE_WIDTH_PX


@dataclass(frozen=True)
class ScanRegion:
    """A PageObject's rectangle expressed in the SCAN's pixel space."""

    x: float
    y: float
    width: float
    height: float


# ---------------------------------------------------------------------------
# Template pixel space -> scan pixel space
# ---------------------------------------------------------------------------


def _scale_factors(
    form_variant_page: FormVariantPage, scan_width_px: int, scan_height_px: int
) -> tuple[float, float]:
    """(x, y) scale from the template's pixel space to the scan's.

    The template's EXPECTED dimensions are the page's own ``width_px`` /
    ``height_px``, falling back to the A4 @ 96 DPI defaults of
    ``pdf_rendering`` when the page was authored without a backing image —
    which is exactly the fallback the export applies when it renders that same
    page, so both sides agree on the size of the sheet.

    The two axes are scaled INDEPENDENTLY. A scan whose aspect ratio differs
    from the template's (a slightly cropped scanner bed, a photo) is therefore
    stretched to fit rather than letterboxed: on a page that could not be
    aligned, honouring both observed ratios keeps the bubbles
    closer to where they really are than forcing a single factor would. On a
    straightened image the two factors are equal up to rounding.
    """
    template_width = form_variant_page.width_px or DEFAULT_PAGE_WIDTH_PX
    template_height = form_variant_page.height_px or DEFAULT_PAGE_HEIGHT_PX
    return scan_width_px / template_width, scan_height_px / template_height


def resolve_scan_region(
    page_object: PageObject,
    form_variant_page: FormVariantPage,
    scan_width_px: int,
    scan_height_px: int,
) -> ScanRegion:
    """Map *page_object*'s authored rectangle onto the real scan.

    Linear, per axis, no rotation term: on the image :func:`_open_gray_scan`
    returns for an ALIGNED page — straightened into the template's frame —
    the proportion is exact; on the original of a page not aligned it is the
    proportional reading (module docstring). Scale
    factors are computed independently per axis from the ratio between the
    image's REAL dimensions and the template page's EXPECTED ones
    (:func:`_scale_factors`).

    It works whatever the image's provenance — our own export re-scanned, an
    external scanner, a phone photo — because it assumes nothing about the DPI
    history of the file, only the dimensional ratio it can observe.

    Pure: no session, no filesystem. ``PageObject.x/y/w/h`` and
    ``FormVariantPage.width_px/height_px`` live in the SAME pixel space (see
    ``app.utils.pdf_rendering``), which is what makes one ratio enough.
    """
    scale_x, scale_y = _scale_factors(
        form_variant_page, scan_width_px, scan_height_px
    )
    return ScanRegion(
        x=page_object.x * scale_x,
        y=page_object.y * scale_y,
        width=page_object.width * scale_x,
        height=page_object.height * scale_y,
    )


# ---------------------------------------------------------------------------
# Validity
# ---------------------------------------------------------------------------


def compute_validity(question_type: QuestionType, n_selected: int) -> Validity:
    """The discrete verdict on a combination of *n_selected* bubbles.

    Public: a SUBSTITUTE in review computes the validity of the
    options the operator entered with this same function, so a human's answer
    and a scanned one are judged by one rule.

    Nothing blackened is NO_ANSWER whatever the question type. More than one
    bubble is TOO_MANY_ANSWERS only for SINGLE_CHOICE — for MULTIPLE_CHOICE
    that IS the answer. Independent from confidence, in both directions: a
    perfectly legible sheet can be invalid, and a barely legible one valid.
    """
    if n_selected == 0:
        return Validity.NO_ANSWER
    if question_type is QuestionType.SINGLE_CHOICE and n_selected > 1:
        return Validity.TOO_MANY_ANSWERS
    return Validity.VALID


# A private alias, still referenced by the extractor's unit tests.
_compute_validity = compute_validity


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def scan_image_path(submission_page: SubmissionPage) -> str | None:
    """The stored path of the image *submission_page*'s objects are read on.

    For a page ALIGNED the straightened image
    (``aligned_file_path``); for any other page — NOT_ALIGNED, or never
    aligned — the original scan (``source_file_path``). None for a page
    ALIGNED whose straightened path is missing: an anomaly the next
    extraction run repairs (``page_alignment_service.needs_alignment``), never
    papered over with the original, on which the readings were not taken.

    The ONE rule shared by :func:`_open_gray_scan` (what the extractors read)
    and ``submission_review_service.get_page_image`` (what the review shows),
    so the two cannot diverge. A page upside down is
    already turned right way up in its straightened image: no case apart.
    """
    if submission_page.alignment_status is PageAlignmentStatus.ALIGNED:
        return submission_page.aligned_file_path or None
    return submission_page.source_file_path


def _open_gray_scan(submission_page: SubmissionPage) -> Image.Image:
    """The image the page's objects are read on, as a greyscale Pillow image.

    Which file is :func:`scan_image_path`'s rule. The one place every
    extractor opens a sheet, so all five read the same image.

    Read through ``storage_service.read_submission_page_image`` — which
    validates the path against SUBMISSION_PAGES_DIR — rather than opening the
    path directly, so a corrupted row can never make the extractor read a
    file outside the storage directory. ``ValueError`` for a page ALIGNED
    without a straightened path: the run realigns such a page before reading
    it (``needs_alignment``), so only an extractor called directly can meet it.
    """
    path = scan_image_path(submission_page)
    if path is None:
        raise ValueError(
            f"SubmissionPage {submission_page.id} is ALIGNED but has no "
            "straightened image"
        )
    data = storage_service.read_submission_page_image(path)
    with Image.open(io.BytesIO(data)) as img:
        # load() before the context closes; convert() detaches from the buffer.
        return img.convert("L")


def sheet_not_aligned(submission_page: SubmissionPage) -> bool:
    """Whether a reading of *submission_page* carries ``sheet_not_aligned``:
    the page was examined and could not be aligned. A page never aligned
    (NULL) does not: it was read by an extractor called directly, in
    proportion. Written by all five extractors, before
    ``compute_review_required``."""
    return submission_page.alignment_status is PageAlignmentStatus.NOT_ALIGNED


def _detect_options(
    gray_image: Image.Image,
    page_object: PageObject,
    region: ScanRegion,
    scale_x: float,
    scale_y: float,
) -> list[OptionDetection]:
    """Read every printed bubble of *page_object*, keep the selected ones.

    Walks the options in ``order_index`` and reproduces
    ``form_variant_export_service._render_question`` step for step — same
    starting offset, same band height, same circle centre, same CLIPPING of
    bands that overflow the object's height. An option the export never printed
    has no bubble on the sheet, so it is not looked for and cannot be read as
    selected.

    The band walk stays in the TEMPLATE's pixel space and only each bubble's
    CENTRE is mapped onto *region*. That is deliberate: done in scan space the
    clipping comparison would be the export's arithmetic multiplied by a scale
    factor, and a band ending exactly on the object's bottom edge — the common
    case, since a QUESTION is usually sized to its options — could fall either
    side of the test on a rounding error. Here the decision is bit-for-bit the
    one the export made when it chose to print the bubble.

    The sampling radius is scaled by the SMALLER of the two axis factors. Under
    an anisotropic scan the printed circle is an ellipse, and taking the
    smaller factor keeps the sampling disc inside it instead of letting it
    spill onto the surrounding paper — which would dilute the fill ratio, and
    with it the confidence that ratio becomes.
    """
    config = page_object.question_config
    radius = question_layout.CIRCLE_R * min(scale_x, scale_y)
    bottom = page_object.y + page_object.height
    center_x = region.x + question_layout.CIRCLE_CX * scale_x

    selected: list[OptionDetection] = []
    band_top = page_object.y + question_layout.HEADER_H
    for option in sorted(config.options, key=lambda o: o.order_index):
        if band_top + question_layout.OPTION_H > bottom:
            break  # clipped by the export — no bubble was ever printed
        offset_from_top = band_top - page_object.y + question_layout.OPTION_H / 2
        center_y = region.y + offset_from_top * scale_y
        fill_ratio = bubble_sampling.sample_circle_fill_ratio(
            gray_image, center_x, center_y, radius
        )
        if fill_ratio > bubble_sampling.SELECTION_FILL_THRESHOLD:
            # The fill ratio does double duty: it decided the selection AND it
            # is the confidence of that reading. No separate score.
            selected.append(
                OptionDetection(option_id=option.id, confidence=fill_ratio)
            )
        band_top += question_layout.OPTION_H

    return selected


def extract_question_result(
    db: Session,
    submission_page: SubmissionPage,
    page_object: PageObject,
    batch: SubmissionBatch,
) -> SubmissionObjectResult:
    """Extract the reading of one QUESTION object off one scanned page.

    *submission_page* must already be MATCHED to a template page: its
    ``form_variant_page_id`` is what tells us which sheet this is, and without
    it there is no template geometry to map from. A page that is not matched
    raises — it is a precondition of the caller (matching runs first), not a
    case to be handled here by guessing.

    Builds and STAGES a ``SubmissionObjectResult`` carrying:

      * ``detected_payload_json`` — a ``QuestionDetectionPayload``:
        the options read as selected with their confidences, plus the
        ``Validity`` of the combination;
      * ``confidence_score`` — the MINIMUM confidence among the selected
        options, or NULL when none was selected;
      * ``validity_forces_review`` — True whenever the validity is not VALID;
      * ``review_required`` — the two axes above OR-ed under the batch's
        effective threshold, via ``confidence_threshold_service``.

    Does NOT flush or commit: the caller owns the transaction. The returned row
    therefore has no database identity yet, and the
    ``(submission_page_id, page_object_id)`` uniqueness is enforced at the
    caller's flush.
    """
    if submission_page.form_variant_page_id is None:
        raise ValueError(
            f"SubmissionPage {submission_page.id} is not matched to a template "
            "page: extraction needs form_variant_page_id, which matching "
            "must have resolved first"
        )
    if page_object.question_config is None:
        raise ValueError(
            f"PageObject {page_object.id} has no question_config: "
            "extract_question_result only handles QUESTION objects"
        )

    gray_image = _open_gray_scan(submission_page)
    template_page = submission_page.form_variant_page
    scale_x, scale_y = _scale_factors(template_page, *gray_image.size)
    region = resolve_scan_region(page_object, template_page, *gray_image.size)
    selected = _detect_options(gray_image, page_object, region, scale_x, scale_y)

    validity = compute_validity(
        page_object.question_config.question_type, len(selected)
    )
    payload = QuestionDetectionPayload(
        selected_options=selected, validity=validity
    )

    result = SubmissionObjectResult(
        submission_page_id=submission_page.id,
        page_object_id=page_object.id,
        result_type=SubmissionResultType.QUESTION,
        detected_payload_json=payload.model_dump(mode="json"),
        # A question is only as sure as its least sure bubble. NULL — not 0.0 —
        # when nothing was selected: there is no reading to be confident about,
        # and NO_ANSWER already says so on the other axis.
        confidence_score=(
            min(option.confidence for option in selected) if selected else None
        ),
        validity_forces_review=validity is not Validity.VALID,
        sheet_not_aligned=sheet_not_aligned(submission_page),
    )
    # Written from the facts just set, under the threshold in force TODAY.
    # A later threshold change re-derives it and touches neither.
    result.review_required = confidence_threshold_service.compute_review_required(
        result, confidence_threshold_service.effective_threshold(batch)
    )
    return submission_object_result_repo.add(db, result)
