# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-17

First public release.

### Added

- Form designer in the browser: multiple-choice questions (single or multiple answer), answer grids, free-text areas, text, pictures, QR codes and barcodes, on any number of A4 pages.
- Several variants of the same form (versions A, B, C…), recognised from a code printed on the sheet.
- Export of each variant as a printable PDF, in a blank version to fill in and a solution version with the correct answers.
- Upload of scanned submissions as PDF or as JPEG and PNG images; sheets straightened using the printed marks and matched to their form, variant and page.
- Automatic reading of the marked answers, and OCR of the handwriting in free-text areas with Tesseract.
- Review screen for the readings that need a human, with several reviewers working on the same batch.
- Scoring with a score for each option or grid cell, negative points included.
- Results export per submission and per answer, as CSV or Excel.
- User accounts with administrator and user roles; projects shared between owners and collaborators; audit log.
- Self-hosted installation with Docker Compose; database migrations applied at every start.
- User guide in `docs/user-guide/`: installation, concepts, a step-by-step walkthrough, reference of the functions, printing and scanning advice.

[Unreleased]: https://github.com/superalberto76/FormScanner/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/superalberto76/FormScanner/releases/tag/v0.1.0
