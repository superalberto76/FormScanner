# Browser end-to-end tests (Playwright)

The upload → extraction → review → closing flow tried in the browser, on the backend and the frontend of the same commit, with the synthetic test kit and the COLL-\* projects of `seed_projects`.

They run **on request**: they are not part of `npm test`.

## What `npm run e2e` does

1. Starts the backend (`uvicorn`, port 18000) on the e2e database, with the storage in `e2e/.work/storage`.
2. Builds the frontend for production (`vite build` into `e2e/.work/dist`, `VITE_API_BASE_URL` pointing at the backend) and serves it with `vite preview` (port 14173).
3. Global setup (`e2e/global-setup.ts`): empties `e2e/.work/kit` and `e2e/.work/storage`, then `python -m tests.synthetic.e2e_env`:
   - creates the e2e database if missing, **empties** it and brings it to the Alembic head;
   - creates the two operators, "Coll Owner" (`e2e-owner@example.com`) and "Coll Collaborator" (`e2e-collab@example.com`), password `E2e-Password-1`;
   - exports the kit into `e2e/.work/kit` (~80 s);
   - creates COLL-MULTI, COLL-SINGLE and COLL-CONFIG owned by the first operator, with the second as a member.
4. Runs the tests in `e2e/*.spec.ts`, one at a time, Chromium only.

Every run starts again from a known state: ~2 minutes of preparation before the first test.

## Prerequisites

- The backend's Python with the dependencies of `requirements-dev.txt` and Tesseract (`eng`, `ita`).
- A reachable PostgreSQL, with a user that can create databases (the first time) and empty the `public` schema of the e2e database.
- Playwright's Chromium and its system libraries:

```bash
cd frontend
npm install
npx playwright install chromium          # browser, in ~/.cache/ms-playwright
sudo npx playwright install-deps chromium  # system libraries (root): once per machine
```

Where there is no root access, the system libraries have to be installed by whoever administers the machine.

## Command

```bash
# backend virtualenv active; TEST_DATABASE_URL (and DATABASE_URL) point at the test database
cd frontend
npm run e2e
```

A test declared with `test.fail` (a known app bug) shows up with ✘ but counts among the passed: it is an expected failure. If it passes, Playwright reports it as a failure: the bug is fixed and the declaration has to go. Today: the return to the tab in `two-operators.spec.ts`.

HTML report in `e2e/.work/report` (`npx playwright show-report e2e/.work/report`); traces and screenshots of the failed tests in `e2e/.work/test-results` (`npx playwright show-trace <file>.zip`).

## Variables

| Variable | Default | Use |
|---|---|---|
| `E2E_DATABASE_URL` | `TEST_DATABASE_URL` with the database renamed `formscanner_e2e` | The e2e database. Its name must end in `_e2e`: it is emptied at every run, and `e2e_env` refuses any other |
| `E2E_PYTHON` | `python` | The backend's interpreter (the one of the active venv) |
| `E2E_BE_PORT` | `18000` | Backend port |
| `E2E_FE_PORT` | `14173` | Frontend port (`vite preview`) |

The e2e database is never pytest's (`formscanner_test`, recreated by the fixtures) nor the app's.

## Test rules

- Selectors by role and text on the English UI (`getByRole`, `getByText`); a `data-testid` only when an element cannot be reached otherwise.
- Expected values come from the sources (the synthetic scenarios and flows), never from what a run observes. An app bug is declared with `test.fail` and a description of the bug, with a reference to its public issue when there is one; it is never worked around.
- All the environment's constants (ports, paths, operators, project names) live in `e2e/env.ts`.

## Screenshots of the user guide

`npm run screenshots` takes the pictures of chapter 3 of the [user guide](../../docs/user-guide/) and writes them into `docs/user-guide/images/` (`03-NN-<step>.png`, in the order of the chapter). It is not a test: it is not part of `npm run e2e` nor of the continuous integration. Run it when a change alters a screen the chapter shows, and commit with the change only the pictures of the steps it touches: every run also changes the others a little (submission ids, upload times), so the other pictures are restored with `git checkout -- docs/user-guide/images`, file by file.

Same prerequisites, servers and variables as the e2e tests; it uses the same e2e database and empties it. What it does:

1. Global setup (`e2e/screenshots-setup.ts`): `python -m tests.synthetic.sample_project prepare` empties the e2e database, brings it to the Alembic head and creates the administrator of a new installation (`admin@example.com`, `ChangeMe!`, as in `.env.example`), with the name "Alex Example" that chapter 3 has the reader set in **Profile**.
2. `e2e/screenshots.spec.ts` walks through chapter 3 in the browser (1280 × 800, light theme, Chromium): the colleague's account, the project "Sample quiz", its variant, one question drawn on the page. Then `sample_project fill` completes the form from its declaration, the browser publishes it and downloads the PDF, `sample_project sheets` writes four filled-in sheets, and the browser uploads, extracts, reviews and exports them. `sample_project render` turns the first page of the PDF and of one sheet into pictures. On the way the script checks the maximum score and the scores the chapter quotes, and stops if a step does not give what the chapter says.

The sample project, its sheets and the readings they give are declared in `backend/tests/synthetic/sample_project.py`; `tests/synthetic/test_sample_project.py` (synthetic suite) checks that each sheet reads as chapter 3 says. Every name is made up. The pictures differ slightly from one run to the next (submission ids, upload times).

```bash
# backend virtualenv active; TEST_DATABASE_URL (and DATABASE_URL) point at the test database
cd frontend
npm run screenshots
```

About two minutes. Traces of a failed run in `e2e/.work/screenshots-results`.
