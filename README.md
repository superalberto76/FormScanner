# FormScanner

[![CI](https://github.com/superalberto76/FormScanner/actions/workflows/ci.yml/badge.svg)](https://github.com/superalberto76/FormScanner/actions/workflows/ci.yml)

FormScanner is a self-hosted platform for paper-based exams and questionnaires. You design multiple-choice forms in the browser, export them as printable PDFs and print them. Respondents fill them in by hand. You scan the completed sheets and upload them; FormScanner identifies each sheet, reads the answers, sends the uncertain readings to a human reviewer, computes the score and exports the results as CSV or Excel.

- Forms with questions (single or multiple choice), answer grids, free-text areas, pictures, QR codes and barcodes, on any number of A4 pages.
- Several variants of the same form (versions A, B, C…), recognised automatically from a code printed on the sheet.
- Scans uploaded as PDF or as JPEG and PNG images; sheets straightened using the marks printed on them.
- A review screen for the readings that need a human; several reviewers can work on the same batch.
- Scores with negative points, and results export per submission and per answer.

FormScanner is the successor of the earlier [FormScanner desktop application](https://sourceforge.net/projects/formscanner/) (Java, on SourceForge), which is no longer maintained. This is a new codebase, rewritten from scratch as a web application.

## Requirements

- Docker Engine with the Compose plugin (`docker compose`, version 2).
- About 2 GB of RAM and 5 GB of disk for the images and the build, plus the space of your scans.

## Quick start

```bash
git clone https://github.com/superalberto76/FormScanner.git
cd FormScanner
cp .env.example .env
```

Edit `.env` and change at least `JWT_SECRET` (a long random string), `POSTGRES_PASSWORD` (the same password inside `DATABASE_URL`), `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD`. Then:

```bash
mkdir -p data/postgres data/storage
docker compose up -d --build
```

Docker Compose names the installation after its folder: use a folder name that no other Compose project on the machine uses.

Open <http://localhost:8080> and sign in with `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD`.

The complete installation, with the first start, access from other machines, the OCR language, updates, backups and the configuration, is in the [user guide, chapter 1](docs/user-guide/01-installation.md).

## Documentation

- [User guide](docs/user-guide/) — how to use FormScanner, step by step.
- [Functional specification](docs/specification/functional-specification.md) — what FormScanner does and the rules it applies.
- Technical documentation: [architecture](docs/specification/architecture.md), [API](docs/specification/api.md), [data model](docs/specification/data-model.md), [printable PDF](docs/specification/pdf-export.md).
- API reference: the running backend serves it at <http://localhost:8000/docs> (Swagger UI) and <http://localhost:8000/redoc>.
- [Frontend](frontend/README.md): development, build and checks.

## Contributing

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Copyright (C) 2026 Alberto Borsetta

FormScanner is free software, released under the GNU Affero General Public License, version 3 or later (`AGPL-3.0-or-later`): see [LICENSE](LICENSE).

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

FormScanner creates PDFs with [PyMuPDF](https://pymupdf.readthedocs.io/), which is available under the AGPL or under a commercial license from Artifex.
