# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for FREE_TEXT_AREA extraction.

Synthetic scans generated with Pillow, read back by
``extract_free_text_result``. What this suite pins down:

  * **real OCR** — a clearly printed string is recognised (approximately) with
    a populated, sensible confidence, Italian accented capitals included; a
    blank area reads as (almost) nothing and never raises. The configured
    language is pinned per test: ``eng`` for the accent-free
    text, ``ita`` for the accented one — the two models of the image, both
    checked as installed. These need the Tesseract BINARY and its English and
    Italian data (tesseract-ocr-eng, tesseract-ocr-ita) — plus the DejaVu
    font (fonts-dejavu-core) for the accented one — and SKIP where the binary
    is missing, the same contract as the pyzbar/libzbar end-to-end tests. In
    Docker (which installs all three) they DO run;
  * **the design rule** — ``validity_forces_review`` is True whatever the
    confidence, and ``review_required`` is True whatever the batch threshold,
    0.0 included. OCR is stubbed there (``_run_ocr``): the rule must hold for
    ANY reading, including a perfect one no real engine would reliably give;
  * **the degenerate region** — no exception, an empty reading, OCR never run;
  * **preconditions** — an unmatched page and an object with no
    ``free_text_config`` each raise; a RESPONDENT_IDENTIFIER-role
    FREE_TEXT_AREA does not: it is read like any other;
  * **result_type** — FREE_TEXT.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import difflib
import io
import uuid

import pytest
import pytesseract
from PIL import Image, ImageDraw, ImageFont

from app.core.config import settings
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import free_text_extraction_service as svc

# The template page these tests author on: A4 @ 96 DPI, the export's default.
TEMPLATE_W = 794
TEMPLATE_H = 1123

# One wide free-text field near the top of the page.
OBJ_X = 60.0
OBJ_Y = 100.0
OBJ_W = 600.0
OBJ_H = 120.0

# Scans are drawn at twice the template's resolution, so the mapping is not
# the identity.
SCALE = 2.0

# Italian, like the forms this reads (the OCR runs with the Italian model).
# No accented letters: Pillow's bundled font, the only one guaranteed in the
# slim Docker image, has no glyphs for them and would print empty boxes.
KNOWN_TEXT = "RISPOSTA DEL CANDIDATO"

# The case the Italian model is there for: every Italian accented vowel
# (À È Ì Ò Ù, plus the acute É of "perché"), over two lines as a free-text
# answer may well be. Printed with DejaVu Sans, which has the glyphs.
ACCENTED_LINES = ("PERCHÉ LA CITTÀ È COSÌ BELLA", "PERÒ PIÙ CARA")
ACCENTED_CHARS = "ÀÈÌÒÙÉ"

# Where fonts-dejavu-core puts DejaVu Sans on Debian (bookworm and trixie, the
# bases of python:3.12-slim) and Ubuntu alike.
DEJAVU_SANS_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

WHITE = 255
BLACK = 0


def _require_tesseract() -> None:
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 — TesseractNotFoundError, or a broken binary
        pytest.skip(
            "tesseract binary not available (installed by backend/Dockerfile; "
            "in a local environment install it with the system packages)"
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    target = tmp_path / "submission_pages"
    target.mkdir()
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    return target


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"free-text-extract-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    p = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Free text extraction project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(p)
    db.flush()
    db.add(
        FormProjectMember(project_id=p.id, user_id=actor.id, role=ProjectRole.OWNER)
    )
    db.flush()
    return p


@pytest.fixture
def template_page(db, project, actor) -> FormVariantPage:
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(
        form_variant_id=variant.id,
        page_index=1,
        width_px=TEMPLATE_W,
        height_px=TEMPLATE_H,
    )
    db.add(page)
    db.flush()
    return page


def _make_free_text(
    db,
    template_page: FormVariantPage,
    *,
    x: float = OBJ_X,
    y: float = OBJ_Y,
    width: float = OBJ_W,
    height: float = OBJ_H,
    role: PageObjectRole | None = None,
    with_config: bool = True,
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.FREE_TEXT_AREA,
        x=x,
        y=y,
        width=width,
        height=height,
    )
    db.add(obj)
    db.flush()
    if with_config:
        db.add(FreeTextAreaConfig(page_object_id=obj.id, placeholder="Answer"))
        db.flush()
    if role is not None:
        db.add(
            FormVariantRoleBinding(
                form_variant_id=template_page.form_variant_id,
                page_object_id=obj.id,
                role=role,
            )
        )
        db.flush()
    db.refresh(obj)
    return obj


def _make_batch(db, project, actor, *, threshold: float | None = None):
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        confidence_threshold=threshold,
    )
    db.add(batch)
    db.flush()
    return batch


def _draw_scan(
    text: str | None = None, *, font: ImageFont.FreeTypeFont | None = None
) -> bytes:
    """A synthetic scan of the template page, as PNG bytes: white paper, with
    *text* (possibly several lines) printed large and black inside the
    free-text object's rectangle.

    *font* defaults to Pillow's bundled FreeType font — no dependency on system
    fonts, but no glyphs for accented letters either (see ``_dejavu_sans``)."""
    img = Image.new("L", (int(TEMPLATE_W * SCALE), int(TEMPLATE_H * SCALE)), WHITE)
    if text is not None:
        draw = ImageDraw.Draw(img)
        draw.multiline_text(
            ((OBJ_X + 20) * SCALE, (OBJ_Y + 30) * SCALE),
            text,
            fill=BLACK,
            font=font or ImageFont.load_default(size=64),
            spacing=16,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _dejavu_sans(size: int) -> ImageFont.FreeTypeFont:
    """DejaVu Sans, for text with Italian accents.

    A missing font FAILS rather than skips: once the tesseract binary is there
    (checked first), the environment is meant to run this test, and a silent
    skip would hide exactly the accent coverage it exists for."""
    try:
        return ImageFont.truetype(DEJAVU_SANS_PATH, size)
    except OSError:
        pytest.fail(
            f"{DEJAVU_SANS_PATH} not found: install fonts-dejavu-core "
            "(backend/Dockerfile; in a local environment, the system packages)"
        )


def _make_scanned_page(
    db,
    pages_dir,
    batch,
    template_page: FormVariantPage,
    *,
    scan: bytes,
    matched: bool = True,
) -> SubmissionPage:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=SubmissionStatus.MATCHED,
    )
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    path = pages_dir / f"{page_id}.png"
    path.write_bytes(scan)
    page = SubmissionPage(
        id=page_id,
        submission_id=submission.id,
        form_variant_page_id=template_page.id if matched else None,
        source_file_path=str(path),
        source_filename="scan.png",
        source_page_order=0,
    )
    db.add(page)
    db.flush()
    return page


def _stub_ocr(monkeypatch, text: str, confidence: float | None) -> list[Image.Image]:
    """Replace the OCR with a fixed reading; record the crops it was given."""
    crops: list[Image.Image] = []

    def fake(crop_image):
        crops.append(crop_image)
        return text, confidence

    monkeypatch.setattr(svc, "_run_ocr", fake)
    return crops


def _forbid_ocr(monkeypatch) -> None:
    def fail(crop_image):
        raise AssertionError("OCR must not run on a degenerate region")

    monkeypatch.setattr(svc, "_run_ocr", fail)


# ---------------------------------------------------------------------------
# Real OCR (needs the tesseract binary)
# ---------------------------------------------------------------------------
def test_the_image_languages_english_and_italian_are_installed():
    """The Docker image ships both models: run in the CI container, this is
    what checks it."""
    _require_tesseract()

    installed = set(pytesseract.get_languages(config=""))

    assert {"eng", "ita"} <= installed, installed


def test_clearly_printed_text_is_recognised(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    _require_tesseract()
    monkeypatch.setattr(settings, "ocr_language", "eng")
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan(KNOWN_TEXT)
    )

    result = svc.extract_free_text_result(db, page, obj, batch)
    suggested = result.detected_payload_json["suggested_text"]

    # Approximately: a simple engine, and a suggestion a human will review.
    similarity = difflib.SequenceMatcher(
        None, suggested.upper().strip(), KNOWN_TEXT
    ).ratio()
    assert similarity >= 0.8, suggested
    assert result.confidence_score is not None
    assert 0.5 <= result.confidence_score <= 1.0
    assert result.result_type is SubmissionResultType.FREE_TEXT


def test_italian_accented_text_is_recognised(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """What the Italian model (``OCR_LANGUAGE=ita``) is for. Kept apart from
    the accent-free test above so that a failure here points at
    accents/model/font, not at OCR as a whole."""
    _require_tesseract()
    monkeypatch.setattr(settings, "ocr_language", "ita")
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan("\n".join(ACCENTED_LINES), font=_dejavu_sans(48)),
    )

    result = svc.extract_free_text_result(db, page, obj, batch)
    suggested = result.detected_payload_json["suggested_text"].upper().strip()
    # ``_run_ocr`` joins every word, lines included, with single spaces.
    expected = " ".join(ACCENTED_LINES)

    similarity = difflib.SequenceMatcher(None, suggested, expected).ratio()
    assert similarity >= 0.8, suggested
    # The accents themselves, not just the base letters: most of them must
    # survive, or the Italian model is not doing its job.
    recognised = [c for c in ACCENTED_CHARS if c in suggested]
    assert len(recognised) >= len(ACCENTED_CHARS) // 2, suggested
    assert result.confidence_score is not None
    assert 0.5 <= result.confidence_score <= 1.0


def test_a_blank_area_reads_as_nothing_and_does_not_raise(
    db, pages_dir, project, actor, template_page
):
    _require_tesseract()
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)

    # Empty or nearly so: an engine may hallucinate a stray glyph on paper
    # texture, but never read anything that could pass for an answer.
    assert len(result.detected_payload_json["suggested_text"].strip()) <= 2
    assert result.confidence_score is None or result.confidence_score < 0.5
    assert result.review_required is True


# ---------------------------------------------------------------------------
# The design rule: always reviewed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("confidence", [None, 0.0, 0.42, 0.99, 1.0])
def test_validity_forces_review_whatever_the_confidence(
    db, pages_dir, project, actor, template_page, monkeypatch, confidence
):
    """The heart of FREE_TEXT_AREA extraction: the flag is not derived from
    the reading. A perfect, fully-confident reading is forced to review
    exactly like an empty one."""
    _stub_ocr(monkeypatch, "Mario Rossi", confidence)
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)

    assert result.validity_forces_review is True
    assert result.confidence_score == confidence
    assert result.review_required is True


@pytest.mark.parametrize("threshold", [0.0, 0.5, 1.0, None])
def test_review_required_whatever_the_batch_threshold(
    db, pages_dir, project, actor, template_page, monkeypatch, threshold
):
    """0.0 is the most permissive threshold there is — no confidence is below
    it — and a 1.0 reading clears any threshold: review is still required,
    because it comes from ``validity_forces_review`` through the shared
    ``compute_review_required``, not from the confidence axis."""
    _stub_ocr(monkeypatch, "Mario Rossi", 1.0)
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor, threshold=threshold)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)

    assert result.review_required is True


def test_the_ocr_reading_lands_in_the_payload_unchanged(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    crops = _stub_ocr(monkeypatch, "Mario Rossi", 0.83)
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)

    assert result.detected_payload_json == {"suggested_text": "Mario Rossi"}
    assert result.confidence_score == 0.83
    # The crop is the object's whole rectangle, mapped onto the 2x scan.
    assert crops[0].size == (int(OBJ_W * SCALE), int(OBJ_H * SCALE))


# ---------------------------------------------------------------------------
# The degenerate region
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "geometry",
    [
        {"width": 0.0},
        {"height": 0.0},
        {"width": -50.0},
        {"height": -10.0},
        {"x": 5000.0},  # wholly off the sheet
    ],
)
def test_a_degenerate_region_reads_as_empty_not_an_error(
    db, pages_dir, project, actor, template_page, monkeypatch, geometry
):
    _forbid_ocr(monkeypatch)
    obj = _make_free_text(db, template_page, **geometry)
    batch = _make_batch(db, project, actor, threshold=0.0)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)

    assert result.detected_payload_json == {"suggested_text": ""}
    assert result.confidence_score is None
    assert result.validity_forces_review is True
    assert result.review_required is True
    assert result.result_type is SubmissionResultType.FREE_TEXT


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
def test_a_respondent_identifier_free_text_area_is_read_like_any_other(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    """No refusal: a FREE_TEXT_AREA holding
    RESPONDENT_IDENTIFIER is read exactly like any other — a FREE_TEXT
    reading, always in review — and review is where the identity it spells is
    confirmed or corrected."""
    monkeypatch.setattr(svc, "_run_ocr", lambda _crop: ("123456", 0.9))
    obj = _make_free_text(
        db, template_page, role=PageObjectRole.RESPONDENT_IDENTIFIER
    )
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)

    assert result.result_type is SubmissionResultType.FREE_TEXT
    assert result.detected_payload_json == {"suggested_text": "123456"}
    assert result.validity_forces_review is True
    assert result.review_required is True


def test_an_unmatched_page_raises_rather_than_guessing(
    db, pages_dir, project, actor, template_page
):
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan(), matched=False
    )

    with pytest.raises(ValueError, match="not matched"):
        svc.extract_free_text_result(db, page, obj, batch)


def test_an_object_without_free_text_config_raises(
    db, pages_dir, project, actor, template_page
):
    obj = _make_free_text(db, template_page, with_config=False)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    with pytest.raises(ValueError, match="free_text_config"):
        svc.extract_free_text_result(db, page, obj, batch)


# ---------------------------------------------------------------------------
# The staged row itself
# ---------------------------------------------------------------------------
def test_the_result_is_typed_free_text_and_staged_not_committed(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    _stub_ocr(monkeypatch, "Mario Rossi", 0.9)
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)

    assert isinstance(result, SubmissionObjectResult)
    assert result.result_type is SubmissionResultType.FREE_TEXT
    assert result.page_object_id == obj.id
    assert result.submission_page_id == page.id
    assert result in db.new


def test_the_reading_survives_a_flush(
    db, pages_dir, project, actor, template_page, monkeypatch
):
    _stub_ocr(monkeypatch, "Mario Rossi", 0.9)
    obj = _make_free_text(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_free_text_result(db, page, obj, batch)
    db.flush()
    db.refresh(result)

    assert result.result_type is SubmissionResultType.FREE_TEXT
    assert result.detected_payload_json == {"suggested_text": "Mario Rossi"}
    assert result.review_required is True
