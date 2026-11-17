# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The files of a batch results export, from rows already computed.

Pure: no database, no request. ``batch_export_service`` builds the
:class:`SummaryRow` list once, and this module turns it into an .xlsx or a
.csv — so the two formats can never disagree about a value. The Detail export
is the same summary cells, written by the same functions, followed by the
columns per question of :class:`DetailRow` (``export_detail``): a text cell
(an answer, ``*``) or a number (``Dn_pt``), ``None`` for an empty one. Its
.xlsx has a second sheet, "Columns", saying what each column is in each
variant; the .csv is the one table.

Cells:

* English headers, in the order of :data:`SUMMARY_HEADERS`.
* A value that does not exist is an EMPTY cell, never "—" nor 0: a
  percentage with a maximum of 0, points and percentage of a closed submission
  with no saved score, the unreviewed objects of an UNRESOLVABLE one. Numeric
  columns stay numeric, so they sort.
* The time is in UTC: a naive UTC datetime in the .xlsx
  (Excel has no time zones), ISO 8601 with ``Z`` in the .csv.
* The percentage is unrounded in the .xlsx (shown with one decimal), written
  with one decimal in the .csv; points are written as they are, without a
  trailing ``.0`` when whole.

No text cell holds a control character: the .xlsx format
cannot store ``\x00``–``\x08``, ``\x0b``, ``\x0c``, ``\x0e``–``\x1f`` (openpyxl
refuses them and the whole export would fail), and they mean nothing in a
candidate or a name — they come only from text pasted by hand. They are
removed from every text, in both formats, so the two files hold the same
values. Tab, line feed and carriage return stay.

No text cell is ever read as a formula: the candidate may come from
handwriting read by OCR and the batch name is typed by a user. In the .xlsx
every text is an explicit string cell; in the .csv a text starting with
``=``, ``+``, ``-``, ``@``, a tab, a line feed or a carriage return gets a leading
apostrophe (the OWASP rule for CSV injection). Numbers are never touched,
negative ones included.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

from app.schemas.submission import CsvDialect, ExportFormat


@dataclass(frozen=True)
class SummaryRow:
    """One submission of the Summary export, as its cells hold it.

    ``None`` is an empty cell. ``missing_pages`` are template page numbers
    (from 1); ``scanned_at`` is timezone-aware.
    """

    submission: str
    candidate: str | None
    variant: str | None
    points: float | None
    max_points: float | None
    percentage: float | None
    status: str
    missing_pages: tuple[int, ...]
    unreviewed_objects: int | None
    batch: str
    scanned_at: datetime


SUMMARY_HEADERS: tuple[str, ...] = (
    "Submission",
    "Candidate",
    "Variant",
    "Points",
    "Max points",
    "Percentage",
    "Status",
    "Missing pages",
    "Unreviewed objects",
    "Batch",
    "Scanned at (UTC)",
)

SUMMARY_SHEET_TITLE = "Summary"
DETAIL_SHEET_TITLE = "Detail"
LEGEND_SHEET_TITLE = "Columns"
LEGEND_FIRST_HEADER = "Column"


@dataclass(frozen=True)
class DetailRow:
    """One submission of the Detail export: its Summary export cells, then the
    columns per question (a text, a number, or ``None`` for an empty cell)."""

    summary: SummaryRow
    cells: tuple[str | float | None, ...]


@dataclass(frozen=True)
class DetailTable:
    """What the Detail export file holds.

    ``headers`` are the columns per question (after :data:`SUMMARY_HEADERS`);
    ``legend`` the rows of the "Columns" sheet: a column name and, per
    variant of ``legend_variants``, what it is there (``None`` = not in that
    variant).
    """

    headers: tuple[str, ...]
    rows: list[DetailRow]
    legend_variants: tuple[str, ...]
    legend: list[tuple[str, tuple[str | None, ...]]]

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

# The first characters that make a spreadsheet read a cell as a formula.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\n", "\r")

_PERCENT_FORMAT = "0.0"
_DATETIME_FORMAT = "yyyy-mm-dd hh:mm:ss"


def _clean_text(value: str) -> str:
    """*value* without the control characters an .xlsx cannot hold (module
    docstring)."""
    return ILLEGAL_CHARACTERS_RE.sub("", value)


def _missing_pages_text(pages: tuple[int, ...]) -> str | None:
    """"2, 3" — the page numbers the queue shows; empty when complete."""
    return ", ".join(str(page) for page in pages) or None


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _csv_text(value: str | None) -> str:
    if value is None:
        return ""
    value = _clean_text(value)
    if value.startswith(_FORMULA_TRIGGERS):
        return "'" + value
    return value


def _csv_number(value: float | int | None, dialect: CsvDialect, *, one_decimal: bool = False) -> str:
    if value is None:
        return ""
    if one_decimal:
        text = f"{value:.1f}"
    elif float(value).is_integer():
        text = str(int(value))
    else:
        # Enough digits for any score a submission can reach, without the float
        # noise of repr (0.1 + 0.2).
        text = f"{value:.10g}"
    if dialect is CsvDialect.SEMICOLON:
        text = text.replace(".", ",")
    return text


def _csv_datetime(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary_csv_cells(row: SummaryRow, dialect: CsvDialect) -> list[str]:
    return [
        _csv_text(row.submission),
        _csv_text(row.candidate),
        _csv_text(row.variant),
        _csv_number(row.points, dialect),
        _csv_number(row.max_points, dialect),
        _csv_number(row.percentage, dialect, one_decimal=True),
        _csv_text(row.status),
        _csv_text(_missing_pages_text(row.missing_pages)),
        _csv_number(row.unreviewed_objects, dialect),
        _csv_text(row.batch),
        _csv_datetime(row.scanned_at),
    ]


def _csv_cell(value: str | float | None, dialect: CsvDialect) -> str:
    """A column per question: text as text, a number as a number."""
    if isinstance(value, str):
        return _csv_text(value)
    return _csv_number(value, dialect)


def _csv_bytes(header: tuple[str, ...], lines: list[list[str]], dialect: CsvDialect) -> bytes:
    buffer = io.StringIO()
    delimiter = ";" if dialect is CsvDialect.SEMICOLON else ","
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(lines)
    return buffer.getvalue().encode("utf-8-sig")


def write_summary_csv(rows: list[SummaryRow], dialect: CsvDialect) -> bytes:
    """The Summary export as CSV bytes: UTF-8 with a BOM, ``\\r\\n`` line
    ends, ``,`` or ``;`` between fields as *dialect* says."""
    return _csv_bytes(
        SUMMARY_HEADERS, [_summary_csv_cells(row, dialect) for row in rows], dialect
    )


def write_detail_csv(table: DetailTable, dialect: CsvDialect) -> bytes:
    """The Detail export as CSV bytes, as :func:`write_summary_csv` with the
    columns per question after the summary ones. No legend: one table."""
    return _csv_bytes(
        SUMMARY_HEADERS + table.headers,
        [
            _summary_csv_cells(row.summary, dialect)
            + [_csv_cell(value, dialect) for value in row.cells]
            for row in table.rows
        ],
        dialect,
    )


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _xlsx_text(sheet, value: str | None) -> WriteOnlyCell | None:
    if value is None:
        return None
    cell = WriteOnlyCell(sheet, value=_clean_text(value))
    # Set AFTER the value: assigning a text starting with "=" makes openpyxl
    # store a formula, and this puts it back to a plain string.
    cell.data_type = "s"
    return cell


def _xlsx_number(sheet, value: float | int | None, number_format: str | None = None):
    if value is None:
        return None
    cell = WriteOnlyCell(sheet, value=value)
    if number_format is not None:
        cell.number_format = number_format
    return cell


def _xlsx_header(sheet, titles: tuple[str, ...]) -> list[WriteOnlyCell]:
    bold = Font(bold=True)
    header = []
    for title in titles:
        cell = _xlsx_text(sheet, title)
        cell.font = bold
        header.append(cell)
    return header


def _summary_xlsx_cells(sheet, row: SummaryRow) -> list:
    return [
        _xlsx_text(sheet, row.submission),
        _xlsx_text(sheet, row.candidate),
        _xlsx_text(sheet, row.variant),
        _xlsx_number(sheet, row.points),
        _xlsx_number(sheet, row.max_points),
        _xlsx_number(sheet, row.percentage, _PERCENT_FORMAT),
        _xlsx_text(sheet, row.status),
        _xlsx_text(sheet, _missing_pages_text(row.missing_pages)),
        _xlsx_number(sheet, row.unreviewed_objects),
        _xlsx_text(sheet, row.batch),
        _xlsx_number(sheet, _utc(row.scanned_at).replace(tzinfo=None), _DATETIME_FORMAT),
    ]


def _xlsx_cell(sheet, value: str | float | None):
    """A column per question: text as text, a number as a number."""
    if isinstance(value, str):
        return _xlsx_text(sheet, value)
    return _xlsx_number(sheet, value)


def _xlsx_bytes(workbook: Workbook) -> bytes:
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def write_summary_xlsx(rows: list[SummaryRow]) -> bytes:
    """The Summary export as an .xlsx workbook with one sheet, header row in
    bold and frozen."""
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(SUMMARY_SHEET_TITLE)
    sheet.freeze_panes = "A2"
    sheet.append(_xlsx_header(sheet, SUMMARY_HEADERS))
    for row in rows:
        sheet.append(_summary_xlsx_cells(sheet, row))
    return _xlsx_bytes(workbook)


def write_detail_xlsx(table: DetailTable) -> bytes:
    """The Detail export as an .xlsx workbook: the "Detail" sheet (header row
    in bold, header and ``Submission`` column frozen), then the "Columns"
    sheet with the legend, one row per column name and one column per
    variant."""
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(DETAIL_SHEET_TITLE)
    sheet.freeze_panes = "B2"
    sheet.append(_xlsx_header(sheet, SUMMARY_HEADERS + table.headers))
    for row in table.rows:
        sheet.append(
            _summary_xlsx_cells(sheet, row.summary)
            + [_xlsx_cell(sheet, value) for value in row.cells]
        )

    legend = workbook.create_sheet(LEGEND_SHEET_TITLE)
    legend.freeze_panes = "B2"
    legend.append(_xlsx_header(legend, (LEGEND_FIRST_HEADER, *table.legend_variants)))
    for name, described in table.legend:
        legend.append(
            [_xlsx_text(legend, name)] + [_xlsx_text(legend, text) for text in described]
        )
    return _xlsx_bytes(workbook)


# ---------------------------------------------------------------------------
# File name
# ---------------------------------------------------------------------------


def _export_filename(
    prefix: str, batch_name: str, now: datetime, export_format: ExportFormat
) -> str:
    name = re.sub(r"[^\w.-]+", "_", batch_name).strip("._") or "batch"
    stamp = _utc(now).strftime("%Y%m%d-%H%M")
    return f"{prefix}_{name}_{stamp}Z.{export_format.value}"


def summary_filename(batch_name: str, now: datetime, export_format: ExportFormat) -> str:
    """``Summary_<batch name>_<YYYYMMDD-HHMM>Z.<ext>``, the time in UTC.

    The FE proposes the same name and the user may change it; this one is the
    ``Content-Disposition`` for a direct API call. In the batch name every run
    of characters other than letters (accented ones too), digits, ``.``,
    ``_`` and ``-`` becomes one ``_``; ``batch`` if nothing is left.
    """
    return _export_filename("Summary", batch_name, now, export_format)


def detail_filename(batch_name: str, now: datetime, export_format: ExportFormat) -> str:
    """``Detail_<batch name>_<YYYYMMDD-HHMM>Z.<ext>``, as
    :func:`summary_filename`."""
    return _export_filename("Detail", batch_name, now, export_format)


def content_disposition(filename: str) -> str:
    """``attachment`` with an ASCII ``filename`` (accents dropped) and the
    exact name in ``filename*`` (RFC 6266 / 5987)."""
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    ).replace('"', "") or "export"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
