# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
# Import all models here so that:
# 1. SQLAlchemy's mapper registry is fully populated before any query runs.
# 2. Alembic's env.py can import Base and find all table metadata via Base.metadata.

from app.models.base import Base
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    MembershipRole,
    PageObjectRole,
    PageOrderSource,
    PageObjectType,
    ProjectRole,
    QuestionType,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.user import User
from app.models.group import Group, GroupMembership
from app.models.audit_log import AuditLog

# ---------------------------------------------------------------------------
# Form/exam authoring + scanning models.
# Imported here so SQLAlchemy's mapper registry and Alembic both see them.
# ---------------------------------------------------------------------------
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject
from app.models.text_object_config import TextObjectConfig
from app.models.code_object_config import CodeObjectConfig
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.image_object_config import ImageObjectConfig
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_cell_def import MatrixCellDef
from app.models.submission_batch import SubmissionBatch
from app.models.submission import Submission
from app.models.submission_page import SubmissionPage
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_review import SubmissionReview

__all__ = [
    # Base
    "Base",
    # Enums
    "UserRole",
    "MembershipRole",
    "ProjectRole",
    "CodeType",
    "MatrixSelectionRule",
    # Authoring + scanning enums
    "FormProjectType",
    "FormProjectStatus",
    "FormVariantStatus",
    "PageObjectType",
    "PageObjectRole",
    "QuestionType",
    "SubmissionStatus",
    "SubmissionResultType",
    # Models
    "User",
    "Group",
    "GroupMembership",
    "AuditLog",
    # Authoring + scanning models
    "FormProject",
    "FormProjectMember",
    "FormVariant",
    "FormVariantPage",
    "FormVariantRoleBinding",
    "PageObject",
    "TextObjectConfig",
    "CodeObjectConfig",
    "FreeTextAreaConfig",
    "ImageObjectConfig",
    "QuestionObjectConfig",
    "QuestionOption",
    "MatrixObjectConfig",
    "MatrixRowDef",
    "MatrixColDef",
    "MatrixCellDef",
    "SubmissionBatch",
    "Submission",
    "SubmissionPage",
    "SubmissionObjectResult",
    "SubmissionReview",
]
