# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filesystem storage helpers and page-rendering logic.

Directory layout
----------------
ORIGINAL_UPLOADS_DIR/
    {version_id}_{safe_filename}          ← original source file

RENDERED_PAGES_DIR/
    {version_id}/
        page_0.png
        page_1.png
        ...

SUBMISSION_ORIGINALS_DIR/
    {submission_id}.pdf                   ← uploaded scan batch

SUBMISSION_PAGES_DIR/
    {submission_page_id}.png              ← one rasterised scanned page

All paths stored in the database are **absolute** so the serve endpoint can
validate them without needing to know the config at query time.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from app.core.config import settings

# DPI used when rasterising PDF pages.
# 150 DPI ≈ 1240×1754 px for A4 — good balance of quality vs file size.
PDF_RENDER_DPI: int = 150


@dataclass(frozen=True)
class RenderedPage:
    page_index: int
    file_path: Path   # absolute path on disk
    width_px: int
    height_px: int
    dpi: float | None  # None for raster-image source


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _safe_name(filename: str) -> str:
    """Strip directory components and replace unsafe characters."""
    name = Path(filename).name
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name) or "upload"


def _safe_ext(ext: str) -> str:
    """Normalise an extension to a leading dot + lowercase alphanumerics."""
    return "." + re.sub(r"[^a-zA-Z0-9]", "", ext.lstrip(".")).lower()


def original_path(version_id: uuid.UUID, filename: str) -> Path:
    return Path(settings.original_uploads_dir) / f"{version_id}_{_safe_name(filename)}"


def rendered_dir(version_id: uuid.UUID) -> Path:
    return Path(settings.rendered_pages_dir) / str(version_id)


# ---------------------------------------------------------------------------
# Directory / file operations
# ---------------------------------------------------------------------------


def ensure_dirs(version_id: uuid.UUID) -> None:
    """Create upload and rendered-pages directories (idempotent)."""
    Path(settings.original_uploads_dir).mkdir(parents=True, exist_ok=True)
    rendered_dir(version_id).mkdir(parents=True, exist_ok=True)


def save_original(data: bytes, version_id: uuid.UUID, filename: str) -> Path:
    dest = original_path(version_id, filename)
    dest.write_bytes(data)
    return dest


def delete_version_files(
    version_id: uuid.UUID, original_file_path: str | None
) -> None:
    """Remove original file and all rendered pages for *version_id*.

    Called before re-uploading to a DRAFT version (overwrite behaviour).
    Missing files are silently ignored.
    """
    if original_file_path:
        Path(original_file_path).unlink(missing_ok=True)

    rdir = rendered_dir(version_id)
    if rdir.is_dir():
        for child in rdir.iterdir():
            child.unlink(missing_ok=True)
        try:
            rdir.rmdir()
        except OSError:
            pass  # non-empty after concurrent write — leave it


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_image(src: Path, version_id: uuid.UUID) -> list[RenderedPage]:
    """Normalise a raster image to PNG and return a single RenderedPage."""
    out_dir = rendered_dir(version_id)
    out_path = out_dir / "page_0.png"
    with Image.open(src) as img:
        width, height = img.size
        # Convert to RGB(A) PNG; handles JPEG, BMP, TIFF, etc.
        img.save(str(out_path), format="PNG")
    return [
        RenderedPage(
            page_index=0,
            file_path=out_path,
            width_px=width,
            height_px=height,
            dpi=None,
        )
    ]


def _iter_pdf_pixmaps(src: Path) -> Iterator[fitz.Pixmap]:
    """Yield one PyMuPDF Pixmap per page of *src*, in page order.

    The single rasterisation primitive of this module — every PDF-to-raster path
    goes through it, so DPI/matrix handling is defined in exactly one place.
    Raises ``fitz.FileDataError`` (subclass of RuntimeError) for corrupted PDFs.
    """
    scale = PDF_RENDER_DPI / 72  # PDF user-space is 72 pts/inch
    mat = fitz.Matrix(scale, scale)

    doc = fitz.open(str(src))
    try:
        for i in range(len(doc)):
            yield doc[i].get_pixmap(matrix=mat)
    finally:
        doc.close()


def render_pdf(src: Path, version_id: uuid.UUID) -> list[RenderedPage]:
    """Rasterise every page of a PDF to a PNG file.

    Returns one RenderedPage per PDF page, in page order.
    Raises ``fitz.FileDataError`` (subclass of RuntimeError) for corrupted PDFs.
    """
    out_dir = rendered_dir(version_id)

    pages: list[RenderedPage] = []
    for i, pix in enumerate(_iter_pdf_pixmaps(src)):
        out_path = out_dir / f"page_{i}.png"
        pix.save(str(out_path))
        pages.append(
            RenderedPage(
                page_index=i,
                file_path=out_path,
                width_px=pix.width,
                height_px=pix.height,
                dpi=float(PDF_RENDER_DPI),
            )
        )

    return pages


# ---------------------------------------------------------------------------
# Path traversal protection (used by serve endpoint)
# ---------------------------------------------------------------------------


def assert_within_rendered_dir(image_path: str) -> Path:
    """Resolve *image_path* and verify it is inside RENDERED_PAGES_DIR.

    Returns the resolved Path.
    Raises ``ValueError`` on traversal attempts or paths outside the dir.
    """
    base = Path(settings.rendered_pages_dir).resolve()
    resolved = Path(image_path).resolve()
    # Check with trailing sep to prevent e.g. /data/rendered_pages_evil
    if not str(resolved).startswith(str(base) + "/") and resolved != base:
        raise ValueError(
            f"Image path {image_path!r} is outside the rendered-pages directory"
        )
    return resolved


# ---------------------------------------------------------------------------
# Per-object IMAGE storage
#
# Each IMAGE PageObject owns exactly one file, stored as
#   OBJECT_IMAGES_DIR/{object_id}{ext}
# The on-disk name is ALWAYS derived from the object id (never the client
# filename), so a malicious filename can never influence the path. The absolute
# path is stored on ImageObjectConfig.file_path, consistent with how this module
# stores absolute paths elsewhere; the serve path validates it back against the
# directory to defeat traversal.
# ---------------------------------------------------------------------------


def object_image_path(object_id: uuid.UUID, ext: str) -> Path:
    """Generated, sanitized on-disk path for an object's image (id + extension)."""
    return Path(settings.object_images_dir) / f"{object_id}{_safe_ext(ext)}"


def save_object_image(data: bytes, object_id: uuid.UUID, ext: str) -> Path:
    """Persist *data* as the object's image; return the absolute path written."""
    Path(settings.object_images_dir).mkdir(parents=True, exist_ok=True)
    dest = object_image_path(object_id, ext)
    dest.write_bytes(data)
    return dest


def assert_within_object_images_dir(file_path: str) -> Path:
    """Resolve *file_path* and verify it is inside OBJECT_IMAGES_DIR.

    Raises ``ValueError`` on traversal attempts or paths outside the dir.
    """
    base = Path(settings.object_images_dir).resolve()
    resolved = Path(file_path).resolve()
    if not str(resolved).startswith(str(base) + "/") and resolved != base:
        raise ValueError(
            f"Image path {file_path!r} is outside the object-images directory"
        )
    return resolved


def read_object_image(file_path: str) -> bytes:
    """Read an object image after validating it lives in OBJECT_IMAGES_DIR."""
    return assert_within_object_images_dir(file_path).read_bytes()


def delete_object_image(file_path: str | None) -> None:
    """Remove an object's image file (idempotent; missing file ignored).

    Only deletes inside OBJECT_IMAGES_DIR — a path outside it is ignored rather
    than acted upon, so a corrupt/foreign reference can never unlink arbitrary
    files.
    """
    if not file_path:
        return
    try:
        resolved = assert_within_object_images_dir(file_path)
    except ValueError:
        return
    resolved.unlink(missing_ok=True)


def copy_object_image(src_file_path: str, new_object_id: uuid.UUID, ext: str) -> Path:
    """Duplicate an existing object image for *new_object_id* (used by clone).

    Variants are independent copies, so a cloned IMAGE gets its OWN file rather
    than sharing the source's — deleting one variant never affects the other.
    """
    data = read_object_image(src_file_path)
    return save_object_image(data, new_object_id, ext)


# ---------------------------------------------------------------------------
# Per-user AVATAR storage
#
# Each user owns at most one avatar file, stored as
#   AVATAR_IMAGES_DIR/{user_id}{ext}
# Exactly mirrors the per-object IMAGE block above (server-generated name from
# the id, never the client filename; absolute path stored on the user; serve
# path re-validates against the directory to defeat traversal), but keyed by
# user id and under a SEPARATE directory.
# ---------------------------------------------------------------------------


def avatar_image_path(user_id: uuid.UUID, ext: str) -> Path:
    """Generated, sanitized on-disk path for a user's avatar (id + extension)."""
    return Path(settings.avatar_images_dir) / f"{user_id}{_safe_ext(ext)}"


def save_avatar_image(data: bytes, user_id: uuid.UUID, ext: str) -> Path:
    """Persist *data* as the user's avatar; return the absolute path written."""
    Path(settings.avatar_images_dir).mkdir(parents=True, exist_ok=True)
    dest = avatar_image_path(user_id, ext)
    dest.write_bytes(data)
    return dest


def assert_within_avatar_images_dir(file_path: str) -> Path:
    """Resolve *file_path* and verify it is inside AVATAR_IMAGES_DIR.

    Raises ``ValueError`` on traversal attempts or paths outside the dir.
    """
    base = Path(settings.avatar_images_dir).resolve()
    resolved = Path(file_path).resolve()
    if not str(resolved).startswith(str(base) + "/") and resolved != base:
        raise ValueError(
            f"Avatar path {file_path!r} is outside the avatar-images directory"
        )
    return resolved


def read_avatar_image(file_path: str) -> bytes:
    """Read an avatar image after validating it lives in AVATAR_IMAGES_DIR."""
    return assert_within_avatar_images_dir(file_path).read_bytes()


def delete_avatar_image(file_path: str | None) -> None:
    """Remove a user's avatar file (idempotent; missing file ignored).

    Only deletes inside AVATAR_IMAGES_DIR — a path outside it is ignored rather
    than acted upon, so a corrupt/foreign reference can never unlink arbitrary
    files.
    """
    if not file_path:
        return
    try:
        resolved = assert_within_avatar_images_dir(file_path)
    except ValueError:
        return
    resolved.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Scanned SUBMISSION storage (scanning pipeline ingestion)
#
# One uploaded PDF = one Submission:
#   SUBMISSION_ORIGINALS_DIR/{submission_id}{ext}       ← the PDF as uploaded
#   SUBMISSION_PAGES_DIR/{submission_page_id}.png       ← one rasterised page
#   SUBMISSION_PAGES_DIR/{submission_page_id}_aligned.png ← straightened
# Exactly the same discipline as the per-object IMAGE / per-user AVATAR blocks
# above: the on-disk name is ALWAYS derived from the row id, never from the
# client-supplied filename, so a malicious filename cannot influence the path.
# Absolute paths are stored on the rows, consistent with the rest of this module.
# ---------------------------------------------------------------------------


def submission_original_path(submission_id: uuid.UUID, ext: str) -> Path:
    """Generated, sanitized on-disk path for a submission's original upload."""
    return Path(settings.submission_originals_dir) / f"{submission_id}{_safe_ext(ext)}"


def save_submission_original(data: bytes, submission_id: uuid.UUID, ext: str) -> Path:
    """Persist the uploaded PDF for *submission_id*; return the path written."""
    Path(settings.submission_originals_dir).mkdir(parents=True, exist_ok=True)
    dest = submission_original_path(submission_id, ext)
    dest.write_bytes(data)
    return dest


def submission_page_image_path(
    submission_page_id: uuid.UUID, ext: str = "png"
) -> Path:
    """Generated, sanitized on-disk path for one rasterised submission page."""
    return Path(settings.submission_pages_dir) / f"{submission_page_id}{_safe_ext(ext)}"


def save_submission_page_image(data: bytes, submission_page_id: uuid.UUID) -> Path:
    """Persist a rasterised page image (PNG); return the path written."""
    Path(settings.submission_pages_dir).mkdir(parents=True, exist_ok=True)
    dest = submission_page_image_path(submission_page_id)
    dest.write_bytes(data)
    return dest


def save_submission_page_upload(
    data: bytes, submission_page_id: uuid.UUID, ext: str
) -> Path:
    """Persist an UPLOADED page image verbatim; return the path.

    The loose-image counterpart of :func:`save_submission_page_image`, into the
    same directory and under the same server-generated name — only the extension
    differs, because the bytes keep the format they arrived in.

    Stored AS UPLOADED, with no re-encoding. A PDF has to be rasterised because a
    page is not an image yet; a scanned JPEG/PNG already is one, and normalising
    it to PNG would only cost a lossy JPEG generation and CPU while making the
    stored sheet differ from what the operator sent. The extension comes from
    content sniffing (never the client filename) so it always describes the real
    bytes on disk.
    """
    Path(settings.submission_pages_dir).mkdir(parents=True, exist_ok=True)
    dest = submission_page_image_path(submission_page_id, ext)
    dest.write_bytes(data)
    return dest


def submission_page_aligned_image_path(submission_page_id: uuid.UUID) -> Path:
    """On-disk path of a page's straightened image: fixed, so a new attempt
    overwrites the previous one."""
    return Path(settings.submission_pages_dir) / f"{submission_page_id}_aligned.png"


def save_submission_page_aligned_image(
    data: bytes, submission_page_id: uuid.UUID
) -> Path:
    """Persist a page's straightened image (PNG) next to the original scan,
    which is never touched; return the path written."""
    Path(settings.submission_pages_dir).mkdir(parents=True, exist_ok=True)
    dest = submission_page_aligned_image_path(submission_page_id)
    dest.write_bytes(data)
    return dest


def assert_within_submission_pages_dir(file_path: str) -> Path:
    """Resolve *file_path* and verify it is inside SUBMISSION_PAGES_DIR.

    Raises ``ValueError`` on traversal attempts or paths outside the dir.
    """
    base = Path(settings.submission_pages_dir).resolve()
    resolved = Path(file_path).resolve()
    if not str(resolved).startswith(str(base) + "/") and resolved != base:
        raise ValueError(
            f"Page image path {file_path!r} is outside the submission-pages directory"
        )
    return resolved


def read_submission_page_image(file_path: str) -> bytes:
    """Read a scanned page image after validating it lives in SUBMISSION_PAGES_DIR.

    The read counterpart of :func:`save_submission_page_image` /
    :func:`save_submission_page_upload`, and the exact analogue of
    :func:`read_object_image` and :func:`read_avatar_image` for this third
    domain. Extraction reads sheets through here rather than
    opening ``SubmissionPage.source_file_path`` directly, so a corrupted or
    hand-edited path on a row can never make the extractor read a file outside
    the storage directory.
    """
    return assert_within_submission_pages_dir(file_path).read_bytes()


def rasterize_submission_pdf(src: Path) -> Iterator[bytes]:
    """Yield one PNG (as bytes) per page of the uploaded PDF at *src*, in order.

    Same rasterisation as ``render_pdf`` (shared ``_iter_pdf_pixmaps``: PyMuPDF,
    PDF_RENDER_DPI, Matrix, get_pixmap) but it hands the bytes back instead of
    choosing a path, because a submission page's filename is its SubmissionPage
    id — which only exists once the row has been created. The caller writes each
    page with ``save_submission_page_image``.

    Raises ``fitz.FileDataError`` (subclass of RuntimeError) for corrupted or
    non-PDF input; the caller maps that to a 4xx.
    """
    for pix in _iter_pdf_pixmaps(src):
        yield pix.tobytes("png")
