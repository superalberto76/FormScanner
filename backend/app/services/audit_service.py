# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Named audit-event helpers.

Each function adds an AuditLog row to the current session.
The caller is responsible for committing (or the entry is committed with the
surrounding business-logic transaction).
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.repositories import audit_repo

# ---------------------------------------------------------------------------
# Event name constants — use these instead of raw strings everywhere
# ---------------------------------------------------------------------------
TEMPLATE_CREATED = "template.created"
VERSION_CREATED = "version.created"
VERSION_CLONED = "version.cloned"
VERSION_UPDATED = "version.updated"
VERSION_PUBLISHED = "version.published"
TEMPLATE_DELETED = "template.deleted"
VERSION_UPLOAD_SOURCE = "version.upload_source"
VERSION_RENDER_PAGES = "version.render_pages"
CODE_DETECTION_RUN = "code.detection_run"
CODE_REGION_CREATED = "code_region.created"
CODE_REGION_UPDATED = "code_region.updated"
CODE_REGION_DELETED = "code_region.deleted"
AREA_CREATED = "area.created"
AREA_UPDATED = "area.updated"
AREA_DELETED = "area.deleted"
AREA_TOGGLED_CONFIGURED = "area.toggled_configured"
MARK_ADDED = "mark.added"
MARK_DELETED = "mark.deleted"
MATRIX_CONFIG_UPSERTED = "matrix_config.upserted"
MATRIX_MARK_ADDED = "matrix_mark.added"
MATRIX_MARK_DELETED = "matrix_mark.deleted"
SCORING_UPSERTED = "scoring.upserted"
# --- User management ---
USER_CREATED = "user.created"
USER_DISABLED = "user.disabled"
USER_ENABLED = "user.enabled"
USER_PASSWORD_RESET = "user.password_reset"
# --- FormProject / FormVariant ---
FORM_PROJECT_CREATED = "form_project.created"
FORM_PROJECT_UPDATED = "form_project.updated"
FORM_PROJECT_DELETED = "form_project.deleted"
# Project membership management
FORM_PROJECT_MEMBER_ADDED = "form_project_member.added"
FORM_PROJECT_MEMBER_REMOVED = "form_project_member.removed"
FORM_PROJECT_MEMBER_ROLE_CHANGED = "form_project_member.role_changed"
FORM_VARIANT_CREATED = "form_variant.created"
FORM_VARIANT_UPDATED = "form_variant.updated"
FORM_VARIANT_PUBLISHED = "form_variant.published"
FORM_VARIANT_CLONED = "form_variant.cloned"
FORM_VARIANT_PAGE_ADDED = "form_variant_page.added"
FORM_VARIANT_PAGE_DUPLICATED = "form_variant_page.duplicated"
FORM_VARIANT_PAGE_REORDERED = "form_variant_page.reordered"
FORM_VARIANT_PAGE_REMOVED = "form_variant_page.removed"
FORM_VARIANT_DELETED = "form_variant.deleted"
# --- PageObject + typed configs / children ---
PAGE_OBJECT_CREATED = "page_object.created"
PAGE_OBJECT_UPDATED = "page_object.updated"
PAGE_OBJECT_DELETED = "page_object.deleted"
PAGE_OBJECT_CONFIG_UPDATED = "page_object.config_updated"
# Shared "template page" objects
PAGE_OBJECT_SHARED = "page_object.shared"
PAGE_OBJECT_UNSHARED = "page_object.unshared"
PAGE_OBJECT_TYPED_CONFIG_RESYNCED = "page_object.typed_config_resynced"
PAGE_OBJECT_RELINKED = "page_object.relinked"
# Special scanning role (identification)
PAGE_OBJECT_ROLE_ASSIGNED = "page_object.role_assigned"
PAGE_OBJECT_ROLE_REMOVED = "page_object.role_removed"
QUESTION_OPTION_CREATED = "question_option.created"
QUESTION_OPTION_UPDATED = "question_option.updated"
QUESTION_OPTION_DELETED = "question_option.deleted"
MATRIX_ROW_CREATED = "matrix_row.created"
MATRIX_ROW_UPDATED = "matrix_row.updated"
MATRIX_ROW_DELETED = "matrix_row.deleted"
MATRIX_COL_CREATED = "matrix_col.created"
MATRIX_COL_UPDATED = "matrix_col.updated"
MATRIX_COL_DELETED = "matrix_col.deleted"
# Scanning pipeline ingestion (identification)
SUBMISSION_BATCH_CREATED = "submission_batch.created"
SUBMISSION_CREATED = "submission.created"
# Loose-image ingestion: one event per upload CALL, because a single call can
# open several submissions, extend others and reject a file or two.
SUBMISSION_IMAGES_INGESTED = "submission.images_ingested"
# Variant matching: one event per submission whose variant was DECIDED —
# resolved or not — by a matching run, plus one summary event per manual re-run
# of a whole batch. A submission whose variant was already resolved logs
# nothing: matching is idempotent and re-deciding a settled fact is not an event.
SUBMISSION_MATCHED = "submission.matched"
SUBMISSION_MATCH_FAILED = "submission.match_failed"
SUBMISSION_MATCHING_RUN = "submission_batch.matching_run"
# Manual recovery of a MATCH_FAILED submission: an operator names the
# variant the engine could not resolve. Deliberately NOT SUBMISSION_MATCHED — the
# audit trail must be able to tell an automatic decision from a human one, since
# the two carry very different evidence (a code that was read vs. a judgement).
SUBMISSION_MANUALLY_MATCHED = "submission.manually_matched"
# A MATCH_FAILED or UNASSIGNED submission marked unresolvable by an operator,
# and that verdict undone; the metadata names the status it moved to and from.
# Who and when live only here: the submission carries the status, and the
# status it was marked from.
SUBMISSION_MARKED_UNRESOLVABLE = "submission.marked_unresolvable"
SUBMISSION_UNRESOLVABLE_UNDONE = "submission.unresolvable_undone"
# Confidence threshold moved on a batch. Worth an event of its own
# because it retroactively rewrites ``review_required`` on results already
# stored: the metadata records the old and the new threshold plus how many
# results actually changed side, which is the only trace of a rewrite that
# touches many rows at once and none of them individually.
SUBMISSION_BATCH_CONFIDENCE_THRESHOLD_UPDATED = (
    "submission_batch.confidence_threshold_updated"
)
# One manual extraction run over a batch. ONE event for the whole call, never
# one per result: a run reads thousands of objects, and a per-object trail would
# bury every other event in the project's history while saying nothing an
# operator could act on. The metadata carries the run's counters — including how
# many objects this build cannot read, which makes that gap visible on real
# data — and the first few failures by coordinate. The results themselves are
# the record of what was read.
SUBMISSION_BATCH_EXTRACTION_RUN = "submission_batch.extraction_run"
# An operator took a submission's review soft lock away from a colleague who was
# still active on it. The ONLY lock event: acquire, renew and release
# happen on every open, save and exit, and a trail of them would be pure noise.
# A take-over is different — it is one operator overriding another, and when
# Mario asks why his save was refused, this is the answer. The metadata names
# the previous holder and their last activity. Taking a free or expired lock
# through the take-over route logs nothing: nobody was overridden.
SUBMISSION_LOCK_TAKEN_OVER = "submission.lock_taken_over"
# An operator brought a MATCHED submission into review although some of its
# objects could not be read ("Review anyway"). A human decision that
# replaces a reading with a manual entry from paper, so it is recorded: the
# metadata counts the placeholders created and names the objects they stand for.
SUBMISSION_REVIEWED_ANYWAY = "submission.reviewed_anyway"
# An operator set or corrected a submission's candidate identifier by hand.
# The candidate attribution is the most sensitive fact of a submission — it
# decides whose answers these are — so every edit keeps the old and the new
# value.
SUBMISSION_CANDIDATE_EDITED = "submission.candidate_edited"
# An operator confirmed, in one click, every still-unresolved reading of a
# submission that may be accepted unseen ("Accept all remaining").
# The single review actions write NO audit event — the append-only review log
# is their trail — but this one act rules on many readings at once, so it is
# recorded as such. The metadata carries ``accepted_count``.
SUBMISSION_ACCEPTED_REMAINING = "submission.accepted_remaining"
# An operator closed a submission: SCANNED → REVIEWED, read-only until
# reopened. Who closed it and when live here only — no column on the
# Submission. The metadata carries ``unresolved_count`` (the results left
# unresolved) and the ``review_version`` the closing produced.
SUBMISSION_CLOSED = "submission.closed"
# An operator reopened a closed submission: REVIEWED → SCANNED, the lock
# theirs. Recorded like the close, in the trail only. The metadata carries
# ``unresolved_count`` (the results unresolved once reopened, after catching up
# with the threshold in force) and the ``review_version`` the reopening
# produced.
SUBMISSION_REOPENED = "submission.reopened"
MATRIX_CELL_CREATED = "matrix_cell.created"
MATRIX_CELL_UPDATED = "matrix_cell.updated"
MATRIX_CELL_DELETED = "matrix_cell.deleted"


def log_template_created(
    db: Session, actor_id: uuid.UUID, template_id: uuid.UUID
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=TEMPLATE_CREATED,
        entity_type="TemplateTest",
        entity_id=template_id,
    )


def log_version_created(
    db: Session,
    actor_id: uuid.UUID,
    version_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=VERSION_CREATED,
        entity_type="TemplateVersion",
        entity_id=version_id,
        metadata_json=metadata,
    )


def log_version_cloned(
    db: Session,
    actor_id: uuid.UUID,
    new_version_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=VERSION_CLONED,
        entity_type="TemplateVersion",
        entity_id=new_version_id,
        metadata_json=metadata,
    )


def log_version_updated(
    db: Session,
    actor_id: uuid.UUID,
    version_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=VERSION_UPDATED,
        entity_type="TemplateVersion",
        entity_id=version_id,
        metadata_json=metadata,
    )


def log_version_published(
    db: Session, actor_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=VERSION_PUBLISHED,
        entity_type="TemplateVersion",
        entity_id=version_id,
    )


def log_template_deleted(
    db: Session, actor_id: uuid.UUID, template_id: uuid.UUID
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=TEMPLATE_DELETED,
        entity_type="TemplateTest",
        entity_id=template_id,
    )


def log_version_upload_source(
    db: Session,
    actor_id: uuid.UUID,
    version_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=VERSION_UPLOAD_SOURCE,
        entity_type="TemplateVersion",
        entity_id=version_id,
        metadata_json=metadata,
    )


def log_version_render_pages(
    db: Session,
    actor_id: uuid.UUID,
    version_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=VERSION_RENDER_PAGES,
        entity_type="TemplateVersion",
        entity_id=version_id,
        metadata_json=metadata,
    )


def log_code_detection_run(
    db: Session,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID,
    entity_type: str,  # "DocumentPage" or "TemplateVersion"
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=CODE_DETECTION_RUN,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=metadata,
    )


def log_code_region_created(
    db: Session,
    actor_id: uuid.UUID,
    code_region_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=CODE_REGION_CREATED,
        entity_type="CodeRegion",
        entity_id=code_region_id,
        metadata_json=metadata,
    )


def log_code_region_updated(
    db: Session,
    actor_id: uuid.UUID,
    code_region_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=CODE_REGION_UPDATED,
        entity_type="CodeRegion",
        entity_id=code_region_id,
        metadata_json=metadata,
    )


def log_code_region_deleted(
    db: Session,
    actor_id: uuid.UUID,
    code_region_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=CODE_REGION_DELETED,
        entity_type="CodeRegion",
        entity_id=code_region_id,
        metadata_json=metadata,
    )


def log_area_created(
    db: Session,
    actor_id: uuid.UUID,
    area_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=AREA_CREATED,
        entity_type="SensitiveArea",
        entity_id=area_id,
        metadata_json=metadata,
    )


def log_area_updated(
    db: Session,
    actor_id: uuid.UUID,
    area_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=AREA_UPDATED,
        entity_type="SensitiveArea",
        entity_id=area_id,
        metadata_json=metadata,
    )


def log_area_deleted(
    db: Session,
    actor_id: uuid.UUID,
    area_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=AREA_DELETED,
        entity_type="SensitiveArea",
        entity_id=area_id,
        metadata_json=metadata,
    )


def log_area_toggled_configured(
    db: Session,
    actor_id: uuid.UUID,
    area_id: uuid.UUID,
    configured: bool,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=AREA_TOGGLED_CONFIGURED,
        entity_type="SensitiveArea",
        entity_id=area_id,
        metadata_json={"configured": configured},
    )


def log_mark_added(
    db: Session,
    actor_id: uuid.UUID,
    mark_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=MARK_ADDED,
        entity_type="AnswerMark",
        entity_id=mark_id,
        metadata_json=metadata,
    )


def log_mark_deleted(
    db: Session,
    actor_id: uuid.UUID,
    mark_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=MARK_DELETED,
        entity_type="AnswerMark",
        entity_id=mark_id,
        metadata_json=metadata,
    )


def log_matrix_config_upserted(
    db: Session,
    actor_id: uuid.UUID,
    area_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=MATRIX_CONFIG_UPSERTED,
        entity_type="MatrixConfig",
        entity_id=area_id,
        metadata_json=metadata,
    )


def log_matrix_mark_added(
    db: Session,
    actor_id: uuid.UUID,
    mark_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=MATRIX_MARK_ADDED,
        entity_type="MatrixCellMark",
        entity_id=mark_id,
        metadata_json=metadata,
    )


def log_matrix_mark_deleted(
    db: Session,
    actor_id: uuid.UUID,
    mark_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=MATRIX_MARK_DELETED,
        entity_type="MatrixCellMark",
        entity_id=mark_id,
        metadata_json=metadata,
    )


def log_scoring_upserted(
    db: Session,
    actor_id: uuid.UUID,
    area_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=SCORING_UPSERTED,
        entity_type="ScoringRule",
        entity_id=area_id,
        metadata_json=metadata,
    )


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


def log_user_created(
    db: Session,
    actor_id: uuid.UUID,
    new_user_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=USER_CREATED,
        entity_type="User",
        entity_id=new_user_id,
        metadata_json=metadata,
    )


# ---------------------------------------------------------------------------
# FormProject / FormVariant
# ---------------------------------------------------------------------------


def log_form_project_created(
    db: Session, actor_id: uuid.UUID, project_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_PROJECT_CREATED,
        entity_type="FormProject",
        entity_id=project_id,
        metadata_json=metadata,
    )


def log_form_project_updated(
    db: Session, actor_id: uuid.UUID, project_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_PROJECT_UPDATED,
        entity_type="FormProject",
        entity_id=project_id,
        metadata_json=metadata,
    )


def log_form_project_deleted(
    db: Session, actor_id: uuid.UUID, project_id: uuid.UUID,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_PROJECT_DELETED,
        entity_type="FormProject",
        entity_id=project_id,
    )


def log_form_variant_created(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_CREATED,
        entity_type="FormVariant",
        entity_id=variant_id,
        metadata_json=metadata,
    )


def log_form_variant_updated(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_UPDATED,
        entity_type="FormVariant",
        entity_id=variant_id,
        metadata_json=metadata,
    )


def log_form_variant_published(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_PUBLISHED,
        entity_type="FormVariant",
        entity_id=variant_id,
    )


def log_form_variant_cloned(
    db: Session, actor_id: uuid.UUID, new_variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_CLONED,
        entity_type="FormVariant",
        entity_id=new_variant_id,
        metadata_json=metadata,
    )


def log_form_variant_page_added(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_PAGE_ADDED,
        entity_type="FormVariant",
        entity_id=variant_id,
        metadata_json=metadata,
    )


def log_form_variant_page_duplicated(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_PAGE_DUPLICATED,
        entity_type="FormVariant",
        entity_id=variant_id,
        metadata_json=metadata,
    )


def log_form_variant_page_reordered(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_PAGE_REORDERED,
        entity_type="FormVariant",
        entity_id=variant_id,
        metadata_json=metadata,
    )


def log_form_variant_page_removed(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_PAGE_REMOVED,
        entity_type="FormVariant",
        entity_id=variant_id,
        metadata_json=metadata,
    )


def log_form_variant_deleted(
    db: Session, actor_id: uuid.UUID, variant_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_id,
        action=FORM_VARIANT_DELETED,
        entity_type="FormVariant",
        entity_id=variant_id,
        metadata_json=metadata,
    )


# ---------------------------------------------------------------------------
# Generic event helper — used by the PageObject editor for the many
# small object/config/child mutations, instead of one named helper each.
# ---------------------------------------------------------------------------


def log_event(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str | None = None,
    metadata: dict | None = None,
) -> None:
    audit_repo.log(
        db,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=metadata,
    )
