# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Endpoints acting on ONE physical scanned sheet.

Router:
  - router  (mounted at /submission-pages)
      GET    /{page_id}/image

Its own module and its own prefix because the resource is a SubmissionPage,
not a Submission: the review focus panel asks for the image of the sheet a
reading sits on, and the page id is what the review read hands it. The logic
lives in ``submission_review_service`` beside the review read it serves, and
authorisation goes through the sheet's submission like every other route of
the domain.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.user import User
from app.services import submission_review_service

router = APIRouter()

# The file served for one URL can change — the original until the first run
# aligns the page, then its straightened image, rewritten by a realignment —
# so the browser keeps its copy but revalidates it on every load against the
# ETag; a 304 spares re-downloading a multi-megabyte scan that has not changed.
_PAGE_IMAGE_CACHE_CONTROL = "private, no-cache"


@router.get("/{page_id}/image")
def get_submission_page_image(
    page_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    if_none_match: Annotated[str | None, Header()] = None,
):
    """Serve the WHOLE image of one sheet the extraction read on.

    No server-side cropping or resizing: the FE crops around the selected
    object from the template page size and raw geometry of
    ``GET /submissions/{id}/review``, in proportion. For a page ALIGNED by the
    extraction run this is its straightened image, so the
    crop falls exactly where the reading was taken; for a page not aligned or
    never aligned, the original scan
    (``submission_review_service.get_page_image``).

    The Content-Type is sniffed from the bytes (``application/octet-stream``
    when they are not a supported image). ``nosniff`` so the browser keeps to
    it. A weak ``ETag`` with ``Cache-Control: private, no-cache``; 304 with no
    body when ``If-None-Match`` names it. Any project member, any status,
    CLOSED projects included; 404 "Scan image not found." when the file is
    gone. Read-only: no audit, no lock.
    """
    image = submission_review_service.get_page_image(
        db, page_id, user, if_none_match=if_none_match
    )
    headers = {
        "Cache-Control": _PAGE_IMAGE_CACHE_CONTROL,
        "ETag": image.etag,
        "X-Content-Type-Options": "nosniff",
    }
    if image.content is None:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=image.content, media_type=image.mime, headers=headers)
