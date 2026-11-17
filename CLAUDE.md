# FormScanner — instructions for coding agents

FormScanner is a self-hosted web platform for paper-based exams and questionnaires: forms are designed in the browser and exported as printable PDFs; the filled-in sheets are scanned and uploaded; FormScanner identifies each sheet, reads the answers, sends uncertain readings to human review, computes the score and exports the results.

## Where things are

- `backend/`: Python, FastAPI, SQLAlchemy, Alembic, PostgreSQL; PDFs with PyMuPDF.
- `frontend/`: React 18, TypeScript, Vite, TanStack Query v5, custom canvas editor ([frontend/README.md](frontend/README.md)).
- `docs/specification/`: what the product does and how it is built. Read the part that covers your change before writing code.
  - `functional-specification.md`: the product's rules and behavior.
  - `architecture.md`: components, repository layout, startup, storage, configuration, test suites.
  - `api.md`: HTTP API conventions, access rules, errors (structured 409 codes).
  - `data-model.md`: tables, enums, constraints.
  - `pdf-export.md`: layout of the printable PDF.
- `docs/user-guide/`: the user guide (installation, concepts, walkthrough, reference, printing and scanning). Update it when a change alters what users see or do.

Domain model: FormProject → FormVariant → FormVariantPage → PageObject (TEXT, CODE, FREE_TEXT_AREA, QUESTION, MATRIX, IMAGE); scanned side: SubmissionBatch → Submission → SubmissionPage → SubmissionObjectResult.

## Commands

```bash
# backend, from backend/ (DATABASE_URL and TEST_DATABASE_URL pointing at a test database)
alembic upgrade head
pytest tests/                          # default suite
pytest -m synthetic tests/synthetic    # synthetic end-to-end scenarios (slow, needs Tesseract)
pytest -m golden tests/golden          # golden set of real scans (slow)

# frontend, from frontend/
npm test
npm run typecheck
npm run typecheck:test
npm run build
```

Never run `tsc` without `-p`.

## Rules for agents

- **Do not decide product behavior in the code.** When a change needs a choice the specification and the issue do not settle (what the product does, who sees what, an API contract, the data model), stop and ask, with the options and a recommendation.
- **Do not assume the structure:** read the files you change and the code that uses them first.
- **A failing test is not adapted to pass**, and expected values of the synthetic and golden suites are never adapted to what the code produces: find the cause, and report it if you cannot fix it.
- **Bugs found by chance**, outside the task: report them, do not fix them inside an unrelated change.
- **Run the checks** of the parts you changed before calling the work done ([CONTRIBUTING.md §3](CONTRIBUTING.md#3-running-the-checks)).
- **Documentation in the same change:** update the specification, the technical documents, the user guide and `CHANGELOG.md` that the change makes outdated ([CONTRIBUTING.md §5](CONTRIBUTING.md#5-documentation-in-the-same-pull-request)).
- No issue or pull request numbers in the code.

## Conventions

The conventions are in [CONTRIBUTING.md](CONTRIBUTING.md), imported here:

@CONTRIBUTING.md
