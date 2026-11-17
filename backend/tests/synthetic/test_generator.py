# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests of the synthetic filled-in sheet generator.

Each test prints its duration (visible with ``pytest -s``); the calibration
test also prints the fill ratio of every mark style as the extractor would
measure it. Skipped without TEST_DATABASE_URL, like the rest of the
integration suite. The two slow ones — page manipulations and the style
calibration — are marked ``synthetic`` and run with
``pytest -m synthetic tests/synthetic``.
"""
from __future__ import annotations

import io
import time
from contextlib import contextmanager

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.enums import (
    PageObjectRole,
    PageObjectType,
    PageOrderSource,
    SubmissionStatus,
    Validity,
)
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import SubmissionBatchCreate
from app.services import (
    free_text_extraction_service,
    storage_service,
    submission_batch_service,
)
from app.utils import bubble_sampling, code_rendering
from tests.synthetic.generator import (
    GeneratedSheet,
    MarkStyle,
    SheetFill,
    SheetOptions,
    digits,
    export_template,
    generate,
)

QUIET = dict(jitter_px=0.0, noise_sigma=0.0, blur=False)


@contextmanager
def _timed(what: str):
    start = time.perf_counter()
    yield
    print(f"\n[synthetic] {what}: {time.perf_counter() - start:.3f}s")


def _payloads(image: Image.Image) -> set[str]:
    return {s.payload for s in code_rendering.decode_symbols_from_pil_image(image)}


def _vid(value: str, page: int) -> str:
    return code_rendering.build_tagged_payload(
        PageObjectRole.VARIANT_IDENTIFIER, value, page
    )


# ---------------------------------------------------------------------------
# 1. Determinism
# ---------------------------------------------------------------------------

_SOME_FILL = SheetFill(
    answers={"Q01": ("B",), "Q05": ("A", "C"), "Q10": ("D",)},
    cells={"MATRICOLA": digits("100037"), "M-RIGHE": ((1, 0), (1, 2))},
    texts={"NOME": "Mario Rossi"},
    styles={("Q01", "B"): MarkStyle.LIGHT},
)


def test_same_seed_same_bytes(db, coll_multi):
    with _timed("3-page submission generated"):
        first = generate(db, coll_multi, "A", _SOME_FILL, SheetOptions(seed=7))
    second = generate(db, coll_multi, "A", _SOME_FILL, SheetOptions(seed=7))
    other = generate(db, coll_multi, "A", _SOME_FILL, SheetOptions(seed=8))

    with _timed("to_pdf"):
        pdf = first.to_pdf()
    assert pdf == second.to_pdf()
    assert pdf != other.to_pdf()

    names = ["p1.png", "p2.png", "p3.png"]
    with _timed("to_images (3 PNG)"):
        pngs = first.to_images(names)
    assert pngs == second.to_images(names)

    jpg = first.to_images(["p1.jpg", "p2.jpg", "p3.jpg"])
    print(
        f"[synthetic] sizes: PDF {len(pdf)} B (3 pages), "
        f"PNG page 1 {len(pngs[0][1])} B, JPEG page 1 {len(jpg[0][1])} B"
    )
    assert first.page_numbers == (1, 2, 3)
    assert all(i.mode == "L" for i in first.page_images)


# ---------------------------------------------------------------------------
# 2. Geometry read off the export
# ---------------------------------------------------------------------------

_MATRIX_SHAPES = {"M-RIGHE": (4, 4), "M-COLONNE": (4, 4), "M-MULTI": (3, 4)}


def test_template_geometry(db, coll_multi):
    for key, respondent_id_cols in (("A", 6), ("B", 6), ("C", 5)):
        with _timed(f"export + read_template {key}"):
            _, geometry = export_template(db, coll_multi, key)
        variant = coll_multi.variants[key]
        shapes = {**_MATRIX_SHAPES, "MATRICOLA": (10, respondent_id_cols)}
        seen = set()
        for page in geometry.pages.values():
            for label, obj in page.objects.items():
                seen.add(label)
                x0, y0, x1, y1 = obj.rect
                for b in obj.bubbles.values():
                    assert x0 <= b.cx <= x1 and y0 <= b.cy <= y1, (key, label)
                if obj.object_type is PageObjectType.QUESTION:
                    assert list(obj.bubbles) == ["A", "B", "C", "D"], (key, label)
                    bubbles = list(obj.bubbles.values())
                    assert len({b.cx for b in bubbles}) == 1, (key, label)
                    ys = [b.cy for b in bubbles]
                    assert ys == sorted(ys) and len(set(ys)) == 4, (key, label)
                elif obj.object_type is PageObjectType.MATRIX:
                    rows, cols = shapes[label]
                    assert set(obj.bubbles) == {
                        (r, c) for r in range(rows) for c in range(cols)
                    }, (key, label)
                elif obj.object_type is PageObjectType.FREE_TEXT_AREA:
                    assert obj.text_box is not None, (key, label)
        assert seen == set(variant.objects), key
        assert geometry.find("NOME") and geometry.find("APERTA")


# ---------------------------------------------------------------------------
# 3. Codes survive (or not, when covered)
# ---------------------------------------------------------------------------


def test_vid_is_readable_and_can_be_covered(db, coll_multi):
    vid = coll_multi.variants["A"].vid_value
    with _timed("blank sheet generated + decoded"):
        sheet = generate(db, coll_multi, "A", SheetFill(), SheetOptions(seed=1, **QUIET))
        found = [_payloads(i) for i in sheet.page_images]
    for n, payloads in enumerate(found, start=1):
        assert _vid(vid, n) in payloads, (n, payloads)

    covered = generate(
        db, coll_multi, "A", SheetFill(),
        SheetOptions(seed=1, cover=(("VID", (2,)),), **QUIET),
    )
    found = [_payloads(i) for i in covered.page_images]
    assert _vid(vid, 1) in found[0] and _vid(vid, 3) in found[2]
    assert _vid(vid, 2) not in found[1]


def test_barcode_is_readable_and_can_be_covered(db, coll_multi):
    vid = coll_multi.variants["A"].vid_value
    sheet = generate(db, coll_multi, "A", SheetFill(), SheetOptions(seed=1, **QUIET))
    assert "LOT-0001" in _payloads(sheet.page_images[1])

    covered = generate(
        db, coll_multi, "A", SheetFill(),
        SheetOptions(seed=1, cover=(("BARCODE", (2,)),), **QUIET),
    )
    found = [_payloads(i) for i in covered.page_images]
    assert "LOT-0001" not in found[1]
    assert _vid(vid, 2) in found[1]


# ---------------------------------------------------------------------------
# 4. Page manipulations
# ---------------------------------------------------------------------------


# Slow (~14 s): marked ``synthetic``.
@pytest.mark.synthetic
def test_page_manipulations(db, coll_multi):
    fill = SheetFill(answers={"Q01": ("A",)})
    with _timed("page manipulations (5 submissions)"):
        plain = generate(db, coll_multi, "A", fill, SheetOptions(seed=3))

        subset = generate(db, coll_multi, "A", fill, SheetOptions(seed=3, pages=(1, 3)))
        assert len(subset.page_images) == 2
        assert subset.page_numbers == (1, 3)

        shuffled = generate(
            db, coll_multi, "A", fill, SheetOptions(seed=3, pages=(3, 1, 2))
        )
        assert shuffled.page_numbers == (3, 1, 2)
        # Same seed: the pages are generated identically, then reordered.
        assert shuffled.page_images[0].tobytes() == plain.page_images[2].tobytes()

        other = generate(db, coll_multi, "B", SheetFill(), SheetOptions(seed=4))
        extended = generate(
            db, coll_multi, "A", fill,
            SheetOptions(seed=3, extra_pages=(other.page_images[2],)),
        )
        assert len(extended.page_images) == 4
        assert extended.page_numbers == (1, 2, 3, None)

        warped = generate(
            db, coll_multi, "A", fill,
            SheetOptions(seed=3, scale=0.98, rotation_deg=0.5),
        )
    assert [i.size for i in warped.page_images] == [i.size for i in plain.page_images]
    assert warped.page_images[0].tobytes() != plain.page_images[0].tobytes()


def test_invalid_fill_is_rejected(db, coll_multi):
    for fill, message in (
        (SheetFill(answers={"Q99": ("A",)}), "no object 'Q99'"),
        (SheetFill(answers={"Q01": ("E",)}), "no option 'E'"),
        (SheetFill(cells={"M-MULTI": ((3, 0),)}), "outside the 3×4 grid"),
        (SheetFill(texts={"Q01": "x"}), "needs a FREE_TEXT_AREA"),
    ):
        with pytest.raises(ValueError, match=message):
            generate(db, coll_multi, "A", fill, SheetOptions(seed=0))


# ---------------------------------------------------------------------------
# 5. Style calibration, measured as the extractor would
# ---------------------------------------------------------------------------

_CALIBRATION = (
    ("Q01", "A", MarkStyle.FULL),
    ("Q01", "B", MarkStyle.LIGHT),
    ("Q01", "C", MarkStyle.HALF),
    ("Q01", "D", MarkStyle.CROSS),
    ("Q02", "A", MarkStyle.RING),
)


_CALIBRATION_SEEDS = (11, 12, 13, 14, 15)


# Slow (~13 s): marked ``synthetic``.
@pytest.mark.synthetic
def test_style_calibration(db, coll_multi, tmp_path):
    fill = SheetFill(
        answers={"Q01": ("A", "B", "C", "D"), "Q02": ("A",)},
        styles={(label, letter): style for label, letter, style in _CALIBRATION},
    )
    _, geometry = export_template(db, coll_multi, "A")
    objects = geometry.pages[1].objects
    k = storage_service.PDF_RENDER_DPI / 72

    # style (None = the empty bubble Q02 B) → one ratio per seed
    ratios: dict[MarkStyle | None, list[float]] = {}
    with _timed(f"calibration ({len(_CALIBRATION_SEEDS)} seeds, rasterised at 150 dpi)"):
        for seed in _CALIBRATION_SEEDS:
            sheet = generate(db, coll_multi, "A", fill, SheetOptions(seed=seed))
            pdf_path = tmp_path / f"calibration-{seed}.pdf"
            pdf_path.write_bytes(sheet.to_pdf())
            png = next(storage_service.rasterize_submission_pdf(pdf_path))
            page = Image.open(io.BytesIO(png)).convert("L")
            for label, letter, style in (*_CALIBRATION, ("Q02", "B", None)):
                b = objects[label].bubbles[letter]
                ratios.setdefault(style, []).append(
                    bubble_sampling.sample_circle_fill_ratio(
                        page, b.cx * k, b.cy * k, b.r * k
                    )
                )

    print("[synthetic] fill ratio @ 150 dpi, seeds " + " ".join(
        f"{s:>6}" for s in _CALIBRATION_SEEDS
    ))
    for style, values in ratios.items():
        name = style.value if style else "(empty)"
        print(f"[synthetic] {name:7}" + " " * 20 + " ".join(f"{v:6.3f}" for v in values))

    empty = ratios[None]
    for i, seed in enumerate(_CALIBRATION_SEEDS):
        assert ratios[MarkStyle.FULL][i] >= 0.90, seed
        assert 0.50 < ratios[MarkStyle.LIGHT][i] < 0.70, seed
        assert 0.50 < ratios[MarkStyle.HALF][i] < 0.70, seed
        assert ratios[MarkStyle.CROSS][i] <= 0.45, seed
        assert abs(ratios[MarkStyle.RING][i] - empty[i]) <= 0.03, seed
        assert ratios[MarkStyle.RING][i] <= 0.30, seed


def test_marks_are_independent(db, coll_multi):
    """Adding a mark on Q05 leaves the pixels around Q01 untouched."""
    base = SheetFill(answers={"Q01": ("B",)}, styles={("Q01", "B"): MarkStyle.LIGHT})
    more = SheetFill(
        answers={"Q01": ("B",), "Q05": ("A", "C")},
        styles={("Q01", "B"): MarkStyle.LIGHT},
    )
    options = SheetOptions(seed=21)
    a = generate(db, coll_multi, "A", base, options)
    b = generate(db, coll_multi, "A", more, options)

    _, geometry = export_template(db, coll_multi, "A")
    k = options.dpi / 72
    x0, y0, x1, y1 = geometry.pages[1].objects["Q01"].rect
    box = (int(x0 * k), int(y0 * k), int(x1 * k) + 1, int(y1 * k) + 1)
    assert a.page_images[0].crop(box).tobytes() == b.page_images[0].crop(box).tobytes()
    # …while the page as a whole did change.
    assert a.page_images[0].tobytes() != b.page_images[0].tobytes()


# ---------------------------------------------------------------------------
# 6. Through the app's real upload + extraction path
# ---------------------------------------------------------------------------


@pytest.fixture
def _storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "submission_pages_dir", str(tmp_path / "pages"))
    monkeypatch.setattr(
        settings, "submission_originals_dir", str(tmp_path / "originals")
    )
    monkeypatch.setattr(free_text_extraction_service, "_run_ocr", lambda _crop: ("", None))


def test_real_upload_path(db, coll_multi, _storage):
    variant = coll_multi.variants["B"]
    owner = db.get(User, coll_multi.owner_id)
    fill = SheetFill(answers={"Q01": ("A", "B", "C", "D"), "Q03": ("A",)})

    with _timed("real path: generate + upload_pdf + run_batch_extraction"):
        sheet: GeneratedSheet = generate(db, coll_multi, "B", fill, SheetOptions(seed=184))
        batch = submission_batch_service.create_batch(
            db, coll_multi.project_id, SubmissionBatchCreate(name="real-path"), owner
        )
        submission = submission_batch_service.upload_pdf(
            db, batch.id, "real-path.pdf", sheet.to_pdf(), owner
        )
        summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)

    assert submission.matched_form_variant_id == variant.variant_id
    pages = sorted(submission.pages, key=lambda p: p.source_page_order)
    assert [p.page_order_source for p in pages] == [PageOrderSource.CODE] * 3
    assert [p.form_variant_page_id for p in pages] == list(variant.page_ids)

    assert summary.errors == 0, summary.error_details
    assert summary.submissions_scanned == 1
    db.refresh(submission)
    assert submission.status is SubmissionStatus.SCANNED

    results = db.scalars(
        select(SubmissionObjectResult)
        .join(SubmissionPage)
        .where(SubmissionPage.submission_id == submission.id)
    ).all()
    by_object = {r.page_object_id: r.detected_payload_json for r in results}

    def reading(label):
        payload = by_object[variant.obj(label).object_id]
        return {o["option_id"] for o in payload["selected_options"]}, payload["validity"]

    q01 = variant.obj("Q01")
    assert reading("Q01") == (
        {str(o.option_id) for o in q01.options.values()},
        Validity.TOO_MANY_ANSWERS.value,
    )
    assert reading("Q02") == (set(), Validity.NO_ANSWER.value)
    q03_a = variant.obj("Q03").option_at("A")
    assert q03_a.text == "Opzione 4"
    assert reading("Q03") == ({str(q03_a.option_id)}, Validity.VALID.value)

    others = [
        label
        for label, refs in variant.objects.items()
        if refs[0].object_type is PageObjectType.QUESTION
        and label not in ("Q01", "Q02", "Q03")
    ]
    assert len(others) == 9
    for label in others:
        assert reading(label) == (set(), Validity.NO_ANSWER.value), label
