# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Observe the golden-set scans.

    python -m tests.golden.observe [--case F07 --case F08 ...] [--database-url URL]

Runs from ``backend/``. Builds COLL-MULTI in the test DB (``TEST_DATABASE_URL``)
inside a transaction rolled back at the end — it never writes to the DB — and
the scanned pages in a temporary folder. For each scan it prints matching,
candidate, score and one row per object: reading, validity, confidence and
review reasons. With ``truth.json`` it also prints, for each case, the
discrepancies from its expectation and warns when the fingerprint differs.

It writes nothing else: the expectations are decided by hand and go into
``truth.json`` by hand.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from tests.golden.golden import (
    CASES,
    CASES_BY_ID,
    REPRINT_MESSAGE,
    TRUTH_PATH,
    case_expectation,
    load_truth,
    observed_rows,
    project_fingerprint,
    render,
    run_case,
)
from tests.synthetic.builders import build_coll_multi
from tests.synthetic.export_kit import code_version
from tests.synthetic.runner import check


def observe(session: Session, case_ids: list[str]) -> str:
    project = build_coll_multi(session)
    truth = load_truth() if TRUTH_PATH.exists() else None
    fingerprint = project_fingerprint(session, project)
    out = [
        f"Code: commit {code_version()}",
        f"Geometry fingerprint of COLL-MULTI A: {fingerprint}",
    ]
    if truth is not None and truth.fingerprint != fingerprint:
        out.append(f"WARNING: truth.json has the fingerprint {truth.fingerprint}: {REPRINT_MESSAGE}")
    out.append("")
    for case_id in case_ids:
        case = CASES_BY_ID[case_id]
        outcome = run_case(session, project, case)
        errors = notes = None
        if truth is not None and case_id in truth.cases:
            scenario, loose = case_expectation(truth, case_id)
            notes = []
            errors = check(outcome, project, scenario, notes, loose=loose)
        out.append(render(case, outcome, observed_rows(outcome, project), errors, notes))
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.golden.observe",
        description="Print the readings observed on the golden-set scans.",
    )
    parser.add_argument(
        "--case", action="append", choices=[c.id for c in CASES],
        help="case to observe (repeatable; default: all)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("TEST_DATABASE_URL"),
        help="DB to build COLL-MULTI in, inside a rolled-back transaction "
             "(default: TEST_DATABASE_URL)",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or TEST_DATABASE_URL is required")
    if shutil.which("tesseract") is None:
        parser.error("Tesseract not installed: extraction uses the real OCR")
    case_ids = args.case or [c.id for c in CASES]
    missing = [c for c in case_ids if not CASES_BY_ID[c].path.exists()]
    if missing:
        parser.error(f"missing scans: {', '.join(missing)}")

    saved = (
        settings.submission_pages_dir,
        settings.submission_originals_dir,
        settings.ocr_language,
    )
    engine = create_engine(args.database_url, future=True)
    with tempfile.TemporaryDirectory(prefix="golden-") as tmp:
        settings.submission_pages_dir = str(Path(tmp) / "pages")
        settings.submission_originals_dir = str(Path(tmp) / "originals")
        # Italian sheets: the OCR language the tests pin too.
        settings.ocr_language = "ita"
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                session = Session(bind=connection, join_transaction_mode="create_savepoint")
                try:
                    print(observe(session, case_ids))
                finally:
                    session.close()
                    transaction.rollback()  # never a commit
        finally:
            (
                settings.submission_pages_dir,
                settings.submission_originals_dir,
                settings.ocr_language,
            ) = saved
            engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
