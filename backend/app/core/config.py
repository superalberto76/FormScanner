# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
import re

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# One Tesseract language code (``eng``, ``ita``, ``chi_sim``), or several
# joined with ``+`` (``ita+eng``). Lowercase letters and underscores only: the
# names ``pytesseract.get_languages`` reports (it drops any other line of
# ``tesseract --list-langs``), so a valid value can always pass the startup
# check. Matched with fullmatch: ``$`` would let a trailing newline through.
OCR_LANGUAGE_PATTERN = re.compile(r'[a-z_]+(\+[a-z_]+)*')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'FormScanner'
    app_env: str = 'development'
    app_debug: bool = True
    cors_origins: str = 'http://localhost:8080'

    backend_host: str = '0.0.0.0'
    backend_internal_port: int = 8000

    jwt_secret: str = 'change_me'
    jwt_algorithm: str = 'HS256'
    # Lifetime of an access token. Read from ACCESS_TOKEN_EXPIRE_MINUTES, the
    # documented name; ACCESS_TOKEN_EXPIRES_MINUTES is still accepted so an
    # environment that already sets it keeps working. When both are set, the
    # documented name wins.
    access_token_expires_minutes: int = Field(
        default=480,
        validation_alias=AliasChoices(
            'ACCESS_TOKEN_EXPIRE_MINUTES', 'ACCESS_TOKEN_EXPIRES_MINUTES'
        ),
    )

    database_url: str = 'postgresql+psycopg://formscanner:change_me_db_password@db:5432/formscanner'

    storage_path: str = '/data/storage'
    original_uploads_dir: str = '/data/storage/originals'
    rendered_pages_dir: str = '/data/storage/rendered_pages'
    # Per-object uploaded images: one file per IMAGE PageObject,
    # named by object id (never by the client filename).
    object_images_dir: str = '/data/storage/object_images'
    # Per-user profile avatars: one file per user, named by
    # user id (never by the client filename). Kept separate from object_images_dir.
    avatar_images_dir: str = '/data/storage/avatar_images'
    # Hard cap for an IMAGE upload; oversize is rejected (413). 10 MiB default.
    # Reused for avatars (same limits as object images) — no separate avatar
    # size setting.
    max_image_upload_bytes: int = 10 * 1024 * 1024
    # Scanning pipeline. One uploaded PDF = one submission: the
    # original file is kept under submission_originals_dir named by Submission
    # id, and every rasterised page under submission_pages_dir named by
    # SubmissionPage id — never by the client filename, as for object images.
    submission_originals_dir: str = '/data/storage/submission_originals'
    submission_pages_dir: str = '/data/storage/submission_pages'
    # Hard cap for a scanned-batch PDF upload; oversize is rejected (413). A
    # multipage scan is far larger than a single image, hence its own setting.
    max_submission_pdf_bytes: int = 50 * 1024 * 1024
    # Hard cap per FILE for a scanned-batch loose-image upload;
    # oversize is rejected without blocking the other files in the request.
    # Its own setting rather than a reuse of max_image_upload_bytes: that one
    # caps an authoring asset placed on a template (10 MiB is generous there),
    # whereas this caps a scanner's output — a 300 DPI colour A4 scan runs to
    # tens of MB. Well under max_submission_pdf_bytes, which covers a whole
    # multipage submission rather than one sheet.
    max_submission_image_bytes: int = 20 * 1024 * 1024

    # Scanning pipeline. A result whose aggregated confidence falls BELOW this
    # needs a human. System-wide default: a SubmissionBatch may override it,
    # and reading it directly is almost always wrong — go through
    # confidence_threshold_service.effective_threshold.
    default_confidence_threshold: float = 0.7

    # Language of the OCR that reads FREE_TEXT_AREA objects.
    # Tesseract codes as they are, combinable with '+' (``ita+eng``). Every
    # component must be installed next to the binary: the backend refuses to
    # start otherwise (free_text_extraction_service.check_ocr_language_at_startup).
    ocr_language: str = 'eng'

    initial_admin_email: str = 'admin@example.com'
    initial_admin_password: str = 'ChangeMe!'
    force_reset_admin_password: bool = False

    @field_validator('ocr_language')
    @classmethod
    def _validate_ocr_language(cls, value: str) -> str:
        if not OCR_LANGUAGE_PATTERN.fullmatch(value):
            raise ValueError(
                f"OCR_LANGUAGE {value!r} is not a Tesseract language code: use codes "
                "such as 'eng', 'ita' or 'chi_sim', combined with '+' (e.g. 'ita+eng')"
            )
        return value


settings = Settings()
