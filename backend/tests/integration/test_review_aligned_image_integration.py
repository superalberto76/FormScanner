# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review shows the image the extraction read on.

COLL-SINGLE exported, filled in and "scanned" by the synthetic generator,
through the app's real path (upload, matching, extraction run with its
alignment), then:

  * the review read exposes each sheet's ``alignment_status`` — ALIGNED,
    NOT_ALIGNED, None;
  * ``GET /submission-pages/{id}/image`` serves the straightened image of a
    page ALIGNED, a page upside down included (straightened by the run), and
    the original scan of a page NOT_ALIGNED or never aligned;
  * a page ALIGNED whose straightened image is gone, never written or outside
    the storage directory is 404, not its original;
  * a weak ETag: 304 with no body for an ``If-None-Match`` naming it, 200 once
    the file served has changed — original → straightened, or a straightened
    image rewritten;
  * ``scan_image_path`` is the rule the extractors read with too.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import io
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from app.api.v1.endpoints.submission_pages import get_submission_page_image
from app.core.config import settings
from app.models.enums import PageAlignmentStatus
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import SubmissionBatchCreate
from app.services import (
    form_variant_export_service as export_svc,
    question_extraction_service,
    submission_batch_service,
    submission_review_service,
)
from tests.synthetic.builders import build_coll_single
from tests.synthetic.generator import SheetFill, SheetOptions, digits, generate

DPI = 150
FILL = SheetFill(
    answers={
        "Q01": ("B",), "Q02": ("A",), "Q03": ("D",),
        "Q04": ("C",), "Q05": ("A",), "Q06": ("B",),
    },
    cells={"MATRICOLA": digits("1022")},
)
STRAIGHT = SheetOptions(seed=245, dpi=DPI)


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
    assert summary.errors == 0
    db.refresh(submission)
    return owner, submission


def _pages(db, submission) -> list[SubmissionPage]:
    return sorted(
        db.scalars(
            select(SubmissionPage).where(SubmissionPage.submission_id == submission.id)
        ).all(),
        key=lambda p: p.source_page_order,
    )


def _pdf(db, project, *, upside_down: bool = False) -> bytes:
    sheet = generate(db, project, "A", FILL, STRAIGHT)
    if upside_down:
        sheet = replace(sheet, page_images=tuple(i.rotate(180) for i in sheet.page_images))
    return sheet.to_pdf()


@pytest.fixture
def flipped(db, project):
    """A submission scanned upside down, every page ALIGNED by the run."""
    owner, submission = _upload_and_run(db, project, _pdf(db, project, upside_down=True), "down")
    pages = _pages(db, submission)
    for page in pages:
        assert page.alignment_status is PageAlignmentStatus.ALIGNED
        assert page.alignment_json["flipped"] is True
    return owner, submission, pages


@pytest.fixture
def unmarked(db, project, monkeypatch):
    """A submission whose export printed no marks: every page NOT_ALIGNED."""
    with monkeypatch.context() as m:
        m.setattr(export_svc, "_render_alignment_marks", lambda *a, **k: None)
        pdf = _pdf(db, project)
    owner, submission = _upload_and_run(db, project, pdf, "unmarked")
    pages = _pages(db, submission)
    for page in pages:
        assert page.alignment_status is PageAlignmentStatus.NOT_ALIGNED
    return owner, submission, pages


def _get(db, page, user, if_none_match: str | None = None):
    return get_submission_page_image(
        page_id=page.id, db=db, user=user, if_none_match=if_none_match
    )


def _assert_404(db, page, user):
    with pytest.raises(HTTPException) as excinfo:
        _get(db, page, user)
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Scan image not found."


# ---------------------------------------------------------------------------
# The review read
# ---------------------------------------------------------------------------


def _read_statuses(db, submission, owner) -> list[PageAlignmentStatus | None]:
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    return [p.alignment_status for p in detail.pages]


def test_the_review_read_exposes_aligned(db, flipped):
    owner, submission, pages = flipped
    assert _read_statuses(db, submission, owner) == [PageAlignmentStatus.ALIGNED] * len(pages)


def test_the_review_read_exposes_not_aligned(db, unmarked):
    owner, submission, pages = unmarked
    assert _read_statuses(db, submission, owner) == [
        PageAlignmentStatus.NOT_ALIGNED
    ] * len(pages)


def test_the_review_read_exposes_never_aligned_as_none(db, flipped):
    owner, submission, pages = flipped
    pages[0].alignment_status = None  # as a page read without alignment
    db.flush()
    statuses = _read_statuses(db, submission, owner)
    assert statuses[0] is None
    assert statuses[1:] == [PageAlignmentStatus.ALIGNED] * (len(pages) - 1)


def test_the_review_read_exposes_no_path(db, flipped):
    owner, submission, _ = flipped
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    dumped = detail.pages[0].model_dump()
    assert "aligned_file_path" not in dumped
    assert "alignment_json" not in dumped
    assert "source_file_path" not in dumped


# ---------------------------------------------------------------------------
# Which image is served
# ---------------------------------------------------------------------------


def test_an_upside_down_aligned_page_is_served_straightened(db, flipped):
    owner, _, pages = flipped
    for page in pages:
        response = _get(db, page, owner)
        assert response.status_code == 200
        assert response.body == Path(page.aligned_file_path).read_bytes()
        assert response.body != Path(page.source_file_path).read_bytes()
        assert response.media_type == "image/png"
        # The template's proportions: the FE crop falls on the pixels read.
        template = page.form_variant_page
        with Image.open(page.aligned_file_path) as img:
            w, h = img.size
        assert w / template.width_px == pytest.approx(h / template.height_px, rel=0.005)


def test_an_upside_down_page_is_served_the_right_way_up(db, project, flipped):
    """The straightened image of the page scanned upside down matches the
    straightened image of the same page scanned straight, not its rotation."""
    owner, _, pages = flipped
    _, straight = _upload_and_run(db, project, _pdf(db, project), "up")
    straight_page = _pages(db, straight)[0]

    def _gray(data: bytes) -> np.ndarray:
        return np.asarray(Image.open(io.BytesIO(data)).convert("L"), dtype=np.float32)

    served = _gray(_get(db, pages[0], owner).body)
    reference = Image.open(straight_page.aligned_file_path).convert("L")
    reference = reference.resize((served.shape[1], served.shape[0]))
    upright = np.asarray(reference, dtype=np.float32)
    turned = np.asarray(reference.rotate(180), dtype=np.float32)
    assert np.abs(served - upright).mean() < np.abs(served - turned).mean() / 2


def test_a_not_aligned_page_is_served_its_original(db, unmarked):
    owner, _, pages = unmarked
    for page in pages:
        assert page.aligned_file_path is None
        assert _get(db, page, owner).body == Path(page.source_file_path).read_bytes()


def test_a_never_aligned_page_is_served_its_original(db, flipped):
    owner, _, pages = flipped
    page = pages[0]
    page.alignment_status = None
    db.flush()
    assert _get(db, page, owner).body == Path(page.source_file_path).read_bytes()


def test_an_aligned_page_whose_straightened_image_is_gone_is_404(db, flipped):
    owner, _, pages = flipped
    Path(pages[0].aligned_file_path).unlink()
    _assert_404(db, pages[0], owner)


def test_an_aligned_page_without_a_straightened_path_is_404(db, flipped):
    owner, _, pages = flipped
    pages[0].aligned_file_path = None
    db.flush()
    _assert_404(db, pages[0], owner)


def test_an_aligned_page_with_a_path_outside_the_storage_is_404(
    db, flipped, tmp_path
):
    owner, _, pages = flipped
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(Path(pages[0].aligned_file_path).read_bytes())
    pages[0].aligned_file_path = str(outside)
    db.flush()
    _assert_404(db, pages[0], owner)


# ---------------------------------------------------------------------------
# ETag and 304
# ---------------------------------------------------------------------------


def test_an_etag_naming_the_served_image_gets_a_304_without_body(db, flipped):
    owner, _, pages = flipped
    first = _get(db, pages[0], owner)
    etag = first.headers["etag"]
    assert etag.startswith('W/"')
    assert first.headers["cache-control"] == "private, no-cache"

    again = _get(db, pages[0], owner, if_none_match=etag)
    assert again.status_code == 304
    assert again.body == b""
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize(
    "header",
    ['"other", {etag}', "{bare}", "*"],
    ids=["in-a-list", "strong-form", "star"],
)
def test_if_none_match_uses_the_weak_comparison(db, flipped, header):
    owner, _, pages = flipped
    etag = _get(db, pages[0], owner).headers["etag"]
    value = header.format(etag=etag, bare=etag.removeprefix("W/"))
    assert _get(db, pages[0], owner, if_none_match=value).status_code == 304


def test_an_etag_of_another_image_gets_the_image(db, flipped):
    owner, _, pages = flipped
    other = _get(db, pages[1], owner).headers["etag"]
    response = _get(db, pages[0], owner, if_none_match=other)
    assert response.status_code == 200
    assert response.body == Path(pages[0].aligned_file_path).read_bytes()


def test_the_first_alignment_changes_the_etag(db, flipped):
    """A browser that saw the original (page never aligned) gets the
    straightened image once the run has aligned the page."""
    owner, _, pages = flipped
    page = pages[0]
    page.alignment_status = None
    db.flush()
    original = _get(db, page, owner)

    page.alignment_status = PageAlignmentStatus.ALIGNED
    db.flush()
    response = _get(db, page, owner, if_none_match=original.headers["etag"])
    assert response.status_code == 200
    assert response.body == Path(page.aligned_file_path).read_bytes()
    assert response.headers["etag"] != original.headers["etag"]


def test_a_rewritten_straightened_image_changes_the_etag(db, flipped):
    owner, _, pages = flipped
    page = pages[0]
    before = _get(db, page, owner).headers["etag"]
    path = Path(page.aligned_file_path)
    stat = path.stat()
    path.write_bytes(path.read_bytes() + b"\0")  # a realignment rewrote it
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    response = _get(db, page, owner, if_none_match=before)
    assert response.status_code == 200
    assert response.headers["etag"] != before


# ---------------------------------------------------------------------------
# One rule for the extractors and the review
# ---------------------------------------------------------------------------


def test_scan_image_path_is_the_rule_of_both(db, flipped):
    _, _, pages = flipped
    page = pages[0]
    assert question_extraction_service.scan_image_path(page) == page.aligned_file_path

    page.alignment_status = PageAlignmentStatus.NOT_ALIGNED
    assert question_extraction_service.scan_image_path(page) == page.source_file_path

    page.alignment_status = None
    assert question_extraction_service.scan_image_path(page) == page.source_file_path

    page.alignment_status = PageAlignmentStatus.ALIGNED
    page.aligned_file_path = None
    assert question_extraction_service.scan_image_path(page) is None
    with pytest.raises(ValueError):
        question_extraction_service._open_gray_scan(page)
