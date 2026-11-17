# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every Python file of the backend carries the license header.

The header is the two REUSE lines at the top of the file, before the module
docstring. This test only checks the license line, so that a new file without
it fails here rather than slipping into a release. No DB.
"""
from __future__ import annotations

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
LICENSE_LINE = "# SPDX-License-Identifier: AGPL-3.0-or-later"
# Only the first lines count: the header belongs at the top of the file.
HEADER_LINES = 5


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in BACKEND_DIR.rglob("*.py")
        if not any(
            part.startswith(".") or part in {"__pycache__", "venv"}
            for part in path.relative_to(BACKEND_DIR).parts
        )
    )


def test_the_backend_has_python_files():
    # Guards the test below against passing vacuously on a wrong BACKEND_DIR.
    assert (BACKEND_DIR / "app" / "main.py") in _python_files()


def test_every_python_file_has_the_spdx_license_header():
    missing = []
    for path in _python_files():
        with path.open(encoding="utf-8") as handle:
            head = [handle.readline().rstrip("\n") for _ in range(HEADER_LINES)]
        if LICENSE_LINE not in head:
            missing.append(str(path.relative_to(BACKEND_DIR)))
    assert not missing, f"missing SPDX license header: {missing}"
