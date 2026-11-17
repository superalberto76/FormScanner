# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""initial schema

Revision ID: a7c3e9d1f5b2
Revises:
Create Date: 2026-11-10 00:00:00.000000

Creates the whole FormScanner schema on an empty database: 19 enum types and
25 tables.

  Users and groups      users, groups, group_memberships, audit_logs
  Projects and variants form_projects, form_project_members, form_variants,
                        form_variant_pages
  Page objects          page_objects and one configuration table per object
                        type (text, code, free text area, question with its
                        options, matrix with its rows, columns and cells,
                        image); form_variant_role_bindings for the objects
                        with a scanning role
  Scanning and review   submission_batches, submissions, submission_pages,
                        submission_object_results, submission_reviews

Every constraint and index has an explicit name: pk_<table>,
fk_<table>_<column>, uq_<...> and ix_<table>_<column>. The partial unique
index uq_submissions_batch_source_group_key keeps two submissions of the same
batch from sharing a source group.

Enum types are created idempotently (DO $$ ... EXCEPTION WHEN
duplicate_object) and the columns use postgresql.ENUM(..., create_type=False),
so a type is never created twice. The order of the values of an enum is part
of the schema: ORDER BY on an enum column follows it.

downgrade() drops every table, then every enum type, leaving an empty
database.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "a7c3e9d1f5b2"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_NOW = sa.text("now()")
_FALSE = sa.text("false")
_TRUE = sa.text("true")

# Enum types and their values, in the order they are created.
_ENUMS: dict[str, tuple[str, ...]] = {
    "user_role_enum": ("USER", "ADMIN"),
    "ui_theme_enum": ("LIGHT", "DARK", "SYSTEM"),
    "membership_role_enum": ("MEMBER", "MANAGER"),
    "form_project_type_enum": ("EXAM", "SURVEY", "QUESTIONNAIRE", "GENERIC_FORM"),
    "form_project_status_enum": ("DRAFT", "ACTIVE", "CLOSED", "ARCHIVED"),
    "project_role_enum": ("OWNER", "COLLABORATOR"),
    "form_variant_status_enum": ("DRAFT", "PUBLISHED", "ARCHIVED"),
    "page_object_type_enum": (
        "TEXT", "CODE", "FREE_TEXT_AREA", "QUESTION", "MATRIX", "IMAGE",
    ),
    "page_object_role_enum": ("VARIANT_IDENTIFIER", "RESPONDENT_IDENTIFIER"),
    "code_object_code_type_enum": ("QR", "BARCODE"),
    "question_type_enum": ("SINGLE_CHOICE", "MULTIPLE_CHOICE"),
    "matrix_object_selection_rule_enum": (
        "ONE_PER_COLUMN", "ONE_PER_ROW", "MANY_PER_COLUMN", "MANY_PER_ROW", "FREE",
    ),
    "submission_status_enum": (
        "UPLOADED", "MATCHED", "SCANNED", "REVIEWED", "FINALIZED",
        "UNASSIGNED", "MATCH_FAILED", "UNRESOLVABLE",
    ),
    "match_failure_reason_enum": (
        "NO_PUBLISHED_VARIANT", "NO_VID_FOUND", "VID_NOT_RECOGNISED",
    ),
    "page_order_source_enum": ("CODE", "PDF_ORDER", "FILENAME"),
    "page_alignment_status_enum": ("ALIGNED", "NOT_ALIGNED"),
    "submission_result_type_enum": (
        "QUESTION", "MATRIX", "FREE_TEXT", "CODE", "RESPONDENT_IDENTIFIER",
    ),
    "review_outcome_enum": ("CONFIRMED", "SUBSTITUTED", "FLAGGED"),
    "review_action_enum": ("CONFIRMED", "SUBSTITUTED", "FLAGGED", "UNDONE"),
}

# Tables in creation order: every table comes after the tables it references.
_TABLES: tuple[str, ...] = (
    "users",
    "groups",
    "group_memberships",
    "audit_logs",
    "form_projects",
    "form_project_members",
    "form_variants",
    "form_variant_pages",
    "page_objects",
    "text_object_configs",
    "code_object_configs",
    "free_text_area_configs",
    "question_object_configs",
    "question_options",
    "matrix_object_configs",
    "matrix_row_defs",
    "matrix_col_defs",
    "matrix_cell_defs",
    "image_object_configs",
    "form_variant_role_bindings",
    "submission_batches",
    "submissions",
    "submission_pages",
    "submission_object_results",
    "submission_reviews",
)


def _enum(name: str) -> postgresql.ENUM:
    """A column type for an enum created in ``upgrade``, never re-created."""
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
    )


def _pk(table: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint("id", name=f"pk_{table}")


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], name=f"fk_{table}_{column}")


def _index(table: str, column: str) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # Enum types (idempotent; never recreated).
    # -----------------------------------------------------------------------
    for name, values in _ENUMS.items():
        literals = ", ".join(f"'{value}'" for value in values)
        op.execute(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({literals});"
            " EXCEPTION WHEN duplicate_object THEN END $$;"
        )

    # =======================================================================
    # Users and groups
    # =======================================================================
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("role", _enum("user_role_enum"), nullable=False),
        sa.Column("disabled", sa.Boolean(), server_default=_FALSE, nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("avatar_path", sa.String(1000), nullable=True),
        sa.Column("avatar_mime", sa.String(100), nullable=True),
        sa.Column("avatar_original_filename", sa.String(500), nullable=True),
        sa.Column("ui_theme", _enum("ui_theme_enum"), nullable=False),
        _pk("users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        _created_at(),
        _updated_at(),
        _pk("groups"),
    )

    op.create_table(
        "group_memberships",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("membership_role", _enum("membership_role_enum"), nullable=False),
        _created_at(),
        _fk("group_memberships", "group_id", "groups.id"),
        _fk("group_memberships", "user_id", "users.id"),
        _pk("group_memberships"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_membership"),
    )
    _index("group_memberships", "user_id")

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(255), nullable=False),
        sa.Column("entity_id", sa.String(255), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        _created_at(),
        _fk("audit_logs", "actor_user_id", "users.id"),
        _pk("audit_logs"),
    )
    _index("audit_logs", "action")
    _index("audit_logs", "actor_user_id")
    _index("audit_logs", "entity_id")
    _index("audit_logs", "entity_type")

    # =======================================================================
    # Projects and variants
    # =======================================================================
    op.create_table(
        "form_projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("type", _enum("form_project_type_enum"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", _enum("form_project_status_enum"), nullable=False),
        _created_at(),
        _updated_at(),
        _fk("form_projects", "owner_id", "users.id"),
        _pk("form_projects"),
    )
    _index("form_projects", "owner_id")
    _index("form_projects", "type")
    _index("form_projects", "status")

    op.create_table(
        "form_project_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", _enum("project_role_enum"), nullable=False),
        _created_at(),
        _fk("form_project_members", "project_id", "form_projects.id"),
        _fk("form_project_members", "user_id", "users.id"),
        _pk("form_project_members"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_form_project_member"),
    )
    _index("form_project_members", "user_id")

    op.create_table(
        "form_variants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("form_project_id", sa.UUID(), nullable=False),
        sa.Column("root_key", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("status", _enum("form_variant_status_enum"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_file_path", sa.String(1000), nullable=True),
        sa.Column("original_filename", sa.String(500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        _created_at(),
        _updated_at(),
        _fk("form_variants", "form_project_id", "form_projects.id"),
        _fk("form_variants", "created_by_user_id", "users.id"),
        _fk("form_variants", "updated_by_user_id", "users.id"),
        _pk("form_variants"),
        sa.UniqueConstraint(
            "form_project_id", "version_number", name="uq_form_variant_version_number"
        ),
    )
    _index("form_variants", "form_project_id")
    _index("form_variants", "root_key")
    _index("form_variants", "status")

    op.create_table(
        "form_variant_pages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("form_variant_id", sa.UUID(), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(1000), nullable=True),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column("dpi", sa.Float(), nullable=True),
        _created_at(),
        _fk("form_variant_pages", "form_variant_id", "form_variants.id"),
        _pk("form_variant_pages"),
        sa.UniqueConstraint(
            "form_variant_id", "page_index", name="uq_form_variant_page_index"
        ),
    )

    # =======================================================================
    # Page objects
    # =======================================================================
    op.create_table(
        "page_objects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("form_variant_page_id", sa.UUID(), nullable=False),
        sa.Column("object_type", _enum("page_object_type_enum"), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("z_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("visible", sa.Boolean(), server_default=_TRUE, nullable=False),
        sa.Column("locked", sa.Boolean(), server_default=_FALSE, nullable=False),
        _created_at(),
        _updated_at(),
        # Linked copies of an object share the same group id.
        sa.Column("shared_group_id", sa.UUID(), nullable=True),
        sa.Column(
            "shared_across_pages", sa.Boolean(), server_default=_FALSE, nullable=False
        ),
        _fk("page_objects", "form_variant_page_id", "form_variant_pages.id"),
        _pk("page_objects"),
    )
    _index("page_objects", "form_variant_page_id")
    _index("page_objects", "object_type")
    _index("page_objects", "shared_group_id")

    op.create_table(
        "text_object_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("font_size", sa.Integer(), server_default=sa.text("12"), nullable=False),
        sa.Column("font_family", sa.String(255), nullable=True),
        sa.Column("bold", sa.Boolean(), server_default=_FALSE, nullable=False),
        sa.Column("italic", sa.Boolean(), server_default=_FALSE, nullable=False),
        _created_at(),
        _updated_at(),
        _fk("text_object_configs", "page_object_id", "page_objects.id"),
        _pk("text_object_configs"),
        sa.UniqueConstraint(
            "page_object_id", name="uq_text_object_configs_page_object_id"
        ),
    )

    op.create_table(
        "code_object_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("code_type", _enum("code_object_code_type_enum"), nullable=False),
        sa.Column("encoding", sa.String(100), nullable=True),
        sa.Column("value", sa.String(2000), nullable=False),
        sa.Column("human_readable", sa.Boolean(), server_default=_FALSE, nullable=False),
        _created_at(),
        _updated_at(),
        sa.Column(
            "include_page_index", sa.Boolean(), server_default=_FALSE, nullable=False
        ),
        _fk("code_object_configs", "page_object_id", "page_objects.id"),
        _pk("code_object_configs"),
        sa.UniqueConstraint(
            "page_object_id", name="uq_code_object_configs_page_object_id"
        ),
    )

    op.create_table(
        "free_text_area_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("placeholder", sa.String(500), nullable=True),
        sa.Column("max_length", sa.Integer(), nullable=True),
        _created_at(),
        _updated_at(),
        _fk("free_text_area_configs", "page_object_id", "page_objects.id"),
        _pk("free_text_area_configs"),
        sa.UniqueConstraint(
            "page_object_id", name="uq_free_text_area_configs_page_object_id"
        ),
    )

    op.create_table(
        "question_object_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("question_type", _enum("question_type_enum"), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("shuffle_answers", sa.Boolean(), server_default=_FALSE, nullable=False),
        _created_at(),
        _updated_at(),
        _fk("question_object_configs", "page_object_id", "page_objects.id"),
        _pk("question_object_configs"),
        sa.UniqueConstraint(
            "page_object_id", name="uq_question_object_configs_page_object_id"
        ),
    )

    op.create_table(
        "question_options",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("question_object_config_id", sa.UUID(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("option_text", sa.Text(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), server_default=_FALSE, nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        _created_at(),
        _updated_at(),
        _fk("question_options", "question_object_config_id", "question_object_configs.id"),
        _pk("question_options"),
        sa.UniqueConstraint(
            "question_object_config_id", "order_index", name="uq_question_option_order"
        ),
    )
    _index("question_options", "question_object_config_id")

    op.create_table(
        "matrix_object_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("rows_count", sa.Integer(), nullable=False),
        sa.Column("cols_count", sa.Integer(), nullable=False),
        sa.Column("evaluative", sa.Boolean(), server_default=_FALSE, nullable=False),
        sa.Column(
            "selection_rule", _enum("matrix_object_selection_rule_enum"), nullable=False
        ),
        _created_at(),
        _updated_at(),
        _fk("matrix_object_configs", "page_object_id", "page_objects.id"),
        _pk("matrix_object_configs"),
        sa.UniqueConstraint(
            "page_object_id", name="uq_matrix_object_configs_page_object_id"
        ),
    )

    op.create_table(
        "matrix_row_defs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("matrix_object_config_id", sa.UUID(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        _created_at(),
        _updated_at(),
        _fk("matrix_row_defs", "matrix_object_config_id", "matrix_object_configs.id"),
        _pk("matrix_row_defs"),
        sa.UniqueConstraint(
            "matrix_object_config_id", "order_index", name="uq_matrix_row_order"
        ),
    )
    _index("matrix_row_defs", "matrix_object_config_id")

    op.create_table(
        "matrix_col_defs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("matrix_object_config_id", sa.UUID(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        _created_at(),
        _updated_at(),
        _fk("matrix_col_defs", "matrix_object_config_id", "matrix_object_configs.id"),
        _pk("matrix_col_defs"),
        sa.UniqueConstraint(
            "matrix_object_config_id", "order_index", name="uq_matrix_col_order"
        ),
    )
    _index("matrix_col_defs", "matrix_object_config_id")

    op.create_table(
        "matrix_cell_defs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("matrix_object_config_id", sa.UUID(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("col_index", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        _created_at(),
        _updated_at(),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        _fk("matrix_cell_defs", "matrix_object_config_id", "matrix_object_configs.id"),
        _pk("matrix_cell_defs"),
        sa.UniqueConstraint(
            "matrix_object_config_id", "row_index", "col_index",
            name="uq_matrix_cell_coords",
        ),
    )
    _index("matrix_cell_defs", "matrix_object_config_id")

    op.create_table(
        "image_object_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("original_filename", sa.String(500), nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column(
            "fit", sa.String(20), server_default=sa.text("'contain'"), nullable=False
        ),
        _created_at(),
        _updated_at(),
        _fk("image_object_configs", "page_object_id", "page_objects.id"),
        _pk("image_object_configs"),
        sa.UniqueConstraint(
            "page_object_id", name="uq_image_object_configs_page_object_id"
        ),
    )

    # The object that carries a scanning role (variant or respondent
    # identifier) in a variant: at most one object per role, one role per
    # object.
    op.create_table(
        "form_variant_role_bindings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("form_variant_id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("role", _enum("page_object_role_enum"), nullable=False),
        _created_at(),
        _fk("form_variant_role_bindings", "form_variant_id", "form_variants.id"),
        _fk("form_variant_role_bindings", "page_object_id", "page_objects.id"),
        _pk("form_variant_role_bindings"),
        sa.UniqueConstraint(
            "form_variant_id", "role", name="uq_form_variant_role_binding_role"
        ),
        sa.UniqueConstraint(
            "page_object_id", name="uq_form_variant_role_binding_object"
        ),
    )
    _index("form_variant_role_bindings", "form_variant_id")

    # =======================================================================
    # Scanning and review
    # =======================================================================
    op.create_table(
        "submission_batches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("form_project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
        _created_at(),
        sa.Column("confidence_threshold", sa.Float(), nullable=True),
        _fk("submission_batches", "form_project_id", "form_projects.id"),
        _fk("submission_batches", "created_by_user_id", "users.id"),
        _pk("submission_batches"),
    )
    _index("submission_batches", "form_project_id")

    op.create_table(
        "submissions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("submission_batch_id", sa.UUID(), nullable=False),
        sa.Column("matched_form_variant_id", sa.UUID(), nullable=True),
        sa.Column("status", _enum("submission_status_enum"), nullable=False),
        sa.Column("respondent_identifier", sa.String(500), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=True),
        _created_at(),
        _updated_at(),
        sa.Column("source_group_key", sa.String(500), nullable=True),
        # Review soft lock and optimistic version.
        sa.Column("locked_by", sa.UUID(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("extraction_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("match_failure_reason", _enum("match_failure_reason_enum"), nullable=True),
        sa.Column("match_failure_code_value", sa.String(500), nullable=True),
        sa.Column("unresolvable_from_status", _enum("submission_status_enum"), nullable=True),
        _fk("submissions", "submission_batch_id", "submission_batches.id"),
        _fk("submissions", "matched_form_variant_id", "form_variants.id"),
        _fk("submissions", "locked_by", "users.id"),
        _pk("submissions"),
    )
    _index("submissions", "submission_batch_id")
    _index("submissions", "matched_form_variant_id")
    _index("submissions", "status")
    op.create_index(
        "uq_submissions_batch_source_group_key",
        "submissions",
        ["submission_batch_id", "source_group_key"],
        unique=True,
        postgresql_where=sa.text("source_group_key IS NOT NULL"),
    )

    op.create_table(
        "submission_pages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("form_variant_page_id", sa.UUID(), nullable=True),
        sa.Column("source_file_path", sa.String(1000), nullable=False),
        sa.Column("source_filename", sa.String(500), nullable=False),
        sa.Column("identified_code_value", sa.String(1000), nullable=True),
        _created_at(),
        _updated_at(),
        sa.Column("source_page_order", sa.Integer(), nullable=False),
        sa.Column("page_order_source", _enum("page_order_source_enum"), nullable=False),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("alignment_status", _enum("page_alignment_status_enum"), nullable=True),
        sa.Column("aligned_file_path", sa.String(1000), nullable=True),
        sa.Column("alignment_json", postgresql.JSONB(), nullable=True),
        _fk("submission_pages", "submission_id", "submissions.id"),
        _fk("submission_pages", "form_variant_page_id", "form_variant_pages.id"),
        _pk("submission_pages"),
    )
    _index("submission_pages", "submission_id")
    _index("submission_pages", "form_variant_page_id")

    op.create_table(
        "submission_object_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_object_id", sa.UUID(), nullable=False),
        sa.Column("result_type", _enum("submission_result_type_enum"), nullable=False),
        sa.Column("detected_payload_json", postgresql.JSONB(), nullable=True),
        sa.Column("reviewed_payload_json", postgresql.JSONB(), nullable=True),
        sa.Column("auto_score", sa.Float(), nullable=True),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.Column("review_required", sa.Boolean(), server_default=_FALSE, nullable=False),
        sa.Column("reviewed", sa.Boolean(), server_default=_FALSE, nullable=False),
        _created_at(),
        _updated_at(),
        sa.Column("submission_page_id", sa.UUID(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column(
            "validity_forces_review", sa.Boolean(), server_default=_FALSE, nullable=False
        ),
        sa.Column("review_outcome", _enum("review_outcome_enum"), nullable=True),
        sa.Column("review_note", sa.String(2000), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sheet_not_aligned", sa.Boolean(), server_default=_FALSE, nullable=False
        ),
        _fk("submission_object_results", "page_object_id", "page_objects.id"),
        _fk("submission_object_results", "submission_page_id", "submission_pages.id"),
        _fk("submission_object_results", "reviewed_by", "users.id"),
        _pk("submission_object_results"),
        sa.UniqueConstraint(
            "submission_page_id", "page_object_id",
            name="uq_submission_object_result_page_object",
        ),
    )
    _index("submission_object_results", "page_object_id")
    _index("submission_object_results", "result_type")
    _index("submission_object_results", "submission_page_id")

    op.create_table(
        "submission_reviews",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("submission_object_result_id", sa.UUID(), nullable=False),
        sa.Column("reviewer_user_id", sa.UUID(), nullable=False),
        sa.Column("review_note", sa.String(2000), nullable=True),
        _created_at(),
        sa.Column("action", _enum("review_action_enum"), nullable=False),
        sa.Column("reviewed_payload_json", postgresql.JSONB(), nullable=True),
        _fk(
            "submission_reviews", "submission_object_result_id",
            "submission_object_results.id",
        ),
        _fk("submission_reviews", "reviewer_user_id", "users.id"),
        _pk("submission_reviews"),
    )
    _index("submission_reviews", "submission_object_result_id")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
    for name in reversed(list(_ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
