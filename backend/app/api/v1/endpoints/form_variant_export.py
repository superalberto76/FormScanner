# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FormVariant PDF export endpoints.

Mounted at /form-variants:
  GET /form-variants/{variant_id}/export/pdf?mode=STUDENT|SOLUTION[&filename=]
  GET /form-variants/{variant_id}/export/preview-metadata

Read-only; owner or admin; any status (DRAFT included).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.user import User
from app.services import form_variant_export_service
from app.services.form_variant_export_service import ExportMode

router = APIRouter()


@router.get(
    "/{variant_id}/export/pdf",
    responses={200: {"content": {"application/pdf": {}}}},
)
def export_variant_pdf(
    variant_id: uuid.UUID,
    mode: ExportMode = Query(
        ExportMode.STUDENT,
        description="STUDENT (blank) or SOLUTION (correct answers marked).",
    ),
    filename: str | None = Query(
        None, description="Optional download filename override (sanitised)."
    ),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Export the variant as a PDF in STUDENT or SOLUTION mode."""
    pdf_bytes, download_name = form_variant_export_service.export_pdf(
        db, variant_id, user, mode, filename
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@router.get("/{variant_id}/export/preview-metadata")
def export_preview_metadata(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Cheap render metadata (page sizes, per-type object counts) for debugging."""
    return form_variant_export_service.get_preview_metadata(db, variant_id, user)
