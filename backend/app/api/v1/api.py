# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from fastapi import APIRouter

from app.api.v1.endpoints import audit, auth, health, users
from app.api.v1.endpoints import (
    form_projects,
    form_variant_export,
    page_objects,
    question_options,
    submission_batches,
    submission_pages,
    submissions,
)
from app.api.v1.endpoints.matrix_defs import (
    cells_router as matrix_cells_router,
    cols_router as matrix_cols_router,
    rows_router as matrix_rows_router,
)
from app.api.v1.endpoints.form_variants import projects_router as form_variants_projects_router, router as form_variants_router

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(audit.router, prefix="/audit", tags=["audit"])
api_router.include_router(users.router, prefix="/users", tags=["users"])

# ---------------------------------------------------------------------------
# FormProject / FormVariant.
# ---------------------------------------------------------------------------
api_router.include_router(
    form_projects.router, prefix="/form-projects", tags=["form-projects"]
)
# form_variants_projects_router mounts variant collection routes at /form-projects
# (GET/POST /{project_id}/variants)
api_router.include_router(
    form_variants_projects_router, prefix="/form-projects", tags=["form-variants"]
)
api_router.include_router(
    form_variants_router, prefix="/form-variants", tags=["form-variants"]
)
# PDF export routes also mount at /form-variants (/{id}/export/pdf, preview-metadata)
api_router.include_router(
    form_variant_export.router, prefix="/form-variants", tags=["form-variants-export"]
)

# ---------------------------------------------------------------------------
# PageObject editor.
# ---------------------------------------------------------------------------
# page_objects.pages_router mounts page-scoped routes at /form-variant-pages
# (GET /{page_id}/objects, POST /{page_id}/objects/{type})
api_router.include_router(
    page_objects.pages_router, prefix="/form-variant-pages", tags=["page-objects"]
)
# page_objects.router mounts object-scoped routes at /page-objects
api_router.include_router(
    page_objects.router, prefix="/page-objects", tags=["page-objects"]
)
api_router.include_router(
    question_options.router, prefix="/question-options", tags=["page-objects"]
)
api_router.include_router(matrix_rows_router, prefix="/matrix-rows", tags=["page-objects"])
api_router.include_router(matrix_cols_router, prefix="/matrix-cols", tags=["page-objects"])
api_router.include_router(matrix_cells_router, prefix="/matrix-cells", tags=["page-objects"])

# ---------------------------------------------------------------------------
# Scanning pipeline ingestion.
# ---------------------------------------------------------------------------
# submission_batches.projects_router mounts the batch collection route at
# /form-projects (POST /{project_id}/submission-batches)
api_router.include_router(
    submission_batches.projects_router,
    prefix="/form-projects",
    tags=["submission-batches"],
)
api_router.include_router(
    submission_batches.router, prefix="/submission-batches", tags=["submission-batches"]
)
# Routes addressing ONE submission rather than a batch (manual variant
# assignment) mount at /submissions.
api_router.include_router(
    submissions.router, prefix="/submissions", tags=["submissions"]
)
# Routes addressing ONE physical sheet (the scanned image the review crops its
# focus panel from) mount at /submission-pages.
api_router.include_router(
    submission_pages.router, prefix="/submission-pages", tags=["submissions"]
)
