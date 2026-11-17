// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Domain API layer: types + endpoint wrappers for FormProject / FormVariant
// / FormVariantPage / PageObject + PDF export. Auth and user administration
// live in api/types.ts and api/endpoints.ts.

import { ApiError, apiErrorFromResponse, apiFetch } from './client'
import { getToken, markSessionExpired } from '../auth/authStore'

const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '/api/v1'
const t = () => getToken()

// ── Enums ────────────────────────────────────────────────────────────────────
export type FormProjectType = 'EXAM' | 'SURVEY' | 'QUESTIONNAIRE' | 'GENERIC_FORM'
export type FormProjectStatus = 'DRAFT' | 'ACTIVE' | 'CLOSED' | 'ARCHIVED'
export type FormVariantStatus = 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
export type PageObjectType = 'TEXT' | 'CODE' | 'FREE_TEXT_AREA' | 'QUESTION' | 'MATRIX' | 'IMAGE'
// Optional special scanning role a PageObject may hold within its variant
// (identification). At most one per role per variant, one role per
// object; the FE mirrors the backend PageObjectRole enum.
export type PageObjectRole = 'VARIANT_IDENTIFIER' | 'RESPONDENT_IDENTIFIER'
// How an IMAGE fills its object area. Default 'contain' (whole image visible).
export type ImageFit = 'contain' | 'cover'
// Whitelisted upload formats (kept in sync with the backend validator). Used for
// the file <input accept> and the client-side pre-check (BE is authoritative).
export const IMAGE_ACCEPT = 'image/png,image/jpeg,image/bmp,image/svg+xml'
export const IMAGE_MAX_BYTES = 10 * 1024 * 1024
export type CodeType = 'QR' | 'BARCODE'
// Barcode symbologies. Mirrors the backend CodeEncoding enum: only meaningful
// for BARCODE (default CODE128); QR carries no encoding (must be null).
export type CodeEncoding = 'CODE128' | 'CODE39' | 'EAN13' | 'EAN8' | 'UPCA'
export const CODE_ENCODINGS: CodeEncoding[] = ['CODE128', 'CODE39', 'EAN13', 'EAN8', 'UPCA']
export type ProjectRole = 'OWNER' | 'COLLABORATOR'
export type QuestionType = 'SINGLE_CHOICE' | 'MULTIPLE_CHOICE'
export type MatrixSelectionRule =
  | 'ONE_PER_COLUMN' | 'ONE_PER_ROW' | 'MANY_PER_COLUMN' | 'MANY_PER_ROW' | 'FREE'
export type ExportMode = 'STUDENT' | 'SOLUTION'

// ── FormProject ──────────────────────────────────────────────────────────────
export interface FormProjectRead {
  id: string
  owner_id: string
  type: FormProjectType
  name: string
  description: string | null
  event_date: string | null
  status: FormProjectStatus
  // The CALLER's role on this project is OWNER (same meaning as in
  // FormProjectListItem). Drives owner-only lifecycle controls on the detail page.
  is_owner: boolean
  created_at: string
  updated_at: string
}

// A project member. Email is NOT included here (the roster is name +
// avatar only); user-search carries email separately.
export interface FormProjectMember {
  user_id: string
  first_name: string | null
  last_name: string | null
  role: ProjectRole
  has_avatar: boolean
}

// A hit from GET /users/search — mirrors FormProjectMember but with id + email.
export interface UserSearchItem {
  id: string
  first_name: string | null
  last_name: string | null
  email: string
  has_avatar: boolean
}

export interface FormProjectListItem {
  id: string
  type: FormProjectType
  name: string
  description: string | null
  event_date: string | null
  status: FormProjectStatus
  owner_id: string
  is_owner: boolean
  variant_count: number
  // Avatar-stack data: first ≤3 members (owners first) +
  // the full member count, so the list renders the stack with no extra calls.
  members_preview: FormProjectMember[]
  member_count: number
  created_at: string
  updated_at: string
}

export interface FormProjectCreate {
  type: FormProjectType
  name: string
  description?: string | null
  event_date?: string | null
}

export interface FormProjectUpdate {
  name?: string
  description?: string | null
  event_date?: string | null
  status?: FormProjectStatus
}

// ── FormVariant ──────────────────────────────────────────────────────────────
export interface FormVariantRead {
  id: string
  form_project_id: string
  root_key: string
  name: string
  description: string | null
  status: FormVariantStatus
  version_number: number
  original_file_path: string | null
  original_filename: string | null
  published_at: string | null
  created_by_user_id: string
  updated_by_user_id: string | null
  created_at: string
  updated_at: string
}

export interface FormVariantPageRead {
  id: string
  form_variant_id: string
  page_index: number
  image_path: string | null
  width_px: number | null
  height_px: number | null
  dpi: number | null
  created_at: string
}

export interface FormVariantDetail extends FormVariantRead {
  pages: FormVariantPageRead[]
  // The parent project's lifecycle status. Always present on the
  // variant-detail payload — the FE mirrors the combined project×variant freeze
  // rules so forbidden actions aren't offered (the BE stays authoritative via 409).
  form_project_status: FormProjectStatus
}

export interface FormVariantListItem {
  id: string
  form_project_id: string
  root_key: string
  name: string
  description: string | null
  version_number: number
  status: FormVariantStatus
  published_at: string | null
  page_count: number
  created_at: string
  updated_at: string
}

export interface FormVariantCreate {
  root_key?: string | null
  name: string
  description?: string | null
}

export interface FormVariantUpdate {
  name?: string
  description?: string | null
}

// ── PageObject ───────────────────────────────────────────────────────────────
export interface PageObjectListItem {
  id: string
  form_variant_page_id: string
  object_type: PageObjectType
  // Optional special scanning role. Rides on every typed read
  // (mirrors PageObjectBaseRead.role), so it is present on both the
  // page-object list and the single detail — inherited by PageObjectDetail.
  role: PageObjectRole | null
  label: string | null
  z_index: number
  x: number
  y: number
  width: number
  height: number
  visible: boolean
  locked: boolean
  // Shared "template page" object. group_id set + shared_across_pages
  // true = a LINKED member shown on every page; false (with group_id) = detached.
  shared_group_id: string | null
  shared_across_pages: boolean
  created_at: string
  updated_at: string
}

// Typed config shapes (read). Only the one matching object_type is present.
export interface TextObjectConfigRead {
  id: string; page_object_id: string
  text: string; font_size: number; font_family: string | null
  bold: boolean; italic: boolean
}
export interface CodeObjectConfigRead {
  id: string; page_object_id: string
  code_type: CodeType; encoding: CodeEncoding | null; value: string; human_readable: boolean
  // Append the hosting page number to the encoded value. Only
  // meaningful while this object (or a shared sibling) holds VARIANT_IDENTIFIER;
  // mirrors CodeObjectConfigBase, default False on the BE.
  include_page_index: boolean
}
export interface FreeTextAreaConfigRead {
  id: string; page_object_id: string
  placeholder: string | null; max_length: number | null
}
export interface QuestionOptionRead {
  id: string; question_object_config_id: string
  order_index: number; option_text: string; is_correct: boolean; score: number | null
}
export interface QuestionObjectConfigRead {
  id: string; page_object_id: string
  question_type: QuestionType; question_text: string; shuffle_answers: boolean
  options: QuestionOptionRead[]
}
export interface MatrixRowDefRead { id: string; matrix_object_config_id: string; order_index: number; label: string }
export interface MatrixColDefRead { id: string; matrix_object_config_id: string; order_index: number; label: string }
export interface MatrixCellDefRead { id: string; matrix_object_config_id: string; row_index: number; col_index: number; is_correct: boolean; score: number | null }
export interface MatrixObjectConfigRead {
  id: string; page_object_id: string
  rows_count: number; cols_count: number; evaluative: boolean; selection_rule: MatrixSelectionRule
  rows: MatrixRowDefRead[]; cols: MatrixColDefRead[]; cells: MatrixCellDefRead[]
}
// IMAGE config (read). file_path is server-internal and never exposed; has_image
// reports whether a file has been uploaded yet.
export interface ImageObjectConfigRead {
  id: string; page_object_id: string
  has_image: boolean; fit: ImageFit
  original_filename: string | null; mime_type: string | null
  width_px: number | null; height_px: number | null
}

// Relink. RECONNECT = "Yes" (re-link + overwrite the detached
// siblings from this page); FRESH = "No" (this object starts a new group).
export type RelinkMode = 'RECONNECT' | 'FRESH'
export interface RelinkInfo {
  is_detached: boolean
  detached_siblings: number
}

// GET /page-objects/{id} — discriminated by object_type; configs optional in TS
// so a single shape covers all variants for read-only display.
export interface PageObjectDetail extends PageObjectListItem {
  description: string | null
  text_config?: TextObjectConfigRead
  code_config?: CodeObjectConfigRead
  free_text_config?: FreeTextAreaConfigRead
  question_config?: QuestionObjectConfigRead
  matrix_config?: MatrixObjectConfigRead
  image_config?: ImageObjectConfigRead
}

// Special scanning role binding (identification). Mirrors the BE's
// FormVariantRoleBinding read (GET /form-variants/{id}/role-bindings), enriched
// with the bound object's label + hosting page index so the Identification
// summary renders "object — page" without a per-object fetch. object_label is
// verbatim (may be null → the FE falls back to the raw id); page_index is
// 1-based, matching the page rail's "Page N".
export interface PageObjectRoleBinding {
  id: string
  form_variant_id: string
  page_object_id: string
  role: PageObjectRole
  created_at: string
  object_label: string | null
  page_index: number
  // The bound object's shared-group id, or null when it is
  // not a shared object. Lets the FE detect whether a shared sibling holds
  // VARIANT_IDENTIFIER to enable "include page number".
  shared_group_id: string | null
}

// ── Create payloads (geometry base + nested typed config) ─────────────────────
export interface GeometryBase {
  x: number; y: number; width: number; height: number
  z_index?: number; label?: string | null; description?: string | null
  // "Show on all pages": create the object as a linked template.
  shared_across_pages?: boolean
}
export interface TextPageObjectCreate extends GeometryBase {
  config: { text: string; font_size?: number; font_family?: string | null; bold?: boolean; italic?: boolean }
}
export interface CodePageObjectCreate extends GeometryBase {
  config: { code_type: CodeType; encoding?: CodeEncoding | null; value: string; human_readable?: boolean; include_page_index?: boolean }
}
export interface FreeTextAreaPageObjectCreate extends GeometryBase {
  config: { placeholder?: string | null; max_length?: number | null }
}
export interface QuestionOptionCreate {
  order_index: number; option_text: string; is_correct?: boolean; score?: number | null
}
export interface QuestionPageObjectCreate extends GeometryBase {
  config: {
    question_type: QuestionType; question_text: string; shuffle_answers?: boolean
    options: QuestionOptionCreate[]
  }
}
export interface MatrixRowDefCreate { order_index: number; label: string }
export interface MatrixColDefCreate { order_index: number; label: string }
export interface MatrixCellDefCreate { row_index: number; col_index: number; is_correct?: boolean; score?: number | null }
export interface MatrixPageObjectCreate extends GeometryBase {
  config: {
    rows_count: number; cols_count: number; evaluative?: boolean; selection_rule: MatrixSelectionRule
    rows: MatrixRowDefCreate[]; cols: MatrixColDefCreate[]; cells: MatrixCellDefCreate[]
  }
}
// IMAGE create carries only geometry + fit; the file is uploaded separately
// (uploadObjectImage) because storage cannot ride along in JSON.
export interface ImagePageObjectCreate extends GeometryBase {
  config?: { fit?: ImageFit }
}

// ── Update payloads (PATCH; all fields optional) ──────────────────────────────
export interface PageObjectUpdate {
  x?: number; y?: number; width?: number; height?: number
  z_index?: number; label?: string | null; description?: string | null
  visible?: boolean; locked?: boolean
}
export interface TextObjectConfigUpdate {
  text?: string; font_size?: number; font_family?: string | null; bold?: boolean; italic?: boolean
}
export interface CodeObjectConfigUpdate {
  code_type?: CodeType; encoding?: CodeEncoding | null; value?: string; human_readable?: boolean
  include_page_index?: boolean
}
export interface FreeTextAreaConfigUpdate {
  placeholder?: string | null; max_length?: number | null
}
export interface QuestionObjectConfigUpdate {
  question_type?: QuestionType; question_text?: string; shuffle_answers?: boolean
}
export interface MatrixObjectConfigUpdate {
  rows_count?: number; cols_count?: number; selection_rule?: MatrixSelectionRule; evaluative?: boolean
}
export interface ImageObjectConfigUpdate {
  fit?: ImageFit
}
export interface QuestionOptionUpdate {
  order_index?: number; option_text?: string; is_correct?: boolean; score?: number | null
}
export interface MatrixRowDefUpdate { order_index?: number; label?: string }
export interface MatrixColDefUpdate { order_index?: number; label?: string }
export interface MatrixCellDefUpdate { row_index?: number; col_index?: number; is_correct?: boolean; score?: number | null }

// ── SubmissionBatch ──────────────────────────────────────────────────────────
// A scan batch groups the submissions ingested in one collection. Creating
// one is the entry point of the scanning pipeline; the BE only allows it while
// the project is ACTIVE (409 otherwise).
export interface SubmissionBatchRead {
  id: string
  form_project_id: string
  created_by_user_id: string
  name: string
  description: string | null
  created_at: string
  // The batch's own override, as stored: null = follow the system default.
  confidence_threshold: number | null
  // …and the threshold actually in force (the override, else the default).
  effective_threshold: number
}

// One batch AS LISTED under its project (GET /form-projects/{id}/submission-
// batches), newest first. Every metric is computed by the BE on each read and
// never stored.
export interface SubmissionBatchListItem extends SubmissionBatchRead {
  // Submissions in the batch, whatever their status.
  total_submissions: number
  // …of which MATCH_FAILED: no variant resolved, a human must assign one.
  match_failed_count: number
  // …of which UNASSIGNED. Per BATCH — unlike SubmissionBatchIngestResult's
  // unassigned_count, which counts only what one upload call opened.
  unassigned_count: number
  // …of which UNRESOLVABLE: closed without a variant. They never
  // hold back the export, and the "Reviewed" progress leaves them out.
  unresolvable_count: number
  // …of which closed by a human: REVIEWED + FINALIZED.
  reviewed_count: number
  // Results awaiting review, counted only in submissions not yet closed.
  pending_review_count: number
  // Computed by the BE — the only source; never recomputed from the counts
  // above. False for an empty batch (nothing to export).
  ready_for_export: boolean
}

export interface SubmissionBatchCreate {
  name: string
  description?: string | null
}

// One submission AS LISTED INSIDE a batch — not SubmissionRead, which is the
// single-submission shape returned by the PDF upload and carries pages/score the
// list has no use for. Mirrors the BE's SubmissionSummary: a page COUNT, no page
// payloads.
export interface SubmissionSummary {
  id: string
  status: SubmissionStatus
  matched_form_variant_id: string | null
  // Why the variant could not be resolved. Written by matching
  // when it fails, cleared when a variant is matched or assigned; kept on an
  // UNRESOLVABLE submission. Null also for one that failed before the reason
  // was stored.
  match_failure_reason: MatchFailureReason | null
  // The variant code read, for VID_NOT_RECOGNISED only; null otherwise.
  match_failure_code_value: string | null
  // On an UNRESOLVABLE submission, the status it was marked from and "Undo"
  // returns it to: MATCH_FAILED or UNASSIGNED. Null on every
  // other status.
  unresolvable_from_status: SubmissionStatus | null
  page_count: number
  created_at: string
  // Results of this submission still awaiting a human: review_required and not
  // yet reviewed. Computed on every read against the batch's current
  // threshold; 0 when there is nothing to review, including never extracted.
  pending_review_count: number
  // The raw candidate identifier stored on the submission, verbatim; null when
  // none was read yet.
  respondent_identifier: string | null
  // 1-based template page indices of the matched variant that no sheet is
  // bound to, sorted. [] when no variant is matched (nothing to be missing).
  missing_page_indices: number[]
  // Whether the matched variant has a RESPONDENT_IDENTIFIER role binding, i.e.
  // whether a candidate id is expected at all. False with no variant.
  respondent_identifier_expected: boolean
  // Who is reviewing this submission right now. Non-null ONLY while
  // the soft lock is active: whether a lock has expired is decided by the BE,
  // and the FE never recomputes it from `locked_at`; it only shows the age.
  lock: SubmissionLockInfo | null
  // Whether an extraction run has ever examined this submission:
  // tells "never extracted" from "extracted, with errors" — both MATCHED.
  extraction_attempted: boolean
  // Objects a run could read but has no result for. Non-zero only for a
  // MATCHED submission a run has examined: what "Review anyway" would turn
  // into placeholders.
  unread_object_count: number
}

// An active review soft lock on a submission. Mirrors the BE's
// SubmissionLockInfo. A display name, never an email: the BE keeps members'
// emails out of every roster, and the lock must not leak them back in.
export interface SubmissionLockInfo {
  user_id: string
  display_name: string
  // ISO-8601 UTC: the holder's LAST activity (open, renewal or review save),
  // not when they first opened the submission.
  locked_at: string
}

// Outcome of acquiring or taking over a submission's review lock.
// Mirrors the BE's SubmissionLockState.
export interface SubmissionLockState {
  // Whether the caller now holds the lock. False only on a plain acquire, when
  // a colleague is actively reviewing — a 200, not an error.
  acquired: boolean
  // The active lock AFTER the call: the caller's own when acquired, otherwise
  // the colleague's who holds it.
  lock: SubmissionLockInfo | null
  // The version the caller must declare on its next review save.
  review_version: number
}

// Outcome of the manual candidate edit, a review save: the stored
// value, the version to declare on the next save, and the lock — now the
// caller's — so the page moves on without a refetch.
export interface SubmissionCandidateState {
  respondent_identifier: string | null
  review_version: number
  lock: SubmissionLockInfo | null
}

// Outcome of "Review anyway".
export interface ReviewAnywayResult {
  // Always SCANNED on success.
  status: SubmissionStatus
  // One placeholder per unread object; 0 when a run read everything meanwhile.
  placeholders_created: number
}

// Mirrors app/models/enums.py::SubmissionResultType.
export type SubmissionResultType =
  | 'QUESTION' | 'MATRIX' | 'FREE_TEXT' | 'CODE' | 'RESPONDENT_IDENTIFIER'

// Why a reading needed a human (BE submission_review_service). The FE must
// still render a code it does not know yet — the BE may grow one.
export type FlagReason =
  | 'LOW_CONFIDENCE' | 'INVALID_ANSWER' | 'ALWAYS_REVIEW' | 'DECODE_FAILED'
  | 'AMBIGUOUS_DIGITS' | 'MISSING_DIGITS' | 'UNREADABLE_GRID' | 'EXTRACTION_FAILED'

// The template page a sheet was bound to, with its size (for the review crop:
// object geometry is in these pixels). Either dimension may be null; the FE
// then falls back to A4.
export interface SubmissionReviewTemplatePage {
  id: string
  // 1-based, like FormVariantPage.page_index.
  page_index: number
  width_px: number | null
  height_px: number | null
}

// A PageObject's rectangle, verbatim: template-page pixels, top-left origin.
export interface SubmissionReviewGeometry {
  x: number
  y: number
  width: number
  height: number
}

// One reading that needed a human (review_required), resolved or not.
export interface SubmissionReviewObject {
  result_id: string
  page_object_id: string
  object_type: PageObjectType
  result_type: SubmissionResultType
  role: PageObjectRole | null
  label: string | null
  // The question's text for a QUESTION, null for every other type.
  question_text: string | null
  geometry: SubmissionReviewGeometry
  // The extractor's payload, verbatim; its shape depends on result_type. Null
  // for a "Review anyway" placeholder: nothing was read, the answer
  // comes from the paper sheet.
  detected_payload: Record<string, unknown> | null
  confidence_score: number | null
  // Derived on read under the batch's CURRENT threshold. Never empty for an
  // unresolved reading; may be empty for a resolved one (threshold moved).
  flag_reasons: (FlagReason | string)[]
  // Whether a human has ruled on it: review_outcome is set. False
  // again after an UNDO.
  resolved: boolean
  // ── The CURRENT ruling; all null while unresolved ──
  review_outcome: ReviewOutcome | null
  // The value a SUBSTITUTE entered, in the shape for the reading's type (see
  // ReviewedPayload); null for any other outcome.
  reviewed_payload: ReviewedPayload | null
  // The note of the current ruling; earlier notes stay in the BE's log.
  review_note: string | null
  reviewed_by: SubmissionReviewer | null
  // ISO timestamp (UTC).
  reviewed_at: string | null
  // The BE's review_rules, applied server-side — never re-derived here.
  // confirmable: CONFIRM is allowed (otherwise only SUBSTITUTE or FLAG).
  confirmable: boolean
  // "Accept all remaining" would confirm this reading if it were unresolved.
  bulk_acceptable: boolean
  // Whether CONFIRM / SUBSTITUTE of this reading sets the candidate: true only
  // for the submission's first identity reading in physical page order.
  // False for a later copy or duplicate of the identifier, and for answers.
  sets_candidate: boolean
}

// ── Review actions ───────────────────────────────────────────────────────────
// Mirror app/models/enums.py (ReviewOutcome, Validity) and
// app/schemas/submission.py / submission_result.py.

export type Validity = 'VALID' | 'NO_ANSWER' | 'TOO_MANY_ANSWERS'
export type ReviewOutcome = 'CONFIRMED' | 'SUBSTITUTED' | 'FLAGGED'
// What the operator does (API-only on the BE: ReviewActionType).
export type ReviewActionType = 'CONFIRM' | 'SUBSTITUTE' | 'FLAG' | 'UNDO'

// Who made a reading's current ruling. A display name, never an email.
export interface SubmissionReviewer {
  user_id: string
  display_name: string
}

// reviewed_payload, per type. Validity is always computed by the server.
export interface QuestionReviewedPayload {
  // In the question's option order.
  selected_option_ids: string[]
  validity: Validity
}
export interface MatrixRowReview {
  row_index: number
  selected_col_indices: number[]
  validity: Validity
}
export interface MatrixColReview {
  col_index: number
  selected_row_indices: number[]
  validity: Validity
}
// Grouped like the detection payload: `rows` under a row rule, `cols` under a
// column rule, every group listed; the other list is empty.
export interface MatrixReviewedPayload {
  rows: MatrixRowReview[]
  cols: MatrixColReview[]
}
// A respondent-ID grid, or a FREE_TEXT_AREA holding RESPONDENT_IDENTIFIER.
export interface RespondentIdentifierReviewedPayload { value: string }
export interface FreeTextReviewedPayload { text: string }
export interface CodeReviewedPayload { value: string }
export type ReviewedPayload =
  | QuestionReviewedPayload
  | MatrixReviewedPayload
  | RespondentIdentifierReviewedPayload
  | FreeTextReviewedPayload
  | CodeReviewedPayload

// SUBSTITUTE input (`value`), per type. The BE trims text and refuses extras.
export interface QuestionSubstitution { selected_option_ids: string[] }
// [row_index, col_index] pairs; the server builds the groups.
export interface MatrixSubstitution { selected_cells: [number, number][] }
export interface RespondentIdentifierSubstitution { value: string }
export interface FreeTextSubstitution { text: string }
export interface CodeSubstitution { value: string }
export type SubstituteValue =
  | QuestionSubstitution
  | MatrixSubstitution
  | RespondentIdentifierSubstitution
  | FreeTextSubstitution
  | CodeSubstitution

// Body of POST /submissions/{id}/results/{result_id}/review. `value` only with
// SUBSTITUTE (422 otherwise); `note` trimmed by the BE, blank → null, ≤ 2000.
export interface SubmissionReviewActionRequest {
  action: ReviewActionType
  review_version: number
  note?: string | null
  value?: SubstituteValue | null
}

// One physical sheet of the submission, placed or not.
// SubmissionPage.alignment_status, as the review read sends it.
export type PageAlignmentStatus = 'ALIGNED' | 'NOT_ALIGNED'

export interface SubmissionReviewPage {
  // The SubmissionPage id.
  id: string
  // 0-based position in the submission; not unique (ties broken by id).
  source_page_order: number
  page_order_source: 'CODE' | 'FILENAME' | 'PDF_ORDER'
  // ALIGNED, NOT_ALIGNED, or null (never aligned: read without alignment,
  // or a scan that could not be read). Which image is served for it is
  // the server's rule; the FE warns on NOT_ALIGNED and keys the image on it.
  alignment_status: PageAlignmentStatus | null
  // Null for a sheet matching could not place.
  template_page: SubmissionReviewTemplatePage | null
  // Its flagged readings in reading order (y, then x); empty when unplaced.
  objects: SubmissionReviewObject[]
}

// GET /submissions/{id}/review: everything the submission review page shows.
// A pure read in any status — it never takes or renews the lock.
export interface SubmissionReviewDetail {
  id: string
  submission_batch_id: string
  batch_name: string
  form_project_id: string
  status: SubmissionStatus
  created_at: string
  matched_form_variant_id: string | null
  // Null when no variant is matched.
  variant_name: string | null
  // Why matching failed: as on SubmissionSummary.
  match_failure_reason: MatchFailureReason | null
  match_failure_code_value: string | null
  // Same meaning as on SubmissionSummary (the queue).
  respondent_identifier: string | null
  respondent_identifier_expected: boolean
  // The sheet (SubmissionPage id) holding the first identity reading, the only
  // one that sets the candidate, listed in review or not. Null when no
  // reading holds an identity.
  candidate_reading_page_id: string | null
  missing_page_indices: number[]
  // The ACTIVE lock only; an expired one is null.
  lock: SubmissionLockInfo | null
  review_version: number
  // The batch's threshold in force, a fraction (0.8 = 80%).
  effective_threshold: number
  // Readings listed in `pages`, and those of them not resolved yet.
  review_count: number
  pending_review_count: number
  // Unresolved readings "Accept all remaining" would confirm.
  bulk_acceptable_count: number
  // The score triple, recomputed at every read and returned
  // by every review action. Null outside SCANNED / REVIEWED / FINALIZED (not
  // extracted yet), with no variant, or when the server could not score a
  // broken reading.
  score: SubmissionScore | null
  // EVERY sheet, in (source_page_order, id) order.
  pages: SubmissionReviewPage[]
}

// The score of a submission (SubmissionScoreRead on the BE).
// Floats as authored: rounding is the display's (utils/scoreFormat.ts).
export interface SubmissionScore {
  raw_score: number
  max_possible_score: number
  // 0–100, negative with a negative total; null when the maximum is 0.
  percentage: number | null
}

// POST /submissions/{id}/accept-remaining: the whole review after it, plus how
// many readings it confirmed (possibly 0).
export interface SubmissionAcceptRemainingResult extends SubmissionReviewDetail {
  accepted_count: number
}

// GET /submission-batches/{id}: the batch plus its submissions. The endpoint also
// takes an optional `?status=` filter; the FE deliberately does not use it — the
// batch is fetched once and bucketed client-side (see BatchSubmissionsList), so
// switching filter never costs a round trip.
export interface SubmissionBatchDetail extends SubmissionBatchRead {
  // Over the whole batch, whatever `?status=` filter.
  export_readiness: ExportReadiness
  submissions: SubmissionSummary[]
}

// Whether a batch can be exported now and, if not, how many submissions hold it
// back and why (ExportReadiness on the BE). Computed by the BE
// on every read: `ready` is the same rule as the list's `ready_for_export`.
// The counts are all 0 when ready, and also for an empty batch or one of
// UNRESOLVABLE submissions only. The same counts are the extras of the 409
// NOT_EXPORT_READY.
export interface ExportReadiness {
  ready: boolean
  // UPLOADED or MATCHED: not extracted yet.
  to_extract: number
  // SCANNED: not closed by a review yet.
  in_review: number
  // MATCH_FAILED: no variant yet.
  to_assign: number
  // UNASSIGNED: an ungroupable sheet.
  unassigned: number
}

// Batch results export: ExportFormat / CsvDialect on the BE.
export type ExportFormat = 'xlsx' | 'csv'
// comma: "," between fields and "." decimals; semicolon: ";" and "," decimals.
export type CsvDialect = 'comma' | 'semicolon'

// One submission. Returned by the PDF ingestion path, where one uploaded file is
// exactly one Submission. (The BE payload also carries `pages`; the upload flow
// never reads them, so they are deliberately not modelled here.)
export interface SubmissionRead {
  id: string
  submission_batch_id: string
  // Stays null until the matching engine resolves the variant.
  matched_form_variant_id: string | null
  status: SubmissionStatus
  // Same meaning as on SubmissionSummary (the queue).
  match_failure_reason: MatchFailureReason | null
  match_failure_code_value: string | null
  unresolvable_from_status: SubmissionStatus | null
  total_score: number | null
  created_at: string
  updated_at: string
}

// Mirrors app/models/enums.py::SubmissionStatus.
export type SubmissionStatus =
  | 'UPLOADED' | 'UNASSIGNED' | 'MATCHED' | 'MATCH_FAILED'
  | 'SCANNED' | 'REVIEWED' | 'FINALIZED' | 'UNRESOLVABLE'

// Mirrors app/models/enums.py::MatchFailureReason: why matching left a
// submission MATCH_FAILED.
export type MatchFailureReason =
  | 'NO_PUBLISHED_VARIANT' | 'NO_VID_FOUND' | 'VID_NOT_RECOGNISED'

// One image the BE refused to ingest, and why. A rejection is per FILE, never
// per request: the other images of the same call are ingested regardless.
export interface RejectedImage {
  filename: string
  reason: string
}

// Outcome of ONE loose-image upload call: unlike a PDF upload (one file = one
// submission), a single call can open several submissions, extend submissions opened
// by earlier calls, and reject a file or two along the way.
export interface SubmissionBatchIngestResult {
  submissions_created: string[]
  submissions_updated: string[]
  pages_added: number
  // How many of submissions_created are UNASSIGNED singletons (grouped by
  // neither a RID code nor a conformant filename).
  unassigned_count: number
  rejected: RejectedImage[]
}

// Outcome of ONE manual extraction run over a batch (POST .../run-extraction).
// About the CALL, in counts: the three "skipped" buckets mean nothing to wait
// for, not supported by this build, and already done.
export interface ExtractionRunSummary {
  // Submissions examined: variant matched, and neither MATCH_FAILED nor UNASSIGNED.
  submissions_processed: number
  // …and, of their sheets, those matching bound to a template page.
  pages_processed: number
  // Results created by THIS run; a re-run over a settled batch reports 0.
  results_created: number
  // Submissions this run moved MATCHED → SCANNED ("ready for review").
  submissions_scanned: number
  // Objects an earlier run had already read; never re-read.
  skipped_already_extracted: number
  // Objects that carry no answer (TEXT, IMAGE, a CODE holding a scanning role).
  skipped_not_applicable: number
  // Objects that carry an answer this build cannot read.
  skipped_not_yet_supported: number
  // Objects whose extraction raised. One bad sheet never stops the run.
  errors: number
  // The first few failures by coordinate. Capped; `errors` is the true total.
  error_details: { submission_page_id: string; page_object_id: string; error: string }[]
}

// GET /form-variants/{id}/max-score — VariantMaxScoreRead.
// Computed on the fly by the server in any status of the variant.
export interface VariantMaxScore {
  max_score: number
}

// Whether a project can start a scan at all. Always 200 —
// `ready: false` is a payload value, not an error. `reason` is human-readable
// text meant for verbatim display (never recomposed from the counters).
export interface ScanReadiness {
  ready: boolean
  reason: string | null
  published_variant_count: number
  variant_identifier_variant_id?: string
  colliding_variant_ids: string[]
}

// ── Endpoint wrappers ──────────────────────────────────────────────────────────
export const nd = {
  // Projects
  listProjects: () =>
    apiFetch<FormProjectListItem[]>('/form-projects', { token: t() }),
  createProject: (data: FormProjectCreate) =>
    apiFetch<FormProjectRead>('/form-projects', { method: 'POST', json: data, token: t() }),
  getProject: (id: string) =>
    apiFetch<FormProjectRead>(`/form-projects/${id}`, { token: t() }),
  updateProject: (id: string, data: FormProjectUpdate) =>
    apiFetch<FormProjectRead>(`/form-projects/${id}`, { method: 'PATCH', json: data, token: t() }),
  deleteProject: (id: string) =>
    apiFetch<void>(`/form-projects/${id}`, { method: 'DELETE', token: t() }),
  // Lifecycle move (owner-only, graph-validated server-side). Illegal jumps /
  // unmet gates (e.g. DRAFT→ACTIVE with no PUBLISHED variant) come back as 409.
  transitionProject: (projectId: string, target_status: FormProjectStatus) =>
    apiFetch<FormProjectRead>(`/form-projects/${projectId}/transition`, {
      method: 'POST', json: { target_status }, token: t(),
    }),

  // Project members. Read is open to any member; the
  // mutations are owner-only (admin bypass) — enforced server-side.
  listProjectMembers: (projectId: string) =>
    apiFetch<FormProjectMember[]>(`/form-projects/${projectId}/members`, { token: t() }),
  addProjectMember: (projectId: string, userId: string) =>
    apiFetch<FormProjectMember>(`/form-projects/${projectId}/members`, {
      method: 'POST', json: { user_id: userId }, token: t(),
    }),
  removeProjectMember: (projectId: string, userId: string) =>
    apiFetch<void>(`/form-projects/${projectId}/members/${userId}`, {
      method: 'DELETE', token: t(),
    }),
  setProjectMemberRole: (projectId: string, userId: string, role: ProjectRole) =>
    apiFetch<FormProjectMember>(`/form-projects/${projectId}/members/${userId}`, {
      method: 'PATCH', json: { role }, token: t(),
    }),
  // Search existing users to add (owner-or-admin). <2 chars → [] server-side.
  searchUsers: (q: string, excludeProjectId?: string) => {
    const params = new URLSearchParams({ q })
    if (excludeProjectId) params.set('exclude_project_id', excludeProjectId)
    return apiFetch<UserSearchItem[]>(`/users/search?${params.toString()}`, { token: t() })
  },

  // Variants
  listVariants: (projectId: string) =>
    apiFetch<FormVariantListItem[]>(`/form-projects/${projectId}/variants`, { token: t() }),
  createVariant: (projectId: string, data: FormVariantCreate) =>
    apiFetch<FormVariantRead>(`/form-projects/${projectId}/variants`, {
      method: 'POST', json: data, token: t(),
    }),
  getVariant: (variantId: string) =>
    apiFetch<FormVariantDetail>(`/form-variants/${variantId}`, { token: t() }),
  getVariantMaxScore: (variantId: string) =>
    apiFetch<VariantMaxScore>(`/form-variants/${variantId}/max-score`, { token: t() }),
  updateVariant: (variantId: string, data: FormVariantUpdate) =>
    apiFetch<FormVariantRead>(`/form-variants/${variantId}`, { method: 'PATCH', json: data, token: t() }),
  publishVariant: (variantId: string) =>
    apiFetch<FormVariantRead>(`/form-variants/${variantId}/publish`, { method: 'POST', token: t() }),
  cloneVariant: (variantId: string) =>
    apiFetch<FormVariantRead>(`/form-variants/${variantId}/clone`, { method: 'POST', token: t() }),
  // Delete a DRAFT variant (cascade). 409 if not DRAFT.
  deleteVariant: (variantId: string) =>
    apiFetch<void>(`/form-variants/${variantId}`, { method: 'DELETE', token: t() }),
  listVariantPages: (variantId: string) =>
    apiFetch<FormVariantPageRead[]>(`/form-variants/${variantId}/pages`, { token: t() }),
  // Append a blank A4 page to a DRAFT variant (page_index computed server-side).
  addVariantPage: (variantId: string) =>
    apiFetch<FormVariantPageRead>(`/form-variants/${variantId}/pages`, {
      method: 'POST', token: t(),
    }),
  // Duplicate one page of a DRAFT variant: create a new page whose
  // content replicates the source, inserted immediately after it, then renumber
  // 1..n. Linked-group members on the source stay linked members of the SAME
  // group on the copy (live sync); ordinary/detached objects become independent
  // copies. Returns the new page. 409 if not DRAFT, 404 if the page is foreign.
  duplicateVariantPage: (variantId: string, pageId: string) =>
    apiFetch<FormVariantPageRead>(`/form-variants/${variantId}/pages/${pageId}/duplicate`, {
      method: 'POST', token: t(),
    }),
  // Persist a new page order (DRAFT only). orderedPageIds is a full permutation of
  // the variant's current page ids; the BE renumbers page_index 1..n. Returns the
  // reordered page list. 409 if not DRAFT, 400 if the ids are not a permutation.
  reorderVariantPages: (variantId: string, orderedPageIds: string[]) =>
    apiFetch<FormVariantPageRead[]>(`/form-variants/${variantId}/pages/reorder`, {
      method: 'PATCH', json: { ordered_page_ids: orderedPageIds }, token: t(),
    }),
  // Remove one page of a DRAFT variant (scoped delete; linked objects on other
  // pages are preserved). Returns the remaining page list. 409 if not DRAFT or if
  // it is the only page.
  deleteVariantPage: (variantId: string, pageId: string) =>
    apiFetch<FormVariantPageRead[]>(`/form-variants/${variantId}/pages/${pageId}`, {
      method: 'DELETE', token: t(),
    }),

  // Page objects. The list returns the enriched per-object typed config: a
  // superset of the base shape, so base-only consumers (e.g. PageObjectList)
  // work unchanged while the canvas renders content previews.
  listPageObjects: (pageId: string) =>
    apiFetch<PageObjectDetail[]>(`/form-variant-pages/${pageId}/objects`, { token: t() }),
  getPageObject: (objectId: string) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}`, { token: t() }),
  createTextObject: (pageId: string, data: TextPageObjectCreate) =>
    apiFetch<PageObjectDetail>(`/form-variant-pages/${pageId}/objects/text`, {
      method: 'POST', json: data, token: t(),
    }),
  createCodeObject: (pageId: string, data: CodePageObjectCreate) =>
    apiFetch<PageObjectDetail>(`/form-variant-pages/${pageId}/objects/code`, {
      method: 'POST', json: data, token: t(),
    }),
  createFreeTextAreaObject: (pageId: string, data: FreeTextAreaPageObjectCreate) =>
    apiFetch<PageObjectDetail>(`/form-variant-pages/${pageId}/objects/free-text-area`, {
      method: 'POST', json: data, token: t(),
    }),
  createQuestionObject: (pageId: string, data: QuestionPageObjectCreate) =>
    apiFetch<PageObjectDetail>(`/form-variant-pages/${pageId}/objects/question`, {
      method: 'POST', json: data, token: t(),
    }),
  createMatrixObject: (pageId: string, data: MatrixPageObjectCreate) =>
    apiFetch<PageObjectDetail>(`/form-variant-pages/${pageId}/objects/matrix`, {
      method: 'POST', json: data, token: t(),
    }),
  // IMAGE: create the object (geometry + fit), then upload the file separately.
  createImageObject: (pageId: string, data: ImagePageObjectCreate) =>
    apiFetch<PageObjectDetail>(`/form-variant-pages/${pageId}/objects/image`, {
      method: 'POST', json: data, token: t(),
    }),
  // Multipart upload (or replace) of an IMAGE object's binary. BE is the
  // authoritative validator (real MIME, size, SVG sanitization).
  uploadObjectImage: (objectId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return apiFetch<PageObjectDetail>(`/page-objects/${objectId}/image`, {
      method: 'POST', formData: fd, token: t(),
    })
  },
  // Fetch the served binary as a Blob (authed). The viewer turns this into an
  // object URL for an <img> — never injects SVG inline (no script execution).
  getObjectImageBlob: (objectId: string) =>
    apiFetch<Blob>(`/page-objects/${objectId}/image`, { token: t() }),

  // Common object update / delete
  updatePageObject: (objectId: string, data: PageObjectUpdate) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}`, { method: 'PATCH', json: data, token: t() }),
  deletePageObject: (objectId: string) =>
    apiFetch<void>(`/page-objects/${objectId}`, { method: 'DELETE', token: t() }),

  // Shared "template page" objects. share() links the object so it
  // shows on all pages (it becomes its group's template — existing pages are NOT
  // copied to; new pages receive a linked copy). unshare() detaches the group.
  shareObject: (objectId: string) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/share`, { method: 'POST', token: t() }),
  unshareObject: (objectId: string) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/share`, { method: 'DELETE', token: t() }),
  // After saving a linked member whose typed config changed, push that
  // config to every linked sibling (BE-authoritative resync). No-op server-side
  // for a non-linked object.
  resyncSharedObject: (objectId: string) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/resync-shared`, { method: 'POST', token: t() }),
  // Reactivating sharing on a DETACHED object. getRelinkInfo tells the FE
  // whether to prompt (detached_siblings > 0). relinkObject applies the choice:
  // RECONNECT re-links + overwrites the detached siblings from this page; FRESH
  // starts a new group and leaves them independent. Neither copies onto existing
  // pages.
  getRelinkInfo: (objectId: string) =>
    apiFetch<RelinkInfo>(`/page-objects/${objectId}/relink-info`, { token: t() }),
  relinkObject: (objectId: string, mode: RelinkMode) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/relink`, { method: 'POST', json: { mode }, token: t() }),

  // Special scanning role (identification, DRAFT only). Assign/remove
  // are IMMEDIATE object mutations (like share, not part of the editor Save flow);
  // the response payload is NOT relied on for UI state — callers pessimistically
  // invalidate ['role-bindings', variantId] / ['page-objects', pageId] /
  // ['page-object', objectId] and let the refetch be the source of truth. A 409
  // (role already taken / illegal reassignment) carries its message in `detail`.
  setPageObjectRole: (objectId: string, role: PageObjectRole) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/role`, {
      method: 'PUT', json: { role }, token: t(),
    }),
  removePageObjectRole: (objectId: string) =>
    apiFetch<void>(`/page-objects/${objectId}/role`, { method: 'DELETE', token: t() }),
  getVariantRoleBindings: (variantId: string) =>
    apiFetch<PageObjectRoleBinding[]>(`/form-variants/${variantId}/role-bindings`, { token: t() }),

  // Typed config updates (each returns the full typed PageObject read)
  updateTextConfig: (objectId: string, data: TextObjectConfigUpdate) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/text-config`, { method: 'PATCH', json: data, token: t() }),
  updateCodeConfig: (objectId: string, data: CodeObjectConfigUpdate) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/code-config`, { method: 'PATCH', json: data, token: t() }),
  updateFreeTextConfig: (objectId: string, data: FreeTextAreaConfigUpdate) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/free-text-config`, { method: 'PATCH', json: data, token: t() }),
  updateQuestionConfig: (objectId: string, data: QuestionObjectConfigUpdate) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/question-config`, { method: 'PATCH', json: data, token: t() }),
  updateMatrixConfig: (objectId: string, data: MatrixObjectConfigUpdate) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/matrix-config`, { method: 'PATCH', json: data, token: t() }),
  updateImageConfig: (objectId: string, data: ImageObjectConfigUpdate) =>
    apiFetch<PageObjectDetail>(`/page-objects/${objectId}/image-config`, { method: 'PATCH', json: data, token: t() }),

  // Question options
  listQuestionOptions: (objectId: string) =>
    apiFetch<QuestionOptionRead[]>(`/page-objects/${objectId}/question-options`, { token: t() }),
  createQuestionOption: (objectId: string, data: QuestionOptionCreate) =>
    apiFetch<QuestionOptionRead>(`/page-objects/${objectId}/question-options`, { method: 'POST', json: data, token: t() }),
  updateQuestionOption: (optionId: string, data: QuestionOptionUpdate) =>
    apiFetch<QuestionOptionRead>(`/question-options/${optionId}`, { method: 'PATCH', json: data, token: t() }),
  deleteQuestionOption: (optionId: string) =>
    apiFetch<void>(`/question-options/${optionId}`, { method: 'DELETE', token: t() }),

  // Matrix rows / cols / cells
  listMatrixRows: (objectId: string) =>
    apiFetch<MatrixRowDefRead[]>(`/page-objects/${objectId}/matrix-rows`, { token: t() }),
  createMatrixRow: (objectId: string, data: MatrixRowDefCreate) =>
    apiFetch<MatrixRowDefRead>(`/page-objects/${objectId}/matrix-rows`, { method: 'POST', json: data, token: t() }),
  updateMatrixRow: (rowId: string, data: MatrixRowDefUpdate) =>
    apiFetch<MatrixRowDefRead>(`/matrix-rows/${rowId}`, { method: 'PATCH', json: data, token: t() }),
  deleteMatrixRow: (rowId: string) =>
    apiFetch<void>(`/matrix-rows/${rowId}`, { method: 'DELETE', token: t() }),

  listMatrixCols: (objectId: string) =>
    apiFetch<MatrixColDefRead[]>(`/page-objects/${objectId}/matrix-cols`, { token: t() }),
  createMatrixCol: (objectId: string, data: MatrixColDefCreate) =>
    apiFetch<MatrixColDefRead>(`/page-objects/${objectId}/matrix-cols`, { method: 'POST', json: data, token: t() }),
  updateMatrixCol: (colId: string, data: MatrixColDefUpdate) =>
    apiFetch<MatrixColDefRead>(`/matrix-cols/${colId}`, { method: 'PATCH', json: data, token: t() }),
  deleteMatrixCol: (colId: string) =>
    apiFetch<void>(`/matrix-cols/${colId}`, { method: 'DELETE', token: t() }),

  listMatrixCells: (objectId: string) =>
    apiFetch<MatrixCellDefRead[]>(`/page-objects/${objectId}/matrix-cells`, { token: t() }),
  createMatrixCell: (objectId: string, data: MatrixCellDefCreate) =>
    apiFetch<MatrixCellDefRead>(`/page-objects/${objectId}/matrix-cells`, { method: 'POST', json: data, token: t() }),
  updateMatrixCell: (cellId: string, data: MatrixCellDefUpdate) =>
    apiFetch<MatrixCellDefRead>(`/matrix-cells/${cellId}`, { method: 'PATCH', json: data, token: t() }),
  deleteMatrixCell: (cellId: string) =>
    apiFetch<void>(`/matrix-cells/${cellId}`, { method: 'DELETE', token: t() }),

  // Submission batches (scanning pipeline). Open a batch on a project — any
  // member, but 409 unless the project is ACTIVE. The single-batch GET is
  // addressed by id (not nested under the project), like /form-variants/{id}.
  createSubmissionBatch: (projectId: string, data: SubmissionBatchCreate) =>
    apiFetch<SubmissionBatchRead>(`/form-projects/${projectId}/submission-batches`, {
      method: 'POST', json: data, token: t(),
    }),
  // A project's batches with their metrics, newest first. Any member, and on
  // any project status: a CLOSED/ARCHIVED project still lists what it collected.
  listSubmissionBatches: (projectId: string) =>
    apiFetch<SubmissionBatchListItem[]>(`/form-projects/${projectId}/submission-batches`, {
      token: t(),
    }),
  getSubmissionBatch: (batchId: string) =>
    apiFetch<SubmissionBatchDetail>(`/submission-batches/${batchId}`, { token: t() }),
  // Ingestion, two paths behind one FE upload area. One PDF = one submission, so
  // this is a single-file multipart POST returning that Submission; 409 when the
  // project is no longer ACTIVE or not ready for scanning (structured,
  // NOT_SCAN_READY), 413 when the file is over the BE's cap.
  uploadSubmissionPdf: (batchId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return apiFetch<SubmissionRead>(`/submission-batches/${batchId}/pdf-uploads`, {
      method: 'POST', formData: fd, token: t(),
    })
  },
  // Loose images: ONE call for the whole set (they are grouped into submissions
  // across the request, so they cannot be sent one by one). Part order is
  // preserved by the BE and is the last-resort page order, so append in
  // selection order. Never call this with an empty array — the BE 422s.
  uploadSubmissionImages: (batchId: string, files: File[]) => {
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    return apiFetch<SubmissionBatchIngestResult>(`/submission-batches/${batchId}/image-uploads`, {
      method: 'POST', formData: fd, token: t(),
    })
  },
  // Manual recovery for ONE submission the matching engine could not resolve:
  // name its variant by hand. Addressed by submission id (its own /submissions
  // prefix), not through the batch. MATCH_FAILED only — structured 409
  // INVALID_STATUS on any other status, VARIANT_NOT_PUBLISHED on a
  // non-PUBLISHED variant, 404 when the variant is not the submission's
  // project's. Every one of those is about THAT submission, so a bulk caller
  // reports them per item and keeps going (see AssignVariantModal).
  assignSubmissionVariant: (submissionId: string, formVariantId: string) =>
    apiFetch<SubmissionRead>(`/submissions/${submissionId}/assign-variant`, {
      method: 'POST', json: { form_variant_id: formVariantId }, token: t(),
    }),
  // The other way out of MATCH_FAILED: close the submission without
  // a variant, and undo it. Mark from MATCH_FAILED only, undo from
  // UNRESOLVABLE only — anything else (a second click included) is the
  // structured 409 INVALID_STATUS with the `status` found.
  markSubmissionUnresolvable: (submissionId: string) =>
    apiFetch<SubmissionRead>(`/submissions/${submissionId}/mark-unresolvable`, {
      method: 'POST', token: t(),
    }),
  undoSubmissionUnresolvable: (submissionId: string) =>
    apiFetch<SubmissionRead>(`/submissions/${submissionId}/undo-unresolvable`, {
      method: 'POST', token: t(),
    }),
  // The submission review page's one read. Any status; writes
  // nothing and leaves the lock alone.
  getSubmissionReview: (submissionId: string) =>
    apiFetch<SubmissionReviewDetail>(`/submissions/${submissionId}/review`, { token: t() }),
  // The whole stored scan of one sheet, typed from its content; the
  // review focus crops it client-side. 404 when the file is missing.
  getSubmissionPageImageBlob: (pageId: string) =>
    apiFetch<Blob>(`/submission-pages/${pageId}/image`, { token: t() }),
  // Review soft lock. Acquire on open (a colleague's active lock is a
  // 200 with acquired=false); take-over always acquires; release is idempotent.
  // Acquire and take-over 409 NOT_REVIEWABLE (structured) unless SCANNED.
  acquireSubmissionLock: (submissionId: string) =>
    apiFetch<SubmissionLockState>(`/submissions/${submissionId}/lock`, {
      method: 'POST', token: t(),
    }),
  takeOverSubmissionLock: (submissionId: string) =>
    apiFetch<SubmissionLockState>(`/submissions/${submissionId}/lock/take-over`, {
      method: 'POST', token: t(),
    }),
  // Set, correct or clear (null / blank) the candidate identifier. A review
  // save: 409 NOT_REVIEWABLE / LOCK_LOST / SUBMISSION_CHANGED (structured),
  // 422 beyond 500 characters.
  editSubmissionCandidate: (
    submissionId: string,
    respondentIdentifier: string | null,
    reviewVersion: number,
  ) =>
    apiFetch<SubmissionCandidateState>(`/submissions/${submissionId}/candidate`, {
      method: 'PATCH',
      json: { respondent_identifier: respondentIdentifier, review_version: reviewVersion },
      token: t(),
    }),
  // Bring a MATCHED submission with unread objects into review: placeholders
  // for them, then SCANNED. 409 EXTRACTION_RUNNING while a run holds the
  // batch, then 409 NOT_ELIGIBLE (reason STATUS | NOT_EXTRACTED).
  reviewSubmissionAnyway: (submissionId: string) =>
    apiFetch<ReviewAnywayResult>(`/submissions/${submissionId}/review-anyway`, {
      method: 'POST', token: t(),
    }),
  // One review action on one reading: CONFIRM, SUBSTITUTE, FLAG or UNDO. A
  // review save: 409 NOT_REVIEWABLE / LOCK_LOST / SUBMISSION_CHANGED, plus
  // NOT_CONFIRMABLE (with `reason`) and NOT_RESOLVED; 422 for a bad `value`;
  // 404 for a result of another submission. Returns the whole review, re-read
  // after the save.
  reviewResult: (submissionId: string, resultId: string, body: SubmissionReviewActionRequest) =>
    apiFetch<SubmissionReviewDetail>(`/submissions/${submissionId}/results/${resultId}/review`, {
      method: 'POST', json: body, token: t(),
    }),
  // Confirm every unresolved bulk-acceptable reading in one save.
  // The version is bumped even when nothing was eligible.
  acceptRemaining: (submissionId: string, reviewVersion: number) =>
    apiFetch<SubmissionAcceptRemainingResult>(`/submissions/${submissionId}/accept-remaining`, {
      method: 'POST', json: { review_version: reviewVersion }, token: t(),
    }),
  // Close a SCANNED submission: REVIEWED, lock released, read-only.
  // Allowed with readings still unresolved; the version is the one the
  // operator confirmed on. 409 EXTRACTION_RUNNING during a run, and the review
  // save guard's NOT_REVIEWABLE / LOCK_LOST / SUBMISSION_CHANGED.
  closeSubmission: (submissionId: string, reviewVersion: number) =>
    apiFetch<SubmissionReviewDetail>(`/submissions/${submissionId}/close`, {
      method: 'POST', json: { review_version: reviewVersion }, token: t(),
    }),
  // Reopen a REVIEWED submission: SCANNED, the lock the caller's,
  // every review kept, unresolved readings back on the batch's threshold. The
  // version is the closed one the operator saw. 409 EXTRACTION_RUNNING during
  // a run, INVALID_STATUS (with `status`) unless REVIEWED, SUBMISSION_CHANGED.
  reopenSubmission: (submissionId: string, reviewVersion: number) =>
    apiFetch<SubmissionReviewDetail>(`/submissions/${submissionId}/reopen`, {
      method: 'POST', json: { review_version: reviewVersion }, token: t(),
    }),
  releaseSubmissionLock: (submissionId: string) =>
    apiFetch<void>(`/submissions/${submissionId}/lock`, { method: 'DELETE', token: t() }),
  // Read the answers off every eligible sheet of the batch. Manual and slow (it
  // opens every page image); idempotent, so pressing it again only reads what
  // is new. Any member; not gated on the project being ACTIVE. 409
  // EXTRACTION_RUNNING while another run or change holds the batch.
  runBatchExtraction: (batchId: string) =>
    apiFetch<ExtractionRunSummary>(`/submission-batches/${batchId}/run-extraction`, {
      method: 'POST', token: t(),
    }),
  // Set the batch's confidence-threshold override (a fraction 0–1), or clear it
  // with null to follow the system default again. 409 EXTRACTION_RUNNING
  // while an extraction run holds the batch.
  updateBatchConfidenceThreshold: (batchId: string, threshold: number | null) =>
    apiFetch<SubmissionBatchRead>(`/submission-batches/${batchId}/confidence-threshold`, {
      method: 'PATCH', json: { confidence_threshold: threshold }, token: t(),
    }),
  // Pre-flight check for the scanning pipeline: is there exactly one way to
  // match a scanned sheet to a variant? Never 4xx on a "not ready" project.
  getScanReadiness: (projectId: string) =>
    apiFetch<ScanReadiness>(`/form-projects/${projectId}/scan-readiness`, { token: t() }),
}

/**
 * Release a submission's review lock while the page is being torn down
 * (`pagehide`). A raw keepalive fetch rather than `navigator.sendBeacon`, which
 * cannot carry the Authorization header, and rather than apiFetch, whose
 * failure handling (session expiry, thrown errors) has nobody left to serve.
 * Never throws: a lost release is covered by the BE's lock timeout.
 */
export function releaseSubmissionLockOnUnload(submissionId: string): void {
  const token = getToken()
  if (!token) return
  try {
    void fetch(`${BASE}/submissions/${submissionId}/lock`, {
      method: 'DELETE',
      keepalive: true,
      headers: { Authorization: `Bearer ${token}` },
    }).catch(() => undefined)
  } catch { /* the page is going away; nothing to report to */ }
}

/**
 * Download a batch export — `summary` for the Summary export, `detail` for
 * the Detail export — and hand it to the browser
 * under *filename*, the name the user confirmed (the server's
 * Content-Disposition is for direct API calls). Raw fetch, as for the variant
 * PDF: apiFetch does not decode xlsx / csv. Errors are built like apiFetch's
 * (`apiErrorFromResponse`), so a 409 NOT_EXPORT_READY keeps its code and counts.
 */
async function downloadBatchExport(
  kind: 'summary' | 'detail',
  batchId: string,
  format: ExportFormat,
  dialect: CsvDialect,
  filename: string,
): Promise<void> {
  const path = `/submission-batches/${batchId}/export/${kind}?format=${format}&csv_dialect=${dialect}`
  const token = getToken()
  const res = await fetch(`${BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) throw await apiErrorFromResponse(res, path)
  saveBlob(await res.blob(), filename)
}

/** The Summary export of a batch: one row per submission. */
export function downloadBatchSummary(
  batchId: string,
  format: ExportFormat,
  dialect: CsvDialect,
  filename: string,
): Promise<void> {
  return downloadBatchExport('summary', batchId, format, dialect, filename)
}

/** The Detail export of a batch: the Summary export plus one column per
    question; the .xlsx also has the "Columns" legend sheet. */
export function downloadBatchDetail(
  batchId: string,
  format: ExportFormat,
  dialect: CsvDialect,
  filename: string,
): Promise<void> {
  return downloadBatchExport('detail', batchId, format, dialect, filename)
}

/** Trigger a browser save of *blob*; where it lands is the browser's choice. */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/**
 * Download a variant's PDF and trigger a browser save. Uses a raw fetch because
 * the shared apiFetch only blob-decodes image/octet-stream responses, not
 * application/pdf. Honours the server's Content-Disposition filename.
 */
export async function downloadVariantPdf(
  variantId: string,
  mode: ExportMode,
): Promise<void> {
  const token = getToken()
  const res = await fetch(
    `${BASE}/form-variants/${variantId}/export/pdf?mode=${mode}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  )
  if (!res.ok) {
    if (res.status === 401) markSessionExpired()
    let detail = `HTTP ${res.status}`
    try {
      const j = await res.json()
      if (typeof j.detail === 'string') detail = j.detail
    } catch { /* not JSON */ }
    throw new ApiError(res.status, detail)
  }
  const blob = await res.blob()
  const cd = res.headers.get('content-disposition') ?? ''
  const match = /filename="?([^"]+)"?/i.exec(cd)
  const filename = match?.[1] ?? `variant_${mode.toLowerCase()}.pdf`

  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
