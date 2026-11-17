# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for FREE_TEXT_AREA extraction.

``_run_ocr``'s aggregation of Tesseract's per-word output and ``_crop_box``'s
clamping, exercised with ``pytesseract.image_to_data`` stubbed out — so they
run whether or not the Tesseract binary is installed. Also the configured OCR
language: its validation in ``Settings``, the value handed to Tesseract and the
startup check. The DB-backed half (real OCR on synthetic scans, preconditions,
the review rule, the degenerate region) lives in
``tests/integration/test_free_text_extraction_integration.py``.
"""
from __future__ import annotations

import pytest
import pytesseract
from PIL import Image
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.services import free_text_extraction_service as svc
from app.services.question_extraction_service import ScanRegion


def _stub_image_to_data(monkeypatch, text: list[str], conf: list) -> list[dict]:
    """Make ``image_to_data`` return *text*/*conf*; record how it was called."""
    calls: list[dict] = []

    def fake(image, **kwargs):
        calls.append(kwargs)
        return {"text": text, "conf": conf}

    monkeypatch.setattr(svc.pytesseract, "image_to_data", fake)
    return calls


def _blank() -> Image.Image:
    return Image.new("L", (100, 40), 255)


# ---------------------------------------------------------------------------
# _run_ocr
# ---------------------------------------------------------------------------
def test_words_are_joined_and_confidence_is_the_normalised_mean(monkeypatch):
    _stub_image_to_data(monkeypatch, ["Mario", "Rossi"], [90, 70])

    text, confidence = svc._run_ocr(_blank())

    assert text == "Mario Rossi"
    assert confidence == pytest.approx(0.8)


def test_non_word_elements_and_empty_tokens_are_ignored(monkeypatch):
    """Tesseract reports -1 for blocks/lines/paragraphs, and whitespace-only
    tokens for gaps: neither is a word, and neither may drag the mean."""
    _stub_image_to_data(
        monkeypatch,
        ["", "  ", "hello", "", "world", "ghost"],
        [-1, 50, 80, -1, "60", -1],
    )

    text, confidence = svc._run_ocr(_blank())

    assert text == "hello world"
    assert confidence == pytest.approx(0.7)


def test_nothing_read_is_an_empty_string_and_no_confidence(monkeypatch):
    _stub_image_to_data(monkeypatch, ["", " "], [-1, -1])

    assert svc._run_ocr(_blank()) == ("", None)


def test_the_configured_language_and_segmentation_mode_are_passed(monkeypatch):
    monkeypatch.setattr(settings, "ocr_language", "ita+eng")
    calls = _stub_image_to_data(monkeypatch, [], [])

    svc._run_ocr(_blank())

    assert calls[0]["lang"] == "ita+eng"
    assert f"--psm {svc.OCR_PSM}" in calls[0]["config"]
    assert calls[0]["output_type"] == pytesseract.Output.DICT


def test_a_missing_ocr_engine_is_raised_not_masked_as_empty(monkeypatch):
    """The deliberate difference from ``decode_codes_from_image``: an absent
    engine must surface as an error, never as a legitimate-looking empty
    reading on every FREE_TEXT_AREA of the batch."""

    def missing(image, **kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(svc.pytesseract, "image_to_data", missing)

    with pytest.raises(pytesseract.TesseractNotFoundError):
        svc._run_ocr(_blank())


# ---------------------------------------------------------------------------
# _crop_box
# ---------------------------------------------------------------------------
def test_a_region_inside_the_scan_maps_to_its_rounded_box():
    box = svc._crop_box(ScanRegion(x=10.4, y=20.6, width=100.0, height=50.0), 800, 600)

    assert box == (10, 21, 110, 71)


def test_a_region_overflowing_the_scan_is_clamped_to_it():
    box = svc._crop_box(ScanRegion(x=-20, y=-10, width=900, height=700), 800, 600)

    assert box == (0, 0, 800, 600)


@pytest.mark.parametrize(
    "region",
    [
        ScanRegion(x=10, y=10, width=0, height=50),
        ScanRegion(x=10, y=10, width=50, height=0),
        ScanRegion(x=10, y=10, width=-5, height=50),
        ScanRegion(x=900, y=10, width=50, height=50),   # wholly right of the scan
        ScanRegion(x=10, y=-80, width=50, height=50),   # wholly above it
    ],
)
def test_a_degenerate_region_has_no_box(region):
    assert svc._crop_box(region, 800, 600) is None


# ---------------------------------------------------------------------------
# Configured OCR language
# ---------------------------------------------------------------------------
def test_the_ocr_language_defaults_to_english(monkeypatch):
    monkeypatch.delenv("OCR_LANGUAGE", raising=False)

    assert Settings(_env_file=None).ocr_language == "eng"


def test_the_default_language_is_what_tesseract_receives(monkeypatch):
    monkeypatch.delenv("OCR_LANGUAGE", raising=False)
    monkeypatch.setattr(settings, "ocr_language", Settings(_env_file=None).ocr_language)
    calls = _stub_image_to_data(monkeypatch, [], [])

    svc._run_ocr(_blank())

    assert calls[0]["lang"] == "eng"


@pytest.mark.parametrize("value", ["eng", "ita", "chi_sim", "ita+eng", "eng+ita+deu"])
def test_tesseract_codes_are_accepted_as_they_are(monkeypatch, value):
    monkeypatch.setenv("OCR_LANGUAGE", value)

    assert Settings(_env_file=None).ocr_language == value


@pytest.mark.parametrize(
    "value",
    [
        "", " ", "ita+", "+eng", "ita++eng", "ita eng", "ita,eng", "it-IT", "eng\n",
        # Names get_languages never reports: they would always be "not installed".
        "Eng", "eng2", "script/Latin",
    ],
)
def test_an_empty_or_malformed_language_is_a_configuration_error(monkeypatch, value):
    monkeypatch.setenv("OCR_LANGUAGE", value)

    with pytest.raises(ValidationError, match="OCR_LANGUAGE"):
        Settings(_env_file=None)


def test_an_installed_language_passes_the_check():
    svc.check_ocr_language("ita", {"eng", "ita", "osd"})
    svc.check_ocr_language("ita+eng", {"eng", "ita", "osd"})


def test_a_language_not_installed_fails_naming_it_and_the_installed_ones():
    with pytest.raises(RuntimeError) as excinfo:
        svc.check_ocr_language("deu", {"ita", "eng"})

    assert str(excinfo.value) == (
        "OCR language 'deu' is not installed (installed: eng, ita). "
        "Install the Tesseract data package or change OCR_LANGUAGE."
    )


def test_a_combination_with_one_component_missing_fails():
    """Not silently run on the installed half of the combination."""
    with pytest.raises(RuntimeError, match="'deu' is not installed"):
        svc.check_ocr_language("ita+deu", {"ita", "eng"})


def test_every_missing_component_is_named():
    with pytest.raises(RuntimeError, match="'deu', 'fra' are not installed"):
        svc.check_ocr_language("deu+ita+fra", {"ita"})


def test_the_startup_check_reads_the_installed_languages(monkeypatch):
    monkeypatch.setattr(settings, "ocr_language", "deu")
    monkeypatch.setattr(svc.pytesseract, "get_languages", lambda **kw: ["eng", "ita", "osd"])

    with pytest.raises(RuntimeError, match="'deu' is not installed"):
        svc.check_ocr_language_at_startup()


def test_the_startup_check_passes_with_the_configured_language_installed(monkeypatch):
    monkeypatch.setattr(settings, "ocr_language", "ita")
    monkeypatch.setattr(svc.pytesseract, "get_languages", lambda **kw: ["eng", "ita", "osd"])

    svc.check_ocr_language_at_startup()


def test_the_startup_check_does_not_block_without_the_tesseract_binary(monkeypatch, caplog):
    """The check does not make Tesseract mandatory — a warning, no error."""

    def missing(**kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(settings, "ocr_language", "deu")
    monkeypatch.setattr(svc.pytesseract, "get_languages", missing)

    with caplog.at_level("WARNING", logger=svc.logger.name):
        svc.check_ocr_language_at_startup()

    assert "Tesseract is not installed" in caplog.text
