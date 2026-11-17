# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for CodeObjectConfig."""
from __future__ import annotations

import uuid
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from app.models.code_object_config import CodeObjectConfig
from app.models.enums import FormVariantStatus, PageObjectRole
from app.models.form_variant import FormVariant
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject


def add(db: Session, config: CodeObjectConfig) -> CodeObjectConfig:
    db.add(config)
    db.flush()
    return config


class VariantIdentifierMatch(NamedTuple):
    """Which published variants claim one VARIANT_IDENTIFIER value.

    Normally a value belongs to exactly one variant, so *candidate_ids* holds a
    single id and :attr:`variant_id` is the answer. It is a LIST rather than a
    lone id purely so the abnormal case stays visible: two published variants of
    one project sharing an identifier value are indistinguishable on paper, and
    silently keeping the first would attribute roughly half the submissions to the
    wrong variant with no signal anywhere.
    """

    # Every published variant carrying the value, ordered by version_number.
    # Never empty — no match is reported as None instead of an empty match.
    candidate_ids: tuple[uuid.UUID, ...]

    @property
    def variant_id(self) -> uuid.UUID:
        """The variant that WINS: lowest version_number, always the same one."""
        return self.candidate_ids[0]

    @property
    def is_ambiguous(self) -> bool:
        """True when more than one published variant claims the value."""
        return len(self.candidate_ids) > 1


def find_published_variant_by_identifier_value(
    db: Session, form_project_id: uuid.UUID, value: str
) -> VariantIdentifierMatch | None:
    """The PUBLISHED variants of *form_project_id* whose VARIANT_IDENTIFIER
    PageObject has ``CodeObjectConfig.value == value``, or None if there are none.

    The lookup at the heart of matching: a scanned sheet carries the
    RAW ``VID:<value>`` payload, and this answers "which printable variant was
    that value printed on?" in ONE query, walking
    FormVariantRoleBinding -> PageObject -> CodeObjectConfig -> FormVariant.

    Compares the PARSED value, never the raw payload: the tag and the optional
    ``|<page_index>`` suffix are added at export time and
    are not part of what the author configured. The caller strips them with
    ``code_rendering.parse_tagged_payload`` before calling.

    At most one variant is expected — ``uq_form_variant_role_binding_role``
    allows a single VARIANT_IDENTIFIER per variant, and two published variants of
    one project sharing an identifier VALUE would be a misconfiguration that
    makes them indistinguishable on paper anyway. Should it happen, the lowest
    ``version_number`` wins DETERMINISTICALLY rather than raising: matching is on
    the "never blow up on real-world input" side of the pipeline, exactly like
    decoding.

    ALL the rows are returned rather than just that winner, in the SAME single
    query — nothing extra is executed, the surplus rows simply stop being
    discarded. The caller needs them to report the ambiguity (see
    ``VariantIdentifierMatch``): resolving deterministically AND surfacing the
    anomaly is the rule everywhere on this path (``has_conflicting_codes``,
    ``has_group_key_mismatch``, ``pages_with_duplicate_position``).
    """
    stmt = (
        select(FormVariant.id)
        .join(
            FormVariantRoleBinding,
            FormVariantRoleBinding.form_variant_id == FormVariant.id,
        )
        .join(PageObject, PageObject.id == FormVariantRoleBinding.page_object_id)
        .join(CodeObjectConfig, CodeObjectConfig.page_object_id == PageObject.id)
        .where(
            FormVariant.form_project_id == form_project_id,
            FormVariant.status == FormVariantStatus.PUBLISHED,
            FormVariantRoleBinding.role == PageObjectRole.VARIANT_IDENTIFIER,
            CodeObjectConfig.value == value,
        )
        .order_by(FormVariant.version_number)
    )
    candidate_ids = tuple(db.scalars(stmt).all())
    return VariantIdentifierMatch(candidate_ids) if candidate_ids else None


def find_duplicate_variant_identifier_values(
    db: Session, form_project_id: uuid.UUID
) -> list[VariantIdentifierMatch]:
    """Every VARIANT_IDENTIFIER value shared by more than one PUBLISHED variant
    of *form_project_id*. Empty list when there is no collision.

    Defence in depth for scan-readiness: ``publish_variant`` prevents
    NEW collisions, but data predating that check can already carry one. Same join
    as :func:`find_published_variant_by_identifier_value`, grouped by value with
    ``HAVING count(*) > 1`` — one query for the whole project rather than a
    per-value lookup.

    Each group becomes a ``VariantIdentifierMatch`` whose ``candidate_ids`` are
    ordered by ``version_number``, exactly like the single-value function, so
    ``variant_id`` still names the variant matching would deterministically pick.
    The ordering is done inside the aggregate (``array_agg(... ORDER BY ...)``) —
    no extra join and no second query to sort in Python. Groups themselves come
    back ordered by value, so the payload is stable across calls.
    """
    stmt = (
        select(
            CodeObjectConfig.value,
            func.array_agg(
                aggregate_order_by(FormVariant.id, FormVariant.version_number)
            ),
        )
        .select_from(FormVariant)
        .join(
            FormVariantRoleBinding,
            FormVariantRoleBinding.form_variant_id == FormVariant.id,
        )
        .join(PageObject, PageObject.id == FormVariantRoleBinding.page_object_id)
        .join(CodeObjectConfig, CodeObjectConfig.page_object_id == PageObject.id)
        .where(
            FormVariant.form_project_id == form_project_id,
            FormVariant.status == FormVariantStatus.PUBLISHED,
            FormVariantRoleBinding.role == PageObjectRole.VARIANT_IDENTIFIER,
        )
        .group_by(CodeObjectConfig.value)
        .having(func.count(FormVariant.id) > 1)
        .order_by(CodeObjectConfig.value)
    )
    return [
        VariantIdentifierMatch(tuple(candidate_ids))
        for _value, candidate_ids in db.execute(stmt).all()
    ]
