# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sample project of the user guide's screenshots.

    cd backend
    python -m tests.synthetic.sample_project prepare --database-url URL
    python -m tests.synthetic.sample_project fill --database-url URL
    python -m tests.synthetic.sample_project sheets --database-url URL --out DIR
    python -m tests.synthetic.sample_project render --pdf FILE --png FILE

``frontend/e2e/screenshots.spec.ts`` (``npm run screenshots``) walks through
chapter 3 of the user guide in the browser and calls these steps on the way:

  * ``prepare`` (global setup): empties the ``*_e2e`` database, brings it to
    the Alembic head and creates the administrator of the guide (the
    browser then creates the colleague's account, ``COLLEAGUE``);
  * ``fill``: once the browser has created the project "Sample quiz", its
    variant and one question, replaces the content of the variant's first
    page with the complete form of ``SAMPLE_QUIZ`` (the editor shows how one
    object is drawn; drawing them all from the browser would be fragile);
  * ``sheets``: once the browser has published the variant, writes the
    filled-in sheets of ``SAMPLE_SHEETS``, one PDF per respondent, from the
    variant's own export (``generator``);
  * ``render``: the first page of a PDF as a PNG, for the pictures of the
    printable form and of a scanned sheet.

Everything is in English and made up: no personal data. The database is the
one of the e2e tests, emptied at every run (``e2e_env`` refuses any other).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every mapper)
from app.core.security import hash_password
from app.models.enums import (
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    QuestionType,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_variant import FormVariant
from app.models.user import User
from app.repositories import form_variant_page_repo, page_object_repo
from app.services import page_object_role_service, page_object_service
from tests.synthetic import e2e_env
from tests.synthetic.builders import (
    CollProject,
    _create_object,  # the kit's own construction of an ObjectSpec
    _read_variant,
)
from tests.synthetic.generator import MarkStyle, SheetFill, SheetOptions, digits, generate
from tests.synthetic.specs import (
    FreeTextSpec,
    MatrixSpec,
    OptionSpec,
    ProjectSpec,
    QuestionSpec,
    TextSpec,
    VariantSpec,
)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    email: str
    password: str
    first_name: str
    last_name: str
    role: UserRole


# The administrator signs in with the values of .env.example, as a new
# installation does. The colleague's password is the temporary one the
# administrator types in "Add user".
ADMIN = Account("admin@example.com", "ChangeMe!", "Alex", "Example", UserRole.ADMIN)
COLLEAGUE = Account("sam@example.com", "Sample-Password-1", "Sam", "Example", UserRole.USER)
ACCOUNTS = (ADMIN, COLLEAGUE)


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------

PROJECT_NAME = "Sample quiz"
VARIANT_NAME = "Version A"
VARIANT_KEY = "A"

NAME_LABEL = "Name"
STUDENT_NUMBER_LABEL = "Student number"
STUDENT_NUMBER_DIGITS = 5


def _question(
    number: int,
    x: float,
    y: float,
    text: str,
    options: tuple[str, ...],
    correct: tuple[int, ...],
    question_type: QuestionType = QuestionType.SINGLE_CHOICE,
) -> QuestionSpec:
    """Question *number*; *correct* holds 1-based option numbers, 1 point each."""
    return QuestionSpec(
        label=f"Question {number}",
        page=1,
        x=x,
        y=y,
        w=330,
        question_type=question_type,
        options=tuple(
            OptionSpec(text=o, is_correct=n in correct, score=1.0 if n in correct else 0.0)
            for n, o in enumerate(options, start=1)
        ),
        text=text,
    )


# The question the walkthrough draws in the browser before ``fill``: the
# script types exactly these values.
FIRST_QUESTION = _question(
    1, 60, 330, "What is the capital of France?", ("Berlin", "Madrid", "Paris", "Rome"), (3,)
)

SAMPLE_QUIZ = ProjectSpec(
    code="SAMPLE",
    name=PROJECT_NAME,
    variants=(
        VariantSpec(
            key=VARIANT_KEY,
            name=VARIANT_NAME,
            root_key="sample-quiz-a",
            page_count=1,
            publish=True,
            objects=(
                # The PDF prints with the base fonts: plain ASCII only (an em
                # dash would print as "?").
                TextSpec("Title", 1, 60, 70, 440, 30, "Sample quiz: general knowledge", 18, bold=True),
                TextSpec(
                    "Instructions", 1, 60, 104, 440, 36,
                    "Fill each bubble completely with a dark pen. "
                    "Write your name in capital letters.",
                    10,
                ),
                FreeTextSpec(NAME_LABEL, "Name and surname", 1, 60, 160, 440, 60),
                MatrixSpec(
                    label=STUDENT_NUMBER_LABEL, page=1, x=530, y=70, w=200, h=232,
                    rows=10, cols=STUDENT_NUMBER_DIGITS,
                    selection_rule=MatrixSelectionRule.ONE_PER_COLUMN,
                    evaluative=False,
                    row_labels=tuple(str(d) for d in range(10)),
                    col_labels=tuple(str(c) for c in range(1, STUDENT_NUMBER_DIGITS + 1)),
                    correct_cells=(),
                    role=PageObjectRole.RESPONDENT_IDENTIFIER,
                ),
                FIRST_QUESTION,
                _question(
                    2, 404, 330, "Which planet is closest to the Sun?",
                    ("Venus", "Mercury", "Mars", "Earth"), (2,),
                ),
                _question(
                    3, 60, 440, "Which of these are prime numbers?",
                    ("2", "4", "7", "9"), (1, 3), QuestionType.MULTIPLE_CHOICE,
                ),
                _question(
                    4, 404, 440, "How many minutes are in an hour?",
                    ("30", "60", "90", "100"), (2,),
                ),
                _question(
                    5, 60, 550, "Which gas do plants take in from the air?",
                    ("Oxygen", "Nitrogen", "Carbon dioxide", "Helium"), (3,),
                ),
                _question(
                    6, 404, 550, "Who wrote \"Romeo and Juliet\"?",
                    ("Charles Dickens", "William Shakespeare", "Jane Austen", "Leo Tolstoy"), (2,),
                ),
            ),
        ),
    ),
)

SAMPLE_VARIANT = SAMPLE_QUIZ.variant(VARIANT_KEY)
# One point per question; question 3 has two correct options.
MAX_SCORE = 7.0


# ---------------------------------------------------------------------------
# The filled-in sheets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleSheet:
    """One respondent: the file ``<stem>.pdf`` and how the sheet was filled in."""

    stem: str
    fill: SheetFill
    seed: int


def _fill(student_number, name: str, answers: dict[int, tuple[str, ...]], **extra) -> SheetFill:
    return SheetFill(
        answers={f"Question {n}": letters for n, letters in answers.items()},
        cells={STUDENT_NUMBER_LABEL: student_number},
        texts={NAME_LABEL: name},
        **extra,
    )


_ALL_RIGHT = {1: ("C",), 2: ("B",), 3: ("A", "C"), 4: ("B",), 5: ("C",), 6: ("B",)}

SAMPLE_SHEETS = (
    # Every answer right, the student number clean: only the name to check.
    SampleSheet("jane-doe", _fill(digits("20451"), "JANE DOE", _ALL_RIGHT), seed=1),
    # A light pencil mark (question 2) and two bubbles on a single-choice
    # question (question 4): two readings in review besides the name.
    SampleSheet(
        "john-smith",
        _fill(
            digits("20452"), "JOHN SMITH",
            {**_ALL_RIGHT, 3: ("A",), 4: ("B", "C"), 6: ("A",)},
            styles={("Question 2", "B"): MarkStyle.LIGHT},
        ),
        seed=2,
    ),
    # Two digits marked in the fourth column of the student number: the
    # candidate waits for the reviewer.
    SampleSheet(
        "erika-example",
        _fill(digits("204", ("5", "6"), "3"), "ERIKA EXAMPLE", {**_ALL_RIGHT, 5: ("A",)}),
        seed=3,
    ),
    # Clean, with two wrong answers.
    SampleSheet(
        "max-sample",
        _fill(digits("20454"), "MAX SAMPLE", {**_ALL_RIGHT, 2: ("A",), 3: ("A", "B")}),
        seed=4,
    ),
)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class SampleError(Exception):
    """A step cannot run on the state it found: nothing was written."""


def prepare(url: str, log=print) -> None:
    """The e2e database emptied, at the Alembic head, with the administrator."""
    created = e2e_env.ensure_database(url)
    e2e_env.empty_database(url)
    e2e_env.upgrade_head(url)
    engine = create_engine(url, future=True)
    try:
        with Session(engine) as db:
            create_accounts(db, (ADMIN,))
            db.commit()
    finally:
        engine.dispose()
    log(
        f"sample_project: database {'created, ' if created else ''}emptied and migrated; "
        f"administrator {ADMIN.email}"
    )


def create_accounts(db: Session, accounts: tuple[Account, ...] = ACCOUNTS) -> dict[str, User]:
    users = {}
    for account in accounts:
        user = User(
            email=account.email,
            password_hash=hash_password(account.password),
            role=account.role,
            first_name=account.first_name,
            last_name=account.last_name,
        )
        db.add(user)
        users[account.email] = user
    db.flush()
    return users


def find_variant(db: Session) -> tuple[FormProject, FormVariant, User]:
    """The project "Sample quiz" of the administrator and its variant."""
    owner = db.scalars(select(User).where(User.email == ADMIN.email)).one_or_none()
    if owner is None:
        raise SampleError(f"no user {ADMIN.email}: run 'prepare' first")
    project = db.scalars(
        select(FormProject).where(
            FormProject.owner_id == owner.id, FormProject.name == PROJECT_NAME
        )
    ).one_or_none()
    if project is None:
        raise SampleError(f"no project {PROJECT_NAME!r} owned by {ADMIN.email}")
    variant = db.scalars(
        select(FormVariant).where(
            FormVariant.form_project_id == project.id, FormVariant.name == VARIANT_NAME
        )
    ).one_or_none()
    if variant is None:
        raise SampleError(f"no variant {VARIANT_NAME!r} in {PROJECT_NAME!r}")
    return project, variant, owner


def fill_variant(db: Session, variant: FormVariant, owner: User) -> None:
    """The first page of the DRAFT *variant* holds exactly ``SAMPLE_VARIANT``.

    What the page held (the question drawn in the browser) is deleted first:
    the spec is the only source of the form, so the sheets match it whatever
    the browser drew."""
    if variant.status is not FormVariantStatus.DRAFT:
        raise SampleError(f"the variant is {variant.status.value}, not DRAFT")
    pages = form_variant_page_repo.list_for_variant(db, variant.id)
    if len(pages) != SAMPLE_VARIANT.page_count:
        raise SampleError(
            f"the variant has {len(pages)} pages, the sample {SAMPLE_VARIANT.page_count}"
        )
    page_id = pages[0].id
    for obj in page_object_repo.list_for_page_with_configs(db, page_id):
        page_object_service.delete_object(db, obj.id, owner)
    for spec in SAMPLE_VARIANT.objects:
        created = _create_object(db, page_id, spec, owner)
        role = getattr(spec, "role", None)
        if role is not None:
            page_object_role_service.assign_role(db, created.id, role, owner)


def sample_project(db: Session, project: FormProject, variant: FormVariant, owner: User,
                   collaborator: User | None = None) -> CollProject:
    """The references ``generator.generate`` takes, for the project as it is."""
    return CollProject(
        code=SAMPLE_QUIZ.code,
        project_id=project.id,
        owner_id=owner.id,
        collaborator_id=(collaborator or owner).id,
        variants={VARIANT_KEY: _read_variant(db, SAMPLE_VARIANT, variant.id)},
        spec=SAMPLE_QUIZ,
    )


def write_sheets(db: Session, project: CollProject, out: Path) -> list[Path]:
    """One PDF per ``SAMPLE_SHEETS`` respondent into *out*; their paths."""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for sheet in SAMPLE_SHEETS:
        generated = generate(db, project, VARIANT_KEY, sheet.fill, SheetOptions(seed=sheet.seed))
        path = out / f"{sheet.stem}.pdf"
        path.write_bytes(generated.to_pdf())
        paths.append(path)
    return paths


def render_first_page(pdf: Path, png: Path, width_px: int = 900) -> None:
    """Page 1 of *pdf* as a greyscale PNG *width_px* wide."""
    with fitz.open(pdf) as doc:
        page = doc[0]
        zoom = width_px / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
        png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(png)


def _with_session(url: str, work) -> None:
    engine = create_engine(url, future=True)
    try:
        with Session(engine) as db:
            work(db)
            db.commit()
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.synthetic.sample_project",
        description="Steps of the user guide's screenshots (see the module docstring).",
    )
    steps = parser.add_subparsers(dest="step", required=True)
    for name in ("prepare", "fill", "sheets"):
        step = steps.add_parser(name)
        step.add_argument("--database-url", required=True, help="the e2e database (name ending in _e2e)")
        if name == "sheets":
            step.add_argument("--out", type=Path, required=True, help="folder of the PDFs")
    render = steps.add_parser("render")
    render.add_argument("--pdf", type=Path, required=True)
    render.add_argument("--png", type=Path, required=True)
    render.add_argument("--width", type=int, default=900)
    args = parser.parse_args(argv)

    try:
        if args.step == "render":
            render_first_page(args.pdf, args.png, args.width)
            return 0
        e2e_env.check_e2e_url(args.database_url)
        if args.step == "prepare":
            prepare(args.database_url)
        elif args.step == "fill":
            def fill(db: Session) -> None:
                _, variant, owner = find_variant(db)
                fill_variant(db, variant, owner)
            _with_session(args.database_url, fill)
        else:
            def sheets(db: Session) -> None:
                project, variant, owner = find_variant(db)
                if variant.status is not FormVariantStatus.PUBLISHED:
                    raise SampleError(f"the variant is {variant.status.value}: publish it first")
                for path in write_sheets(db, sample_project(db, project, variant, owner), args.out):
                    print(f"sample_project: {path}")
            _with_session(args.database_url, sheets)
    except (SampleError, e2e_env.E2eEnvError) as exc:
        print(f"sample_project: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
