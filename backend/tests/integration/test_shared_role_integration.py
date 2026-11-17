# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the role of a shared object.

A "show on every page" object is one PageObject row per page, linked by
``shared_group_id``; its role binding lives on exactly ONE of those rows. Every
linked copy carries the group's role, for both roles and every object type,
through ``PageObject.effective_role``, as the PDF export does: reading only the
row's own binding would extract the copies of the VID on pages 2 and 3 as
role-less CODEs. What this suite pins down:

  * **the VID copies** — the copies of a shared VARIANT_IDENTIFIER are never
    evaluable: a run stores no CODE result for any page, and "Review anyway"
    finds nothing unread on them (F17);
  * **wherever the binding is** — the copies are excluded even when the sheet of
    the page holding the binding is missing from the submission;
  * **linked only** — a detached copy does not inherit the role;
  * **every type** — the copies of a shared respondent-ID grid are read as a
    respondent ID,
    the copies of a shared RESPONDENT_IDENTIFIER free text are reviewed as an
    identity (``role`` in the review payload);
  * **the property itself** — own binding first, then the group's;
  * **scoring** — a shared respondent-ID grid flagged evaluative is never
    scored, on any copy, while a detached copy is
    (``scoring_rules.is_scored_matrix``).

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import (
    extraction_orchestrator_service as svc,
    free_text_extraction_service,
    review_anyway_service,
    review_rules,
    scoring_rules,
    submission_review_service,
)


TEMPLATE_W = 794
TEMPLATE_H = 1123
PAGE_COUNT = 3


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    target = tmp_path / "submission_pages"
    target.mkdir()
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    return target


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"shared-role-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Shared role project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


@pytest.fixture
def template_pages(db, project, actor) -> list[FormVariantPage]:
    """A published variant of PAGE_COUNT pages (page_index 1-based)."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="A",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    pages = []
    for index in range(1, PAGE_COUNT + 1):
        page = FormVariantPage(
            form_variant_id=variant.id,
            page_index=index,
            width_px=TEMPLATE_W,
            height_px=TEMPLATE_H,
        )
        db.add(page)
        pages.append(page)
    db.flush()
    return pages


@pytest.fixture
def batch(db, project, actor) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        confidence_threshold=0.3,
    )
    db.add(batch)
    db.flush()
    return batch


def _add_config(db, obj: PageObject) -> None:
    if obj.object_type is PageObjectType.CODE:
        db.add(
            CodeObjectConfig(
                page_object_id=obj.id, code_type=CodeType.QR, value="VARIANT-A"
            )
        )
    elif obj.object_type is PageObjectType.MATRIX:
        db.add(
            MatrixObjectConfig(
                page_object_id=obj.id,
                rows_count=2,
                cols_count=3,
                selection_rule=MatrixSelectionRule.ONE_PER_ROW,
            )
        )
    elif obj.object_type is PageObjectType.FREE_TEXT_AREA:
        db.add(FreeTextAreaConfig(page_object_id=obj.id))


def _add_shared(
    db,
    template_pages: list[FormVariantPage],
    object_type: PageObjectType,
    *,
    role: PageObjectRole,
    bound_on: int = 1,
    detached_on: tuple[int, ...] = (),
) -> dict[int, PageObject]:
    """One linked copy of the same object on every page, the role bound on the
    copy of page *bound_on*; the copies of *detached_on* are detached.

    Returns {page_index: copy}.
    """
    group_id = uuid.uuid4()
    copies: dict[int, PageObject] = {}
    for page in template_pages:
        obj = PageObject(
            form_variant_page_id=page.id,
            object_type=object_type,
            x=500.0,
            y=100.0,
            width=200.0,
            height=200.0,
            shared_group_id=group_id,
            shared_across_pages=page.page_index not in detached_on,
        )
        db.add(obj)
        db.flush()
        _add_config(db, obj)
        copies[page.page_index] = obj
    db.add(
        FormVariantRoleBinding(
            form_variant_id=template_pages[0].form_variant_id,
            page_object_id=copies[bound_on].id,
            role=role,
        )
    )
    db.flush()
    for obj in copies.values():
        db.refresh(obj)
    return copies


def _blank_scan() -> bytes:
    img = Image.new("L", (TEMPLATE_W * 2, TEMPLATE_H * 2), 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_submission(
    db,
    pages_dir,
    batch,
    template_pages: list[FormVariantPage],
    *,
    sheets: tuple[int, ...] = tuple(range(1, PAGE_COUNT + 1)),
    status: SubmissionStatus = SubmissionStatus.MATCHED,
) -> Submission:
    """A submission with one blank sheet bound to each page index in *sheets*."""
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_pages[0].form_variant_id,
        status=status,
    )
    db.add(submission)
    db.flush()
    by_index = {p.page_index: p for p in template_pages}
    for order, index in enumerate(sheets):
        page_id = uuid.uuid4()
        path = pages_dir / f"{page_id}.png"
        path.write_bytes(_blank_scan())
        db.add(
            SubmissionPage(
                id=page_id,
                submission_id=submission.id,
                form_variant_page_id=by_index[index].id,
                source_file_path=str(path),
                source_filename=f"scan-{index}.png",
                source_page_order=order,
            )
        )
    db.flush()
    db.refresh(submission)
    return submission


def _results(db, submission: Submission) -> list[SubmissionObjectResult]:
    page_ids = [p.id for p in submission.pages]
    return list(
        db.scalars(
            select(SubmissionObjectResult).where(
                SubmissionObjectResult.submission_page_id.in_(page_ids)
            )
        ).all()
    )


def _fresh(db, *objs: PageObject) -> None:
    """Expire *objs*, so their relationships are read from the database as a
    new request would, not from what the builder left in the session."""
    for obj in objs:
        db.expire(obj)


# ---------------------------------------------------------------------------
# The copies of a shared VARIANT_IDENTIFIER
# ---------------------------------------------------------------------------


def test_no_copy_of_a_shared_variant_identifier_is_extracted(
    db, pages_dir, batch, template_pages
):
    """The core case: the VID is replicated on every page, the binding sits on
    the copy of page 1, and none of the three copies may be read as a CODE."""
    copies = _add_shared(
        db, template_pages, PageObjectType.CODE,
        role=PageObjectRole.VARIANT_IDENTIFIER,
    )
    submission = _make_submission(db, pages_dir, batch, template_pages)

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert _results(db, submission) == []
    assert summary.results_created == 0
    assert summary.skipped_not_applicable == PAGE_COUNT
    assert summary.errors == 0
    for copy in copies.values():
        assert svc.is_extractable(copy) is False


def test_review_anyway_finds_nothing_unread_on_the_copies(
    db, pages_dir, batch, template_pages
):
    """F17: a covered VID on page 2 must not become a placeholder in review."""
    _add_shared(
        db, template_pages, PageObjectType.CODE,
        role=PageObjectRole.VARIANT_IDENTIFIER,
    )
    submission = _make_submission(db, pages_dir, batch, template_pages)

    assert review_anyway_service.unread_objects_for_submission(db, submission) == []


def test_the_copies_are_excluded_without_the_sheet_holding_the_binding(
    db, pages_dir, batch, template_pages
):
    """The sheet of page 1 (the one holding the binding) is missing: the walk
    never loads that copy, and the others must still know their role."""
    _add_shared(
        db, template_pages, PageObjectType.CODE,
        role=PageObjectRole.VARIANT_IDENTIFIER,
    )
    submission = _make_submission(
        db, pages_dir, batch, template_pages, sheets=(2, 3)
    )
    db.expire_all()

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert _results(db, submission) == []
    assert summary.skipped_not_applicable == 2


def test_the_binding_may_sit_on_any_copy(db, pages_dir, batch, template_pages):
    copies = _add_shared(
        db, template_pages, PageObjectType.CODE,
        role=PageObjectRole.VARIANT_IDENTIFIER, bound_on=3,
    )
    _fresh(db, *copies.values())

    assert all(
        c.effective_role is PageObjectRole.VARIANT_IDENTIFIER
        for c in copies.values()
    )
    assert copies[1].role is None


def test_a_detached_copy_does_not_inherit_the_role(
    db, pages_dir, batch, template_pages, monkeypatch
):
    """A detached copy remembers its origin group but is no longer that object:
    it is a role-less CODE again, read by the CODE extractor."""
    calls: list[uuid.UUID] = []
    monkeypatch.setitem(
        svc._EXTRACTORS,
        PageObjectType.CODE,
        lambda db_, page, obj, batch_: calls.append(obj.id),
    )
    copies = _add_shared(
        db, template_pages, PageObjectType.CODE,
        role=PageObjectRole.RESPONDENT_IDENTIFIER, detached_on=(3,),
    )
    _make_submission(db, pages_dir, batch, template_pages)
    _fresh(db, *copies.values())

    svc.run_extraction_for_batch(db, batch.id)

    assert copies[2].effective_role is PageObjectRole.RESPONDENT_IDENTIFIER
    assert copies[3].effective_role is None
    assert calls == [copies[3].id]


# ---------------------------------------------------------------------------
# Every type
# ---------------------------------------------------------------------------


def test_every_copy_of_a_shared_respondent_id_is_read_as_a_respondent_id(
    db, pages_dir, batch, template_pages
):
    copies = _add_shared(
        db, template_pages, PageObjectType.MATRIX,
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
    )
    submission = _make_submission(db, pages_dir, batch, template_pages)

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.errors == 0
    results = _results(db, submission)
    assert {r.page_object_id for r in results} == {c.id for c in copies.values()}
    assert {r.result_type for r in results} == {
        SubmissionResultType.RESPONDENT_IDENTIFIER
    }
    for copy in copies.values():
        assert svc.result_type_for(copy) is SubmissionResultType.RESPONDENT_IDENTIFIER


def test_the_candidate_comes_from_the_first_copy_in_physical_order(
    db, pages_dir, batch, template_pages, monkeypatch
):
    """Each copy of a shared respondent-ID grid is a respondent-ID reading,
    and the candidate comes from the first one in physical order only. The
    readings are stubbed with a different clean value per page; the sheets are
    scanned 2, 1, 3, so the first one is page 2's."""
    by_page_id = {p.id: p.page_index for p in template_pages}

    def fake_reading(db_, page, obj, batch_):
        index = by_page_id[page.form_variant_page_id]
        db_.add(
            SubmissionObjectResult(
                submission_page_id=page.id,
                page_object_id=obj.id,
                result_type=SubmissionResultType.RESPONDENT_IDENTIFIER,
                detected_payload_json={
                    "raw_value": f"{index}0000{index}",
                    "digits": [],
                    "ambiguous_columns": [],
                },
                confidence_score=1.0,
                validity_forces_review=False,
                review_required=False,
            )
        )

    monkeypatch.setattr(
        svc.respondent_identifier_service,
        "extract_respondent_identifier_result",
        fake_reading,
    )
    _add_shared(
        db, template_pages, PageObjectType.MATRIX,
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
    )
    submission = _make_submission(
        db, pages_dir, batch, template_pages, sheets=(2, 1, 3)
    )

    summary = svc.run_extraction_for_batch(db, batch.id)
    db.flush()

    assert summary.results_created == PAGE_COUNT
    assert summary.candidates_set == 1
    assert submission.respondent_identifier == "200002"


def test_every_copy_of_a_shared_identifier_free_text_is_reviewed_as_an_identity(
    db, pages_dir, actor, batch, template_pages, monkeypatch
):
    monkeypatch.setattr(
        free_text_extraction_service, "_run_ocr", lambda crop: ("123456", 0.9)
    )
    copies = _add_shared(
        db, template_pages, PageObjectType.FREE_TEXT_AREA,
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
    )
    submission = _make_submission(db, pages_dir, batch, template_pages)
    svc.run_extraction_for_batch(db, batch.id)
    db.flush()
    db.expire_all()

    detail = submission_review_service.get_review_detail(db, submission.id, actor)

    reviewed = [o for p in detail.pages for o in p.objects]
    assert {o.page_object_id for o in reviewed} == {c.id for c in copies.values()}
    assert {o.role for o in reviewed} == {PageObjectRole.RESPONDENT_IDENTIFIER}
    for result in _results(db, submission):
        assert review_rules.holds_respondent_identifier(result, result.page_object)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_no_copy_of_a_shared_evaluative_respondent_id_is_scored(db, template_pages):
    """The RESPONDENT_IDENTIFIER role takes a MATRIX out of scoring even when
    it is flagged evaluative, and the role of a linked copy is the group's —
    only the detached copy, no longer a respondent-ID grid, counts."""
    copies = _add_shared(
        db,
        template_pages,
        PageObjectType.MATRIX,
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
        bound_on=2,
        detached_on=(3,),
    )
    for obj in copies.values():
        obj.matrix_config.evaluative = True
    db.flush()
    _fresh(db, *copies.values())

    assert {
        index: scoring_rules.is_scored_matrix(obj) for index, obj in copies.items()
    } == {1: False, 2: False, 3: True}


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def test_an_object_outside_any_group_has_only_its_own_role(
    db, template_pages
):
    plain = PageObject(
        form_variant_page_id=template_pages[0].id,
        object_type=PageObjectType.CODE,
        x=0.0, y=0.0, width=10.0, height=10.0,
    )
    db.add(plain)
    db.flush()
    _fresh(db, plain)

    assert plain.effective_role is None


def test_a_linked_group_with_no_binding_has_no_role(db, template_pages):
    group_id = uuid.uuid4()
    copies = []
    for page in template_pages:
        obj = PageObject(
            form_variant_page_id=page.id,
            object_type=PageObjectType.CODE,
            x=0.0, y=0.0, width=10.0, height=10.0,
            shared_group_id=group_id,
            shared_across_pages=True,
        )
        db.add(obj)
        copies.append(obj)
    db.flush()
    _fresh(db, *copies)

    assert [c.effective_role for c in copies] == [None] * PAGE_COUNT


def test_two_roles_in_one_group_own_binding_first_then_the_lowest_page(
    db, template_pages
):
    """Pathological: two copies of one group holding different roles.
    A copy holding a binding keeps its own; one without takes the role of the
    copy on the lowest page."""
    copies = _add_shared(
        db, template_pages, PageObjectType.CODE,
        role=PageObjectRole.RESPONDENT_IDENTIFIER, bound_on=3,
    )
    db.add(
        FormVariantRoleBinding(
            form_variant_id=template_pages[0].form_variant_id,
            page_object_id=copies[2].id,
            role=PageObjectRole.VARIANT_IDENTIFIER,
        )
    )
    db.flush()
    _fresh(db, *copies.values())

    assert copies[3].effective_role is PageObjectRole.RESPONDENT_IDENTIFIER
    assert copies[2].effective_role is PageObjectRole.VARIANT_IDENTIFIER
    assert copies[1].effective_role is PageObjectRole.VARIANT_IDENTIFIER
