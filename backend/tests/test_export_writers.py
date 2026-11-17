# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The files of the batch export: ``export_writers``, no DB.

CSV in both dialects (separator, BOM, decimals, empty cells, formula
neutralisation, control characters removed), the .xlsx read back with openpyxl
(cell types, headers, UTC time, a ``=…`` text kept as a string), the file name,
and ``scoring_rules.percentage``. The Detail export: the summary cells
unchanged, then the columns per question (text, numbers, the ``*`` symbol,
empty cells), the "Columns" legend sheet and the ``Detail_`` file name.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

from app.schemas.submission import CsvDialect, ExportFormat
from app.services import scoring_rules
from app.services.export_writers import (
    SUMMARY_HEADERS,
    DetailRow,
    DetailTable,
    SummaryRow,
    content_disposition,
    detail_filename,
    summary_filename,
    write_detail_csv,
    write_detail_xlsx,
    write_summary_csv,
    write_summary_xlsx,
)

_ROME = timezone(timedelta(hours=1))


def _row(**changes) -> SummaryRow:
    values = dict(
        submission="abcd…wxyz",
        candidate="S12345",
        variant="A",
        points=12.0,
        max_points=18.0,
        percentage=12 / 18 * 100,
        status="REVIEWED",
        missing_pages=(),
        unreviewed_objects=0,
        batch="Mid-term",
        # 10:30 in Rome is 09:30 UTC.
        scanned_at=datetime(2026, 11, 3, 10, 30, 15, tzinfo=_ROME),
    )
    values.update(changes)
    return SummaryRow(**values)


def _csv(rows, dialect=CsvDialect.COMMA) -> list[list[str]]:
    data = write_summary_csv(rows, dialect)
    assert data.startswith("﻿".encode("utf-8"))
    delimiter = ";" if dialect is CsvDialect.SEMICOLON else ","
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=delimiter))


def _xlsx(rows):
    return load_workbook(io.BytesIO(write_summary_xlsx(rows))).active


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_the_comma_csv_has_the_headers_and_dot_decimals():
    header, line = _csv([_row(points=12.5)])

    assert tuple(header) == SUMMARY_HEADERS
    assert line == [
        "abcd…wxyz",
        "S12345",
        "A",
        "12.5",
        "18",
        "66.7",
        "REVIEWED",
        "",
        "0",
        "Mid-term",
        "2026-11-03T09:30:15Z",
    ]


def test_the_semicolon_csv_has_decimal_commas():
    _header, line = _csv([_row(points=12.5, missing_pages=(2, 3))], CsvDialect.SEMICOLON)

    assert line[3:9] == ["12,5", "18", "66,7", "REVIEWED", "2, 3", "0"]


def test_lines_end_with_crlf():
    assert b"\r\n" in write_summary_csv([_row()], CsvDialect.COMMA)


def test_missing_values_are_empty_cells_not_zero():
    _header, line = _csv(
        [
            _row(
                candidate=None,
                variant=None,
                points=None,
                max_points=None,
                percentage=None,
                status="UNRESOLVABLE",
                unreviewed_objects=None,
            )
        ]
    )

    assert line[1:9] == ["", "", "", "", "", "UNRESOLVABLE", "", ""]


def test_negative_numbers_stay_numbers():
    _header, line = _csv([_row(points=-2.0, percentage=-11.11)])

    assert line[3] == "-2"
    assert line[5] == "-11.1"


@pytest.mark.parametrize("dangerous", ["=1+1", "+39", "-5", "@SUM(A1)", "\tx", "\nx", "\rx"])
def test_a_text_that_would_be_a_formula_gets_an_apostrophe(dangerous):
    _header, line = _csv([_row(candidate=dangerous, batch=dangerous)])

    assert line[1] == "'" + dangerous
    assert line[9] == "'" + dangerous


def test_control_characters_are_removed_from_the_csv():
    _header, line = _csv([_row(candidate="Ma\x0brio\x00", batch="\x1fMid-term")])

    assert line[1] == "Mario"
    assert line[9] == "Mid-term"


def test_a_control_character_before_a_formula_does_not_hide_it():
    _header, line = _csv([_row(candidate="\x0b=1+1")])

    assert line[1] == "'=1+1"


def test_an_ordinary_text_is_untouched():
    _header, line = _csv([_row(candidate="Rossi, Mario")])

    assert line[1] == "Rossi, Mario"


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def test_the_xlsx_has_typed_cells():
    sheet = _xlsx([_row(missing_pages=(2,))])

    assert sheet.title == "Summary"
    assert tuple(cell.value for cell in sheet[1]) == SUMMARY_HEADERS
    assert sheet[1][0].font.bold
    assert sheet.freeze_panes == "A2"
    cells = sheet[2]
    assert [cell.value for cell in cells[:3]] == ["abcd…wxyz", "S12345", "A"]
    assert (cells[3].value, cells[4].value) == (12, 18)
    assert cells[5].value == pytest.approx(66.6666, rel=1e-4)
    assert cells[5].number_format == "0.0"
    assert cells[7].value == "2"
    assert cells[8].value == 0
    assert cells[10].value == datetime(2026, 11, 3, 9, 30, 15)


def test_the_xlsx_leaves_missing_values_empty():
    cells = _xlsx([_row(points=None, percentage=None, unreviewed_objects=None)])[2]

    assert (cells[3].value, cells[5].value, cells[8].value) == (None, None, None)


def test_control_characters_are_removed_from_the_xlsx():
    # openpyxl refuses them: without the cleaning the whole export would fail.
    cells = _xlsx([_row(candidate="Ma\x0brio", variant="A\x00", batch="Mid\x1f-term")])[2]

    assert (cells[1].value, cells[2].value, cells[9].value) == ("Mario", "A", "Mid-term")


def test_the_xlsx_never_writes_a_formula():
    cells = _xlsx([_row(candidate="=HYPERLINK(\"x\")", batch="+1")])[2]

    assert cells[1].value == '=HYPERLINK("x")'
    assert cells[1].data_type == "s"
    assert cells[9].data_type == "s"


# ---------------------------------------------------------------------------
# File name
# ---------------------------------------------------------------------------


def test_the_file_name_has_the_batch_and_the_utc_time():
    now = datetime(2026, 11, 3, 15, 30, tzinfo=_ROME)

    assert summary_filename("Mid-term exam", now, ExportFormat.CSV) == (
        "Summary_Mid-term_exam_20261103-1430Z.csv"
    )
    assert summary_filename("Mid-term exam", now, ExportFormat.XLSX).endswith("Z.xlsx")


def test_the_file_name_cleans_the_batch_name():
    now = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)

    assert summary_filename("Teacher's exam / 3ª", now, ExportFormat.CSV) == (
        "Summary_Teacher_s_exam_3ª_20261103-1430Z.csv"
    )
    assert summary_filename("///", now, ExportFormat.CSV) == (
        "Summary_batch_20261103-1430Z.csv"
    )


def test_the_content_disposition_carries_an_ascii_and_a_utf8_name():
    header = content_disposition("Summary_Città_20261103-1430Z.csv")

    assert header == (
        'attachment; filename="Summary_Citta_20261103-1430Z.csv"; '
        "filename*=UTF-8''Summary_Citt%C3%A0_20261103-1430Z.csv"
    )


# ---------------------------------------------------------------------------
# Detail export
# ---------------------------------------------------------------------------


def _table(*rows: DetailRow) -> DetailTable:
    return DetailTable(
        headers=("D1", "D1_pt", "T1"),
        rows=list(rows),
        legend_variants=("A", "B"),
        legend=[
            ("D1", ("p. 1 · Capital?", "p. 1 · Largest?")),
            ("T1", ("p. 2 · Comment", None)),
        ],
    )


_DETAIL = DetailRow(summary=_row(), cells=("A, C", 1.5, "=SUM(A1)"))
_EMPTY = DetailRow(summary=_row(status="UNRESOLVABLE"), cells=(None, None, None))


def test_the_detail_csv_has_the_summary_then_the_columns_per_question():
    data = write_detail_csv(_table(_DETAIL, _EMPTY), CsvDialect.COMMA)
    header, full, empty = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))

    assert tuple(header) == SUMMARY_HEADERS + ("D1", "D1_pt", "T1")
    assert full[: len(SUMMARY_HEADERS)] == _csv([_row()])[1]
    assert full[len(SUMMARY_HEADERS):] == ["A, C", "1.5", "'=SUM(A1)"]
    assert empty[len(SUMMARY_HEADERS):] == ["", "", ""]


def test_the_detail_csv_symbol_and_semicolon_decimals():
    row = DetailRow(summary=_row(), cells=("*", -0.5, "*"))
    data = write_detail_csv(_table(row), CsvDialect.SEMICOLON)
    _header, line = list(
        csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";")
    )

    assert line[len(SUMMARY_HEADERS):] == ["*", "-0,5", "*"]


def _detail_book(*rows: DetailRow):
    return load_workbook(io.BytesIO(write_detail_xlsx(_table(*rows))))


def test_the_detail_xlsx_has_typed_cells_and_a_frozen_first_column():
    sheet = _detail_book(_DETAIL, _EMPTY)["Detail"]
    width = len(SUMMARY_HEADERS)

    assert sheet.freeze_panes == "B2"
    assert tuple(cell.value for cell in sheet[1]) == SUMMARY_HEADERS + ("D1", "D1_pt", "T1")
    assert [cell.value for cell in sheet[2][:width]] == [
        cell.value for cell in _xlsx([_row()])[2]
    ]
    full = sheet[2][width:]
    assert [cell.value for cell in full] == ["A, C", 1.5, "=SUM(A1)"]
    assert full[2].data_type == "s"
    assert [cell.value for cell in sheet[3][width:]] == [None, None, None]


def test_the_detail_xlsx_has_the_columns_legend():
    book = _detail_book(_DETAIL)

    assert book.sheetnames == ["Detail", "Columns"]
    legend = book["Columns"]
    assert [[cell.value for cell in row] for row in legend.iter_rows()] == [
        ["Column", "A", "B"],
        ["D1", "p. 1 · Capital?", "p. 1 · Largest?"],
        ["T1", "p. 2 · Comment", None],
    ]


def test_the_detail_file_name():
    now = datetime(2026, 11, 3, 15, 30, tzinfo=_ROME)

    assert detail_filename("Mid-term exam", now, ExportFormat.XLSX) == (
        "Detail_Mid-term_exam_20261103-1430Z.xlsx"
    )


# ---------------------------------------------------------------------------
# Percentage
# ---------------------------------------------------------------------------


def test_the_percentage_formula():
    assert scoring_rules.percentage(12.0, 18.0) == pytest.approx(66.6667, rel=1e-4)
    assert scoring_rules.percentage(-2.0, 18.0) == pytest.approx(-11.111, rel=1e-4)
    assert scoring_rules.percentage(0.0, 0.0) is None
