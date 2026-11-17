# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic filled-in sheets for the scanning pipeline.

Given a COLL-* project, a variant and a DECLARATIVE description of how the
sheet was filled in (``SheetFill``), produce the "scanned" submission — a PDF or
loose images — to feed to the app's real upload path.

The page is the variant's own STUDENT export, rasterised in greyscale; the
marks are drawn where ``template_geometry`` found the printed bubbles, never
where the layout modules say they should be (see that module for why). Then
the usual scanner artefacts: per-mark jitter, page scale and rotation, noise,
blur.

Determinism: every random choice comes from a ``random.Random`` seeded from
``options.seed`` and WHAT it is for — ``"{seed}:{page}:{label}:{key}"`` for a
mark's jitter and LIGHT speckles (key = letter, or ``"r,c"`` for a cell),
``"{seed}:{page}:noise"`` for a page's noise. Same project, fill and options
give the same pixels — and, through ``to_pdf``, the same bytes. Independent
streams also mean that adding, removing or restyling one mark leaves every
other mark (and the noise) exactly where it was, so a test can vary one thing
at a time. String seeds are hashed with SHA-512 by ``random``, so they do not
depend on PYTHONHASHSEED.
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePath
from typing import TYPE_CHECKING, Sequence

import fitz
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from app.models.enums import PageObjectType
from app.models.user import User
from app.services.form_variant_export_service import ExportMode, export_pdf
from tests.synthetic.template_geometry import (
    PX_TO_PT,
    ObjectGeometry,
    TemplateGeometry,
    objects_by_page,
    read_template,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from tests.synthetic.builders import CollProject


# ---------------------------------------------------------------------------
# Mark styles — sizes relative to the radius r of the printed bubble
# ---------------------------------------------------------------------------


class MarkStyle(Enum):
    FULL = "FULL"    # a properly filled bubble
    LIGHT = "LIGHT"  # a pale, patchy pencil fill
    HALF = "HALF"    # only the lower half filled
    CROSS = "CROSS"  # an X instead of a fill
    RING = "RING"    # a circle drawn AROUND the bubble, inside left empty


INK = 25  # grey level of a dark pen/pencil mark

FULL_RADIUS = 1.15      # FULL: disc of 1.15 r, INK
# LIGHT: disc of 1.15 r — NOT 1.0 r. A disc exactly as large as the
# extractor's sampling disc loses a sliver of it to every jitter, which would
# make the measured ratio swing with the jitter on top of the speckle
# randomness; 0.15 r of overhang (~1.2 px at 150 dpi) absorbs ±1 px of shift.
LIGHT_RADIUS = 1.15
LIGHT_GREY = 115        # … grey 115 …
LIGHT_COVERAGE = 0.88   # … each pixel drawn with this probability (mark rng)
HALF_RADIUS = 1.15      # HALF: lower half of a disc of 1.15 r, INK
CROSS_HALF_SPAN = 1.0   # CROSS: two diagonals from −r to +r …
CROSS_WIDTH = 0.12      # … 0.12 r thick (2 px at 300 dpi), INK
RING_RADIUS = 1.6       # RING: circle of 1.6 r …
RING_WIDTH = 0.2        # … 0.2 r thick, INK

# Handwriting stand-in for FREE_TEXT_AREA answers.
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TEXT_MARGIN_PX = 8           # output px, inside the printed answer box
TEXT_SINGLE_LINE_RATIO = 0.4  # body = 40% of the box height (one-line boxes: NOME)
TEXT_TALL_SIZE_PX = 28       # body for tall boxes (APERTA), output px
TEXT_TALL_BOX_PT = 72        # a box taller than one inch is "tall"
TEXT_LINE_SPACING = 1.2

COVER_MARGIN_PX = 4  # template px around a covered CODE
BLUR_RADIUS = 0.5
NOISE_TILE = 256

JPEG_QUALITY = 90


# ---------------------------------------------------------------------------
# What was filled in
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SheetFill:
    # "Q01": ("B",) — letters as PRINTED on this variant.
    answers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # "M-RIGHE": ((1, 0), (1, 2)) — (row, column), 0-based.
    cells: dict[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    # "NOME": "Mario Rossi"
    texts: dict[str, str] = field(default_factory=dict)
    # (label, letter | (row, col)) → style of that single mark.
    styles: dict[tuple[str, object], MarkStyle] = field(default_factory=dict)
    default_style: MarkStyle = MarkStyle.FULL


def digits(*columns: str | tuple[str, ...] | None) -> tuple[tuple[int, int], ...]:
    """MATRICOLA cells, one column per (expanded) argument.

    * ``"100037"`` — one column per character;
    * ``("0", "3")`` — ONE column holding several marks (ambiguous, F09);
    * ``None`` — one empty column (F10).

    A cell is (row = digit value, column = index).
    """
    cells: list[tuple[int, int]] = []
    col = 0
    for arg in columns:
        if arg is None:
            col += 1
        elif isinstance(arg, tuple):
            cells.extend((int(d), col) for d in arg)
            col += 1
        else:
            for d in arg:
                cells.append((int(d), col))
                col += 1
    return tuple(cells)


@dataclass(frozen=True)
class SheetOptions:
    seed: int
    dpi: int = 300
    jitter_px: float = 2.0      # random shift of each mark, output px, per axis
    scale: float = 1.0          # page scale around its centre
    rotation_deg: float = 0.0
    noise_sigma: float = 4.0    # 0 = no noise
    blur: bool = True           # GaussianBlur(0.5)
    pages: tuple[int, ...] | None = None    # subset / order of template pages
    extra_pages: tuple[Image.Image, ...] = ()  # already generated, appended
    cover: tuple[tuple[str, tuple[int, ...]], ...] = ()  # ("VID", (2,))


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedSheet:
    page_images: tuple[Image.Image, ...]  # mode L, A4 at dpi
    page_numbers: tuple[int | None, ...]  # template page of each image; None = extra
    dpi: int
    # PDF page size of the export (pt), reused by to_pdf.
    page_size_pt: tuple[float, float] = (595.5, 842.25)

    def to_pdf(self) -> bytes:
        """One A4 page per image, the image as JPEG q90 filling it.

        Byte-for-byte reproducible: no metadata, no fresh /ID.
        """
        width, height = self.page_size_pt
        doc = fitz.open()
        try:
            for image in self.page_images:
                page = doc.new_page(width=width, height=height)
                page.insert_image(page.rect, stream=_encode(image, "JPEG"))
            doc.set_metadata({})
            return doc.tobytes(garbage=3, deflate=True, no_new_id=True)
        finally:
            doc.close()

    def to_images(self, names: Sequence[str]) -> list[tuple[str, bytes]]:
        """``(name, bytes)`` per image, PNG or JPEG q90 by the name's extension —
        the shape ``submission_batch_service.upload_images`` takes."""
        if len(names) != len(self.page_images):
            raise ValueError(
                f"{len(names)} names for {len(self.page_images)} page images"
            )
        out = []
        for name, image in zip(names, self.page_images):
            ext = PurePath(name).suffix.lower()
            if ext == ".png":
                fmt = "PNG"
            elif ext in (".jpg", ".jpeg"):
                fmt = "JPEG"
            else:
                raise ValueError(f"{name}: unsupported extension {ext!r}")
            out.append((name, _encode(image, fmt)))
        return out


def _encode(image: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        image.save(buf, format="JPEG", quality=JPEG_QUALITY)
    else:
        image.save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def export_template(
    db: Session, project: CollProject, variant_key: str
) -> tuple[bytes, TemplateGeometry]:
    """The variant's STUDENT export and the geometry read off it."""
    variant = project.variants[variant_key]
    owner = db.get(User, project.owner_id)
    pdf_bytes, _ = export_pdf(db, variant.variant_id, owner, ExportMode.STUDENT)
    geometry = read_template(
        pdf_bytes,
        objects_by_page(db, variant.variant_id),
        variant=f"{project.code}-{variant_key}",
    )
    return pdf_bytes, geometry


def generate(
    db: Session,
    project: CollProject,
    variant_key: str,
    fill: SheetFill,
    options: SheetOptions,
) -> GeneratedSheet:
    pdf_bytes, geometry = export_template(db, project, variant_key)
    _validate(geometry, fill, options)

    k = options.dpi / 72  # pt → output px

    images: dict[int, Image.Image] = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, pdf_page in enumerate(doc):
            number = index + 1
            pix = pdf_page.get_pixmap(matrix=fitz.Matrix(k, k), colorspace=fitz.csGRAY)
            image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            page_geom = geometry.pages[number]

            draw = ImageDraw.Draw(image)
            for label, letters in fill.answers.items():
                obj = page_geom.objects.get(label)
                if obj is not None:
                    for key in letters:
                        _mark(image, draw, obj, number, label, key, fill, options, k)
            for label, cells in fill.cells.items():
                obj = page_geom.objects.get(label)
                if obj is not None:
                    for key in cells:
                        _mark(image, draw, obj, number, label, tuple(key), fill, options, k)
            for label, text in fill.texts.items():
                obj = page_geom.objects.get(label)
                if obj is not None:
                    _write_text(draw, obj, text, k)
            for label, cover_pages in options.cover:
                if number in cover_pages:
                    x0, y0, x1, y1 = page_geom.objects[label].rect
                    m = COVER_MARGIN_PX * PX_TO_PT
                    draw.rectangle(
                        ((x0 - m) * k, (y0 - m) * k, (x1 + m) * k, (y1 + m) * k),
                        fill=255,
                    )

            image = _scanner_artefacts(
                image, options, random.Random(f"{options.seed}:{number}:noise")
            )
            images[number] = image

    numbers = options.pages if options.pages is not None else tuple(sorted(images))
    page_images = tuple(images[n] for n in numbers) + tuple(options.extra_pages)
    page_numbers = tuple(numbers) + (None,) * len(options.extra_pages)
    first = geometry.pages[min(geometry.pages)]
    return GeneratedSheet(
        page_images=page_images,
        page_numbers=page_numbers,
        dpi=options.dpi,
        page_size_pt=(first.width, first.height),
    )


def _validate(geometry: TemplateGeometry, fill: SheetFill, options: SheetOptions) -> None:
    variant = geometry.variant

    def objects_of(label: str, kind: PageObjectType, what: str) -> list[ObjectGeometry]:
        found = [g for _, g in geometry.find(label)]
        if not found:
            raise ValueError(f"variant {variant}: no object {label!r} ({what})")
        if found[0].object_type is not kind:
            raise ValueError(
                f"variant {variant}: {label!r} is a {found[0].object_type.value}, "
                f"{what} needs a {kind.value}"
            )
        return found

    marked: set[tuple[str, object]] = set()
    for label, letters in fill.answers.items():
        obj = objects_of(label, PageObjectType.QUESTION, "answers")[0]
        for letter in letters:
            if letter not in obj.bubbles:
                raise ValueError(
                    f"variant {variant}: {label} has no option {letter!r} "
                    f"(printed: {''.join(obj.bubbles)})"
                )
            marked.add((label, letter))
    for label, cells in fill.cells.items():
        obj = objects_of(label, PageObjectType.MATRIX, "cells")[0]
        for cell in cells:
            if tuple(cell) not in obj.bubbles:
                rows = 1 + max(r for r, _ in obj.bubbles)
                cols = 1 + max(c for _, c in obj.bubbles)
                raise ValueError(
                    f"variant {variant}: cell {tuple(cell)} outside the "
                    f"{rows}×{cols} grid of {label}"
                )
            marked.add((label, tuple(cell)))
    for label in fill.texts:
        objects_of(label, PageObjectType.FREE_TEXT_AREA, "texts")
    for key in fill.styles:
        if key not in marked:
            raise ValueError(f"variant {variant}: style for {key!r}, which is not marked")

    if options.pages is not None:
        unknown = [n for n in options.pages if n not in geometry.pages]
        if unknown:
            raise ValueError(f"variant {variant}: no template page(s) {unknown}")
    for label, cover_pages in options.cover:
        for n in cover_pages:
            page = geometry.pages.get(n)
            obj = page.objects.get(label) if page is not None else None
            if obj is None or obj.object_type is not PageObjectType.CODE:
                raise ValueError(
                    f"variant {variant}: no CODE {label!r} on page {n} to cover"
                )


def _mark(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    obj: ObjectGeometry,
    page: int,
    label: str,
    key: object,
    fill: SheetFill,
    options: SheetOptions,
    k: float,
) -> None:
    key_text = f"{key[0]},{key[1]}" if isinstance(key, tuple) else key
    rng = random.Random(f"{options.seed}:{page}:{label}:{key_text}")
    bubble = obj.bubbles[key]
    style = fill.styles.get((label, key), fill.default_style)
    j = options.jitter_px
    cx = bubble.cx * k + rng.uniform(-j, j)
    cy = bubble.cy * k + rng.uniform(-j, j)
    r = bubble.r * k

    def box(radius: float):
        return (cx - radius, cy - radius, cx + radius, cy + radius)

    if style is MarkStyle.FULL:
        draw.ellipse(box(FULL_RADIUS * r), fill=INK)
    elif style is MarkStyle.LIGHT:
        _speckled_disc(image, cx, cy, LIGHT_RADIUS * r, rng)
    elif style is MarkStyle.HALF:
        # Pillow angles run clockwise from 3 o'clock: 0→180 is the lower half.
        draw.pieslice(box(HALF_RADIUS * r), 0, 180, fill=INK)
    elif style is MarkStyle.CROSS:
        s = CROSS_HALF_SPAN * r
        width = max(1, round(CROSS_WIDTH * r))
        draw.line((cx - s, cy - s, cx + s, cy + s), fill=INK, width=width)
        draw.line((cx - s, cy + s, cx + s, cy - s), fill=INK, width=width)
    elif style is MarkStyle.RING:
        draw.ellipse(
            box(RING_RADIUS * r), outline=INK, width=max(1, round(RING_WIDTH * r))
        )


def _speckled_disc(
    image: Image.Image, cx: float, cy: float, radius: float, rng: random.Random
) -> None:
    """LIGHT: every pixel of the disc painted LIGHT_GREY with LIGHT_COVERAGE."""
    px = image.load()
    width, height = image.size
    r2 = radius * radius
    for y in range(max(int(cy - radius), 0), min(int(cy + radius) + 1, height)):
        for x in range(max(int(cx - radius), 0), min(int(cx + radius) + 1, width)):
            dx, dy = x + 0.5 - cx, y + 0.5 - cy
            if dx * dx + dy * dy <= r2 and rng.random() < LIGHT_COVERAGE:
                px[x, y] = LIGHT_GREY


def _write_text(draw: ImageDraw.ImageDraw, obj: ObjectGeometry, text: str, k: float) -> None:
    x0, y0, x1, y1 = obj.text_box
    if y1 - y0 > TEXT_TALL_BOX_PT:
        size = TEXT_TALL_SIZE_PX
    else:
        size = max(1, round(TEXT_SINGLE_LINE_RATIO * (y1 - y0) * k))
    font = ImageFont.truetype(FONT_PATH, size)
    left, top = x0 * k + TEXT_MARGIN_PX, y0 * k + TEXT_MARGIN_PX
    right, bottom = x1 * k - TEXT_MARGIN_PX, y1 * k - TEXT_MARGIN_PX
    line_h = size * TEXT_LINE_SPACING
    y = top
    for line in _wrap(text, font, right - left):
        if y + size > bottom:
            break  # the box is full: the rest does not fit
        draw.text((left, y), line, fill=0, font=font)
        y += line_h


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if current and font.getlength(candidate) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def _scanner_artefacts(
    image: Image.Image, options: SheetOptions, rng: random.Random
) -> Image.Image:
    width, height = image.size
    if options.scale != 1.0:
        scaled = image.resize(
            (round(width * options.scale), round(height * options.scale)),
            resample=Image.Resampling.BICUBIC,
        )
        canvas = Image.new("L", (width, height), 255)
        # A negative offset (scale > 1) crops: paste clips to the canvas.
        canvas.paste(
            scaled, ((width - scaled.width) // 2, (height - scaled.height) // 2)
        )
        image = canvas
    if options.rotation_deg != 0.0:
        image = image.rotate(
            options.rotation_deg, resample=Image.Resampling.BICUBIC, fillcolor=255
        )
    if options.noise_sigma > 0:
        image = ImageChops.add(
            image, _noise(width, height, options.noise_sigma, rng), scale=1.0, offset=-128
        )
    if options.blur:
        image = image.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    return image


def _noise(width: int, height: int, sigma: float, rng: random.Random) -> Image.Image:
    """A seeded 256×256 tile of 128 + N(0, sigma), tiled from a random offset.

    Not ``Image.effect_noise``: it takes no seed."""
    n = NOISE_TILE
    tile = Image.frombytes(
        "L",
        (n, n),
        bytes(
            min(255, max(0, round(128 + rng.gauss(0, sigma)))) for _ in range(n * n)
        ),
    )
    ox, oy = rng.randrange(n), rng.randrange(n)
    big = Image.new("L", (width + n, height + n))
    for y in range(0, height + n, n):
        for x in range(0, width + n, n):
            big.paste(tile, (x, y))
    return big.crop((ox, oy, ox + width, oy + height))
