# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alignment of a scanned page in the extraction run.

COLL-SINGLE (a respondent ID and Q01–Q06 over two pages, no free text, no code),
its STUDENT export filled in and "scanned" by the synthetic generator at
150 dpi, through the app's real path: upload (matching included), run
extraction, review read. The generator's page scale and rotation are about the
page centre on a canvas of the same size, like a scanner bed as large as the
sheet: 3.5° at 93% keeps the marks on the image.

  * a page turned and shrunk reads like the straight one;
  * a page without marks: every reading in review with SHEET_NOT_ALIGNED
    first, whatever its confidence, after a threshold change and after a
    reopening too, excluded from "Accept all remaining", and the respondent ID
    sets no candidate;
  * a second run reuses the alignment and its file, and aligns again a page
    whose straightened image is gone;
  * an alignment failing on a readable scan sends the page to review;
  * a page upside down is straightened and read;
  * a page stretched unevenly along the feed, like the ADF does, is
    straightened strip by strip on the side marks and reads like the straight
    one; a failure of the strip correction keeps the affine image and the
    page ALIGNED.
"""
from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.enums import PageAlignmentStatus, SubmissionStatus
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import SubmissionBatchCreate
from app.services import (
    confidence_threshold_service,
    form_variant_export_service as export_svc,
    page_alignment_service,
    review_action_service,
    storage_service,
    review_rules,
    submission_batch_service,
    submission_close_service,
    submission_review_service,
)
from app.utils import alignment_marks, sheet_alignment
from tests.synthetic.builders import build_coll_single
from tests.synthetic.generator import SheetFill, SheetOptions, digits, generate

DPI = 150
RESPONDENT_ID = "1022"
FILL = SheetFill(
    answers={
        "Q01": ("B",), "Q02": ("A",), "Q03": ("D",),
        "Q04": ("C",), "Q05": ("A",), "Q06": ("B",),
    },
    cells={"MATRICOLA": digits(RESPONDENT_ID)},
)
STRAIGHT = SheetOptions(seed=244, dpi=DPI)
TURNED = SheetOptions(seed=244, dpi=DPI, scale=0.93, rotation_deg=3.5)


@pytest.fixture
def pages_dir(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "submission_pages"
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    monkeypatch.setattr(settings, "submission_originals_dir", str(tmp_path / "originals"))
    return target


@pytest.fixture
def project(db, pages_dir):
    return build_coll_single(db)


def _upload_and_run(db, project, pdf: bytes, name: str):
    owner = db.get(User, project.owner_id)
    batch = submission_batch_service.create_batch(
        db, project.project_id, SubmissionBatchCreate(name=name), owner
    )
    submission = submission_batch_service.upload_pdf(db, batch.id, f"{name}.pdf", pdf, owner)
    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    db.refresh(submission)
    return owner, batch, submission, summary


def _results(db, submission) -> dict[str, SubmissionObjectResult]:
    rows = db.scalars(
        select(SubmissionObjectResult)
        .join(SubmissionPage)
        .where(SubmissionPage.submission_id == submission.id)
    ).all()
    return {r.page_object.label: r for r in rows}


def _reading(result: SubmissionObjectResult):
    payload = result.detected_payload_json
    if "raw_value" in payload:
        return payload["raw_value"]
    return sorted(o["option_id"] for o in payload["selected_options"])


def _pages(db, submission) -> list[SubmissionPage]:
    return sorted(
        db.scalars(
            select(SubmissionPage).where(SubmissionPage.submission_id == submission.id)
        ).all(),
        key=lambda p: p.source_page_order,
    )


def _stretch(image: Image.Image, peak_mm: float = 1.0) -> Image.Image:
    """An ADF-like uneven stretch along the feed: nothing at the corner
    marks, *peak_mm* pushed down halfway between them (a half sine)."""
    gray = np.asarray(image.convert("L"))
    h, w = gray.shape
    k = DPI / 96
    half = alignment_marks.MARK_SIZE / 2
    top = (alignment_marks.MARK_INSET + half) * k
    bottom = h - top
    ys = np.arange(h, dtype=np.float32)
    s = np.clip((ys - top) / (bottom - top), 0, 1)
    source_y = ys - peak_mm * DPI / 25.4 * np.sin(np.pi * s)
    map_y = np.repeat(source_y[:, None], w, axis=1).astype(np.float32)
    map_x = np.repeat(np.arange(w, dtype=np.float32)[None, :], h, axis=0)
    out = cv2.remap(
        gray, map_x, map_y, cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )
    return Image.fromarray(out)


def _pdf(
    db, project, options: SheetOptions, *, upside_down: bool = False, stretched: bool = False
) -> bytes:
    sheet = generate(db, project, "A", FILL, options)
    if stretched:
        sheet = replace(sheet, page_images=tuple(_stretch(i) for i in sheet.page_images))
    if upside_down:
        sheet = replace(sheet, page_images=tuple(i.rotate(180) for i in sheet.page_images))
    return sheet.to_pdf()


# ---------------------------------------------------------------------------
# Aligned pages
# ---------------------------------------------------------------------------


def test_a_turned_and_shrunk_page_reads_like_the_straight_one(db, project, pages_dir):
    _, _, straight, _ = _upload_and_run(db, project, _pdf(db, project, STRAIGHT), "straight")
    _, _, turned, summary = _upload_and_run(db, project, _pdf(db, project, TURNED), "turned")
    assert summary.errors == 0

    for page in _pages(db, turned):
        assert page.alignment_status is PageAlignmentStatus.ALIGNED, page.alignment_json
        assert page.alignment_json["flipped"] is False
        assert abs(page.alignment_json["rotation_deg"]) == pytest.approx(3.5, abs=0.3)
        # The straightened image is a new file; the original is untouched.
        assert Path(page.aligned_file_path) == pages_dir / f"{page.id}_aligned.png"
        assert Path(page.aligned_file_path).exists() and Path(page.source_file_path).exists()

    want = {label: _reading(r) for label, r in _results(db, straight).items()}
    got = {label: _reading(r) for label, r in _results(db, turned).items()}
    assert got == want
    assert want["MATRICOLA"] == RESPONDENT_ID
    assert not any(r.sheet_not_aligned for r in _results(db, turned).values())
    assert not any(r.review_required for r in _results(db, turned).values())
    assert turned.status is SubmissionStatus.SCANNED
    assert turned.respondent_identifier == RESPONDENT_ID


def test_a_stretched_page_reads_like_the_straight_one(db, project):
    """The ADF's uneven stretch, corrected on the side marks."""
    _, _, straight, _ = _upload_and_run(db, project, _pdf(db, project, STRAIGHT), "flat")
    _, _, stretched, summary = _upload_and_run(
        db, project, _pdf(db, project, STRAIGHT, stretched=True), "adf"
    )
    assert summary.errors == 0
    for page in _pages(db, stretched):
        assert page.alignment_status is PageAlignmentStatus.ALIGNED
        strips = page.alignment_json["strips"]
        assert strips["applied"] is True and strips["reason"] is None, strips
        assert 0.7 < strips["max_shift_mm"] < 1.3
    for page in _pages(db, straight):
        assert page.alignment_json["strips"]["applied"] is True
        assert page.alignment_json["strips"]["max_shift_mm"] < 0.3

    want = {label: _reading(r) for label, r in _results(db, straight).items()}
    results = _results(db, stretched)
    threshold = confidence_threshold_service.effective_threshold(stretched.submission_batch)
    assert {label: _reading(r) for label, r in results.items()} == want
    for label, r in results.items():
        assert not r.sheet_not_aligned and not r.review_required, label
        assert r.confidence_score >= threshold, label
    assert stretched.status is SubmissionStatus.SCANNED
    assert stretched.respondent_identifier == RESPONDENT_ID


@pytest.mark.parametrize(
    "target", ["find_strip_correction", "_strip_maps"], ids=["finding", "remap"]
)
def test_a_strip_failure_keeps_the_page_aligned_on_the_affine_image(
    db, project, monkeypatch, target
):
    """An exception in the strip correction — finding the side marks, or the
    remap that applies it — is not an alignment failure: the page stays
    ALIGNED, on the corners' affine image."""
    def broken(*args, **kwargs):
        raise RuntimeError("strips exploded")

    monkeypatch.setattr(sheet_alignment, target, broken)
    owner, _, submission, summary = _upload_and_run(
        db, project, _pdf(db, project, STRAIGHT), "strips-error"
    )
    assert summary.errors == 0
    for page in _pages(db, submission):
        assert page.alignment_status is PageAlignmentStatus.ALIGNED, page.alignment_json
        assert Path(page.aligned_file_path).is_file()
        strips = page.alignment_json["strips"]
        assert strips["applied"] is False
        assert strips["reason"] == sheet_alignment.SIDE_ERROR
        assert "strips exploded" in strips["error"]
    results = _results(db, submission)
    assert results and not any(r.sheet_not_aligned for r in results.values())
    assert not any(r.review_required for r in results.values())
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    assert all(
        submission_review_service.SHEET_NOT_ALIGNED not in o.flag_reasons
        for p in detail.pages for o in p.objects
    )


def test_the_aligned_image_is_in_the_template_frame(db, project):
    _, _, submission, _ = _upload_and_run(db, project, _pdf(db, project, TURNED), "frame")
    for page in _pages(db, submission):
        template = page.form_variant_page
        with Image.open(page.aligned_file_path) as img:
            assert img.mode == "RGB"
            w, h = img.size
        # Template size × the estimated scale: the proportions of the template.
        assert w / template.width_px == pytest.approx(h / template.height_px, rel=0.005)
        assert w / template.width_px == pytest.approx(DPI / 96 * 0.93, rel=0.02)


def test_an_upside_down_page_is_straightened_and_read(db, project):
    _, _, straight, _ = _upload_and_run(db, project, _pdf(db, project, STRAIGHT), "up")
    _, _, flipped, summary = _upload_and_run(
        db, project, _pdf(db, project, STRAIGHT, upside_down=True), "down"
    )
    assert summary.errors == 0
    for page in _pages(db, flipped):
        assert page.alignment_status is PageAlignmentStatus.ALIGNED
        assert page.alignment_json["flipped"] is True
    want = {label: _reading(r) for label, r in _results(db, straight).items()}
    assert {label: _reading(r) for label, r in _results(db, flipped).items()} == want
    assert flipped.respondent_identifier == RESPONDENT_ID


def test_a_second_run_reuses_the_alignment_and_its_file(db, project):
    owner, batch, submission, _ = _upload_and_run(
        db, project, _pdf(db, project, TURNED), "rerun"
    )
    page = _pages(db, submission)[0]
    before = (dict(page.alignment_json), Path(page.aligned_file_path).stat().st_mtime_ns)
    # One reading of page 1 gone: the second run reads it again, on that page.
    q01 = _results(db, submission)["Q01"]
    reading = _reading(q01)
    db.delete(q01)
    db.flush()

    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    db.refresh(page)
    assert summary.results_created == 1
    assert (page.alignment_json, Path(page.aligned_file_path).stat().st_mtime_ns) == before
    assert _reading(_results(db, submission)["Q01"]) == reading


def test_a_page_with_nothing_left_to_read_is_not_aligned_again(db, project):
    owner, batch, submission, _ = _upload_and_run(
        db, project, _pdf(db, project, STRAIGHT), "settled"
    )
    page = _pages(db, submission)[0]
    page.alignment_status = None  # as a page read without alignment
    page.alignment_json = None
    db.flush()
    submission_batch_service.run_batch_extraction(db, batch.id, owner)
    db.refresh(page)
    assert page.alignment_status is None and page.alignment_json is None


# ---------------------------------------------------------------------------
# Pages not aligned
# ---------------------------------------------------------------------------


@pytest.fixture
def unmarked(db, project, monkeypatch):
    """A submission whose export printed no marks, through upload and one run."""
    with monkeypatch.context() as m:
        m.setattr(export_svc, "_render_alignment_marks", lambda *a, **k: None)
        pdf = _pdf(db, project, STRAIGHT)
    return _upload_and_run(db, project, pdf, "unmarked")


def test_without_marks_every_reading_goes_to_review(db, unmarked):
    owner, batch, submission, summary = unmarked
    assert summary.errors == 0
    # A normal outcome, not an error: the submission moves to SCANNED.
    assert submission.status is SubmissionStatus.SCANNED
    for page in _pages(db, submission):
        assert page.alignment_status is PageAlignmentStatus.NOT_ALIGNED
        assert page.aligned_file_path is None
        assert page.alignment_json["reason"] == "MARKERS_NOT_FOUND"

    results = _results(db, submission)
    assert len(results) == 7
    for label, r in results.items():
        assert r.sheet_not_aligned and r.review_required, label
    # Read in proportion on the original: the clean sheet still reads right,
    # with a high confidence — and is in review all the same.
    assert results["Q01"].confidence_score > 0.9
    assert results["MATRICOLA"].detected_payload_json["raw_value"] == RESPONDENT_ID
    # A respondent ID in review sets no candidate.
    assert submission.respondent_identifier is None

    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    objects = [o for p in detail.pages for o in p.objects]
    assert len(objects) == 7
    for o in objects:
        assert o.flag_reasons[0] == submission_review_service.SHEET_NOT_ALIGNED
        assert o.bulk_acceptable is False
        assert o.confirmable is True
    assert detail.bulk_acceptable_count == 0


def test_accept_all_remaining_leaves_them_alone(db, unmarked):
    owner, _, submission, _ = unmarked
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    accepted = review_action_service.accept_remaining(
        db, submission.id, owner, detail.review_version
    )
    assert accepted.accepted_count == 0
    assert all(r.review_outcome is None for r in _results(db, submission).values())


def test_a_threshold_change_keeps_them_in_review(db, unmarked):
    owner, batch, submission, _ = unmarked
    submission_batch_service.update_confidence_threshold(db, batch.id, 0.0, owner)
    for r in _results(db, submission).values():
        db.refresh(r)
        assert r.review_required


def test_a_reopening_keeps_them_in_review(db, unmarked):
    owner, batch, submission, _ = unmarked
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    closed = submission_close_service.close_submission(
        db, submission.id, owner, detail.review_version
    )
    submission_batch_service.update_confidence_threshold(db, batch.id, 0.0, owner)
    submission_close_service.reopen_submission(db, submission.id, owner, closed.review_version)
    for r in _results(db, submission).values():
        db.refresh(r)
        assert r.review_required


def test_is_bulk_acceptable_refuses_a_reading_of_a_page_not_aligned(db, unmarked):
    _, _, submission, _ = unmarked
    q01 = _results(db, submission)["Q01"]
    assert review_rules.is_confirmable(q01, q01.page_object)
    assert not review_rules.is_bulk_acceptable(q01, q01.page_object)
    q01.sheet_not_aligned = False
    assert review_rules.is_bulk_acceptable(q01, q01.page_object)


# ---------------------------------------------------------------------------
# A file that cannot be read
# ---------------------------------------------------------------------------


def test_an_unreadable_file_leaves_the_page_untouched_and_counts_errors(db, project):
    owner = db.get(User, project.owner_id)
    batch = submission_batch_service.create_batch(
        db, project.project_id, SubmissionBatchCreate(name="broken"), owner
    )
    submission = submission_batch_service.upload_pdf(
        db, batch.id, "broken.pdf", _pdf(db, project, STRAIGHT), owner
    )
    page = _pages(db, submission)[0]
    Path(page.source_file_path).write_bytes(b"not an image")

    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    db.refresh(page)
    db.refresh(submission)
    assert page.alignment_status is None and page.alignment_json is None
    assert summary.errors == 4  # MATRICOLA, Q01, Q02, Q03 of page 1
    assert submission.status is SubmissionStatus.MATCHED
    assert _pages(db, submission)[1].alignment_status is PageAlignmentStatus.ALIGNED


def test_an_alignment_failure_on_a_readable_scan_sends_the_page_to_review(
    db, project, monkeypatch
):
    """The straightened image cannot be written. The scan is readable, so the
    extractors read it in proportion: the page must be NOT_ALIGNED and its
    readings in review, never silently accepted."""
    def broken(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(storage_service, "save_submission_page_aligned_image", broken)
    owner, _, submission, summary = _upload_and_run(
        db, project, _pdf(db, project, STRAIGHT), "disk-full"
    )
    assert summary.errors == 0
    for page in _pages(db, submission):
        assert page.alignment_status is PageAlignmentStatus.NOT_ALIGNED
        assert page.aligned_file_path is None
        assert page.alignment_json["reason"] == page_alignment_service.ALIGNMENT_ERROR
        assert "No space left on device" in page.alignment_json["error"]
    results = _results(db, submission)
    assert results and all(r.sheet_not_aligned and r.review_required for r in results.values())
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    assert detail.bulk_acceptable_count == 0
    assert submission.respondent_identifier is None


def test_an_aligned_page_whose_image_is_gone_is_aligned_again(db, project):
    """Without its straightened image an ALIGNED page would fail object by
    object at every run; the run aligns it again."""
    owner, batch, submission, _ = _upload_and_run(
        db, project, _pdf(db, project, TURNED), "gone"
    )
    page = _pages(db, submission)[0]
    aligned = Path(page.aligned_file_path)
    aligned.unlink()
    q01 = _results(db, submission)["Q01"]
    reading = _reading(q01)
    db.delete(q01)
    db.flush()

    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    db.refresh(page)
    assert summary.errors == 0 and summary.results_created == 1
    assert page.alignment_status is PageAlignmentStatus.ALIGNED
    assert aligned.is_file()
    assert _reading(_results(db, submission)["Q01"]) == reading


def test_needs_alignment():
    page = SubmissionPage()
    assert page_alignment_service.needs_alignment(page)  # never attempted
    page.alignment_status = PageAlignmentStatus.NOT_ALIGNED
    assert not page_alignment_service.needs_alignment(page)  # final
    page.alignment_status = PageAlignmentStatus.ALIGNED
    page.aligned_file_path = None
    assert page_alignment_service.needs_alignment(page)
    page.aligned_file_path = "/etc/passwd"  # outside the storage directory
    assert page_alignment_service.needs_alignment(page)


def test_the_pdf_page_bytes_are_a_png(db, project):
    """Precondition of the tests above: the upload rasterises each PDF page to
    a PNG the alignment opens."""
    _, _, submission, _ = _upload_and_run(db, project, _pdf(db, project, STRAIGHT), "png")
    with Image.open(io.BytesIO(Path(_pages(db, submission)[0].source_file_path).read_bytes())) as img:
        assert img.format == "PNG"
