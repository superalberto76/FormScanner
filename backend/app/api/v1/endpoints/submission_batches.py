# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan-batch ingestion endpoints.

Routers:
  - projects_router  (mounted at /form-projects)
      POST /{project_id}/submission-batches
      GET  /{project_id}/submission-batches   (newest first, with metrics)
  - router           (mounted at /submission-batches)
      POST  /{batch_id}/pdf-uploads
      POST  /{batch_id}/image-uploads
      POST  /{batch_id}/run-matching
      POST  /{batch_id}/run-extraction
      PATCH /{batch_id}/confidence-threshold
      GET   /{batch_id}          (?status= to list only submissions in one status)
      GET   /{batch_id}/export/summary  (?format=xlsx|csv&csv_dialect=comma|semicolon)
      GET   /{batch_id}/export/detail   (same parameters)

One uploaded PDF = one Submission. Loose images are grouped into submissions
instead, so that upload returns a per-CALL result rather than a single
Submission. The GET is deliberately minimal — just enough to verify an upload
landed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, List

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_db, require_user
from app.models.enums import SubmissionStatus
from app.models.user import User
from app.schemas.submission import (
    ConfidenceThresholdUpdate,
    CsvDialect,
    ExportFormat,
    ExtractionRunSummary,
    SubmissionBatchCreate,
    SubmissionBatchDetail,
    SubmissionBatchIngestResult,
    SubmissionBatchListItem,
    SubmissionBatchMatchingResult,
    SubmissionBatchRead,
    SubmissionRead,
)
from app.services import batch_export_service, export_writers, submission_batch_service

projects_router = APIRouter()
router = APIRouter()


@projects_router.post(
    "/{project_id}/submission-batches",
    response_model=SubmissionBatchRead,
    status_code=status.HTTP_201_CREATED,
)
def create_submission_batch(
    project_id: uuid.UUID,
    data: SubmissionBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Open a scan batch on a project (any member; 409 unless the project is ACTIVE,
    structured 409 ``NOT_SCAN_READY`` unless it is ready for scanning).

    An optional ``confidence_threshold`` in [0, 1] overrides the system default
    for this batch; omitting it (or sending null) means "follow the default".
    The response reports both the override and the threshold actually in force.
    """
    batch = submission_batch_service.create_batch(db, project_id, data, user)
    return submission_batch_service.to_batch_read(batch)


@projects_router.get(
    "/{project_id}/submission-batches",
    response_model=list[SubmissionBatchListItem],
)
def list_submission_batches(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """A project's batches, newest first, each with its metrics.

    The entry point of the Scanning tab. Per batch: submissions by status,
    results still awaiting review and whether it could be exported now — all
    computed on this read, never stored. Any member may list, and the project
    need NOT be ACTIVE: a CLOSED or ARCHIVED project must still show what it
    collected. Unpaginated: a project holds tens of batches, not thousands.
    """
    return submission_batch_service.list_batches_for_project(db, project_id, user)


@router.patch(
    "/{batch_id}/confidence-threshold", response_model=SubmissionBatchRead
)
def update_batch_confidence_threshold(
    batch_id: uuid.UUID,
    data: ConfidenceThresholdUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Move this batch's confidence threshold, retroactively.

    Sets the override to exactly what is sent — ``null`` CLEARS it and returns
    the batch to the system default — then re-derives ``review_required`` across
    the batch's results in the same transaction. Results a human has already
    reviewed are left untouched: their flag records what was true at review
    time and moving the threshold must not rewrite that.

    Any project member may do it, and the project need NOT be ACTIVE: no sheet
    is collected, only already-stored results are re-read, and a concluded batch
    is exactly the one whose results may deserve a second look.

    409 ``EXTRACTION_RUNNING`` (structured ``detail``) while an extraction run
    is in progress on the batch.
    """
    batch = submission_batch_service.update_confidence_threshold(
        db, batch_id, data.confidence_threshold, user
    )
    return submission_batch_service.to_batch_read(batch)


@router.post(
    "/{batch_id}/pdf-uploads",
    response_model=SubmissionRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_submission_pdf(
    batch_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Ingest one multipage PDF as a single submission.

    Every page is rasterised and stored as a SubmissionPage in physical order;
    each page's QR/barcode is read to record where the page claims to sit. A page
    whose code will not decode falls back to the physical order and never blocks
    the rest of the upload. Reads at most max+1 bytes so an oversize upload is
    rejected (413) without buffering the whole file. 409 unless the project is
    ACTIVE; structured 409 ``NOT_SCAN_READY`` unless it is ready for scanning.
    """
    data = await file.read(settings.max_submission_pdf_bytes + 1)
    submission = submission_batch_service.upload_pdf(
        db, batch_id, file.filename or "upload.pdf", data, user
    )
    return SubmissionRead.model_validate(submission)


@router.post(
    "/{batch_id}/image-uploads",
    response_model=SubmissionBatchIngestResult,
    status_code=status.HTTP_201_CREATED,
)
async def upload_submission_images(
    batch_id: uuid.UUID,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Ingest loose scanned images (JPEG/PNG), grouping them into submissions.

    Each image is grouped by its RID code, else by the ``<submission>`` token of
    a filename of the form ``<submission>_page_<n>.<ext>`` (``_pagina_`` is
    accepted as well), else it becomes an UNASSIGNED single-page submission.
    Its position comes from the VID page index, else from the filename's
    ``<n>``, else from its position in this request.

    Order matters, and is preserved: ``List[UploadFile]`` is read straight out of
    Starlette's ``FormData.getlist``, which keeps multipart parts in the order
    they were sent. (This would NOT hold for ``List[bytes]``, which FastAPI reads
    concurrently into a list — hence UploadFile here, deliberately.)

    A file that is too large, empty or not a real JPEG/PNG is reported in
    ``rejected`` and never blocks the others. Each file is read to at most max+1
    bytes so an oversize upload is rejected without buffering all of it. The
    whole request is refused with 409 unless the project is ACTIVE, and with the
    structured 409 ``NOT_SCAN_READY`` unless it is ready for scanning.
    """
    payloads: list[tuple[str, bytes]] = [
        (
            upload.filename or "upload",
            await upload.read(settings.max_submission_image_bytes + 1),
        )
        for upload in files
    ]
    return submission_batch_service.upload_images(db, batch_id, payloads, user)


@router.post("/{batch_id}/run-matching", response_model=SubmissionBatchMatchingResult)
def run_batch_matching(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Re-run variant matching over every not-yet-fully-resolved submission.

    Both upload paths already match what they ingest, in their own transaction;
    this is for afterwards, when the state matching depends on has changed — a
    variant published, a VARIANT_IDENTIFIER role assigned, a bad code corrected.

    Idempotent: nothing already resolved is recomputed or overwritten, so
    pressing it again on a settled batch is a no-op that reports 0.

    Any project member may run it, and the project need NOT be ACTIVE: re-running
    matching stores no new sheet, it only finishes interpreting sheets already
    uploaded, and a batch found to be mismatched after collection closed is
    exactly the one that must still be repairable.
    """
    return submission_batch_service.run_batch_matching(db, batch_id, user)


@router.post("/{batch_id}/run-extraction", response_model=ExtractionRunSummary)
def run_batch_extraction(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Read the answers off every eligible sheet of the batch.

    Explicitly manual, unlike matching, which both upload paths run on their own.
    Matching compares strings already stored; extraction opens every page image
    and reads pixels out of it, so on a batch of hundreds of submissions it is
    minutes of work — something an operator asks for when they are ready, not
    something an upload does to them silently.

    Idempotent: an object already extracted is skipped, never re-read and never
    overwritten, so pressing it twice changes nothing the first press decided.
    That also makes it the way FORWARD — once the MATRIX extractor ships,
    pressing this again on an already-extracted batch picks up exactly the
    objects this build had to leave behind, and touches no existing result.

    Objects this build cannot read yet are reported under
    ``skipped_not_yet_supported``: not an error, and never a reason to stop. Nor
    is a failure — a missing or corrupt page image is counted in ``errors`` with
    its coordinates and the run carries on with the next object.

    Any project member may run it, and the project need NOT be ACTIVE: the same
    reasoning as run-matching, since extraction collects no new sheet and only
    interprets what is already stored.

    One run at a time per batch: 409 ``EXTRACTION_RUNNING`` (structured
    ``detail``) while another run, a "Review anyway" or a threshold change is
    in progress on it.
    """
    return submission_batch_service.run_batch_extraction(db, batch_id, user)


@router.get("/{batch_id}", response_model=SubmissionBatchDetail)
def get_submission_batch(
    batch_id: uuid.UUID,
    # Annotated, so the Python default is a real None rather than a Query object:
    # the filter is optional and "no filter" has to mean None however this
    # function is reached, including when it is called directly.
    status: Annotated[
        SubmissionStatus | None,
        Query(description="Only list submissions in this status (e.g. MATCH_FAILED)."),
    ] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """A batch with its submissions and their page counts (any member or admin).

    ``?status=`` narrows the list to one SubmissionStatus and nothing else: it is
    how an operator pulls up the MATCH_FAILED submissions of a batch to work
    them with the manual-assignment endpoint. Omitting it returns every
    submission, unchanged. A real review queue — cross-batch, paginated, sorted —
    is a review concern and is deliberately not this.
    """
    return submission_batch_service.get_batch_detail(db, batch_id, user, status)


@router.get(
    "/{batch_id}/export/summary",
    responses={
        200: {
            "content": {
                export_writers.XLSX_MEDIA_TYPE: {},
                export_writers.CSV_MEDIA_TYPE: {},
            }
        }
    },
)
def export_batch_summary(
    batch_id: uuid.UUID,
    format: Annotated[ExportFormat, Query(description="xlsx or csv.")],
    csv_dialect: Annotated[
        CsvDialect,
        Query(
            description=(
                "comma: ',' and '.' decimals; semicolon: ';' and ',' decimals. "
                "Ignored with xlsx."
            )
        ),
    ] = CsvDialect.COMMA,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """The Summary export of a batch ready for export, one row per
    submission; any member or admin.

    409 ``NOT_EXPORT_READY`` (structured ``detail``, with the counts by
    reason) on a batch that is not ready. Gate and rows are read in
    one ``REPEATABLE READ`` snapshot (``batch_export_service``).
    """
    batch_export_service.begin_read_snapshot(db)
    export = batch_export_service.export_summary(db, batch_id, user)
    if format is ExportFormat.XLSX:
        content = export_writers.write_summary_xlsx(export.rows)
        media_type = export_writers.XLSX_MEDIA_TYPE
    else:
        content = export_writers.write_summary_csv(export.rows, csv_dialect)
        media_type = export_writers.CSV_MEDIA_TYPE
    filename = export_writers.summary_filename(
        export.batch.name, datetime.now(timezone.utc), format
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": export_writers.content_disposition(filename)},
    )


@router.get(
    "/{batch_id}/export/detail",
    responses={
        200: {
            "content": {
                export_writers.XLSX_MEDIA_TYPE: {},
                export_writers.CSV_MEDIA_TYPE: {},
            }
        }
    },
)
def export_batch_detail(
    batch_id: uuid.UUID,
    format: Annotated[ExportFormat, Query(description="xlsx or csv.")],
    csv_dialect: Annotated[
        CsvDialect,
        Query(
            description=(
                "comma: ',' and '.' decimals; semicolon: ';' and ',' decimals. "
                "Ignored with xlsx."
            )
        ),
    ] = CsvDialect.COMMA,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """The Detail export of a batch ready for export: the Summary export
    columns plus the columns per question; any member or admin.

    Same gate, errors and snapshot as ``export_batch_summary``; the .xlsx has
    a second sheet, "Columns", with what each column is in each variant.
    """
    batch_export_service.begin_read_snapshot(db)
    export = batch_export_service.export_detail_table(db, batch_id, user)
    if format is ExportFormat.XLSX:
        content = export_writers.write_detail_xlsx(export.table)
        media_type = export_writers.XLSX_MEDIA_TYPE
    else:
        content = export_writers.write_detail_csv(export.table, csv_dialect)
        media_type = export_writers.CSV_MEDIA_TYPE
    filename = export_writers.detail_filename(
        export.batch.name, datetime.now(timezone.utc), format
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": export_writers.content_disposition(filename)},
    )
