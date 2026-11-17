# Golden set of real scans

Sheets of the test kit printed, filled in by hand and scanned, with their truth sheet: they measure the extractors on real sheets, where the synthetic generator (`tests/synthetic`) cannot reach.

## Contents

| File | Content |
| --- | --- |
| `scans/<case>.pdf` | F01, F07, F08, F12, F13, F21a, F21b: COLL-MULTI A, three pages each, 300 dpi greyscale. 30 MB at most altogether |
| `scans/no_side_marks/F01.pdf` | An F01 printed with the four corner marks only, without the side marks: not a case, it only checks that such a sheet is still aligned on its corners |
| `truth.json` | The truth sheet, written by hand |
| `golden.py` | Cases, geometry fingerprint, loading of `truth.json` and the expectation of each case, observed table |
| `test_golden_set.py` | The test: one case per scan |
| `test_golden_infra.py` | The infrastructure, on synthetic sheets: fingerprint, `run_pdf`, consistency of `truth.json`, known limits |
| `observe.py` | The observation command |

## Running the tests

```bash
# backend virtualenv active; TEST_DATABASE_URL (and DATABASE_URL) point to the test DB
cd backend
pytest -m golden tests/golden          # -s to see the notes on the known limits
python -m tests.golden.observe         # observed table; --case F07 for one case only
```

Marker `golden`, excluded from the default suite like the synthetic tests. Tesseract with the Italian model is required: without Tesseract, the tests that extract are skipped. The OCR language is pinned to `ita` (autouse fixture in `conftest.py`, and the same in `observe.py`) whatever the environment's `OCR_LANGUAGE`, because the sheets are Italian. Run them by hand when you touch the extractors, the alignment or the export layout.

## The truth sheet

Every case starts from the expectations of the synthetic scenario F01 (`base`), with its own respondent ID and score. `objects` lists only the objects that the case's filling instructions change:

- **`requirement`**: exact reading and review reasons, checked strictly (today only questions);
- **`known_limit`**: reading and reasons are not compared, but the object must go to review. A known limit read without review fails the test with a message saying so: record it as an application bug, do not rewrite it as an expectation. If instead the reading reported in the message is right (an improved extractor), the object is reclassified as `requirement`, by a deliberate decision. A free text among the known limits must still have ALWAYS_REVIEW.

The expectations are decided by hand, never copied from what was observed.

## When the layout changes

`fingerprint` is the hash of the positions of objects, bubbles and marks read back from the "student" PDF of COLL-MULTI A. If the code exports a different layout, every case fails with "golden set printed with another layout: reprint it". An intended change is closed in the same branch: reprint the sheets from the COLL-\* projects created by `seed_projects`, fill them in following the instructions, scan them and update `fingerprint`, `print` and `scan` in `truth.json`.

## Scans

300 dpi, greyscale, PDF, from the automatic document feeder (ADF) or the flatbed, with the sheet upright; one PDF per submission, named after the case. The scanner of each case, and any printing note, are in `truth.json`.
