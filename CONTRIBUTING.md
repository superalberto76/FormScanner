# Contributing to FormScanner

Thank you for your interest in FormScanner. This guide explains how to propose a change, how to set up a development environment, which checks a change must pass and the conventions of the code.

## 1. Before you start

- **Bug fixes and small corrections** (a typo, a wrong message, a clear defect with an obvious fix): open a pull request directly.
- **New features, or changes to how FormScanner behaves**: open an issue first ([feature request](../../issues/new/choose)) and wait for a maintainer's opinion before writing the code. A large pull request on a feature the project does not want is wasted work for everybody.
- Issues, pull requests, commit messages, code, comments and documentation are written in **English**.
- **Security vulnerabilities** are never reported in a public issue: follow [SECURITY.md](SECURITY.md).
- Everyone taking part in the project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## 2. Development environment

FormScanner has a Python backend (`backend/`), a React frontend (`frontend/`) and a PostgreSQL database. The layout of the repository is described in [architecture.md](docs/specification/architecture.md#2-repository-layout).

### Backend

- Python 3.12 (the version of `backend/Dockerfile`).
- A PostgreSQL database for the tests (version 16, as in `docker-compose.yml`). The tests need a real PostgreSQL: the models use PostgreSQL types. Use a database that holds no data you care about: the integration tests empty it.
- Optional: Tesseract with the English and Italian data (`tesseract-ocr`, `tesseract-ocr-eng`, `tesseract-ocr-ita` on Debian and Ubuntu), and the ZBar library (`libzbar0`). The tests that need them are skipped when they are missing.

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

export DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/formscanner_test
export TEST_DATABASE_URL=$DATABASE_URL
alembic upgrade head
```

`alembic` reads `DATABASE_URL`, the tests read `TEST_DATABASE_URL`: point both at the test database. Run `alembic upgrade head` again whenever the migrations change.

To run the backend in development, point `DATABASE_URL` at a development database (not the test one) and repeat the steps of the container ([startup sequence](docs/specification/architecture.md#6-startup-sequence)):

```bash
alembic upgrade head
python -m app.bootstrap
uvicorn app.main:app --reload --port 8000
```

The variables the backend reads, with their defaults, are in the [configuration reference](docs/specification/architecture.md#82-read-by-the-backend).

### Frontend

- Node.js 24 (the version of `frontend/Dockerfile` and of the continuous integration). `npm test` does not run on older versions.

```bash
cd frontend
npm install
```

Development server, `.env.local`, build and the structure of `src/`: see [frontend/README.md](frontend/README.md).

## 3. Running the checks

Every pull request must pass the checks of the parts it changes.

The continuous integration (GitHub Actions, [architecture.md](docs/specification/architecture.md#91-continuous-integration)) runs them on every pull request and every push to `main`: the backend suite, the synthetic and golden suites below, and the frontend checks, plus a `dco` check that every commit of the pull request is signed off ([§6](#6-commits-and-pull-requests)). The backend suites run in the image of `backend/Dockerfile`, with Tesseract and ZBar, so no test is skipped there for a missing library. A pull request is merged only when every check is green. Run the checks locally as well before you open it: they are faster to read than a CI log.

**Backend**, from `backend/` with the virtualenv active and the two variables set:

```bash
alembic upgrade head
pytest tests/
```

The default run excludes two slow suites. The continuous integration always runs them; run them locally too when your change touches what they cover:

| Suite | Command | Run it when you change |
|---|---|---|
| Synthetic end-to-end | `pytest -m synthetic tests/synthetic` | Upload, sheet identification, extraction, review, export ([README](backend/tests/synthetic/README.md)) |
| Golden set of real scans | `pytest -m golden tests/golden` | Sheet alignment, mark and code reading, OCR ([README](backend/tests/golden/README.md)) |

There is no linter and no type checker for the backend: the check is the test suite.

**Frontend**, from `frontend/`:

```bash
npm test                 # unit tests of the pure modules
npm run typecheck        # the app
npm run typecheck:test   # the tests
npm run build
```

Always run `tsc` with `-p` (`npm run typecheck` does). There is no ESLint.

**Browser end-to-end tests** (`npm run e2e`, Playwright) run on request: use them when you change a flow across backend and frontend. Prerequisites in [frontend/e2e/README.md](frontend/e2e/README.md).

A failing test is never adapted just to make it pass: first find out whether it shows a problem in the change. The same holds for the expected values of the synthetic and golden suites, which come from the specification, not from what the code produces.

## 4. Conventions

### General

- Code, comments, identifiers, user interface and documentation in English.
- No references to issue or pull request numbers in the code: a comment explains the reason in its own words. The only exception is a known bug declared in a test (`xfail`, or a deviation of a synthetic scenario), which names the issue that tracks it.
- Every source file starts with the license header (Python shown; `//` in TypeScript, `/* */` in CSS):

  ```python
  # SPDX-FileCopyrightText: 2026 Alberto Borsetta
  # SPDX-License-Identifier: AGPL-3.0-or-later
  ```

  A backend test fails when a Python file lacks it.
- Changes are additive and incremental: do not break existing API contracts.
- Prefer explicit, readable code to clever code. Introduce a shared abstraction only after three nearly identical implementations.
- Aggregate values (counts, pending items, the score during review) are computed when they are read, not stored.
- Write the tests together with the code they cover.

### Backend

Python, FastAPI, SQLAlchemy, Alembic, PostgreSQL; the printable PDFs are built with PyMuPDF.

- **Layers:** endpoints in `app/api/v1/endpoints/`, business rules in `app/services/`, queries in `app/repositories/`, request and response models in `app/schemas/` ([architecture.md](docs/specification/architecture.md#3-backend)).
- **Tests:** unit tests in `backend/tests/`, tests that need the database in `backend/tests/integration/` (fixture `db` in its `conftest.py`). `httpx` is not a dependency: there is no `TestClient`, routes are tested by calling their functions. `tests/` has no `__init__.py`, so every test file name must be unique across the whole folder.
- **Migrations:** find the current head with `alembic heads`. Create new enum types with the `DO $$ … EXCEPTION WHEN duplicate_object` pattern. Follow the docstring style of the latest migration and the column and foreign-key syntax of the initial migration, `a7c3e9d1f5b2`.
- **Structured 409:** when the client has to tell conflicts apart, `detail` is `{code, message, ...}`, built with `structured_conflict`. The codes in use and their extra fields are listed in [api.md](docs/specification/api.md#6-errors); a new code is added there in the same pull request.
- **Times** leave the backend in UTC.
- **Review:** every review save goes through `guard_review_save`; the review rules live only in `services/review_rules.py` (`is_confirmable`, `is_bulk_acceptable`); a reading is resolved when its `review_outcome` is not null; review actions return the whole submission.
- **Served images:** the MIME type comes from the content, with the `X-Content-Type-Options: nosniff` header.
- **PDF layout:** imperative, with explicit constants ([pdf-export.md](docs/specification/pdf-export.md)). The frontend editor replicates the constants in `frontend/src/utils/exportLayout.ts`: change both together.

### Frontend

React 18, TypeScript, Vite, react-router-dom v6, TanStack Query v5. No UI component library; the page editor is a custom canvas.

- **User interface strings** in English: labels, errors, banners, tooltips, buttons.
- **Types** in `src/api/` follow the backend schemas: when a schema changes, its frontend type changes in the same pull request.
- **Pessimistic mutations:** no optimistic updates; only server responses are written into the query cache. Invalidate instead of polling, once at the end of a flow. Use safe default values while data is loading.
- **Query client:** global `staleTime` of 30 seconds and `refetchOnWindowFocus: false`. Exceptions are declared on the single query (the batch queue and the submission page; sheet images use `staleTime: Infinity`).
- **Errors:** `ApiError.code` and `ApiError.data` carry the structured 409; read the code with `getErrorCode(err)`. On the submission page, save errors go through `handleSaveError`. Use the `confirmable` and `bulk_acceptable` flags sent by the server instead of recomputing them.
- **No placeholders** in the user interface for features that do not exist yet.
- **Pure, testable logic:** a `.ts` module without JSX, with its `*.test.ts` next to it. A pure module may import values from another pure module using the `.ts` extension.

## 5. Documentation in the same pull request

A pull request updates the documentation that its change makes outdated, in the same pull request:

- the user guide (`docs/user-guide/`), when the behavior visible to users changes. When the interface changes, regenerate the screenshots of chapter 3 with `npm run screenshots` (from `frontend/`, prerequisites as the browser tests, [frontend/e2e/README.md](frontend/e2e/README.md#screenshots-of-the-user-guide)) and commit only the pictures of the steps your change touches: every run also changes the others a little (submission ids, upload times);
- the functional specification (`docs/specification/functional-specification.md`), when a rule of the product changes;
- the technical documents (`docs/specification/architecture.md`, `api.md`, `data-model.md`, `pdf-export.md`), when the architecture, the API, the database schema or the PDF layout change;
- `CHANGELOG.md`, with an entry under "Unreleased" for every change a user or an administrator would notice.

The specification describes the product as it is: it changes together with the code, not before.

## 6. Commits and pull requests

- **Sign off every commit** (`git commit -s`). The `Signed-off-by` line certifies that you wrote the change or have the right to submit it under the project's license, as stated by the [Developer Certificate of Origin](https://developercertificate.org/). A pull request with a commit that is not signed off cannot be merged: the `dco` check of the continuous integration fails and lists the commits. To sign off the last commit afterwards: `git commit --amend -s`; for several commits: `git rebase --signoff main`.
- Commit messages in English, with a short first line that says what the commit does.
- One topic per pull request. Fill in the [template](.github/pull_request_template.md): summary, related issue, checklist.
- Link the issue in the pull request description with `Closes #N`, so that it closes when the pull request is merged. Use `Refs #N` in commit messages: a `Closes` in a commit would close the issue as soon as the commit reaches `main`.

## 7. License of contributions

FormScanner is released under the GNU Affero General Public License, version 3 or later (`AGPL-3.0-or-later`, see [LICENSE](LICENSE)). By signing off your commits you agree that your contribution is distributed under the same license.
