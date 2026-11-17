# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Materialize a ``ProjectSpec`` in the DB through the app's services.

Every write goes through a service, in the order the services impose; the only
direct ORM inserts are the two users (owner + collaborator), as in the existing
integration tests. Reads afterwards use the repositories, so the replicated VID
copies are found exactly as the app stores them.

The returned ``CollProject`` holds ids and plain data only — no ORM instances —
so callers can keep it across commits / expiry without surprises.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    UserRole,
)
from app.models.user import User
from app.repositories import (
    form_variant_page_repo,
    form_variant_repo,
    page_object_repo,
)
from app.schemas.form_project import FormProjectCreate
from app.schemas.form_variant import FormVariantCreate
from app.schemas.matrix_object import (
    MatrixCellDefCreate,
    MatrixColDefCreate,
    MatrixObjectConfigCreate,
    MatrixPageObjectCreate,
    MatrixRowDefCreate,
)
from app.schemas.page_object import (
    CodeObjectConfigCreate,
    CodeObjectConfigUpdate,
    CodePageObjectCreate,
    FreeTextAreaConfigCreate,
    FreeTextAreaPageObjectCreate,
    TextObjectConfigCreate,
    TextPageObjectCreate,
)
from app.schemas.question_object import (
    QuestionObjectConfigCreate,
    QuestionOptionCreate,
    QuestionPageObjectCreate,
)
from app.services import (
    code_object_service,
    form_project_service,
    form_variant_service,
    free_text_area_service,
    matrix_object_service,
    page_object_role_service,
    question_object_service,
    text_object_service,
)
from tests.synthetic.specs import (
    COLL_CONFIG,
    COLL_MULTI,
    COLL_SINGLE,
    CodeSpec,
    FreeTextSpec,
    MatrixSpec,
    ObjectSpec,
    ProjectSpec,
    QuestionSpec,
    TextSpec,
    VariantSpec,
)

LETTERS = "ABCD"


# ---------------------------------------------------------------------------
# Returned references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionRef:
    option_id: uuid.UUID
    letter: str
    text: str
    is_correct: bool
    score: float | None


@dataclass(frozen=True)
class ObjRef:
    object_id: uuid.UUID
    label: str
    page_number: int
    object_type: PageObjectType
    # Keyed by option text; empty unless object_type is QUESTION.
    options: dict[str, OptionRef]

    def option_at(self, letter: str) -> OptionRef:
        """The option printed at *letter* (A–D) on this variant."""
        if self.object_type is not PageObjectType.QUESTION:
            raise LookupError(
                f"{self.label}: option_at needs a QUESTION, got {self.object_type.value}"
            )
        for opt in self.options.values():
            if opt.letter == letter:
                return opt
        raise LookupError(
            f"{self.label}: no option at letter {letter!r} "
            f"(letters: {''.join(sorted(o.letter for o in self.options.values()))})"
        )


@dataclass(frozen=True)
class CollVariant:
    key: str
    variant_id: uuid.UUID
    status: FormVariantStatus
    vid_value: str | None
    page_ids: tuple[uuid.UUID, ...]
    # One tuple per label: a single ObjRef, except VID (one per page, by page).
    objects: dict[str, tuple[ObjRef, ...]]

    def objs(self, label: str) -> tuple[ObjRef, ...]:
        return self.objects.get(label, ())

    def obj(self, label: str) -> ObjRef:
        refs = self.objs(label)
        if len(refs) != 1:
            raise LookupError(
                f"variant {self.key}: expected one object {label!r}, got {len(refs)}"
            )
        return refs[0]


@dataclass(frozen=True)
class CollProject:
    code: str
    project_id: uuid.UUID
    owner_id: uuid.UUID
    collaborator_id: uuid.UUID
    variants: dict[str, CollVariant]
    spec: ProjectSpec


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_coll_multi(db: Session) -> CollProject:
    return build_project(db, COLL_MULTI)


def build_coll_single(db: Session) -> CollProject:
    return build_project(db, COLL_SINGLE)


def build_coll_config(db: Session) -> CollProject:
    return build_project(db, COLL_CONFIG)


def build_project(
    db: Session,
    spec: ProjectSpec,
    owner: User | None = None,
    collaborator: User | None = None,
) -> CollProject:
    """*spec* as a real ACTIVE project of *owner*, *collaborator* a member.

    Users not given are made up through the ORM (the tests); ``seed_projects``
    passes the real users of a deployed environment.
    """
    if owner is None:
        owner = _make_user(db, "owner", OWNER_LAST_NAME)
    if collaborator is None:
        collaborator = _make_user(db, "collab", COLLABORATOR_LAST_NAME)

    project = form_project_service.create_project(
        db, owner, FormProjectCreate(type=FormProjectType.EXAM, name=spec.name)
    )
    form_project_service.add_collaborator(db, project.id, collaborator.id, owner)

    variant_ids = {v.key: _build_variant(db, project.id, v, owner) for v in spec.variants}

    for v in spec.variants:
        if v.publish:
            form_variant_service.publish_variant(db, variant_ids[v.key], owner)

    form_project_service.transition_project(
        db, project.id, FormProjectStatus.ACTIVE, owner
    )

    return CollProject(
        code=spec.code,
        project_id=project.id,
        owner_id=owner.id,
        collaborator_id=collaborator.id,
        variants={
            v.key: _read_variant(db, v, variant_ids[v.key]) for v in spec.variants
        },
        spec=spec,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

# The two operators are named, so a lock reads as an operator sees it
# ("In review by Coll Owner"), not as "Unnamed user".
USER_FIRST_NAME = "Coll"
OWNER_LAST_NAME = "Owner"
COLLABORATOR_LAST_NAME = "Collaborator"
OWNER_NAME = f"{USER_FIRST_NAME} {OWNER_LAST_NAME}"
COLLABORATOR_NAME = f"{USER_FIRST_NAME} {COLLABORATOR_LAST_NAME}"


def _make_user(db: Session, kind: str, last_name: str) -> User:
    u = User(
        email=f"coll-{kind}-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
        first_name=USER_FIRST_NAME,
        last_name=last_name,
    )
    db.add(u)
    db.flush()
    return u


def _build_variant(
    db: Session, project_id: uuid.UUID, spec: VariantSpec, owner: User
) -> uuid.UUID:
    variant = form_variant_service.create_variant(
        db, project_id, FormVariantCreate(name=spec.name, root_key=spec.root_key), owner
    )
    variant_id = variant.id
    first_page_id = form_variant_page_repo.list_for_variant(db, variant_id)[0].id

    # The VID must be shared, hold its role, and only THEN accept
    # include_page_index — all before the other pages exist, so add_page
    # replicates it (config included) onto each of them.
    vid = spec.vid
    if vid is not None:
        created = _create_code(db, first_page_id, vid, owner, initial=True)
        page_object_role_service.assign_role(
            db, created.id, PageObjectRole.VARIANT_IDENTIFIER, owner
        )
        code_object_service.update_code_config(
            db,
            created.id,
            CodeObjectConfigUpdate(include_page_index=vid.include_page_index),
            owner,
        )

    for _ in range(spec.page_count - 1):
        form_variant_service.add_page(db, variant_id, owner)

    page_ids = {
        p.page_index: p.id for p in form_variant_page_repo.list_for_variant(db, variant_id)
    }
    for obj in spec.objects:
        if obj is vid:
            continue
        created = _create_object(db, page_ids[obj.page], obj, owner)
        role = getattr(obj, "role", None)
        if role is not None:
            page_object_role_service.assign_role(db, created.id, role, owner)

    return variant_id


def _create_object(db: Session, page_id: uuid.UUID, spec: ObjectSpec, owner: User):
    if isinstance(spec, QuestionSpec):
        return question_object_service.create_question(
            db,
            page_id,
            QuestionPageObjectCreate(
                x=spec.x, y=spec.y, width=spec.w, height=spec.h,
                label=spec.label,
                config=QuestionObjectConfigCreate(
                    question_type=spec.question_type,
                    question_text=spec.question_text,
                    options=[
                        QuestionOptionCreate(
                            order_index=i,
                            option_text=o.text,
                            is_correct=o.is_correct,
                            score=o.score,
                        )
                        for i, o in enumerate(spec.options)
                    ],
                ),
            ),
            owner,
        )
    if isinstance(spec, MatrixSpec):
        return matrix_object_service.create_matrix(
            db,
            page_id,
            MatrixPageObjectCreate(
                x=spec.x, y=spec.y, width=spec.w, height=spec.h,
                label=spec.label,
                config=MatrixObjectConfigCreate(
                    rows_count=spec.rows,
                    cols_count=spec.cols,
                    evaluative=spec.evaluative,
                    selection_rule=spec.selection_rule,
                    rows=[
                        MatrixRowDefCreate(order_index=i, label=label)
                        for i, label in enumerate(spec.row_labels)
                    ],
                    cols=[
                        MatrixColDefCreate(order_index=i, label=label)
                        for i, label in enumerate(spec.col_labels)
                    ],
                    cells=[
                        MatrixCellDefCreate(
                            row_index=r, col_index=c, is_correct=True, score=1.0
                        )
                        for r, c in spec.correct_cells
                    ],
                ),
            ),
            owner,
        )
    if isinstance(spec, FreeTextSpec):
        return free_text_area_service.create_free_text_area(
            db,
            page_id,
            FreeTextAreaPageObjectCreate(
                x=spec.x, y=spec.y, width=spec.w, height=spec.h,
                label=spec.label, description=spec.description,
                config=FreeTextAreaConfigCreate(),
            ),
            owner,
        )
    if isinstance(spec, CodeSpec):
        return _create_code(db, page_id, spec, owner, initial=False)
    if isinstance(spec, TextSpec):
        return text_object_service.create_text(
            db,
            page_id,
            TextPageObjectCreate(
                x=spec.x, y=spec.y, width=spec.w, height=spec.h,
                label=spec.label,
                config=TextObjectConfigCreate(
                    text=spec.text, font_size=spec.font_size, bold=spec.bold
                ),
            ),
            owner,
        )
    raise TypeError(f"unsupported object spec {spec!r}")


def _create_code(
    db: Session, page_id: uuid.UUID, spec: CodeSpec, owner: User, *, initial: bool
):
    """Create a CODE. With *initial* the page index is left off: the service only
    accepts it once the object holds VARIANT_IDENTIFIER, so the VID sequence sets
    it afterwards."""
    return code_object_service.create_code(
        db,
        page_id,
        CodePageObjectCreate(
            x=spec.x, y=spec.y, width=spec.w, height=spec.h,
            label=spec.label,
            shared_across_pages=spec.shared_across_pages,
            config=CodeObjectConfigCreate(
                code_type=spec.code_type,
                encoding=spec.encoding,
                value=spec.value,
                human_readable=spec.human_readable,
                include_page_index=False if initial else spec.include_page_index,
            ),
        ),
        owner,
    )


# ---------------------------------------------------------------------------
# Read back
# ---------------------------------------------------------------------------


def _read_variant(db: Session, spec: VariantSpec, variant_id: uuid.UUID) -> CollVariant:
    pages = form_variant_page_repo.list_for_variant(db, variant_id)

    objects: dict[str, list[ObjRef]] = {}
    for page in pages:  # already ordered by page_index
        for obj in page_object_repo.list_for_page_with_configs(db, page.id):
            objects.setdefault(obj.label, []).append(_obj_ref(obj, page.page_index))

    vid = spec.vid
    return CollVariant(
        key=spec.key,
        variant_id=variant_id,
        status=form_variant_repo.get(db, variant_id).status,
        vid_value=vid.value if vid is not None else None,
        page_ids=tuple(p.id for p in pages),
        objects={label: tuple(refs) for label, refs in objects.items()},
    )


def _obj_ref(obj, page_number: int) -> ObjRef:
    options: dict[str, OptionRef] = {}
    if obj.object_type is PageObjectType.QUESTION and obj.question_config is not None:
        for opt in obj.question_config.options:
            options[opt.option_text] = OptionRef(
                option_id=opt.id,
                letter=LETTERS[opt.order_index],
                text=opt.option_text,
                is_correct=opt.is_correct,
                score=opt.score,
            )
    return ObjRef(
        object_id=obj.id,
        label=obj.label,
        page_number=page_number,
        object_type=obj.object_type,
        options=options,
    )
