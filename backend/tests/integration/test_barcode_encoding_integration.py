# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the BARCODE symbology.

The ``encoding`` chosen in authoring (CODE128, CODE39, EAN13, EAN8, UPCA) must
be the symbology the export prints. Rules covered:

  1. the export prints the chosen symbology, readable at the upload's 150 dpi,
     and the human-readable strip shows what a scanner reads (EAN/UPC with the
     check digit);
  2. the value is validated against the symbology in authoring: 422 on
     create and on update (merged final state);
  3. a role on a BARCODE requires CODE128: 409 when assigning it, and when
     changing the encoding of a barcode whose shared group holds a role;
  4. a stored row (written without the authoring validation) with a value its
     symbology cannot encode is exported as Code128, with a warning in the log.

The per-symbology rules themselves are unit-tested in
``tests/test_code_rendering_barcode.py``. The decoding tests SKIP when the
barcode encoder or pyzbar/libzbar is missing.

Requires TEST_DATABASE_URL — see conftest.py (the suite is skipped without it).
"""
from __future__ import annotations

import io
import logging

import fitz
import pytest
from fastapi import HTTPException
from PIL import Image

from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeEncoding,
    CodeType,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject
from app.repositories import form_variant_role_binding_repo, page_object_repo
from app.schemas.page_object import (
    CodeObjectConfigCreate,
    CodeObjectConfigUpdate,
    CodePageObjectCreate,
)
from app.services import (
    code_object_service,
    form_variant_service,
    page_object_role_service,
    page_object_service,
)
from app.services.form_variant_export_service import ExportMode, export_pdf
from app.services.storage_service import rasterize_submission_pdf

# COLL-MULTI's BARCODE geometry (tests/synthetic/specs.py), with the strip.
BOX = dict(x=404, y=310, width=250, height=70)


# ---------------------------------------------------------------------------
# Builders (self-contained, mirroring test_include_page_index_integration.py)
# ---------------------------------------------------------------------------


def _make_project(db, owner) -> FormProject:
    project = FormProject(owner_id=owner.id, type=FormProjectType.EXAM, name="Barcode IT")
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER))
    db.flush()
    return project


def _setup(db, owner) -> tuple[FormVariant, FormVariantPage]:
    """project (+OWNER member) -> DRAFT variant -> first page."""
    project = _make_project(db, owner)
    variant = FormVariant(
        form_project_id=project.id, root_key="root", name="v1",
        status=FormVariantStatus.DRAFT, version_number=1,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(form_variant_id=variant.id, page_index=0)
    db.add(page)
    db.flush()
    return variant, page


def _create_barcode(db, page_id, user, *, encoding, value, human_readable=True):
    return code_object_service.create_code(
        db,
        page_id,
        CodePageObjectCreate(
            **BOX,
            config=CodeObjectConfigCreate(
                code_type=CodeType.BARCODE, encoding=encoding, value=value,
                human_readable=human_readable,
            ),
        ),
        user,
    )


def _require_toolchain() -> None:
    try:
        import barcode  # noqa: F401
        import pyzbar.pyzbar  # noqa: F401
    except Exception:
        pytest.skip("python-barcode encoder or pyzbar/libzbar not available")


def _decoded(db, variant, user, tmp_path) -> set[tuple[str, str]]:
    """(symbol type, payload) read off page 1 as the upload rasterises it."""
    from pyzbar.pyzbar import decode

    pdf_bytes, _ = export_pdf(db, variant.id, user, ExportMode.STUDENT)
    pdf = tmp_path / "export.pdf"
    pdf.write_bytes(pdf_bytes)
    png = next(iter(rasterize_submission_pdf(pdf)))
    with Image.open(io.BytesIO(png)) as image:
        return {(s.type, s.data.decode()) for s in decode(image.convert("L"))}


def _page_text(db, variant, user) -> str:
    pdf_bytes, _ = export_pdf(db, variant.id, user, ExportMode.STUDENT)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc[0].get_text()


# ---------------------------------------------------------------------------
# 1. the export prints the chosen symbology, readable at 150 dpi
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoding, value, expected",
    [
        (CodeEncoding.CODE128, "LOT-0001", ("CODE128", "LOT-0001")),
        (CodeEncoding.CODE39, "LOT-0001", ("CODE39", "LOT-0001")),
        (CodeEncoding.EAN13, "590123412345", ("EAN13", "5901234123457")),
        (CodeEncoding.EAN8, "9638507", ("EAN8", "96385074")),
        # zbar reports a UPC-A as its EAN-13 form (leading 0).
        (CodeEncoding.UPCA, "03600029145", ("EAN13", "0036000291452")),
    ],
)
def test_export_prints_the_chosen_symbology(db, user, tmp_path, encoding, value, expected):
    _require_toolchain()
    variant, page = _setup(db, user)
    _create_barcode(db, page.id, user, encoding=encoding, value=value)

    assert _decoded(db, variant, user, tmp_path) == {expected}


def test_human_readable_strip_shows_the_check_digit(db, user):
    _require_toolchain()
    variant, page = _setup(db, user)
    _create_barcode(db, page.id, user, encoding=CodeEncoding.EAN13, value="590123412345")

    assert "5901234123457" in _page_text(db, variant, user)


# ---------------------------------------------------------------------------
# 2. value validated against the symbology in authoring (422)
# ---------------------------------------------------------------------------


def test_create_with_a_value_the_symbology_cannot_encode_is_422(db, user):
    _, page = _setup(db, user)

    with pytest.raises(HTTPException) as exc:
        _create_barcode(db, page.id, user, encoding=CodeEncoding.EAN13, value="LOT-0001")

    assert exc.value.status_code == 422
    assert "EAN13" in exc.value.detail
    assert page_object_repo.list_for_page(db, page.id) == []


def test_create_with_a_wrong_check_digit_is_422(db, user):
    _, page = _setup(db, user)

    with pytest.raises(HTTPException) as exc:
        _create_barcode(db, page.id, user, encoding=CodeEncoding.EAN13, value="5901234123450")

    assert exc.value.status_code == 422


def test_update_is_validated_on_the_merged_final_state(db, user):
    _, page = _setup(db, user)
    created = _create_barcode(db, page.id, user, encoding=CodeEncoding.CODE128, value="LOT-0001")

    # Only the encoding changes: the stored value no longer fits it.
    with pytest.raises(HTTPException) as exc:
        code_object_service.update_code_config(
            db, created.id, CodeObjectConfigUpdate(encoding=CodeEncoding.EAN8), user
        )
    assert exc.value.status_code == 422

    # Only the value changes: it does not fit the stored encoding.
    with pytest.raises(HTTPException) as exc:
        code_object_service.update_code_config(
            db, created.id, CodeObjectConfigUpdate(value="LOT 0001 è"), user
        )
    assert exc.value.status_code == 422

    # Both together, consistent: accepted.
    read = code_object_service.update_code_config(
        db, created.id,
        CodeObjectConfigUpdate(encoding=CodeEncoding.EAN8, value="9638507"), user,
    )
    assert read.code_config.encoding is CodeEncoding.EAN8
    assert read.code_config.value == "9638507"


# ---------------------------------------------------------------------------
# 3. a role on a BARCODE requires CODE128 (409)
# ---------------------------------------------------------------------------


def test_role_on_a_non_code128_barcode_is_409(db, user):
    _, page = _setup(db, user)
    created = _create_barcode(db, page.id, user, encoding=CodeEncoding.EAN13, value="590123412345")

    with pytest.raises(HTTPException) as exc:
        page_object_role_service.assign_role(
            db, created.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
        )

    assert exc.value.status_code == 409
    assert "CODE128" in exc.value.detail
    assert form_variant_role_binding_repo.get_by_page_object(db, created.id) is None


def test_role_on_a_code128_barcode_is_accepted(db, user):
    _, page = _setup(db, user)
    created = _create_barcode(db, page.id, user, encoding=CodeEncoding.CODE128, value="S-001")

    read = page_object_role_service.assign_role(
        db, created.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )

    assert read.role is PageObjectRole.RESPONDENT_IDENTIFIER


def test_changing_the_encoding_of_a_role_holder_is_409_also_via_a_sibling(db, user):
    variant, page = _setup(db, user)
    created = _create_barcode(db, page.id, user, encoding=CodeEncoding.CODE128, value="123456")
    page_object_service.share_object(db, created.id, user)
    page2 = form_variant_service.add_page(db, variant.id, user)
    sibling_id = page_object_repo.list_for_page(db, page2.id)[0].id
    page_object_role_service.assign_role(
        db, created.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )

    for object_id in (created.id, sibling_id):
        with pytest.raises(HTTPException) as exc:
            code_object_service.update_code_config(
                db, object_id,
                CodeObjectConfigUpdate(encoding=CodeEncoding.EAN8, value="9638507"), user,
            )
        assert exc.value.status_code == 409, object_id
        assert "CODE128" in exc.value.detail


# ---------------------------------------------------------------------------
# 4. stored rows with an incompatible value: Code128 + warning
# ---------------------------------------------------------------------------


def _unvalidated_barcode(db, page, *, encoding: str | None, value: str) -> PageObject:
    """A BARCODE inserted directly, bypassing the authoring validation."""
    obj = PageObject(
        form_variant_page_id=page.id, object_type=PageObjectType.CODE, **BOX
    )
    db.add(obj)
    db.flush()
    db.add(
        CodeObjectConfig(
            page_object_id=obj.id, code_type=CodeType.BARCODE,
            encoding=encoding, value=value, human_readable=True,
        )
    )
    db.flush()
    return obj


@pytest.mark.parametrize(
    "encoding",
    [
        "EAN13",     # a valid symbology the value does not fit
        "CODE_128",  # a free-text encoding hint, not a CodeEncoding
    ],
)
def test_unvalidated_incompatible_row_is_exported_as_code128_with_a_warning(
    db, user, tmp_path, caplog, encoding
):
    _require_toolchain()
    variant, page = _setup(db, user)
    obj = _unvalidated_barcode(db, page, encoding=encoding, value="LOT-0001")

    with caplog.at_level(logging.WARNING, logger="app.services.form_variant_export_service"):
        decoded = _decoded(db, variant, user, tmp_path)

    assert decoded == {("CODE128", "LOT-0001")}
    assert any(str(obj.id) in record.getMessage() for record in caplog.records)


def test_unvalidated_role_holder_in_another_symbology_keeps_its_tag_in_code128(
    db, user, tmp_path, caplog
):
    """The costly case: a RESPONDENT_IDENTIFIER stored as EAN8.

    Its value alone fits EAN8, but the payload actually printed is tagged
    ("RID:…"): it must come out in Code128 so identification still works.
    (A 12-digit value would make a 16-character Code128 payload, too dense for
    this 250 px box at 150 dpi: a known, still open density limit — not what
    this test is about.)
    """
    _require_toolchain()
    variant, page = _setup(db, user)
    obj = _unvalidated_barcode(db, page, encoding="EAN8", value="9638507")
    db.add(
        FormVariantRoleBinding(
            form_variant_id=variant.id, page_object_id=obj.id,
            role=PageObjectRole.RESPONDENT_IDENTIFIER,
        )
    )
    db.flush()

    with caplog.at_level(logging.WARNING, logger="app.services.form_variant_export_service"):
        decoded = _decoded(db, variant, user, tmp_path)

    assert decoded == {("CODE128", "RID:9638507")}
    assert any(str(obj.id) in record.getMessage() for record in caplog.records)


def test_unvalidated_barcode_without_encoding_counts_as_code128_for_a_role(db, user):
    """A row with no encoding is printed and shown as CODE128."""
    _, page = _setup(db, user)
    obj = _unvalidated_barcode(db, page, encoding=None, value="S-001")

    read = page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )

    assert read.role is PageObjectRole.RESPONDENT_IDENTIFIER
