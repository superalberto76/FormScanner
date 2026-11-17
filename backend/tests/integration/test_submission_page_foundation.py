# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed structural tests for the Submission (one candidate's whole set of
pages) and the SubmissionPage entity.

A Submission holds everything one candidate handed in (1 candidate, 1
FormVariant, N physical pages); each physical page is a ``SubmissionPage``,
optionally linked to the template ``FormVariantPage`` once matching resolves
it. Per-object detection results (``SubmissionObjectResult``) hang off a
SubmissionPage, not the whole Submission.

These are pure data-structure tests against a real Postgres (the shared ``db``
fixture from conftest.py), without going through the API or service layer.
They assert the FK graph, the ``pages`` / ``object_results`` relationships,
and the computed ``missing_page_indices`` property. Skipped without
TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

from app.models.enums import (
    FormProjectType,
    FormVariantStatus,
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
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _make_user(db) -> User:
    u = User(
        email=f"subpage-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(db, owner) -> FormProject:
    project = FormProject(
        owner_id=owner.id, type=FormProjectType.EXAM, name="Submission page project"
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_variant(db, project, owner, *, page_count: int) -> FormVariant:
    """A PUBLISHED variant with ``page_count`` template pages (index 0..n-1)."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    for idx in range(page_count):
        db.add(FormVariantPage(form_variant_id=variant.id, page_index=idx))
    db.flush()
    return variant


def _make_batch(db, project, owner) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id, name="batch", created_by_user_id=owner.id
    )
    db.add(batch)
    db.flush()
    return batch


def _make_submission(db, batch, *, matched_variant=None) -> Submission:
    sub = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=matched_variant.id if matched_variant else None,
        status=SubmissionStatus.UPLOADED,
    )
    db.add(sub)
    db.flush()
    return sub


def _make_page(
    db, submission, *, template_page=None, filename="p.png", source_page_order=0
) -> SubmissionPage:
    """A physical page. ``source_page_order`` (its 0-based position in the
    uploaded PDF) is NOT NULL, so every builder must supply it;
    ``page_order_source`` defaults to PDF_ORDER on the model."""
    page = SubmissionPage(
        submission_id=submission.id,
        form_variant_page_id=template_page.id if template_page else None,
        source_file_path=f"/scans/{filename}",
        source_filename=filename,
        source_page_order=source_page_order,
    )
    db.add(page)
    db.flush()
    return page


# ---------------------------------------------------------------------------
# A submission owns its physical pages via Submission.pages
# ---------------------------------------------------------------------------
def test_submission_owns_physical_pages(db):
    user = _make_user(db)
    project = _make_project(db, user)
    variant = _make_variant(db, project, user, page_count=3)
    batch = _make_batch(db, project, user)
    submission = _make_submission(db, batch, matched_variant=variant)

    template_pages = sorted(variant.pages, key=lambda p: p.page_index)
    matched = _make_page(db, submission, template_page=template_pages[0], source_page_order=0)
    unmatched = _make_page(db, submission, template_page=None, source_page_order=1)

    db.refresh(submission)
    assert {p.id for p in submission.pages} == {matched.id, unmatched.id}
    # The source-file fields live on the physical page, not the submission.
    assert not hasattr(submission, "source_file_path")
    assert matched.source_filename == "p.png"
    assert matched.form_variant_page_id == template_pages[0].id
    assert unmatched.form_variant_page_id is None


# ---------------------------------------------------------------------------
# missing_page_indices — template pages not yet present as SubmissionPage
# ---------------------------------------------------------------------------
def test_missing_page_indices_reports_absent_template_pages(db):
    user = _make_user(db)
    project = _make_project(db, user)
    variant = _make_variant(db, project, user, page_count=3)  # indices 0,1,2
    batch = _make_batch(db, project, user)
    submission = _make_submission(db, batch, matched_variant=variant)

    template_pages = sorted(variant.pages, key=lambda p: p.page_index)
    # Present: template index 0 (matched) + one unmatched page (contributes no
    # index). Expected {0,1,2} minus present {0} -> [1, 2].
    _make_page(db, submission, template_page=template_pages[0], source_page_order=0)
    _make_page(db, submission, template_page=None, source_page_order=1)

    db.refresh(submission)
    assert submission.missing_page_indices == [1, 2]


def test_missing_page_indices_empty_when_all_present(db):
    user = _make_user(db)
    project = _make_project(db, user)
    variant = _make_variant(db, project, user, page_count=2)
    batch = _make_batch(db, project, user)
    submission = _make_submission(db, batch, matched_variant=variant)

    for order, tpl in enumerate(sorted(variant.pages, key=lambda p: p.page_index)):
        _make_page(db, submission, template_page=tpl, source_page_order=order)

    db.refresh(submission)
    assert submission.missing_page_indices == []


def test_missing_page_indices_empty_when_not_matched(db):
    """No matched variant -> no template to compare against -> [] (not None)."""
    user = _make_user(db)
    project = _make_project(db, user)
    batch = _make_batch(db, project, user)
    submission = _make_submission(db, batch, matched_variant=None)
    _make_page(db, submission, template_page=None)

    db.refresh(submission)
    assert submission.missing_page_indices == []


# ---------------------------------------------------------------------------
# SubmissionObjectResult binds to a SubmissionPage, not a Submission
# ---------------------------------------------------------------------------
def test_object_result_binds_to_submission_page(db):
    user = _make_user(db)
    project = _make_project(db, user)
    variant = _make_variant(db, project, user, page_count=1)
    batch = _make_batch(db, project, user)
    submission = _make_submission(db, batch, matched_variant=variant)

    template_page = sorted(variant.pages, key=lambda p: p.page_index)[0]
    sub_page = _make_page(db, submission, template_page=template_page)

    page_object = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.QUESTION,
        x=0, y=0, width=10, height=10, label="q1",
    )
    db.add(page_object)
    db.flush()

    result = SubmissionObjectResult(
        submission_page_id=sub_page.id,
        page_object_id=page_object.id,
        result_type=SubmissionResultType.QUESTION,
    )
    db.add(result)
    db.flush()

    db.refresh(sub_page)
    # The result hangs off the physical page...
    assert result.submission_page_id == sub_page.id
    assert result.submission_page is sub_page
    assert result.id in {r.id for r in sub_page.object_results}
    # ...and not directly off the submission.
    assert not hasattr(result, "submission_id")
    assert not hasattr(result, "submission")
    assert not hasattr(submission, "object_results")
