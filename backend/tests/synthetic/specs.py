# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declarative description of the three acceptance-test projects.

COLL-MULTI, COLL-SINGLE and COLL-CONFIG of the test kit, as frozen
dataclasses only — no DB, no services. ``tests.synthetic.builders`` turns a
``ProjectSpec`` into real rows by calling the app's services.

Conventions:
  * ``page`` is 1-based, like ``FormVariantPage.page_index``;
  * coordinates are pixels of an A4 page at 96 DPI (794×1123), every object
    inside the area outside the reserved bands (57 ≤ x, x + w ≤ 737,
    57 ≤ y, y + h ≤ 1066);
  * ``label`` is the kit code (Q01, MATRICOLA, …), ``description`` the readable
    text; FREE_TEXT_AREAs carry no placeholder (it would be printed and OCR'd);
  * an option's TEXT is its stable key across variants, its letter A–D is just
    its position (``order_index``) on a given variant. Options in a spec tuple are
    already in ``order_index`` order.

Variants B/C/D of COLL-MULTI are derived from A by functions, never copied.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from app.models.enums import (
    CodeEncoding,
    CodeType,
    MatrixSelectionRule,
    PageObjectRole,
    QuestionType,
)
from app.utils import question_layout

PAGE_W = 794
PAGE_H = 1123

OPTION_COUNT = 4
# Every QUESTION is exactly tall enough for its header and four option bands.
QUESTION_H = question_layout.HEADER_H + OPTION_COUNT * question_layout.OPTION_H


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionSpec:
    text: str
    is_correct: bool
    score: float


@dataclass(frozen=True)
class QuestionSpec:
    label: str
    page: int
    x: float
    y: float
    w: float
    question_type: QuestionType
    options: tuple[OptionSpec, ...]
    # The printed question; None: "<label> · <kind>", as in the test kit.
    text: str | None = None

    @property
    def h(self) -> float:
        return QUESTION_H

    @property
    def question_text(self) -> str:
        if self.text is not None:
            return self.text
        kind = (
            "scelta singola"
            if self.question_type is QuestionType.SINGLE_CHOICE
            else "scelta multipla"
        )
        return f"{self.label} · {kind}"


@dataclass(frozen=True)
class MatrixSpec:
    label: str
    page: int
    x: float
    y: float
    w: float
    h: float
    rows: int
    cols: int
    selection_rule: MatrixSelectionRule
    evaluative: bool
    row_labels: tuple[str, ...]
    col_labels: tuple[str, ...]
    correct_cells: tuple[tuple[int, int], ...]
    role: PageObjectRole | None = None


@dataclass(frozen=True)
class FreeTextSpec:
    label: str
    description: str
    page: int
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class CodeSpec:
    label: str
    page: int
    x: float
    y: float
    w: float
    h: float
    code_type: CodeType
    encoding: CodeEncoding | None
    value: str
    human_readable: bool
    role: PageObjectRole | None
    shared_across_pages: bool
    include_page_index: bool


@dataclass(frozen=True)
class TextSpec:
    """Printed text: read by nothing, so absent from the test kit; the sample
    project of the user guide has a title and instructions."""

    label: str
    page: int
    x: float
    y: float
    w: float
    h: float
    text: str
    font_size: int
    bold: bool = False


ObjectSpec = QuestionSpec | MatrixSpec | FreeTextSpec | CodeSpec | TextSpec


@dataclass(frozen=True)
class VariantSpec:
    key: str
    name: str
    root_key: str
    page_count: int
    objects: tuple[ObjectSpec, ...]
    publish: bool

    @property
    def vid(self) -> CodeSpec | None:
        """The VARIANT_IDENTIFIER code, if the variant carries one."""
        for o in self.objects:
            if isinstance(o, CodeSpec) and o.role is PageObjectRole.VARIANT_IDENTIFIER:
                return o
        return None

    def obj(self, label: str) -> ObjectSpec:
        (match,) = [o for o in self.objects if o.label == label]
        return match


@dataclass(frozen=True)
class ProjectSpec:
    code: str
    name: str
    variants: tuple[VariantSpec, ...]

    def variant(self, key: str) -> VariantSpec:
        (match,) = [v for v in self.variants if v.key == key]
        return match


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def _options(correct: dict[int, float], others: dict[int, float] | None = None):
    """"Opzione 1".."Opzione 4"; *correct* maps 1-based option number → score,
    *others* overrides the 0.0 score of a non-correct option."""
    others = others or {}
    return tuple(
        OptionSpec(
            text=f"Opzione {n}",
            is_correct=n in correct,
            score=correct.get(n, others.get(n, 0.0)),
        )
        for n in range(1, OPTION_COUNT + 1)
    )


_S = QuestionType.SINGLE_CHOICE
_M = QuestionType.MULTIPLE_CHOICE

# Q code → (type, options) — the answer key of the test kit, shared by all projects.
QUESTION_CONTENT: dict[str, tuple[QuestionType, tuple[OptionSpec, ...]]] = {
    "Q01": (_S, _options({2: 1.0})),
    "Q02": (_S, _options({3: 1.0})),
    "Q03": (_S, _options({1: 1.0})),
    "Q04": (_S, _options({4: 1.0})),
    "Q05": (_M, _options({1: 1.0, 3: 1.0})),
    "Q06": (_M, _options({2: 1.0, 4: 1.0})),
    "Q07": (_S, _options({1: 1.0})),
    "Q08": (_S, _options({2: 1.0})),
    "Q09": (_S, _options({3: 1.0}, others={4: -1.0})),
    "Q10": (_S, _options({4: 1.0})),
    "Q11": (_S, _options({1: 1.0})),
    "Q12": (_S, _options({2: 1.0})),
}


def question(label: str, page: int, x: float, y: float, w: float) -> QuestionSpec:
    qtype, options = QUESTION_CONTENT[label]
    return QuestionSpec(label, page, x, y, w, qtype, options)


def respondent_id(cols: int, page: int, x: float, y: float, w: float, h: float) -> MatrixSpec:
    """Respondent-ID grid: one digit 0–9 per column, not evaluative."""
    return MatrixSpec(
        label="MATRICOLA", page=page, x=x, y=y, w=w, h=h,
        rows=10, cols=cols,
        selection_rule=MatrixSelectionRule.ONE_PER_COLUMN,
        evaluative=False,
        row_labels=tuple(str(d) for d in range(10)),
        col_labels=tuple(str(c) for c in range(1, cols + 1)),
        correct_cells=(),
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
    )


def _rc_labels(n: int, prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}{i}" for i in range(1, n + 1))


def vid(value: str) -> CodeSpec:
    return CodeSpec(
        label="VID", page=1, x=654, y=60, w=80, h=80,
        code_type=CodeType.QR, encoding=None, value=value, human_readable=False,
        role=PageObjectRole.VARIANT_IDENTIFIER,
        shared_across_pages=True, include_page_index=True,
    )


# ---------------------------------------------------------------------------
# COLL-MULTI
# ---------------------------------------------------------------------------

MULTI_CODE = "COLL-MULTI"
MULTI_PAGE_COUNT = 3


def _multi_vid_value(key: str) -> str:
    return f"{MULTI_CODE}-{key}"


_MULTI_A = VariantSpec(
    key="A",
    name="A",
    root_key="coll-multi-a",
    page_count=MULTI_PAGE_COUNT,
    publish=True,
    objects=(
        vid(_multi_vid_value("A")),
        # --- page 1
        respondent_id(6, 1, 60, 60, 200, 232),
        FreeTextSpec("NOME", "Nome e cognome", 1, 280, 60, 354, 60),
        question("Q01", 1, 60, 320, 330),
        question("Q02", 1, 404, 320, 330),
        question("Q03", 1, 60, 430, 330),
        question("Q04", 1, 404, 430, 330),
        question("Q05", 1, 60, 540, 330),
        question("Q06", 1, 404, 540, 330),
        # --- page 2 (below the linked VID, which sits top right on every page)
        MatrixSpec(
            label="M-RIGHE", page=2, x=60, y=150, w=300, h=128,
            rows=4, cols=4, selection_rule=MatrixSelectionRule.ONE_PER_ROW,
            evaluative=True,
            row_labels=_rc_labels(4, "R"), col_labels=_rc_labels(4, "C"),
            correct_cells=tuple((r, r) for r in range(4)),
        ),
        MatrixSpec(
            label="M-COLONNE", page=2, x=380, y=150, w=300, h=128,
            rows=4, cols=4, selection_rule=MatrixSelectionRule.ONE_PER_COLUMN,
            evaluative=True,
            row_labels=_rc_labels(4, "R"), col_labels=_rc_labels(4, "C"),
            correct_cells=tuple((r, 3 - r) for r in range(4)),
        ),
        question("Q07", 2, 60, 310, 330),
        question("Q08", 2, 404, 310, 330),
        question("Q09", 2, 60, 420, 330),
        CodeSpec(
            label="BARCODE", page=2, x=404, y=420, w=250, h=70,
            code_type=CodeType.BARCODE, encoding=CodeEncoding.CODE128,
            value="LOT-0001", human_readable=True, role=None,
            shared_across_pages=False, include_page_index=False,
        ),
        # --- page 3
        MatrixSpec(
            label="M-MULTI", page=3, x=60, y=60, w=300, h=104,
            rows=3, cols=4, selection_rule=MatrixSelectionRule.MANY_PER_ROW,
            evaluative=True,
            row_labels=_rc_labels(3, "R"), col_labels=_rc_labels(4, "C"),
            correct_cells=((0, 0), (0, 2), (1, 1), (1, 3), (2, 0), (2, 3)),
        ),
        question("Q10", 3, 60, 200, 330),
        question("Q11", 3, 404, 200, 330),
        question("Q12", 3, 60, 310, 330),
        FreeTextSpec("APERTA", "Risposta aperta", 3, 60, 420, 674, 200),
    ),
)


def _for_key(base: VariantSpec, key: str, **changes) -> VariantSpec:
    """*base* renamed to *key*, with its own VID value (if it has a VID)."""
    objects = tuple(
        replace(o, value=_multi_vid_value(key))
        if isinstance(o, CodeSpec) and o.role is PageObjectRole.VARIANT_IDENTIFIER
        else o
        for o in changes.pop("objects", base.objects)
    )
    return replace(
        base, key=key, name=key, root_key=f"coll-multi-{key.lower()}",
        objects=objects, **changes,
    )


def rotate_options(q: QuestionSpec) -> QuestionSpec:
    """Option at position i moves to (i+1) mod 4: new order = [A3, A0, A1, A2]."""
    return replace(q, options=q.options[-1:] + q.options[:-1])


# Variant B: Q10–Q12 moved here, page 3 keeps only M-MULTI and APERTA.
_B_MOVES = {"Q10": (2, 60, 530), "Q11": (2, 404, 530), "Q12": (2, 60, 640)}


def derive_b(a: VariantSpec) -> VariantSpec:
    objects = []
    for o in a.objects:
        if isinstance(o, QuestionSpec):
            o = rotate_options(o)
            if o.label in _B_MOVES:
                page, x, y = _B_MOVES[o.label]
                o = replace(o, page=page, x=x, y=y)
        objects.append(o)
    return _for_key(a, "B", objects=tuple(objects))


def derive_c(a: VariantSpec) -> VariantSpec:
    """A without Q12 and with a 5-column MATRICOLA (same geometry)."""
    objects = []
    for o in a.objects:
        if o.label == "Q12":
            continue
        if o.label == "MATRICOLA":
            o = replace(
                o, cols=5, col_labels=tuple(str(c) for c in range(1, 6))
            )
        objects.append(o)
    return _for_key(a, "C", objects=tuple(objects))


def derive_d(a: VariantSpec) -> VariantSpec:
    """Identical to A, own VID value, left DRAFT."""
    return _for_key(a, "D", publish=False)


COLL_MULTI = ProjectSpec(
    code=MULTI_CODE,
    name="COLL-MULTI — Collaudo multi-variante",
    variants=(_MULTI_A, derive_b(_MULTI_A), derive_c(_MULTI_A), derive_d(_MULTI_A)),
)


# ---------------------------------------------------------------------------
# COLL-SINGLE
# ---------------------------------------------------------------------------

COLL_SINGLE = ProjectSpec(
    code="COLL-SINGLE",
    name="COLL-SINGLE — Collaudo variante singola",
    variants=(
        VariantSpec(
            key="A",
            name="A",
            root_key="coll-single-a",
            page_count=2,
            publish=True,
            objects=(
                respondent_id(4, 1, 60, 60, 160, 232),
                question("Q01", 1, 60, 320, 330),
                question("Q02", 1, 404, 320, 330),
                question("Q03", 1, 60, 430, 330),
                question("Q04", 2, 60, 60, 330),
                question("Q05", 2, 404, 60, 330),
                question("Q06", 2, 60, 170, 330),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# COLL-CONFIG — two published variants and no VARIANT_IDENTIFIER: not scannable
# ---------------------------------------------------------------------------


def _config_variant(key: str) -> VariantSpec:
    return VariantSpec(
        key=key,
        name=key,
        root_key=f"coll-config-{key.lower()}",
        page_count=1,
        publish=True,
        objects=(question("Q01", 1, 60, 60, 330),),
    )


COLL_CONFIG = ProjectSpec(
    code="COLL-CONFIG",
    name="COLL-CONFIG — Collaudo configurazione errata",
    variants=(_config_variant("A"), _config_variant("B")),
)
