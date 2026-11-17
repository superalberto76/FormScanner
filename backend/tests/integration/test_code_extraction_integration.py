# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for role-less CODE extraction.

Synthetic scans generated with Pillow carrying REAL QR codes (``qrcode``),
decoded by real pyzbar through ``extract_code_result``. What this suite pins
down:

  * **a readable code** — decoded, its raw payload stored, no confidence, no
    review;
  * **no code where expected** — a failed decode, forced to review whatever the
    batch threshold, the most permissive included;
  * **matching by position** — the whole page is decoded, and ONLY the symbol
    near the object's expected region is attributed to it: a VARIANT_IDENTIFIER
    printed on the same page is never mistaken for it, and a readable symbol
    too far away counts as a failed decode for this object;
  * **preconditions** — a role-carrying CODE, an unmatched page and an object
    with no ``code_config`` each raise;
  * **result_type** — CODE.

The real-QR tests SKIP when the qrcode encoder or pyzbar/libzbar is missing,
like the ingestion end-to-end tests. Skipped without TEST_DATABASE_URL, like the
rest of tests/integration.
"""
from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image

from app.core.config import settings
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
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
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import code_extraction_service as svc
from app.utils.code_rendering import build_tagged_payload

# The template page these tests author on: A4 @ 96 DPI, the export's default.
TEMPLATE_W = 794
TEMPLATE_H = 1123

# Scans are drawn at twice the template's resolution, so the mapping is not
# the identity.
SCALE = 2.0

# Where the role-less CODE is authored (template pixels).
CODE_X, CODE_Y, CODE_SIZE = 500.0, 400.0, 100.0
# Where the VARIANT_IDENTIFIER CODE is authored: the opposite corner.
VID_X, VID_Y, VID_SIZE = 60.0, 60.0, 100.0

CODE_VALUE = "CONTENUTO-42"
VID_VALUE = "VAR-A"


def _require_qr_toolchain() -> None:
    try:
        import qrcode  # noqa: F401
        import pyzbar.pyzbar  # noqa: F401
    except Exception:
        pytest.skip("qrcode encoder or pyzbar/libzbar not available")


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
        email=f"code-extract-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    p = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Code extraction project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(p)
    db.flush()
    db.add(
        FormProjectMember(project_id=p.id, user_id=actor.id, role=ProjectRole.OWNER)
    )
    db.flush()
    return p


@pytest.fixture
def template_page(db, project, actor) -> FormVariantPage:
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(
        form_variant_id=variant.id,
        page_index=1,
        width_px=TEMPLATE_W,
        height_px=TEMPLATE_H,
    )
    db.add(page)
    db.flush()
    return page


def _make_code(
    db,
    template_page: FormVariantPage,
    *,
    x: float = CODE_X,
    y: float = CODE_Y,
    size: float = CODE_SIZE,
    height: float | None = None,
    value: str = CODE_VALUE,
    role: PageObjectRole | None = None,
    with_config: bool = True,
) -> PageObject:
    """A CODE *size* wide and *height* high (square when *height* is None)."""
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.CODE,
        x=x,
        y=y,
        width=size,
        height=size if height is None else height,
    )
    db.add(obj)
    db.flush()
    if with_config:
        db.add(
            CodeObjectConfig(page_object_id=obj.id, code_type=CodeType.QR, value=value)
        )
        db.flush()
    if role is not None:
        db.add(
            FormVariantRoleBinding(
                form_variant_id=template_page.form_variant_id,
                page_object_id=obj.id,
                role=role,
            )
        )
        db.flush()
    db.refresh(obj)
    return obj


def _make_batch(db, project, actor, *, threshold: float | None = None):
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        confidence_threshold=threshold,
    )
    db.add(batch)
    db.flush()
    return batch


def _draw_scan(codes: list[tuple[str, float, float, float]] = ()) -> bytes:
    """A synthetic scan of the template page, as PNG bytes: white paper with one
    real QR per ``(payload, x, y, size)`` — template coordinates — printed to
    fill that square, as the export would."""
    img = Image.new("L", (int(TEMPLATE_W * SCALE), int(TEMPLATE_H * SCALE)), 255)
    if codes:
        import qrcode

        for payload, x, y, size in codes:
            side = int(size * SCALE)
            qr = qrcode.make(payload).convert("L").resize(
                (side, side), Image.NEAREST
            )
            img.paste(qr, (int(x * SCALE), int(y * SCALE)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_scanned_page(
    db,
    pages_dir,
    batch,
    template_page: FormVariantPage,
    *,
    scan: bytes,
    matched: bool = True,
) -> SubmissionPage:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=SubmissionStatus.MATCHED,
    )
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    path = pages_dir / f"{page_id}.png"
    path.write_bytes(scan)
    page = SubmissionPage(
        id=page_id,
        submission_id=submission.id,
        form_variant_page_id=template_page.id if matched else None,
        source_file_path=str(path),
        source_filename="scan.png",
        source_page_order=0,
    )
    db.add(page)
    db.flush()
    return page


# ---------------------------------------------------------------------------
# A readable code
# ---------------------------------------------------------------------------
def test_a_readable_code_is_decoded_and_needs_no_review(
    db, pages_dir, project, actor, template_page
):
    _require_qr_toolchain()
    obj = _make_code(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(CODE_VALUE, CODE_X, CODE_Y, CODE_SIZE)]),
    )

    result = svc.extract_code_result(db, page, obj, batch)

    assert result.detected_payload_json == {
        "decoded_value": CODE_VALUE,
        "decode_successful": True,
    }
    assert result.confidence_score is None
    assert result.validity_forces_review is False
    assert result.review_required is False
    assert result.result_type is SubmissionResultType.CODE
    assert result.submission_page_id == page.id
    assert result.page_object_id == obj.id


@pytest.mark.parametrize("threshold", [0.0, 0.5, 1.0, None])
def test_a_decoded_code_needs_no_review_whatever_the_threshold(
    db, pages_dir, project, actor, template_page, threshold
):
    """A NULL confidence never contributes: even the strictest threshold (1.0)
    does not send a successful decode to review."""
    _require_qr_toolchain()
    obj = _make_code(db, template_page)
    batch = _make_batch(db, project, actor, threshold=threshold)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(CODE_VALUE, CODE_X, CODE_Y, CODE_SIZE)]),
    )

    result = svc.extract_code_result(db, page, obj, batch)

    assert result.review_required is False


# ---------------------------------------------------------------------------
# No code where expected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("threshold", [0.0, 0.5, 1.0, None])
def test_no_code_on_the_page_is_a_failed_decode_forced_to_review(
    db, pages_dir, project, actor, template_page, threshold
):
    """0.0 is the most permissive threshold there is: review is still required,
    because it comes from ``validity_forces_review`` through the shared
    ``compute_review_required``, not from the (NULL) confidence."""
    obj = _make_code(db, template_page)
    batch = _make_batch(db, project, actor, threshold=threshold)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_code_result(db, page, obj, batch)

    assert result.detected_payload_json == {
        "decoded_value": None,
        "decode_successful": False,
    }
    assert result.confidence_score is None
    assert result.validity_forces_review is True
    assert result.review_required is True
    assert result.result_type is SubmissionResultType.CODE


# ---------------------------------------------------------------------------
# Matching by position — the heart of role-less CODE extraction
# ---------------------------------------------------------------------------
def test_only_the_symbol_near_the_expected_region_is_attributed(
    db, pages_dir, project, actor, template_page
):
    """Two codes on one page: the VARIANT_IDENTIFIER (consumed by matching,
    never extracted) and the role-less one. The whole page is decoded, so BOTH
    symbols are found — and the extractor must take the one printed where the
    role-less CODE was authored, not the other."""
    _require_qr_toolchain()
    _make_code(
        db, template_page, x=VID_X, y=VID_Y, size=VID_SIZE, value=VID_VALUE,
        role=PageObjectRole.VARIANT_IDENTIFIER,
    )
    content = _make_code(db, template_page)
    vid_payload = build_tagged_payload(PageObjectRole.VARIANT_IDENTIFIER, VID_VALUE, 1)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([
            (vid_payload, VID_X, VID_Y, VID_SIZE),
            (CODE_VALUE, CODE_X, CODE_Y, CODE_SIZE),
        ]),
    )

    result = svc.extract_code_result(db, page, content, batch)

    assert result.detected_payload_json == {
        "decoded_value": CODE_VALUE,
        "decode_successful": True,
    }
    assert result.review_required is False


def test_two_role_less_codes_each_read_their_own_symbol(
    db, pages_dir, project, actor, template_page
):
    _require_qr_toolchain()
    first = _make_code(db, template_page, x=VID_X, y=VID_Y, value="FIRST")
    second = _make_code(db, template_page, value="SECOND")
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([
            ("FIRST", VID_X, VID_Y, CODE_SIZE),
            ("SECOND", CODE_X, CODE_Y, CODE_SIZE),
        ]),
    )

    assert svc.extract_code_result(db, page, first, batch).detected_payload_json[
        "decoded_value"
    ] == "FIRST"
    assert svc.extract_code_result(db, page, second, batch).detected_payload_json[
        "decoded_value"
    ] == "SECOND"


def test_a_readable_symbol_too_far_away_is_a_failed_decode_for_this_object(
    db, pages_dir, project, actor, template_page
):
    """The symbol exists and decodes — it is simply not where this object was
    authored. With its centre outside the region widened by
    ``MATCH_MARGIN_FACTOR`` it is never attributed, even as the page's only
    code."""
    _require_qr_toolchain()
    obj = _make_code(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(CODE_VALUE, VID_X, VID_Y, CODE_SIZE)]),
    )
    # The premise: that symbol's centre really is outside the widened region.
    assert VID_X + CODE_SIZE / 2 < CODE_X - svc.MATCH_MARGIN_FACTOR * CODE_SIZE

    result = svc.extract_code_result(db, page, obj, batch)

    assert result.detected_payload_json == {
        "decoded_value": None,
        "decode_successful": False,
    }
    assert result.validity_forces_review is True
    assert result.review_required is True


# Regression geometry from COLL-MULTI A, page 2: the role-less BARCODE
# (404,310, 250x70) sits ~1.24 of its diagonals from the VID QR (674,40,
# 80x80). With its own symbol unreadable, a nearest-within-1.5-diagonals rule
# handed it the VID.
COLL_BARCODE = (404.0, 310.0, 250.0, 70.0)
COLL_VID = (674.0, 40.0, 80.0)


def test_a_covered_code_never_takes_the_nearby_variant_identifier(
    db, pages_dir, project, actor, template_page
):
    """The code is covered, so its own symbol is missing — the VID
    printed nearby must not be read in its place: a failed decode, in review."""
    _require_qr_toolchain()
    vid_x, vid_y, vid_size = COLL_VID
    _make_code(
        db, template_page, x=vid_x, y=vid_y, size=vid_size, value=VID_VALUE,
        role=PageObjectRole.VARIANT_IDENTIFIER,
    )
    code_x, code_y, code_w, code_h = COLL_BARCODE
    obj = _make_code(db, template_page, x=code_x, y=code_y, size=code_w, height=code_h)
    vid_payload = build_tagged_payload(PageObjectRole.VARIANT_IDENTIFIER, VID_VALUE, 2)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(vid_payload, vid_x, vid_y, vid_size)]),
    )

    result = svc.extract_code_result(db, page, obj, batch)

    assert result.detected_payload_json == {
        "decoded_value": None,
        "decode_successful": False,
    }
    assert result.validity_forces_review is True
    assert result.review_required is True


def test_a_covered_code_never_takes_a_neighbouring_role_less_code(
    db, pages_dir, project, actor, template_page
):
    """Between two role-less codes: the neighbour's symbol is within
    1.5 diagonals of this one but outside its widened region."""
    _require_qr_toolchain()
    covered = _make_code(db, template_page, value="FIRST")
    neighbour_x = CODE_X + CODE_SIZE + 60.0
    neighbour = _make_code(db, template_page, x=neighbour_x, value="SECOND")
    # The premise: the neighbour's centre is within a 1.5-diagonal radius
    # (same row, so the distance is along x) but beyond the widened region.
    neighbour_centre_x = neighbour_x + CODE_SIZE / 2
    distance = neighbour_centre_x - (CODE_X + CODE_SIZE / 2)
    assert distance <= 1.5 * (2 ** 0.5) * CODE_SIZE
    assert neighbour_centre_x > CODE_X + CODE_SIZE * (1 + svc.MATCH_MARGIN_FACTOR)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([("SECOND", neighbour_x, CODE_Y, CODE_SIZE)]),
    )

    assert svc.extract_code_result(db, page, covered, batch).detected_payload_json == {
        "decoded_value": None,
        "decode_successful": False,
    }
    assert svc.extract_code_result(db, page, neighbour, batch).detected_payload_json[
        "decoded_value"
    ] == "SECOND"


def test_a_tagged_symbol_inside_the_margin_is_never_attributed(
    db, pages_dir, project, actor, template_page
):
    """A VID: / RID: payload never belongs to a role-less CODE,
    even printed right next to it, within its widened region."""
    _require_qr_toolchain()
    obj = _make_code(db, template_page)
    vid_size = 60.0
    vid_x = CODE_X + CODE_SIZE + 10.0
    # The premise: the VID's centre is inside the widened region.
    assert vid_x + vid_size / 2 < CODE_X + CODE_SIZE * (1 + svc.MATCH_MARGIN_FACTOR)
    vid_payload = build_tagged_payload(PageObjectRole.VARIANT_IDENTIFIER, VID_VALUE, 1)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan([(vid_payload, vid_x, CODE_Y, vid_size)]),
    )

    result = svc.extract_code_result(db, page, obj, batch)

    assert result.detected_payload_json == {
        "decoded_value": None,
        "decode_successful": False,
    }
    assert result.review_required is True


def test_a_slightly_offset_symbol_is_still_attributed(
    db, pages_dir, project, actor, template_page
):
    """What decoding the whole page buys over cropping: a code printed half
    outside the mapped rectangle (a shifted sheet) is still read."""
    _require_qr_toolchain()
    obj = _make_code(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            [(CODE_VALUE, CODE_X + CODE_SIZE / 2, CODE_Y + CODE_SIZE / 2, CODE_SIZE)]
        ),
    )

    result = svc.extract_code_result(db, page, obj, batch)

    assert result.detected_payload_json["decoded_value"] == CODE_VALUE


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "role", [PageObjectRole.VARIANT_IDENTIFIER, PageObjectRole.RESPONDENT_IDENTIFIER]
)
def test_a_role_carrying_code_is_refused(
    db, pages_dir, project, actor, template_page, role
):
    obj = _make_code(db, template_page, role=role)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    with pytest.raises(ValueError, match=role.value):
        svc.extract_code_result(db, page, obj, batch)


def test_an_unmatched_page_is_refused(db, pages_dir, project, actor, template_page):
    obj = _make_code(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan(), matched=False
    )

    with pytest.raises(ValueError, match="not matched"):
        svc.extract_code_result(db, page, obj, batch)


def test_an_object_without_code_config_is_refused(
    db, pages_dir, project, actor, template_page
):
    obj = _make_code(db, template_page, with_config=False)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    with pytest.raises(ValueError, match="code_config"):
        svc.extract_code_result(db, page, obj, batch)
