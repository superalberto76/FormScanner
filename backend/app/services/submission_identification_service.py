# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read the identification codes off a scanned Submission.

Turns the raw strings decoded from the QR/barcodes on a physical page into the
identification facts ingestion needs, using ONLY the self-describing
role tag carried by the payload (``VID:`` / ``RID:`` — see
``app.utils.code_rendering.build_tagged_payload``).

Why the tag matters: without it, telling a VARIANT_IDENTIFIER apart from a
RESPONDENT_IDENTIFIER would require knowing the FormVariant the sheet belongs
to — but resolving the variant is *itself* the job of matching, which runs
after ingestion. Ingestion would have to guess, e.g. "whichever code carries a
page index must be the variant identifier". The tag removes the guess: the
payload alone says what it is, with no lookup at all.

Rules (deliberately explicit, no heuristics):
  * ``VID`` — the variant identifier. Its RAW payload lands in
    ``SubmissionPage.identified_code_value`` (verbatim, so matching can re-parse
    it with ``parse_tagged_payload``), and its embedded page index — present only
    when the code was rendered with the "include page number" option
    — sets ``page_order_source`` to CODE.
  * ``RID`` — the respondent identifier. Found on ANY page of the submission it
    populates ``Submission.respondent_identifier``, and never overwrites a value
    already set (manual review / an earlier run wins). It says nothing about the
    sheet's position, so it never affects the page order.
  * ``UNTAGGED`` — not relevant for identification; treated exactly like "no
    readable code", so third-party codes cannot be mistaken for an identifier.
  * Several codes with the SAME tag on one page is anomalous. The FIRST one in
    decode order wins (deterministic, never random) and the rest are reported in
    ``extra_*`` so the caller can flag the sheet for review.

Loose-image ingestion reuses all of the above unchanged and adds ONE signal
the PDF path never had: the uploaded filename (see
``app.utils.image_filename``). A pile of images has no intrinsic submission and
no intrinsic order, so both have to be resolved per image, from the code and the
filename together — that combination is :func:`resolve_image_page`, at the bottom
of this module. It stays here, next to the code rules it arbitrates against,
rather than in the batch service, so the priority ladders remain pure and
directly testable.

Kept separate from ``submission_batch_service`` so these rules stay pure and
directly testable: nothing here touches the session, the filesystem or HTTP.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.models.enums import PageOrderSource
from app.models.submission import Submission
from app.models.submission_page import SubmissionPage
from app.utils.code_rendering import (
    TAG_RESPONDENT_IDENTIFIER,
    TAG_VARIANT_IDENTIFIER,
    parse_tagged_payload,
)
from app.utils.image_filename import parse_image_filename


@dataclass(frozen=True)
class PageIdentification:
    """What the tagged codes on one physical page say about it."""

    # Value of the VARIANT_IDENTIFIER code with its tag and page index stripped
    # (None if absent) — what matching compares against CodeObjectConfig.value.
    variant_identifier: str | None = None
    # The same code's payload exactly as decoded, tag included. This is what gets
    # persisted, so no information is lost before matching runs.
    variant_identifier_raw: str | None = None
    # Page index embedded in the VID payload, when the variant identifier was
    # rendered with "include page number". Not None means the sheet knows its
    # own position without any matching pass, i.e. PageOrderSource.CODE.
    # 1-based, like FormVariantPage.page_index.
    page_index: int | None = None
    # Value of the RESPONDENT_IDENTIFIER code, tag stripped (None if absent).
    respondent_identifier: str | None = None
    # Anomalies: further codes carrying the same tag on the same page, in decode
    # order, excluding the winner. Non-empty means the sheet deserves a look.
    extra_variant_identifiers: tuple[str, ...] = ()
    extra_respondent_identifiers: tuple[str, ...] = ()

    @property
    def has_conflicting_codes(self) -> bool:
        return bool(self.extra_variant_identifiers or self.extra_respondent_identifiers)

    @property
    def page_order_source(self) -> PageOrderSource:
        """Where this page's position comes from — CODE only if the code said so."""
        return (
            PageOrderSource.CODE
            if self.page_index is not None
            else PageOrderSource.PDF_ORDER
        )


def identify_page_codes(raw_codes: Iterable[str]) -> PageIdentification:
    """Classify every code decoded on one physical page by its role tag.

    Pure function: no DB, no ORM, never raises — a malformed or untagged code is
    simply ignored for identification purposes.
    """
    variants: list[tuple[str, str, int | None]] = []
    respondents: list[str] = []
    for raw in raw_codes or ():
        tag, value, page_index = parse_tagged_payload(raw)
        if tag == TAG_VARIANT_IDENTIFIER:
            variants.append((raw, value, page_index))
        elif tag == TAG_RESPONDENT_IDENTIFIER:
            respondents.append(value)
        # UNTAGGED: same treatment as "no readable code" — deliberately dropped.

    raw_value, variant_value, variant_page_index = (
        variants[0] if variants else (None, None, None)
    )
    return PageIdentification(
        variant_identifier=variant_value,
        variant_identifier_raw=raw_value,
        page_index=variant_page_index,
        respondent_identifier=respondents[0] if respondents else None,
        extra_variant_identifiers=tuple(value for _raw, value, _ in variants[1:]),
        extra_respondent_identifiers=tuple(respondents[1:]),
    )


def apply_identification(
    page: SubmissionPage,
    identification: PageIdentification,
    *,
    page_order_source: PageOrderSource | None = None,
) -> None:
    """Record an ALREADY-CLASSIFIED result on *page*.

    Sets ``identified_code_value`` (the VID payload, RAW) and
    ``page_order_source``. Only a VID sets the former: an untagged or
    respondent-only page keeps it None, exactly like a page whose code would not
    decode at all.

    *page_order_source* overrides where the position came from, for callers that
    resolved it from something OTHER than the codes. PDF ingestion leaves it None
    — the codes are its only positional signal — while loose-image ingestion
    passes what :func:`resolve_image_page` decided, which may be FILENAME. The
    override is deliberately narrow: everything the CODES say is still decided
    here, in one place, for both paths.

    Does not flush or commit — transaction control stays with the caller.
    """
    page.identified_code_value = identification.variant_identifier_raw
    page.page_order_source = (
        identification.page_order_source
        if page_order_source is None
        else page_order_source
    )


def apply_page_codes(
    page: SubmissionPage, raw_codes: Iterable[str]
) -> PageIdentification:
    """Classify *raw_codes* and record what they say about *page*.

    The PDF path's entry point: the codes are its only positional signal, so
    classification and recording happen together. Loose-image ingestion splits
    the two (:func:`resolve_image_page` then :func:`apply_identification`)
    because it has to weigh the filename in between.
    """
    identification = identify_page_codes(raw_codes)
    apply_identification(page, identification)
    return identification


def lift_respondent_identifier(
    submission: Submission, identification: PageIdentification
) -> None:
    """Promote a page's RID to its submission, without ever overwriting.

    The precedence rule in one place, shared by both ingestion paths: the first
    readable RID wins and an already-populated ``respondent_identifier`` is left
    alone, so manual review or an earlier upload always keeps precedence. "First"
    means first in the caller's iteration order — physical PDF order for a PDF,
    arrival order within the request for loose images.
    """
    if identification.respondent_identifier and not submission.respondent_identifier:
        submission.respondent_identifier = identification.respondent_identifier


def apply_submission_codes(
    submission: Submission,
    pages_with_codes: Iterable[tuple[SubmissionPage, Sequence[str]]],
) -> dict[uuid.UUID, PageIdentification]:
    """Apply :func:`apply_page_codes` to every (page, decoded codes) pair.

    Additionally lifts the respondent identifier to the submission: the first
    readable RID in the given order wins (the caller controls that order, which
    for ingestion is the physical PDF order), and an already-populated
    ``respondent_identifier`` is never overwritten — manual review or an earlier
    run keeps precedence.

    Takes explicit pairs rather than reading ``submission.pages`` so it works on
    pages still pending in the session, before any flush. Returns the per-page
    result keyed by ``SubmissionPage.id`` so the caller can surface anomalies
    (see ``PageIdentification.has_conflicting_codes``).
    """
    results: dict[uuid.UUID, PageIdentification] = {}
    for page, raw_codes in pages_with_codes:
        identification = apply_page_codes(page, raw_codes)
        results[page.id] = identification
        lift_respondent_identifier(submission, identification)
    return results


# ---------------------------------------------------------------------------
# Loose-image ingestion: code signal + filename signal
#
# A PDF answers "same submission?" and "which position?" by itself — one upload
# is one submission and the physical page order is right there. A pile of loose
# images answers neither, so both are resolved per image from two INDEPENDENT
# signals, each with its own priority ladder (filename convention in
# ``app.utils.image_filename``).
#
# Both signals are always computed, whatever the other one did: they answer
# different questions, and a sheet routinely has one without the other (a RID
# with a careless filename; a conforming filename with an unreadable code).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImagePageResolution:
    """Where one uploaded image belongs and where it sits, from both signals."""

    # Everything the codes on the image said (same rules as the PDF path).
    identification: PageIdentification
    # The submission this image joins: the RID if one was readable, else the
    # filename's <submission> token, else None = ungroupable (a singleton
    # Submission with status UNASSIGNED).
    group_key: str | None = None
    # True when *group_key* came from the RID rather than the filename. Audit
    # detail only — the key itself is deliberately opaque about its origin.
    group_key_from_code: bool = False
    # The <submission> token, whenever the filename conformed — kept even when the
    # RID outranked it, because that is what makes a mismatch detectable.
    filename_token: str | None = None
    # 0-based position DECLARED for this sheet, or None when nothing declared one
    # and the caller must fall back to the order of arrival within the group.
    declared_page_index: int | None = None
    page_order_source: PageOrderSource = PageOrderSource.PDF_ORDER

    @property
    def has_group_key_mismatch(self) -> bool:
        """A RID and a conforming filename that disagree on the submission.

        Very likely an operator naming slip (the RID is printed on the sheet, the
        filename is typed). Never blocks the upload — the RID simply wins and the
        sheet is reported for review, in the same spirit as
        ``has_conflicting_codes``.
        """
        return (
            self.group_key_from_code
            and self.filename_token is not None
            and self.filename_token != self.group_key
        )


def resolve_image_page(
    filename: str | None, raw_codes: Iterable[str]
) -> ImagePageResolution:
    """Combine the code and filename signals for ONE uploaded image.

    Grouping, in order: readable RID > conforming filename token > ungroupable.
    Position, in order: VID page index > conforming filename <n> > None (the
    caller assigns the order of arrival within the group).

    Both ladders read the SAME two signals but rank them differently on purpose.
    A RID outranks the filename for grouping because it is printed on the sheet
    and a filename is typed by an operator. A VID index outranks the filename for
    position for the same reason — and note the VID index is 1-BASED (it is
    ``FormVariantPage.page_index``, encoded verbatim by the export), so it is
    converted here to the 0-based ``SubmissionPage.source_page_order``.

    Pure and total, like everything else on this path: no DB, no filesystem, and
    a malformed filename or payload is simply "no signal", never an exception.
    """
    identification = identify_page_codes(raw_codes)
    parsed = parse_image_filename(filename) if filename else None
    filename_token, filename_page_index = parsed if parsed else (None, None)

    # --- which submission ---------------------------------------------------
    respondent = identification.respondent_identifier
    if respondent:
        group_key, group_key_from_code = respondent, True
    else:
        group_key, group_key_from_code = filename_token, False

    # --- which position ------------------------------------------------------
    code_page_index = identification.page_index
    if code_page_index is not None and code_page_index >= 1:
        # 1-based -> 0-based. A code claiming index 0 or a negative one cannot be
        # a real FormVariantPage.page_index, so it is treated as no signal at all
        # rather than converted into a negative position.
        declared_page_index = code_page_index - 1
        page_order_source = PageOrderSource.CODE
    elif filename_page_index is not None:
        # Already 0-based: parse_image_filename does the conversion.
        declared_page_index = filename_page_index
        page_order_source = PageOrderSource.FILENAME
    else:
        # Nothing declared a position. PDF_ORDER is reused as the generic
        # "no declared order, just a sequence" bucket (see the enum docstring).
        declared_page_index = None
        page_order_source = PageOrderSource.PDF_ORDER

    return ImagePageResolution(
        identification=identification,
        group_key=group_key,
        group_key_from_code=group_key_from_code,
        filename_token=filename_token,
        declared_page_index=declared_page_index,
        page_order_source=page_order_source,
    )
