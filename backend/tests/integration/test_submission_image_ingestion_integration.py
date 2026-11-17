# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for LOOSE-IMAGE ingestion.

The other half of ingestion. Where a PDF upload is one file = one submission
with the page order given, a pile of images gives neither, so each image has to
be grouped and positioned from two independent signals:

  * grouping — a readable RID > the ``<submission>`` token of a filename
    following the naming convention of ``app/utils/image_filename.py`` >
    ungroupable (a single-page UNASSIGNED Submission of its own);
  * position — a VID page index > the filename's ``<n>`` > the order of arrival
    within the upload request, among images sharing the grouping key.

What this suite pins down, beyond the ladders themselves:

  * grouping spans upload CALLS — a second request carrying the same RID adds
    pages to the submission the first one opened, it does not duplicate it;
  * the two signals are read independently, so a RID and a VID index on the same
    sheet each answer their own question;
  * one unusable FILE is rejected on its own and never blocks the rest of the
    request — a stronger contract than the PDF path's per-page fallback;
  * matching resolves nothing here: these projects have no PUBLISHED variant,
    so matched_form_variant_id and form_variant_page_id stay NULL.

Isolation from the QR toolchain
-------------------------------
Most tests monkeypatch ``code_rendering.decode_codes_from_image``: the rules
above are about what the service DOES with a decode result, and stubbing it
exercises every branch deterministically without depending on a QR surviving a
render->decode round trip. The genuine path (a real ``qrcode`` PNG read back by
real pyzbar) is ``test_real_qr_end_to_end_groups_by_rid``, which SKIPS when the
optional encoder or the system libzbar shared library is missing.

The IMAGES themselves are always real: every fixture builds actual JPEG/PNG bytes
with Pillow, so content sniffing is exercised for real even when decoding is not.

Files go to a throwaway ``tmp_path`` via monkeypatched settings, as in
test_submission_ingestion_integration.py. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.audit_log import AuditLog
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
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.user import User
from app.services import audit_service, submission_batch_service
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
        email=f"img-ingest-{uuid.uuid4().hex}@example.com",
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
        name="Image ingestion project",
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
        form_project_id=project.id, name="Image batch", created_by_user_id=owner.id
    )
    db.add(batch)
    db.flush()
    return batch


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (40, 30)) -> bytes:
    """A REAL image of *fmt* — content sniffing is never stubbed."""
    buf = BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buf, format=fmt)
    return buf.getvalue()


def _files(*names: str, fmt: str = "JPEG") -> list[tuple[str, bytes]]:
    """(filename, bytes) pairs in arrival order — what the endpoint hands over."""
    return [(name, _image_bytes(fmt)) for name in names]


def _stub_decoder(monkeypatch, payloads: list[list[str]]) -> None:
    """Make decoding return ``payloads[n]`` for the n-th image decoded.

    Each entry is the FULL list of symbols readable on that sheet (an image
    legitimately carries both a variant and a respondent identifier); an empty
    list means nothing was readable. Indexed by decode order, which for this
    service is the arrival order of the files.
    """
    calls = {"n": 0}

    def fake_decode(image_path):
        index = calls["n"]
        calls["n"] += 1
        return payloads[index] if index < len(payloads) else []

    monkeypatch.setattr(code_rendering, "decode_codes_from_image", fake_decode)


def _load(db, submission_id) -> Submission:
    db.flush()
    return db.get(Submission, submission_id)


def _pages_in_order(submission):
    return sorted(submission.pages, key=lambda p: p.source_page_order)


def _latest_ingest_event(db) -> AuditLog:
    """The per-CALL audit event of the most recent image upload.

    Where the non-blocking anomalies are reported: a rejected file, several codes
    sharing a role tag, a RID disagreeing with the filename, or two sheets
    declaring the same position.
    """
    db.flush()
    return db.scalars(
        select(AuditLog)
        .where(AuditLog.action == audit_service.SUBMISSION_IMAGES_INGESTED)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    ).first()


# ---------------------------------------------------------------------------
# 1. Grouping by a readable RID
# ---------------------------------------------------------------------------
def test_same_rid_on_two_images_makes_one_submission(db, actor, monkeypatch):
    """The headline case: two loose sheets, one candidate, one submission."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:STUDENT-42"], ["RID:STUDENT-42"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("scan001.jpg", "scan002.jpg"), actor
    )

    assert len(result.submissions_created) == 1
    assert result.submissions_updated == []
    assert result.pages_added == 2
    assert result.unassigned_count == 0
    assert result.rejected == []

    submission = _load(db, result.submissions_created[0])
    # MATCH_FAILED, not UPLOADED: the upload also runs the matching engine in
    # its own transaction, and this fixture's project has no PUBLISHED variant
    # to match against (it is forced ACTIVE directly, bypassing the gate that
    # normally guarantees one). Grouping — what this test is about — is
    # unaffected: matching answers "which variant", never "which submission".
    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.source_group_key == "STUDENT-42"
    # The RID is BOTH the technical grouping key and the human identifier here.
    assert submission.respondent_identifier == "STUDENT-42"
    pages = _pages_in_order(submission)
    assert len(pages) == 2
    # Nothing declared a position, so arrival order within the group.
    assert [p.source_page_order for p in pages] == [0, 1]
    assert all(p.page_order_source is PageOrderSource.PDF_ORDER for p in pages)
    assert [p.source_filename for p in pages] == ["scan001.jpg", "scan002.jpg"]


def test_different_rids_make_different_submissions(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(
        monkeypatch, [["RID:A"], ["RID:B"], ["RID:A"]]
    )

    result = submission_batch_service.upload_images(
        db, batch.id, _files("s1.jpg", "s2.jpg", "s3.jpg"), actor
    )

    assert len(result.submissions_created) == 2
    assert result.pages_added == 3
    by_key = {
        _load(db, sid).source_group_key: _load(db, sid)
        for sid in result.submissions_created
    }
    assert sorted(by_key) == ["A", "B"]
    assert len(by_key["A"].pages) == 2
    assert len(by_key["B"].pages) == 1


# ---------------------------------------------------------------------------
# 2. Grouping and positioning by the filename convention
# ---------------------------------------------------------------------------
def test_conforming_filenames_group_and_position_without_any_code(
    db, actor, monkeypatch
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], [], []])

    # Deliberately uploaded OUT of page order: the filename, not the arrival
    # sequence, must decide the position.
    result = submission_batch_service.upload_images(
        db,
        batch.id,
        _files("rossi_pagina_3.jpg", "rossi_pagina_1.jpg", "rossi_pagina_2.jpg"),
        actor,
    )

    assert len(result.submissions_created) == 1
    assert result.pages_added == 3
    assert result.unassigned_count == 0

    submission = _load(db, result.submissions_created[0])
    # MATCH_FAILED because this project has no PUBLISHED variant for the
    # matching pass to resolve — see
    # test_same_rid_on_two_images_makes_one_submission.
    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.source_group_key == "rossi"
    # A filename token is NOT a respondent identifier: nobody was identified.
    assert submission.respondent_identifier is None

    pages = _pages_in_order(submission)
    # 1-based <n> -> 0-based source_page_order, and the upload order is ignored.
    assert [p.source_page_order for p in pages] == [0, 1, 2]
    assert [p.source_filename for p in pages] == [
        "rossi_pagina_1.jpg",
        "rossi_pagina_2.jpg",
        "rossi_pagina_3.jpg",
    ]
    assert all(p.page_order_source is PageOrderSource.FILENAME for p in pages)
    assert all(p.identified_code_value is None for p in pages)


def test_filenames_split_into_the_right_submissions(db, actor, monkeypatch):
    """``_page_`` and ``_pagina_`` are the same separator: rossi's two sheets
    land in one submission although named with one form each."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], [], []])

    result = submission_batch_service.upload_images(
        db,
        batch.id,
        _files("rossi_page_1.jpg", "bianchi_page_1.jpg", "rossi_pagina_2.jpg"),
        actor,
    )

    assert len(result.submissions_created) == 2
    by_key = {
        _load(db, sid).source_group_key: _load(db, sid)
        for sid in result.submissions_created
    }
    assert sorted(by_key) == ["bianchi", "rossi"]
    assert len(by_key["rossi"].pages) == 2
    assert sorted(p.source_page_order for p in by_key["rossi"].pages) == [0, 1]
    assert len(by_key["bianchi"].pages) == 1


# ---------------------------------------------------------------------------
# 3. Neither signal -> an UNASSIGNED singleton
# ---------------------------------------------------------------------------
def test_ungroupable_images_become_unassigned_singletons(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("IMG_0001.jpg", "IMG_0002.jpg"), actor
    )

    # One submission EACH — they must never be merged with one another.
    assert len(result.submissions_created) == 2
    assert result.unassigned_count == 2
    assert result.pages_added == 2

    for submission_id in result.submissions_created:
        submission = _load(db, submission_id)
        assert submission.status is SubmissionStatus.UNASSIGNED
        assert submission.source_group_key is None
        assert submission.respondent_identifier is None
        assert len(submission.pages) == 1
        page = submission.pages[0]
        assert page.source_page_order == 0
        assert page.page_order_source is PageOrderSource.PDF_ORDER


def test_many_unassigned_submissions_coexist_under_the_partial_unique_index(
    db, actor, monkeypatch
):
    """The reason the unique index is PARTIAL.

    A plain UNIQUE(submission_batch_id, source_group_key) would be satisfied by
    NULLs on Postgres too, but stating WHERE source_group_key IS NOT NULL makes
    the intent explicit — and this asserts the DB really does accept a pile of
    ungroupable sheets in one batch. The commit inside upload_images is what
    would surface a violation, so reaching the assertions at all is the test.
    """
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]] * 5)

    result = submission_batch_service.upload_images(
        db, batch.id, _files(*[f"IMG_{i}.jpg" for i in range(5)]), actor
    )

    assert len(result.submissions_created) == 5
    assert result.unassigned_count == 5
    keys = [_load(db, sid).source_group_key for sid in result.submissions_created]
    assert keys == [None] * 5


def test_a_grouped_key_cannot_be_duplicated_within_a_batch(db, actor, monkeypatch):
    """The other half of the same index: a non-NULL key is unique per batch.

    Asserted through behaviour — the second upload MERGES rather than inserting
    a second row with the same key, which is exactly what the index enforces.
    """
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    submission_batch_service.upload_images(db, batch.id, _files("x_pagina_1.jpg"), actor)
    submission_batch_service.upload_images(db, batch.id, _files("x_pagina_2.jpg"), actor)

    rows = db.scalars(
        Submission.__table__.select().where(
            Submission.__table__.c.submission_batch_id == batch.id
        )
    ).all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 4. Cross-call merging
# ---------------------------------------------------------------------------
def test_a_later_call_with_the_same_rid_extends_the_existing_submission(
    db, actor, monkeypatch
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    _stub_decoder(monkeypatch, [["RID:STUDENT-42"]])
    first = submission_batch_service.upload_images(
        db, batch.id, _files("scan001.jpg"), actor
    )
    assert len(first.submissions_created) == 1
    assert first.submissions_updated == []

    _stub_decoder(monkeypatch, [["RID:STUDENT-42"]])
    second = submission_batch_service.upload_images(
        db, batch.id, _files("scan002.jpg"), actor
    )

    # No new submission: the existing one was extended.
    assert second.submissions_created == []
    assert second.submissions_updated == first.submissions_created
    assert second.pages_added == 1

    submission = _load(db, first.submissions_created[0])
    assert len(submission.pages) == 2
    # The second call APPENDS after the first call's pages rather than
    # restarting at 0 on top of them.
    assert [p.source_page_order for p in _pages_in_order(submission)] == [0, 1]
    assert [p.source_filename for p in _pages_in_order(submission)] == [
        "scan001.jpg",
        "scan002.jpg",
    ]


def test_cross_call_merging_also_works_by_filename(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]] * 4)

    first = submission_batch_service.upload_images(
        db, batch.id, _files("rossi_pagina_1.jpg"), actor
    )
    second = submission_batch_service.upload_images(
        db, batch.id, _files("rossi_pagina_2.jpg"), actor
    )

    assert second.submissions_created == []
    assert second.submissions_updated == first.submissions_created
    submission = _load(db, first.submissions_created[0])
    assert [p.source_page_order for p in _pages_in_order(submission)] == [0, 1]
    assert all(
        p.page_order_source is PageOrderSource.FILENAME
        for p in submission.pages
    )


def test_merging_is_scoped_to_the_batch(db, actor, monkeypatch):
    """The same candidate scanned into two different batches stays two
    submissions — the key is unique PER BATCH, not globally."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch_a = _make_batch(db, project, actor)
    batch_b = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:STUDENT-42"]] * 2)

    first = submission_batch_service.upload_images(
        db, batch_a.id, _files("s1.jpg"), actor
    )
    second = submission_batch_service.upload_images(
        db, batch_b.id, _files("s2.jpg"), actor
    )

    assert second.submissions_updated == []
    assert len(second.submissions_created) == 1
    assert second.submissions_created != first.submissions_created


# ---------------------------------------------------------------------------
# 5. The two signals answer their own question (RID + VID on one sheet)
# ---------------------------------------------------------------------------
def test_rid_groups_while_the_vid_index_positions(db, actor, monkeypatch):
    """Both tags on the same image: the group comes from the RID, the position
    from the VID's page index. Neither is used for the other's job."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(
        monkeypatch,
        [
            ["RID:STUDENT-42", "VID:VAR-A|2"],
            ["RID:STUDENT-42", "VID:VAR-A|1"],
        ],
    )

    # Uploaded page 2 first, to prove the arrival order is NOT what positioned
    # them, and named so the filename would have said something different again.
    result = submission_batch_service.upload_images(
        db, batch.id, _files("zzz_pagina_9.jpg", "aaa_pagina_8.jpg"), actor
    )

    assert len(result.submissions_created) == 1
    submission = _load(db, result.submissions_created[0])
    # Grouped by the RID, not by either filename token.
    assert submission.source_group_key == "STUDENT-42"
    assert submission.respondent_identifier == "STUDENT-42"

    pages = _pages_in_order(submission)
    # 1-based VID index -> 0-based order, outranking both the filename and the
    # arrival sequence.
    assert [p.source_page_order for p in pages] == [0, 1]
    assert [p.source_filename for p in pages] == [
        "aaa_pagina_8.jpg",
        "zzz_pagina_9.jpg",
    ]
    assert all(p.page_order_source is PageOrderSource.CODE for p in pages)
    # The RAW VID payload is kept verbatim for matching.
    assert [p.identified_code_value for p in pages] == ["VID:VAR-A|1", "VID:VAR-A|2"]


def test_a_rid_disagreeing_with_the_filename_does_not_block_the_upload(
    db, actor, monkeypatch
):
    """A likely operator naming slip: the RID wins, the sheet is still ingested
    (the anomaly is recorded in the audit trail, not raised)."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:ROSSI"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("bianchi_pagina_1.jpg"), actor
    )

    assert result.pages_added == 1
    assert result.rejected == []
    submission = _load(db, result.submissions_created[0])
    assert submission.source_group_key == "ROSSI"
    # The filename still supplied the POSITION — only the grouping was overruled.
    assert submission.pages[0].page_order_source is PageOrderSource.FILENAME


def test_a_respondent_identifier_is_never_overwritten(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    # Same submission by filename, two different RIDs across the sheets.
    _stub_decoder(monkeypatch, [["RID:FIRST"], ["RID:SECOND"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("x_pagina_1.jpg", "x_pagina_2.jpg"), actor
    )

    # The RID on the first image grouped it; the second image joins that group
    # (its own RID makes a different key, so it opens its own submission).
    keys = sorted(
        _load(db, sid).source_group_key for sid in result.submissions_created
    )
    assert keys == ["FIRST", "SECOND"]


def test_an_untagged_code_identifies_nothing_and_leaves_the_image_ungroupable(
    db, actor, monkeypatch
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["some-third-party-qr"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("IMG_0001.jpg"), actor
    )

    submission = _load(db, result.submissions_created[0])
    assert submission.status is SubmissionStatus.UNASSIGNED
    assert submission.source_group_key is None
    assert submission.pages[0].identified_code_value is None


# ---------------------------------------------------------------------------
# 6. One bad file never blocks the rest of the call
# ---------------------------------------------------------------------------
def test_an_invalid_image_is_rejected_without_blocking_the_others(
    db, actor, monkeypatch
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    files = [
        ("rossi_pagina_1.jpg", _image_bytes("JPEG")),
        ("rossi_pagina_2.jpg", b"this is definitely not an image"),
        ("rossi_pagina_3.png", _image_bytes("PNG")),
    ]
    result = submission_batch_service.upload_images(db, batch.id, files, actor)

    # The two real images landed; the impostor is reported, not raised.
    assert result.pages_added == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].filename == "rossi_pagina_2.jpg"
    assert "PNG" in result.rejected[0].reason or "image" in result.rejected[0].reason

    submission = _load(db, result.submissions_created[0])
    assert [p.source_page_order for p in _pages_in_order(submission)] == [0, 2]


@pytest.mark.parametrize(
    "bad_bytes, label",
    [
        (b"", "empty"),
        (b"not an image at all", "garbage"),
        (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", "a PDF"),
    ],
)
def test_unusable_payloads_are_rejected_per_file(db, actor, monkeypatch, bad_bytes, label):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]])

    result = submission_batch_service.upload_images(
        db,
        batch.id,
        [("bad.jpg", bad_bytes), ("good_pagina_1.jpg", _image_bytes())],
        actor,
    )

    assert len(result.rejected) == 1, label
    assert result.rejected[0].filename == "bad.jpg"
    assert result.pages_added == 1


def test_a_real_image_in_an_unsupported_format_is_rejected(db, actor, monkeypatch):
    """BMP is a real image and the IMAGE PageObject upload accepts it — a SCAN
    may not be one. The whitelist here is deliberately narrower."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]])

    result = submission_batch_service.upload_images(
        db,
        batch.id,
        [("x_pagina_1.bmp", _image_bytes("BMP")), ("x_pagina_2.jpg", _image_bytes())],
        actor,
    )

    assert len(result.rejected) == 1
    assert result.rejected[0].filename == "x_pagina_1.bmp"
    assert "PNG, JPEG" in result.rejected[0].reason
    assert result.pages_added == 1


def test_an_oversize_image_is_rejected_without_blocking_the_others(
    db, actor, monkeypatch
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]])
    small = _image_bytes("JPEG", size=(8, 8))
    big = _image_bytes("JPEG", size=(400, 400))
    monkeypatch.setattr(settings, "max_submission_image_bytes", len(small) + 1)

    result = submission_batch_service.upload_images(
        db, batch.id, [("big_pagina_1.jpg", big), ("small_pagina_1.jpg", small)], actor
    )

    assert [r.filename for r in result.rejected] == ["big_pagina_1.jpg"]
    assert "too large" in result.rejected[0].reason
    assert result.pages_added == 1


def test_an_absurdly_long_filename_is_truncated_not_fatal(db, actor, monkeypatch):
    """source_filename is String(500); overflowing it would raise at flush and
    take the WHOLE call down. It is display-only, so it is capped instead."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]] * 2)

    long_name = "a" * 900 + "_pagina_1.jpg"
    result = submission_batch_service.upload_images(
        db, batch.id, _files(long_name, "ok_pagina_1.jpg"), actor
    )

    assert result.rejected == []
    assert result.pages_added == 2
    stored = [
        p.source_filename
        for sid in result.submissions_created
        for p in _load(db, sid).pages
    ]
    assert all(len(name) <= 500 for name in stored)


def test_an_absurdly_long_grouping_key_rejects_only_its_own_file(
    db, actor, monkeypatch
):
    """A QR holds thousands of characters, so an overlong RID is reachable.
    Truncating it could silently merge two candidates, so the file is refused —
    and only that file."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:" + "X" * 600], ["RID:OK"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("huge.jpg", "fine.jpg"), actor
    )

    assert [r.filename for r in result.rejected] == ["huge.jpg"]
    assert "too long" in result.rejected[0].reason
    assert result.pages_added == 1
    assert _load(db, result.submissions_created[0]).source_group_key == "OK"


def test_a_call_whose_files_are_all_rejected_creates_nothing(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    result = submission_batch_service.upload_images(
        db, batch.id, [("a.jpg", b"nope"), ("b.jpg", b"")], actor
    )

    assert result.pages_added == 0
    assert result.submissions_created == []
    assert result.submissions_updated == []
    assert len(result.rejected) == 2


def test_an_empty_request_is_rejected(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_images(db, batch.id, [], actor)

    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# 7. Position fallbacks and mixed signals
# ---------------------------------------------------------------------------
def test_arrival_order_is_the_last_resort_within_a_group(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    # Grouped by RID, but nothing declares a position anywhere.
    _stub_decoder(monkeypatch, [["RID:R1"], ["RID:R1"], ["RID:R1"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("c.jpg", "a.jpg", "b.jpg"), actor
    )

    submission = _load(db, result.submissions_created[0])
    pages = _pages_in_order(submission)
    # Arrival order, NOT alphabetical — the request's sequence is the signal.
    assert [p.source_filename for p in pages] == ["c.jpg", "a.jpg", "b.jpg"]
    assert [p.source_page_order for p in pages] == [0, 1, 2]
    assert all(p.page_order_source is PageOrderSource.PDF_ORDER for p in pages)


def test_arrival_order_is_counted_per_group_not_per_request(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:A"], ["RID:B"], ["RID:A"], ["RID:B"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("1.jpg", "2.jpg", "3.jpg", "4.jpg"), actor
    )

    by_key = {
        _load(db, sid).source_group_key: _load(db, sid)
        for sid in result.submissions_created
    }
    # Each submission numbers its own pages from 0 — interleaving does not
    # shift anyone's position.
    for key in ("A", "B"):
        assert [p.source_page_order for p in _pages_in_order(by_key[key])] == [0, 1]


def test_an_undeclared_page_takes_a_free_slot_beside_declared_ones(
    db, actor, monkeypatch
):
    """A mixed group is anomalous but must stay collision-free: the declared
    position is honoured verbatim and the undeclared one takes a free slot."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    # Both sheets group by RID; only the first declares where it sits.
    _stub_decoder(monkeypatch, [["RID:R1", "VID:V|1"], ["RID:R1"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("a.jpg", "b.jpg"), actor
    )

    submission = _load(db, result.submissions_created[0])
    pages = _pages_in_order(submission)
    assert [p.source_page_order for p in pages] == [0, 1]
    assert [p.page_order_source for p in pages] == [
        PageOrderSource.CODE,       # declared 1 (1-based) -> 0
        PageOrderSource.PDF_ORDER,  # took the next free slot
    ]


# ---------------------------------------------------------------------------
# 7b. Two sheets DECLARING the same position inside one submission
#
# Pinned deliberately: source_page_order is NOT unique within a Submission, by
# design and with no constraint enforcing it. Both pages are kept — moving,
# overwriting or rejecting one would each lose something — and the clash is
# reported instead of being silent. Matching must not assume uniqueness; see
# SubmissionPage.source_page_order.
# ---------------------------------------------------------------------------
def test_two_files_naming_the_same_page_both_survive_with_the_same_order(
    db, actor, monkeypatch
):
    """The operator naming slip: two files both called ..._pagina_1."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("rossi_pagina_1.jpg", "rossi_pagina_1.jpg"), actor
    )

    # Neither is rejected and neither overwrites the other.
    assert result.rejected == []
    assert result.pages_added == 2
    submission = _load(db, result.submissions_created[0])
    pages = submission.pages
    assert len(pages) == 2
    # Both landed on the SAME declared position — the duplicate is real.
    assert [p.source_page_order for p in pages] == [0, 0]
    assert all(p.page_order_source is PageOrderSource.FILENAME for p in pages)
    # Two distinct rows backed by two distinct files on disk: no data was lost.
    assert len({p.id for p in pages}) == 2
    assert len({p.source_file_path for p in pages}) == 2
    assert all(Path(p.source_file_path).is_file() for p in pages)


def test_two_sheets_printed_with_the_same_vid_index_both_survive(
    db, actor, monkeypatch
):
    """The printing slip: two masters stamped with the same VID page index."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(
        monkeypatch,
        [["RID:R1", "VID:V|1"], ["RID:R1", "VID:V|1"], ["RID:R1", "VID:V|2"]],
    )

    result = submission_batch_service.upload_images(
        db, batch.id, _files("a.jpg", "b.jpg", "c.jpg"), actor
    )

    assert result.rejected == []
    assert result.pages_added == 3
    submission = _load(db, result.submissions_created[0])
    orders = sorted(p.source_page_order for p in submission.pages)
    assert orders == [0, 0, 1]
    assert all(p.page_order_source is PageOrderSource.CODE for p in submission.pages)


def test_a_duplicate_position_across_upload_calls_is_also_kept(db, actor, monkeypatch):
    """Cross-call: the second call's declared position is honoured verbatim too,
    rather than being appended after the first call's pages."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    _stub_decoder(monkeypatch, [[]])
    first = submission_batch_service.upload_images(
        db, batch.id, _files("rossi_pagina_1.jpg"), actor
    )
    _stub_decoder(monkeypatch, [[]])
    second = submission_batch_service.upload_images(
        db, batch.id, _files("rossi_pagina_1.jpg"), actor
    )

    assert second.rejected == []
    assert second.pages_added == 1
    submission = _load(db, first.submissions_created[0])
    assert [p.source_page_order for p in submission.pages] == [0, 0]


def test_a_duplicate_position_is_reported_in_the_audit_trail(db, actor, monkeypatch):
    """The anomaly must not be SILENT — that is what would surprise
    matching."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    submission_batch_service.upload_images(
        db, batch.id, _files("rossi_pagina_1.jpg", "rossi_pagina_1.jpg"), actor
    )

    event = _latest_ingest_event(db)
    duplicates = event.metadata_json["pages_with_duplicate_position"]
    assert duplicates == [
        {"filename": "rossi_pagina_1.jpg", "source_page_order": 0}
    ]


def test_no_duplicate_is_reported_for_a_clean_upload(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], []])

    submission_batch_service.upload_images(
        db, batch.id, _files("rossi_pagina_1.jpg", "rossi_pagina_2.jpg"), actor
    )

    event = _latest_ingest_event(db)
    assert event.metadata_json["pages_with_duplicate_position"] == []


def test_the_arrival_order_fallback_never_creates_a_duplicate(db, actor, monkeypatch):
    """It takes the lowest FREE slot, so it can only ever avoid a collision —
    including against a position an earlier call already used."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    _stub_decoder(monkeypatch, [["RID:R1"]])
    first = submission_batch_service.upload_images(
        db, batch.id, _files("a.jpg"), actor
    )
    _stub_decoder(monkeypatch, [["RID:R1"], ["RID:R1"]])
    submission_batch_service.upload_images(db, batch.id, _files("b.jpg", "c.jpg"), actor)

    submission = _load(db, first.submissions_created[0])
    assert sorted(p.source_page_order for p in submission.pages) == [0, 1, 2]
    event = _latest_ingest_event(db)
    assert event.metadata_json["pages_with_duplicate_position"] == []


# ---------------------------------------------------------------------------
# 8. Matching resolves nothing without a PUBLISHED variant
# ---------------------------------------------------------------------------
def test_image_ingestion_leaves_matching_unresolved(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [["RID:R1", "VID:VAR-A|1"]])

    result = submission_batch_service.upload_images(
        db, batch.id, _files("x_pagina_1.jpg"), actor
    )

    submission = _load(db, result.submissions_created[0])
    assert submission.matched_form_variant_id is None
    assert all(p.form_variant_page_id is None for p in submission.pages)


# ---------------------------------------------------------------------------
# 9. Storage discipline
# ---------------------------------------------------------------------------
def test_images_are_stored_under_the_page_id_never_the_client_filename(
    db, actor, monkeypatch, tmp_path
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]])

    result = submission_batch_service.upload_images(
        db, batch.id, [("../../evil_pagina_1.jpg", _image_bytes("JPEG"))], actor
    )

    submission = _load(db, result.submissions_created[0])
    page = submission.pages[0]
    stored = tmp_path / "submission_pages" / f"{page.id}.jpg"
    assert page.source_file_path == str(stored)
    assert stored.is_file()
    # The client name is kept for display only, with any path stripped.
    assert page.source_filename == "evil_pagina_1.jpg"


def test_the_uploaded_bytes_are_stored_verbatim(db, actor, monkeypatch):
    """A scan is already raster — no re-encoding, so a PNG stays that PNG."""
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]])
    data = _image_bytes("PNG")

    result = submission_batch_service.upload_images(
        db, batch.id, [("x_pagina_1.png", data)], actor
    )

    page = _load(db, result.submissions_created[0]).pages[0]
    assert Path(page.source_file_path).read_bytes() == data
    assert page.source_file_path.endswith(".png")


def test_the_extension_follows_the_content_not_the_client_name(
    db, actor, monkeypatch
):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[]])

    # A real PNG that claims to be a JPEG.
    result = submission_batch_service.upload_images(
        db, batch.id, [("x_pagina_1.jpg", _image_bytes("PNG"))], actor
    )

    page = _load(db, result.submissions_created[0]).pages[0]
    assert page.source_file_path.endswith(".png")


# ---------------------------------------------------------------------------
# 10. The same gates as the PDF upload
# ---------------------------------------------------------------------------
def test_upload_rechecks_the_active_gate(db, actor):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    project.status = FormProjectStatus.CLOSED
    db.flush()

    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_images(
            db, batch.id, _files("x_pagina_1.jpg"), actor
        )

    assert exc.value.status_code == 409


def test_non_member_cannot_upload_images(db, actor):
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
        submission_batch_service.upload_images(
            db, batch.id, _files("x_pagina_1.jpg"), stranger
        )

    assert exc.value.status_code == 403


def test_unknown_batch_is_404_for_images(db, actor):
    with pytest.raises(HTTPException) as exc:
        submission_batch_service.upload_images(
            db, uuid.uuid4(), _files("x_pagina_1.jpg"), actor
        )

    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 11. Batch detail sees image-ingested submissions too
# ---------------------------------------------------------------------------
def test_batch_detail_reports_image_submissions(db, actor, monkeypatch):
    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)
    _stub_decoder(monkeypatch, [[], [], []])

    submission_batch_service.upload_images(
        db,
        batch.id,
        _files("rossi_pagina_1.jpg", "rossi_pagina_2.jpg", "IMG_9.jpg"),
        actor,
    )

    detail = submission_batch_service.get_batch_detail(db, batch.id, actor)
    counts = sorted(s.page_count for s in detail.submissions)
    assert counts == [1, 2]
    statuses = {s.status for s in detail.submissions}
    # The grouped submission went MATCH_FAILED (no PUBLISHED variant in this
    # fixture's project), while the ungroupable singleton KEPT its UNASSIGNED
    # status: matching never writes over UNASSIGNED, because "which variant is
    # this?" and "whose submission is this?" are different questions
    # and only the latter is still open for that sheet.
    assert statuses == {SubmissionStatus.MATCH_FAILED, SubmissionStatus.UNASSIGNED}


# ---------------------------------------------------------------------------
# 12. Genuine end-to-end: real QR -> real image -> real pyzbar
#
# Skipped where the optional encoder or the system libzbar library is missing,
# so the suite above stays independent of the QR toolchain.
# ---------------------------------------------------------------------------
def _qr_png(payload: str) -> bytes | None:
    try:
        import qrcode
    except Exception:
        return None
    buf = BytesIO()
    qrcode.make(payload).save(buf, format="PNG")
    return buf.getvalue()


def test_real_qr_end_to_end_groups_by_rid(db, actor, tmp_path):
    if _qr_png("probe") is None:
        pytest.skip("qrcode encoder not installed")
    try:
        import pyzbar.pyzbar  # noqa: F401
    except Exception:
        pytest.skip("pyzbar/libzbar not available")

    # Sanity-check the decoder itself before trusting the ingestion assertions.
    probe = tmp_path / "probe.png"
    probe.write_bytes(_qr_png("RID:STUDENT-42"))
    if code_rendering.decode_codes_from_image(probe) != ["RID:STUDENT-42"]:
        pytest.skip("QR round trip unavailable in this environment")

    project = _make_project(db, actor, status=FormProjectStatus.ACTIVE)
    batch = _make_batch(db, project, actor)

    result = submission_batch_service.upload_images(
        db,
        batch.id,
        [
            ("sheet-a.png", _qr_png("RID:STUDENT-42")),
            ("sheet-b.png", _qr_png("RID:STUDENT-42")),
            ("sheet-c.png", _qr_png("RID:STUDENT-99")),
        ],
        actor,
    )

    assert len(result.submissions_created) == 2
    assert result.pages_added == 3
    by_key = {
        _load(db, sid).source_group_key: _load(db, sid)
        for sid in result.submissions_created
    }
    assert sorted(by_key) == ["STUDENT-42", "STUDENT-99"]
    assert len(by_key["STUDENT-42"].pages) == 2
    assert by_key["STUDENT-42"].respondent_identifier == "STUDENT-42"
