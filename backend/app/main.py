# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router
from app.core.config import settings
from app.services.free_text_extraction_service import check_ocr_language_at_startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Before the first request: the configured OCR language is installed
    (the backend does not start otherwise), and the required
    storage directories exist."""
    check_ocr_language_at_startup()
    Path(settings.original_uploads_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.rendered_pages_dir).mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in settings.cors_origins.split(',') if x.strip()],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(api_router, prefix='/api/v1')
