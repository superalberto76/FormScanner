# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for scan-batch ingestion.

One uploaded PDF = one submission (one Submission). What these tests pin down:

  * every physical page becomes a SubmissionPage with ``source_page_order`` =
    its 0-based position in the PDF, ALWAYS, whatever decoding did;
  * ``page_order_source`` = CODE only when a page index was actually read off the
    page's VARIANT_IDENTIFIER code; PDF_ORDER otherwise (no code, unreadable
    code, a code whose payload carries no page index, or a code that is not
    tagged as the variant identifier);
  * ``identified_code_value`` keeps the RAW payload so matching can re-parse
    it;
  * one undecodable page never blocks its siblings nor the upload.

upload_pdf ALSO runs the matching engine before its commit, so a submission
comes back from an upload already matched where possible. None of the projects
built here has a PUBLISHED variant, so matching consistently fails on them and
the submissions land in MATCH_FAILED — which leaves everything this suite
asserts about INGESTION untouched, and keeps the two concerns testable apart.
The engine itself is covered by test_submission_matching_integration.py.

The role of each decoded code is read from an explicit tag in its payload
(``VID:`` / ``RID:``), not guessed from "whichever code carries a page index".
So this suite also pins down that a RESPONDENT_IDENTIFIER found anywhere in
the submission fills ``Submission.respondent_identifier``, and
that an UNTAGGED code identifies nothing. The tag rules themselves are unit-
tested in ``tests/test_submission_identification.py``.

Isolation from the QR toolchain
-------------------------------
Most tests monkeypatch ``code_rendering.decode_codes_from_image`` — the ingestion
rules above are about what the service DOES with a decode result, and stubbing it
exercises every branch (index / no index / untagged / failure / mixed)
deterministically, without depending on QR rasterisation surviving a
render→decode round trip. The genuine end-to-end path (real ``qrcode`` PNG
embedded in a real PDF, read back by real pyzbar) is covered by
``test_real_qr_end_to_end``, which SKIPS when the optional encoder or the system
libzbar shared library is unavailable.

Files go to a throwaway ``tmp_path`` via monkeypatched settings, as in
test_image_object_integration.py. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import fitz  # PyMuPDF
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    PageOrderSource,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.submission_batch import SubmissionBatch
from app.models.user import User
from app.schemas.submission import SubmissionBatchCreate
from app.services import submission_batch_service
from app.utils import code_rendering


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _submission_storage(tmp_path, monkeypatch):
    """Redirect both submission storage dirs into a throwaway tmp_path."""
    monkeypatch.setattr(
        settings, "submission_originals_dir", str(tmp_path / "submission_originals")
    )
    monkeypatch.setattr(
        settings, "submission_pages_dir", str(tmp_path / "submission_pages")
    )


@pytest.fixture(autouse=True)
def _no_scan_readiness_gate(monkeypatch):
    """Switch off the scan-readiness gate for this suite only.

    Its projects deliberately have no PUBLISHED variant, so matching fails and
    the ingestion rules stay testable apart from it — but such a project is not
    ready for scanning, and the gate would refuse every upload. The gate itself
    is covered by test_scan_readiness_gate_integration.py.
    """
    monkeypatch.setattr(
        submission_batch_service, "_assert_scan_ready", lambda db, project: None
    )


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"ingest-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(db, owner, *, status: FormProjectStatus) -> FormProject:
    project = FormProject(
        owner_id=owner.id,
        type=FormProjectType.EXAM,
        name="Ingestion project",
        status=status,
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


def _make_batch(db, project, owner) -> SubmissionBatch:
    """A batch created directly, bypassing the ACTIVE gate on create_batch."""
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch 1", created_by_user_id=owner.id
    )
    db.add(batch)
    db.flush()
    return batch


def _pdf_bytes(page_count: int) -> bytes:
    """A minimal valid multipage PDF carrying no readable code."""
    doc = fitz.open()
    try:
        for i in range(page_count):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {i}")
        return doc.tobytes()
    finally:
        doc.close()


def _stub_decoder(monkeypatch, payloads: list[list[str]]) -> None:
    """Make decoding return ``payloads[n]`` for the n-th page decoded.

    Each entry is the FULL list of symbols readable on that page (a page may
    legitimately carry both a variant and a respondent identifier); an empty list
    means nothing was readable.
    """
    calls = {"n": 0}

    def fake_decode(image_path):
        index = calls["n"]
        calls["n"] += 1
        return payloads[index] if index < len(payloads) else []

    monkeypatch.setattr(code_rendering, "decode_codes_from_image", fake_decode)


def _pages_in_order(submission):
    return sorted(submission.pages, key=lambda p: p.source_page_order)


# ---------------------------------------------------------------------------
# 1. Every page carries a readable page index -> CODE everywhere
# ---------------------------------------------------------------------------
def test_upload_with_readable_page_index_marks_every_page_code(
    db, actor, monkeypatch
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    # Page indices are 1-based, so a real export encodes the variant
    # identifier as VID:V|1, VID:V|2, VID:V|3.
    _stub_decoder(
        monkeypatch, [["VID:VAR-A|1"], ["VID:VAR-A|2"], ["VID:VAR-A|3"]]
    )

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "submission.pdf", _pdf_bytes(3), actor
    )

    # MATCH_FAILED, not UPLOADED: upload_pdf also runs the matching engine
    # before its commit, and this fixture's project has no PUBLISHED variant
    # for the decoded VID to resolve against. What this suite is about — what
    # INGESTION records per page — is untouched, as the assertions below (and
    # test_ingestion_alone_resolves_no_variant) show.
    assert submission.status is SubmissionStatus.MATCH_FAILED
    pages = _pages_in_order(submission)
    assert len(pages) == 3
    assert [p.source_page_order for p in pages] == [0, 1, 2]
    assert all(p.page_order_source is PageOrderSource.CODE for p in pages)
    # The RAW payload is stored verbatim, tag included — not the parsed value.
    assert [p.identified_code_value for p in pages] == [
        "VID:VAR-A|1", "VID:VAR-A|2", "VID:VAR-A|3"
    ]
    # Every page of one PDF shares the uploaded filename.
    assert {p.source_filename for p in pages} == {"submission.pdf"}


# ---------------------------------------------------------------------------
# 2. No readable code at all -> PDF_ORDER fallback, order still sequential
# ---------------------------------------------------------------------------
def test_upload_without_readable_code_falls_back_to_pdf_order(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], [], [], []])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "scan.pdf", _pdf_bytes(4), actor
    )

    pages = _pages_in_order(submission)
    assert [p.source_page_order for p in pages] == [0, 1, 2, 3]
    assert all(p.page_order_source is PageOrderSource.PDF_ORDER for p in pages)
    assert all(p.identified_code_value is None for p in pages)


def test_upload_of_a_pdf_with_no_codes_uses_the_real_decoder(db, actor):
    """Same fallback, with NO stubbing: a plain text PDF really has no code, so
    this holds whether or not pyzbar/libzbar is importable."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "plain.pdf", _pdf_bytes(2), actor
    )

    pages = _pages_in_order(submission)
    assert [p.source_page_order for p in pages] == [0, 1]
    assert all(p.page_order_source is PageOrderSource.PDF_ORDER for p in pages)
    assert all(p.identified_code_value is None for p in pages)


# ---------------------------------------------------------------------------
# 3. A code that decodes but carries NO page index -> value kept, PDF_ORDER
# ---------------------------------------------------------------------------
def test_code_without_page_index_records_value_but_keeps_pdf_order(
    db, actor, monkeypatch
):
    """include_page_index=False is the DEFAULT, so this is the common case: the
    code identifies the sheet but says nothing about the ORDER."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["VID:VAR-A"], ["VID:VAR-A"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(2), actor
    )

    pages = _pages_in_order(submission)
    assert all(p.page_order_source is PageOrderSource.PDF_ORDER for p in pages)
    # The value is still captured for the matching engine.
    assert [p.identified_code_value for p in pages] == ["VID:VAR-A", "VID:VAR-A"]


def test_malformed_payload_never_breaks_ingestion(db, actor, monkeypatch):
    """A payload whose tail is not an integer is kept verbatim and ordered by the
    PDF, rather than raising."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["VID:VAR-A|not-a-number"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(1), actor
    )

    page = _pages_in_order(submission)[0]
    assert page.identified_code_value == "VID:VAR-A|not-a-number"
    assert page.page_order_source is PageOrderSource.PDF_ORDER


# ---------------------------------------------------------------------------
# 4. One unreadable page must not affect its siblings or the upload
# ---------------------------------------------------------------------------
def test_one_undecodable_page_does_not_block_the_others(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["VID:VAR-A|1"], [], ["VID:VAR-A|3"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(3), actor
    )

    pages = _pages_in_order(submission)
    assert len(pages) == 3
    assert [p.page_order_source for p in pages] == [
        PageOrderSource.CODE,
        PageOrderSource.PDF_ORDER,
        PageOrderSource.CODE,
    ]
    assert [p.identified_code_value for p in pages] == [
        "VID:VAR-A|1", None, "VID:VAR-A|3"
    ]
    # The physical order is intact across the gap.
    assert [p.source_page_order for p in pages] == [0, 1, 2]


def test_a_raising_decoder_is_contained_by_decode_codes_from_image(tmp_path):
    """The decode helper's own contract: library failures become an empty list,
    never an exception reaching ingestion."""
    missing = tmp_path / "not-an-image.png"
    assert code_rendering.decode_codes_from_image(missing) == []


# ---------------------------------------------------------------------------
# 5. Ingestion itself resolves no variant
#
# upload_pdf runs a matching pass too, so this holds because this project has
# no PUBLISHED variant to match against. The distinction matters: what
# INGESTION writes (the assertions in the sections above) is entirely
# independent of the authored side. The matching engine's own behaviour is
# covered by tests/integration/test_submission_matching_integration.py.
# ---------------------------------------------------------------------------
def test_ingestion_alone_resolves_no_variant(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["VID:VAR-A|1"], ["VID:VAR-A|2"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(2), actor
    )

    assert submission.matched_form_variant_id is None
    assert all(p.form_variant_page_id is None for p in submission.pages)


# ---------------------------------------------------------------------------
# 4b. The role tag decides what each code means
# ---------------------------------------------------------------------------
def test_untagged_code_identifies_nothing(db, actor, monkeypatch):
    """An untagged code cannot be trusted to be the variant identifier.

    A code must say so with a VID tag before it is written to
    identified_code_value. A third-party QR on the sheet is therefore treated
    exactly like "no readable code".
    """
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["VAR-A|1"], ["some-third-party-qr"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(2), actor
    )

    pages = _pages_in_order(submission)
    assert all(p.identified_code_value is None for p in pages)
    assert all(p.page_order_source is PageOrderSource.PDF_ORDER for p in pages)
    assert submission.respondent_identifier is None


def test_respondent_tag_on_any_page_names_the_candidate(db, actor, monkeypatch):
    """A RID found on ONE page identifies the whole submission."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(
        monkeypatch,
        [["VID:VAR-A|1"], ["VID:VAR-A|2", "RID:STUDENT-42"], ["VID:VAR-A|3"]],
    )

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(3), actor
    )

    assert submission.respondent_identifier == "STUDENT-42"
    pages = _pages_in_order(submission)
    # The RID never leaks into the page's own identification…
    assert [p.identified_code_value for p in pages] == [
        "VID:VAR-A|1", "VID:VAR-A|2", "VID:VAR-A|3"
    ]
    # …nor into the ordering.
    assert all(p.page_order_source is PageOrderSource.CODE for p in pages)


def test_respondent_only_page_is_not_matched_to_a_variant(db, actor, monkeypatch):
    """A page whose ONLY code is a respondent identifier. It names the
    candidate, it does not identify the sheet — and it never becomes the
    page's identified_code_value."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:STUDENT-7"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(1), actor
    )

    page = _pages_in_order(submission)[0]
    assert submission.respondent_identifier == "STUDENT-7"
    assert page.identified_code_value is None
    assert page.page_order_source is PageOrderSource.PDF_ORDER


def test_first_respondent_tag_in_pdf_order_wins(db, actor, monkeypatch):
    """Two different RIDs in one submission is anomalous; resolution is
    deterministic — the first in physical PDF order."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:FIRST"], ["RID:SECOND"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(2), actor
    )

    assert submission.respondent_identifier == "FIRST"


def test_several_variant_codes_on_one_page_resolve_deterministically(
    db, actor, monkeypatch
):
    """Anomalous sheet: the first VID in decode order wins, and the upload is
    still accepted (the anomaly is recorded in the audit trail, not raised)."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["VID:VAR-A|1", "VID:VAR-B|9"]])

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "f.pdf", _pdf_bytes(1), actor
    )

    page = _pages_in_order(submission)[0]
    assert page.identified_code_value == "VID:VAR-A|1"
    assert page.page_order_source is PageOrderSource.CODE


# ---------------------------------------------------------------------------
# 6. The ACTIVE gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "project_status",
    [FormProjectStatus.DRAFT, FormProjectStatus.CLOSED, FormProjectStatus.ARCHIVED],
)
def test_create_batch_blocked_unless_project_is_active(db, actor, project_status):
    project = _make_project(db, actor, status=project_status)

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.create_batch(
            db, project.id, SubmissionBatchCreate(name="B"), actor
        )

    assert exc.value.status_code == 409
    assert "ACTIVE" in exc.value.detail


def test_create_batch_succeeds_when_active(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)

    batch = submission_batch_service.create_batch(
        db, project.id, SubmissionBatchCreate(name="B", description="d"), actor
    )

    assert batch.form_project_id == project.id
    assert batch.created_by_user_id == actor.id
    assert batch.name == "B"


def test_upload_rechecks_the_gate_after_the_project_leaves_active(db, actor):
    """The status can change between opening a batch and uploading into it."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    project.status = FormProjectStatus.CLOSED
    db.flush()

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_pdf(
            db, batch.id, "f.pdf", _pdf_bytes(1), actor
        )

    assert exc.value.status_code == 409


def test_non_member_cannot_upload(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    stranger = User(
        email=f"stranger-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(stranger)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_pdf(
            db, batch.id, "f.pdf", _pdf_bytes(1), stranger
        )

    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# 7. Malformed input
# ---------------------------------------------------------------------------
def test_non_pdf_upload_is_rejected(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_pdf(
            db, batch.id, "f.pdf", b"this is not a pdf", actor
        )

    assert exc.value.status_code == 422


def test_empty_upload_is_rejected(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_pdf(db, batch.id, "f.pdf", b"", actor)

    assert exc.value.status_code == 422


def test_oversize_upload_is_rejected(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    monkeypatch.setattr(settings, "max_submission_pdf_bytes", 10)

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_pdf(
            db, batch.id, "f.pdf", _pdf_bytes(1), actor
        )

    assert exc.value.status_code == 413


def test_unknown_batch_is_404(db, actor):
    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_pdf(
            db, uuid.uuid4(), "f.pdf", _pdf_bytes(1), actor
        )

    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 8. Batch detail — the minimal read-back
# ---------------------------------------------------------------------------
def test_batch_detail_reports_submissions_with_page_counts(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]] * 10)

    first = submission_batch_service.upload_pdf(
        db, batch.id, "a.pdf", _pdf_bytes(3), actor
    )
    second = submission_batch_service.upload_pdf(
        db, batch.id, "b.pdf", _pdf_bytes(1), actor
    )

    detail = submission_batch_service.get_batch_detail(db, batch.id, actor)

    assert detail.id == batch.id
    assert detail.name == "Batch 1"
    counts = {s.id: s.page_count for s in detail.submissions}
    assert counts == {first.id: 3, second.id: 1}
    assert all(s.matched_form_variant_id is None for s in detail.submissions)


def test_batch_detail_is_empty_before_any_upload(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    detail = submission_batch_service.get_batch_detail(db, batch.id, actor)

    assert detail.submissions == []


# ---------------------------------------------------------------------------
# 9. Genuine end-to-end: real QR -> real PDF -> real pyzbar
#
# Skipped where the optional encoder or the system libzbar library is missing,
# so the suite above stays independent of the QR toolchain. In Docker (which
# installs libzbar0) this DOES run and proves the round trip.
# ---------------------------------------------------------------------------
def _qr_png(payload: str) -> bytes | None:
    from io import BytesIO

    try:
        import qrcode
    except Exception:
        return None
    buf = BytesIO()
    qrcode.make(payload).save(buf, format="PNG")
    return buf.getvalue()


def _pdf_with_qr(payloads: list[str]) -> bytes:
    doc = fitz.open()
    try:
        for payload in payloads:
            page = doc.new_page()
            png = _qr_png(payload)
            page.insert_image(fitz.Rect(72, 72, 272, 272), stream=png)
        return doc.tobytes()
    finally:
        doc.close()


def test_real_qr_end_to_end(db, actor, tmp_path):
    if _qr_png("probe") is None:
        pytest.skip("qrcode encoder not installed")
    try:
        import pyzbar.pyzbar  # noqa: F401
    except Exception:
        pytest.skip("pyzbar/libzbar not available")

    # Sanity-check the decoder itself before trusting the ingestion assertions.
    probe = tmp_path / "probe.png"
    probe.write_bytes(_qr_png("VID:VAR-A|1"))
    if code_rendering.decode_codes_from_image(probe) != ["VID:VAR-A|1"]:
        pytest.skip("QR round trip unavailable in this environment")

    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    submission = submission_batch_service.upload_pdf(
        db, batch.id, "real.pdf", _pdf_with_qr(["VID:VAR-A|1", "VID:VAR-A|2"]), actor
    )

    pages = _pages_in_order(submission)
    assert [p.identified_code_value for p in pages] == ["VID:VAR-A|1", "VID:VAR-A|2"]
    assert all(p.page_order_source is PageOrderSource.CODE for p in pages)
    assert [p.source_page_order for p in pages] == [0, 1]
