# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the enriched page-objects list.

``page_object_service.list_objects`` returns each object together with its
typed config, so the authoring canvas can render content previews in a
single request (no N+1, no per-object fetch). These service-level tests assert
that — for every object type — the right typed config is populated on the matching
item and that the others stay None.

Reuses the shared user/page fixtures (conftest.py) and the IMAGE suite's temp
storage + PNG helper. Requires TEST_DATABASE_URL — skipped without it.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.core.config import settings
from app.models.enums import (
    CodeType,
    MatrixSelectionRule,
    PageObjectType,
    QuestionType,
)
from app.schemas.matrix_object import (
    MatrixCellDefCreate,
    MatrixColDefCreate,
    MatrixObjectConfigCreate,
    MatrixPageObjectCreate,
    MatrixRowDefCreate,
)
from app.schemas.page_object import (
    CodeObjectConfigCreate,
    CodePageObjectCreate,
    FreeTextAreaConfigCreate,
    FreeTextAreaPageObjectCreate,
    ImagePageObjectCreate,
    TextObjectConfigCreate,
    TextPageObjectCreate,
)
from app.schemas.question_object import (
    QuestionObjectConfigCreate,
    QuestionOptionCreate,
    QuestionPageObjectCreate,
)
from app.services import (
    code_object_service,
    free_text_area_service,
    image_object_service,
    matrix_object_service,
    page_object_service,
    question_object_service,
    text_object_service,
)


@pytest.fixture(autouse=True)
def _image_storage(tmp_path, monkeypatch):
    """Redirect object-image storage to a temp dir for the whole module."""
    monkeypatch.setattr(settings, "object_images_dir", str(tmp_path / "object_images"))
    yield


def _png_bytes(color="red", size=(4, 6)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# A small geometry that keeps each object inside the page area (outside the
# reserved bands) without overlapping checks.
def _geom(i: int) -> dict:
    return {"x": 60 + i * 10, "y": 60, "width": 8, "height": 8}


def _by_type(items):
    """Index the returned list by object_type (one object per type here)."""
    return {item.object_type: item for item in items}


def test_list_objects_exposes_typed_config_per_object(db, page, user):
    # One object of every type on the same page.
    text_object_service.create_text(
        db, page.id,
        TextPageObjectCreate(**_geom(0), config=TextObjectConfigCreate(text="Hello", bold=True)),
        user,
    )
    code_object_service.create_code(
        db, page.id,
        CodePageObjectCreate(
            **_geom(1),
            config=CodeObjectConfigCreate(code_type=CodeType.QR, value="ABC-123"),
        ),
        user,
    )
    free_text_area_service.create_free_text_area(
        db, page.id,
        FreeTextAreaPageObjectCreate(
            **_geom(2), config=FreeTextAreaConfigCreate(placeholder="Write here"),
        ),
        user,
    )
    question_object_service.create_question(
        db, page.id,
        QuestionPageObjectCreate(
            **_geom(3),
            config=QuestionObjectConfigCreate(
                question_type=QuestionType.SINGLE_CHOICE,
                question_text="Capital of France?",
                options=[
                    QuestionOptionCreate(order_index=0, option_text="Paris", is_correct=True),
                    QuestionOptionCreate(order_index=1, option_text="Rome"),
                ],
            ),
        ),
        user,
    )
    matrix_object_service.create_matrix(
        db, page.id,
        MatrixPageObjectCreate(
            **_geom(4),
            config=MatrixObjectConfigCreate(
                rows_count=2, cols_count=2, evaluative=True,
                selection_rule=MatrixSelectionRule.ONE_PER_ROW,
                rows=[MatrixRowDefCreate(order_index=i, label=f"R{i}") for i in range(2)],
                cols=[MatrixColDefCreate(order_index=i, label=f"C{i}") for i in range(2)],
                cells=[MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True)],
            ),
        ),
        user,
    )
    image_read = image_object_service.create_image(
        db, page.id, ImagePageObjectCreate(**_geom(5)), user
    )
    image_object_service.upload_image(db, image_read.id, _png_bytes(), "p.png", user)

    items = page_object_service.list_objects(db, page.id, user)
    assert len(items) == 6
    by_type = _by_type(items)

    # TEXT
    text = by_type[PageObjectType.TEXT]
    assert text.text_config is not None and text.text_config.text == "Hello"
    assert text.text_config.bold is True
    # Only the matching typed config is set.
    assert text.question_config is None and text.image_config is None

    # CODE
    code = by_type[PageObjectType.CODE]
    assert code.code_config is not None
    assert code.code_config.code_type == CodeType.QR
    assert code.code_config.value == "ABC-123"

    # FREE_TEXT_AREA
    free = by_type[PageObjectType.FREE_TEXT_AREA]
    assert free.free_text_config is not None
    assert free.free_text_config.placeholder == "Write here"

    # QUESTION — options come back, correct one flagged.
    question = by_type[PageObjectType.QUESTION]
    assert question.question_config is not None
    assert len(question.question_config.options) == 2
    correct = [o for o in question.question_config.options if o.is_correct]
    assert len(correct) == 1 and correct[0].option_text == "Paris"

    # MATRIX — rows/cols/cells all eager-loaded.
    matrix = by_type[PageObjectType.MATRIX]
    assert matrix.matrix_config is not None
    assert matrix.matrix_config.rows_count == 2 and matrix.matrix_config.cols_count == 2
    assert len(matrix.matrix_config.rows) == 2
    assert len(matrix.matrix_config.cols) == 2
    assert len(matrix.matrix_config.cells) == 1
    assert matrix.matrix_config.cells[0].is_correct is True

    # IMAGE — has_image reflects the uploaded binary.
    image = by_type[PageObjectType.IMAGE]
    assert image.image_config is not None
    assert image.image_config.has_image is True


def test_list_objects_image_without_file_reports_has_image_false(db, page, user):
    image_object_service.create_image(db, page.id, ImagePageObjectCreate(**_geom(0)), user)

    items = page_object_service.list_objects(db, page.id, user)
    assert len(items) == 1
    image = items[0]
    assert image.object_type == PageObjectType.IMAGE
    assert image.image_config is not None
    assert image.image_config.has_image is False
