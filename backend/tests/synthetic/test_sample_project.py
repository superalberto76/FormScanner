# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sample project of the user guide's screenshots.

Chapter 3 of the guide shows these four sheets in review: each must give the
readings, the candidate and the score the guide describes. The expected
values come from the declarations of ``sample_project`` (which answers are
right, which mark is light, which column holds two digits), never from a
run. Marked ``synthetic``::

    pytest -m synthetic tests/synthetic/test_sample_project.py
"""
from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

from app.models.enums import FormProjectStatus, FormProjectType
from app.schemas.form_project import FormProjectCreate
from app.schemas.form_variant import FormVariantCreate
from app.repositories import form_variant_page_repo, page_object_repo
from app.services import form_project_service, form_variant_service
from tests.synthetic import sample_project as sample
from tests.synthetic.builders import _create_object
from tests.synthetic.generator import SheetOptions, generate
from tests.synthetic.runner import run_pdf
from tests.synthetic.scenarios import (
    ALWAYS_REVIEW,
    AMBIGUOUS_DIGITS,
    INVALID_ANSWER,
    LOW_CONFIDENCE,
)

pytestmark = [
    pytest.mark.synthetic,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Tesseract not installed: the free-text area is read by OCR",
    ),
]

NAME = {sample.NAME_LABEL: frozenset({ALWAYS_REVIEW})}

# stem → (candidate, readings in review with their reasons, points)
EXPECTED = {
    "jane-doe": ("20451", NAME, 7.0),
    "john-smith": (
        "20452",
        {
            **NAME,
            "Question 2": frozenset({LOW_CONFIDENCE}),
            "Question 4": frozenset({INVALID_ANSWER}),
        },
        # 1 + 0 (in review) + 1 (one prime of two) + 0 (in review) + 1 + 0
        3.0,
    ),
    "erika-example": (
        None,  # the student number waits for the reviewer
        {**NAME, sample.STUDENT_NUMBER_LABEL: frozenset({AMBIGUOUS_DIGITS})},
        6.0,
    ),
    "max-sample": ("20454", NAME, 5.0),  # questions 2 and 3 partly wrong
}


def _build(db):
    """The sample project as the walkthrough leaves it: created, one question
    drawn somewhere else, filled, published and activated."""
    users = sample.create_accounts(db)
    owner = users[sample.ADMIN.email]
    colleague = users[sample.COLLEAGUE.email]
    project = form_project_service.create_project(
        db, owner, FormProjectCreate(type=FormProjectType.EXAM, name=sample.PROJECT_NAME)
    )
    form_project_service.add_collaborator(db, project.id, colleague.id, owner)
    variant = form_variant_service.create_variant(
        db, project.id, FormVariantCreate(name=sample.VARIANT_NAME), owner
    )
    page_id = form_variant_page_repo.list_for_variant(db, variant.id)[0].id
    # The question drawn in the browser, somewhere else on the page.
    _create_object(db, page_id, replace(sample.FIRST_QUESTION, x=200, y=700), owner)

    sample.fill_variant(db, variant, owner)
    form_variant_service.publish_variant(db, variant.id, owner)
    form_project_service.transition_project(db, project.id, FormProjectStatus.ACTIVE, owner)
    return sample.sample_project(db, project, variant, owner, colleague), page_id


def test_fill_replaces_the_page_with_the_spec(db):
    _, page_id = _build(db)
    objects = page_object_repo.list_for_page_with_configs(db, page_id)
    assert sorted(o.label for o in objects) == sorted(o.label for o in sample.SAMPLE_VARIANT.objects)
    first = next(o for o in objects if o.label == sample.FIRST_QUESTION.label)
    assert (first.x, first.y) == (sample.FIRST_QUESTION.x, sample.FIRST_QUESTION.y)


@pytest.mark.parametrize("sheet", sample.SAMPLE_SHEETS, ids=lambda s: s.stem)
def test_sheet_reads_as_the_guide_shows(db, scan_storage, sheet):
    project, _ = _build(db)
    candidate, review, points = EXPECTED[sheet.stem]
    pdf = generate(
        db, project, sample.VARIANT_KEY, sheet.fill, SheetOptions(seed=sheet.seed)
    ).to_pdf()

    outcome = run_pdf(db, project, sheet.stem, pdf)

    detail = outcome.detail
    assert detail.status.value == "SCANNED"
    assert detail.respondent_identifier == candidate
    listed = {o.label: frozenset(o.flag_reasons) for p in detail.pages for o in p.objects}
    assert listed == review
    assert detail.score is not None
    assert (detail.score.raw_score, detail.score.max_possible_score) == (points, sample.MAX_SCORE)


def test_cli_refuses_a_database_not_ending_in_e2e(capsys):
    assert sample.main(["fill", "--database-url", "postgresql+psycopg://u:p@h/formscanner_test"]) == 1
    assert "_e2e" in capsys.readouterr().err
