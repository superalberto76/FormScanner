# Synthetic tests and test kit

This folder makes most of the end-to-end acceptance tests repeatable without paper.

## What is here

| File | Contents |
| --- | --- |
| `specs.py` | The test-kit projects COLL-MULTI (variants A, B, C published and D in DRAFT), COLL-SINGLE and COLL-CONFIG, as data: positions, answer key, VID values |
| `builders.py` | Builds the projects in the DB through the app's services |
| `generator.py`, `template_geometry.py` | Generator of filled-in sheets: starts from the variant's "student" PDF and draws the marks where the PDF prints the bubbles |
| `scenarios.py` | The kit cases: filling, seed and hand-written expectations (`SCENARIOS`; G03–G05 in `LOOSE_CASES`) |
| `runner.py` | Puts a case through upload, matching, extraction and the review read, and compares it with the expectations |
| `test_*.py` | Tests of the generator, scenarios, matching, extraction and review flows, faulty uploads, kit export |
| `export_kit.py` | Exports the kit for manual testing |
| `seed_projects.py` | Creates the test-kit projects in an environment's DB |
| `e2e_env.py` | Prepares the `*_e2e` DB and the kit of the browser e2e tests: Playwright's global setup runs it, see `frontend/e2e/README.md`. Its test, `test_e2e_env_prepare.py`, is marked `synthetic` |
| `sample_project.py` | The sample project of the user guide's screenshots ("Sample quiz", four filled-in sheets) and the steps `npm run screenshots` calls: `prepare`, `fill`, `sheets`, `render`. Its test, `test_sample_project.py`, checks that each sheet reads as the guide shows |

## Running the tests

From the repo root:

```bash
# backend virtualenv active; TEST_DATABASE_URL (and DATABASE_URL) point to the test DB
cd backend
alembic upgrade head
pytest tests/                        # default suite: the slow synthetic tests are excluded
pytest -m synthetic tests/synthetic  # end-to-end scenarios, about 7 minutes
```

The scenarios that use OCR need Tesseract: without it, they are skipped. A known app bug is declared in the scenario as a `Deviation` with the issue number; the expectations are never adapted to what is observed.

## The kit for manual testing

The kit is the set of already filled-in submissions, to upload from the UI without printing or scanning, together with the truth sheet. The files are the same bytes the scenarios upload: with `pytest -m synthetic tests/synthetic` green, the kit is the verified one.

### 1. Once per environment: create the test-kit projects

The kit's sheets are recognised only by projects built from the same spec as the tests: matching reads the VID value (`COLL-MULTI-A`…) and the printed geometry depends on the spec. A project created by hand from the UI does **not** work.

1. Use the code of the release deployed in the environment: the command stops if the DB is not at the code's Alembic head.
2. Two users must exist in the environment: the owner and a second operator (for the two-operator checks).
3. Check that the environment's DB is reachable and at the Alembic head:

   ```bash
   psql "postgresql://USER:PASSWORD@HOST:5432/DB_NAME" -c "select version_num from alembic_version"
   ```

4. Run:

   ```bash
   cd backend   # the tests.synthetic modules are found from here, not from the repo root
   python -m tests.synthetic.seed_projects \
       --database-url "postgresql+psycopg://USER:PASSWORD@HOST:5432/DB_NAME" \
       --owner owner@example.com --collaborator collaborator@example.com
   ```

It creates COLL-MULTI, COLL-SINGLE and COLL-CONFIG, ACTIVE, with the second user as COLLABORATOR, all three together or none. It writes nothing if a user does not exist or is disabled, if the two emails are the same user, if the DB is not at the head, or if the owner already has a project with one of the three names. It does not change or delete existing projects. The projects stay for the following test campaigns: each release only needs a new batch.

The authoring checks on test-kit projects (roles, rejections, duplicate VID) are done by hand on a trial project, not on the COLL-* ones.

### 2. For each campaign: export the kit

```bash
cd backend   # as for seed_projects
python -m tests.synthetic.export_kit --out /path/kit-YYYY-MM-DD
```

The folder must be new or empty; if an export is interrupted, delete it and run again. The command builds the projects in the test DB (`TEST_DATABASE_URL`, or `--database-url`) inside a transaction rolled back at the end: it never writes to the DB. About 80 s, 37 files, about 120 MB.

**Export from the commit deployed in the environment where you will upload the kit.** The printed geometry depends on the export code: a kit exported from a branch with a different layout would be misread, without any warning. At the end the command prints the commit it was exported from.

U03 is generated just above the size limit of the local configuration (`max_submission_pdf_bytes`, 50 MiB). If the environment raises it through an environment variable, U03 is no longer rejected.

| Folder | Batch, project | Contents |
| --- | --- | --- |
| `1-kit-pdf` | "Kit PDF", COLL-MULTI | F01–F23 of the synthetic kit, except F22 (20 PDFs) |
| `2-kit-images-multi` | "Kit images", COLL-MULTI | G01 (`verdi_pagina_*.png`), U01 (`.docx`), U02 (empty and corrupt PDF), U03 (PDF over the limit) |
| `3-kit-images-single` | "Kit images", COLL-SINGLE | `G03/upload-1` and then, in a second upload, `G03/upload-2`; `G04/a` and `G04/b` added to the same selection (same name, different contents) and uploaded together; `G05` |
| `4-kit-single` | COLL-SINGLE | F22, G02 (`rossi-mario_pagina_*.jpg`) |

The truth sheet is in the kit's root, UTF-8 with BOM:

- `truth_objects.csv`: one row per case, sheet and object, with what is marked on the sheet, the expected reading, whether it goes to review and for which reason;
- `truth_submissions.csv`: one row per case, with the status after upload and after extraction, the candidate, the missing pages, how many objects in review, expected points and maximum. For G03–G05 and U01–U03 only the fields the case declares, and the expected outcome in `notes`.

The "observed" columns and `outcome` are empty: they are filled in during the campaign.

### Cases that stay on paper

F08 (eraser and correction fluid), F12 and F13 (handwriting for the OCR), F21a (scaled print) and F21b (rotated scan) are not in the kit: the generator does not reproduce them meaningfully. These sheets, plus F01 and F07 also printed on paper, make up the golden set of real scans. Scans, truth sheet and tests are in `tests/golden` (see the folder's README).

### Extraction error

With the synthetic kit, use F01 (on paper the case uses F12). Not on the "Kit PDF" batch, where after the first Run F01 is already SCANNED and a new run does not re-read objects already extracted: create a "Kit error" batch on COLL-MULTI, upload `1-kit-pdf/F01.pdf` again, delete on the server the image file of one of its pages **before** the first Run, then launch the Run. The submission stays MATCHED with "⚠ N objects unread" and opens with "Review anyway".
