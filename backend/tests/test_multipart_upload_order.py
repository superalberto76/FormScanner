# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The multipart ordering assumption behind loose-image ingestion.

The last-resort positional signal for an uploaded sheet is "where it sat in the
upload request" (``PageOrderSource.PDF_ORDER``, reused as the generic sequence
bucket). That is only meaningful if the framework hands the files over in the
order the client sent them — an assumption about Starlette/FastAPI, not about our
own code, and therefore one that a dependency bump could silently break. Hence
this test: if it ever fails, the fallback ladder in
``submission_identification_service.resolve_image_page`` has lost its bottom rung
and the endpoint must stop relying on arrival order.

Driven straight through Starlette's ``MultiPartParser`` rather than through
``TestClient``, which would need httpx (not a dependency of this project).

Two layers are checked, because the endpoint's correctness depends on both:
  1. the parser preserves the order of same-named parts in its ``FormData``;
  2. ``FormData.getlist`` — what FastAPI calls to build a ``List[UploadFile]``
     (see ``fastapi.dependencies.utils._get_multidict_value``) — returns them in
     that same order.

Note this holds for ``List[UploadFile]`` specifically. ``List[bytes] = File(...)``
goes down a different FastAPI branch that reads the parts CONCURRENTLY into a
list, so the endpoint deliberately takes UploadFile and reads it itself.
"""
from __future__ import annotations

import asyncio

from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser

BOUNDARY = "----formscannertestboundary"

# Deliberately NOT in alphabetical or numeric order, so a parser that sorted or
# bucketed the parts anywhere would produce a different sequence.
FILENAMES = [
    "zz_pagina_3.jpg",
    "aa_pagina_1.jpg",
    "mm_pagina_2.jpg",
    "IMG_0009.jpg",
    "aa_pagina_10.jpg",
]


def _multipart_body(filenames: list[str]) -> bytes:
    parts = [
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
        f"payload-{index}\r\n"
        for index, name in enumerate(filenames)
    ]
    return ("".join(parts) + f"--{BOUNDARY}--\r\n").encode()


async def _parse(filenames: list[str], chunk_size: int):
    body = _multipart_body(filenames)
    headers = Headers({"content-type": f"multipart/form-data; boundary={BOUNDARY}"})

    async def stream():
        # Chunked on purpose, and at a size that splits parts mid-header and
        # mid-body: streaming reassembly must not reorder anything either.
        for start in range(0, len(body), chunk_size):
            yield body[start:start + chunk_size]

    form = await MultiPartParser(headers, stream()).parse()
    uploads = form.getlist("files")
    return [u.filename for u in uploads], [(await u.read()).decode() for u in uploads]


def test_getlist_preserves_the_order_files_were_sent_in():
    names, payloads = asyncio.run(_parse(FILENAMES, chunk_size=4096))

    assert names == FILENAMES
    # The BODIES travel with their names — a stable name order with shuffled
    # contents would be just as broken.
    assert payloads == [f"payload-{i}" for i in range(len(FILENAMES))]


def test_order_survives_a_chunked_stream():
    names, payloads = asyncio.run(_parse(FILENAMES, chunk_size=17))

    assert names == FILENAMES
    assert payloads == [f"payload-{i}" for i in range(len(FILENAMES))]


def test_order_holds_for_a_realistic_batch_size():
    """A scanning batch is tens to hundreds of sheets, not five."""
    many = [f"sheet_{i:03d}_pagina_{(i % 4) + 1}.jpg" for i in range(120)][::-1]

    names, payloads = asyncio.run(_parse(many, chunk_size=1024))

    assert names == many
    assert payloads == [f"payload-{i}" for i in range(len(many))]


def test_fastapi_reads_sequence_form_fields_through_getlist():
    """Pins the link between the layer tested above and what FastAPI actually
    does, so this file keeps testing the real path after a version bump."""
    import inspect

    from fastapi.dependencies.utils import _get_multidict_value

    source = inspect.getsource(_get_multidict_value)
    assert "getlist" in source
