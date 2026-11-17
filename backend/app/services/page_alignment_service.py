# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Aligning one scanned page to its template before its objects are read.

The geometry is :mod:`app.utils.sheet_alignment` (pure); this module is the
part with a file and a row: it reads the stored scan, asks the pure module for
the alignment, writes the straightened image next to the original and the
page's three alignment fields.

Called by the extraction run (``extraction_orchestrator_service``) for a page
bound to a template page, before its objects, only when the page has an
object to read in that run and has never been aligned
(``alignment_status`` NULL): it is computed ONCE and reused by every later
run, so all the readings of a page stand on the same image.

  * ALIGNED — ``aligned_file_path`` is the straightened scan, in the template's
    frame at the scan's resolution (RGB PNG, ``{page_id}_aligned.png``). The
    extractors open it instead of the original
    (``question_extraction_service._open_gray_scan``). The image is also
    straightened strip by strip on the side marks when they hold
    (``sheet_alignment.find_strip_correction``), else it is the corners'
    affine image; ``alignment_json["strips"]`` says which, and why. The
    strips never change the status.
  * NOT_ALIGNED — a normal outcome, not an error: the page is read in
    proportion, and every reading on it carries ``sheet_not_aligned`` and
    goes to review.

A failure to read or decode the file raises: the caller leaves the page
untouched (status still NULL) and the extractors then fail on the same file,
counted per object; the next run retries. Any OTHER failure — the geometry,
the warp, writing the straightened image — does not raise: the scan is
readable, so the extractors WILL read it, in proportion, and the page must say
so. It becomes NOT_ALIGNED with the reason :data:`ALIGNMENT_ERROR`: every
reading on it goes to review, as for any page that could not be aligned.

A page ALIGNED whose straightened image is gone is aligned again
(:func:`needs_alignment`): otherwise every later run would fail on it object
by object, with nothing to bring it back. Nothing here flushes or commits.
The image file is written before the run commits: if the run is rolled back
the file stays orphaned with the status still NULL, and the next run
recomputes it and overwrites it at the same path.
"""
from __future__ import annotations

import io
import logging

import numpy as np
from PIL import Image

from app.models.enums import PageAlignmentStatus
from app.models.submission_page import SubmissionPage
from app.services import storage_service
from app.utils import sheet_alignment
from app.utils.pdf_rendering import DEFAULT_PAGE_HEIGHT_PX, DEFAULT_PAGE_WIDTH_PX

logger = logging.getLogger(__name__)

# ``alignment_json.reason`` of a page whose alignment failed on a readable scan
# (module docstring). Next to the reasons of ``sheet_alignment``.
ALIGNMENT_ERROR = "ALIGNMENT_ERROR"


def template_size(submission_page: SubmissionPage) -> tuple[float, float]:
    """The bound template page's size in its pixel space, with the export's
    A4 @ 96 dpi fallback (the same as ``question_extraction_service
    ._scale_factors``)."""
    template = submission_page.form_variant_page
    return (
        template.width_px or DEFAULT_PAGE_WIDTH_PX,
        template.height_px or DEFAULT_PAGE_HEIGHT_PX,
    )


def needs_alignment(submission_page: SubmissionPage) -> bool:
    """Whether the run must (re)align *submission_page*: never attempted
    (NULL), or ALIGNED with its straightened image missing or outside the
    storage directory (module docstring). NOT_ALIGNED is final."""
    status = submission_page.alignment_status
    if status is None:
        return True
    if status is not PageAlignmentStatus.ALIGNED:
        return False
    path = submission_page.aligned_file_path
    if not path:
        return True
    try:
        return not storage_service.assert_within_submission_pages_dir(path).is_file()
    except ValueError:
        return True


def align_page(submission_page: SubmissionPage) -> PageAlignmentStatus:
    """Align *submission_page* (bound to a template page) and record the
    outcome on it (module docstring). Returns the status written. Raises only
    when the scan itself cannot be read or decoded."""
    if submission_page.form_variant_page_id is None:
        raise ValueError(
            f"SubmissionPage {submission_page.id} is not matched to a template "
            "page: alignment needs its geometry"
        )
    data = storage_service.read_submission_page_image(
        submission_page.source_file_path
    )
    with Image.open(io.BytesIO(data)) as img:
        rgb = np.asarray(img.convert("RGB"))
    gray = np.asarray(Image.fromarray(rgb).convert("L"))
    template_w, template_h = template_size(submission_page)

    try:
        _align_readable(submission_page, rgb, gray, template_w, template_h)
    except Exception as exc:  # noqa: BLE001 — a readable scan is read anyway
        logger.warning(
            "Alignment failed on readable submission_page %s",
            submission_page.id,
            exc_info=True,
        )
        submission_page.alignment_status = PageAlignmentStatus.NOT_ALIGNED
        submission_page.aligned_file_path = None
        submission_page.alignment_json = {
            "aligned": False,
            "reason": ALIGNMENT_ERROR,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return submission_page.alignment_status


def _align_readable(
    submission_page: SubmissionPage,
    rgb: np.ndarray,
    gray: np.ndarray,
    template_w: float,
    template_h: float,
) -> None:
    alignment = sheet_alignment.find_alignment(gray, template_w, template_h)
    strips_json = None
    if alignment.aligned:
        straight, strips_json = _straighten(
            submission_page, rgb, gray, alignment, template_w, template_h
        )
        buf = io.BytesIO()
        Image.fromarray(straight).save(buf, format="PNG")
        path = storage_service.save_submission_page_aligned_image(
            buf.getvalue(), submission_page.id
        )
        submission_page.alignment_status = PageAlignmentStatus.ALIGNED
        submission_page.aligned_file_path = str(path)
    else:
        submission_page.alignment_status = PageAlignmentStatus.NOT_ALIGNED
        submission_page.aligned_file_path = None
    diagnostics = alignment.to_json()
    if strips_json is not None:
        diagnostics["strips"] = strips_json
    submission_page.alignment_json = diagnostics


def _straighten(
    submission_page: SubmissionPage,
    rgb: np.ndarray,
    gray: np.ndarray,
    alignment: sheet_alignment.Alignment,
    template_w: float,
    template_h: float,
) -> tuple[np.ndarray, dict]:
    """The straightened image of an ALIGNED page and the diagnostics of its
    strip correction. The corners alone decided that the page is aligned: if
    the side marks are not enough, or the strip correction fails, the image is
    the affine one — never a page NOT_ALIGNED. Only the affine warp itself, if
    it fails, makes the caller record ``ALIGNMENT_ERROR``."""
    try:
        strips = sheet_alignment.find_strip_correction(
            gray, alignment, template_w, template_h
        )
        if strips.applied:
            image = sheet_alignment.warp_to_template(
                rgb, alignment, template_w, template_h, strips
            )
            return image, strips.to_json()
        strips_json = strips.to_json()
    except Exception as exc:  # noqa: BLE001 — the affine image is still good
        logger.warning(
            "Strip correction failed on submission_page %s: affine image kept",
            submission_page.id,
            exc_info=True,
        )
        strips_json = {
            "applied": False,
            "reason": sheet_alignment.SIDE_ERROR,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return (
        sheet_alignment.warp_to_template(rgb, alignment, template_w, template_h),
        strips_json,
    )
