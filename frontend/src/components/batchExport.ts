// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the batch results export: the
// two exports (summary and detail) and the texts of their dialog, the format
// choices, the proposed file name and its checks, and the line that says why a
// batch cannot be exported yet. A plain
// .ts module (no JSX) so it is unit-tested with `node --test`
// (batchExport.test.ts); the markup is in BatchExportControl.tsx.
import type { CsvDialect, ExportFormat, ExportReadiness } from '../api/domain'

/** The two exports of a batch: the Summary export and the Detail export. */
export type ExportKind = 'summary' | 'detail'

export interface ExportKindInfo {
  /** The toolbar button. */
  button: string
  /** The dialog title. */
  title: string
  /** After «One row per submission of <batch> (N)». */
  rowsSuffix: string
  /** The proposed file name starts with it. */
  prefix: string
}

const EXPORT_KINDS: Record<ExportKind, ExportKindInfo> = {
  summary: {
    button: 'Export summary…',
    title: 'Export summary',
    rowsSuffix: '.',
    prefix: 'Summary',
  },
  detail: {
    button: 'Export detail…',
    title: 'Export detail',
    rowsSuffix: ', one column per question.',
    prefix: 'Detail',
  },
}

export function exportKindInfo(kind: ExportKind): ExportKindInfo {
  return EXPORT_KINDS[kind]
}

/** One of the three entries of the dialog's format choice. */
export type ExportChoice = 'csv-comma' | 'csv-semicolon' | 'xlsx'

export interface ExportChoiceInfo {
  value: ExportChoice
  label: string
  hint: string | null
  format: ExportFormat
  dialect: CsvDialect
  extension: string
}

/** In the dialog's order; the first is the default. */
export const EXPORT_CHOICES: readonly ExportChoiceInfo[] = [
  {
    value: 'csv-comma',
    label: 'CSV — comma separated',
    hint: '“,” between fields, “.” as decimal mark',
    format: 'csv',
    dialect: 'comma',
    extension: 'csv',
  },
  {
    value: 'csv-semicolon',
    label: 'CSV — semicolon separated',
    hint: '“;” between fields, “,” as decimal mark (Excel, Italian settings)',
    format: 'csv',
    dialect: 'semicolon',
    extension: 'csv',
  },
  {
    value: 'xlsx',
    label: 'Excel workbook (.xlsx)',
    hint: null,
    format: 'xlsx',
    // Ignored by the BE with xlsx; sent anyway so the request is always complete.
    dialect: 'comma',
    extension: 'xlsx',
  },
]

export const DEFAULT_EXPORT_CHOICE: ExportChoice = 'csv-comma'

export function exportChoiceInfo(choice: ExportChoice): ExportChoiceInfo {
  return EXPORT_CHOICES.find(c => c.value === choice) ?? EXPORT_CHOICES[0]
}

/** A remembered value back to a choice; anything unknown is the default. */
export function parseExportChoice(stored: string | null | undefined): ExportChoice {
  return EXPORT_CHOICES.some(c => c.value === stored)
    ? (stored as ExportChoice)
    : DEFAULT_EXPORT_CHOICE
}

// ── File name ────────────────────────────────────────────────────────────────

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/**
 * `<Prefix>_<batch name>_<YYYYMMDD-HHMM>Z`, without the extension, the time in
 * UTC. The same scheme as the BE's `summary_filename` / `detail_filename`: in
 * the batch name every run of characters other than letters (accented ones
 * too), digits, `.`, `_` and `-` becomes one `_`; `batch` if nothing is left.
 */
export function proposedExportName(kind: ExportKind, batchName: string, now: Date): string {
  const name = batchName.replace(/[^\p{L}\p{N}_.-]+/gu, '_').replace(/^[._]+|[._]+$/g, '') || 'batch'
  const stamp =
    `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}` +
    `-${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}`
  return `${exportKindInfo(kind).prefix}_${name}_${stamp}Z`
}

/** `Summary_<batch name>_<YYYYMMDD-HHMM>Z`. */
export function proposedSummaryName(batchName: string, now: Date): string {
  return proposedExportName('summary', batchName, now)
}

/** `Detail_<batch name>_<YYYYMMDD-HHMM>Z`. */
export function proposedDetailName(batchName: string, now: Date): string {
  return proposedExportName('detail', batchName, now)
}

/** The characters a file name cannot contain (Windows' list, the strictest). */
export const INVALID_FILE_NAME_CHARS = '/ \\ : * ? " < > |'
const INVALID_FILE_NAME_RE = /[/\\:*?"<>|]/

/**
 * Why *name* cannot be used, or null if it can ("Download" stays disabled,
 * nothing is replaced silently). Spaces around the
 * name do not count: they are trimmed at download.
 */
export function fileNameError(name: string): string | null {
  if (name.trim() === '') return 'Enter a file name.'
  if (INVALID_FILE_NAME_RE.test(name)) {
    return `A file name cannot contain these characters: ${INVALID_FILE_NAME_CHARS}`
  }
  return null
}

/** The name the browser saves: the trimmed name plus the choice's extension. */
export function downloadFileName(name: string, choice: ExportChoice): string {
  return `${name.trim()}.${exportChoiceInfo(choice).extension}`
}

// ── Why the batch cannot be exported ─────────────────────────────────────────

const REASONS: ReadonlyArray<[keyof Omit<ExportReadiness, 'ready'>, string]> = [
  ['to_extract', 'to extract'],
  ['in_review', 'in review'],
  ['to_assign', 'to assign'],
  ['unassigned', 'unassigned'],
]

export interface ExportBlocked {
  text: string
  // A warning when something is in progress; muted when there is simply
  // nothing to export.
  tone: 'warning' | 'muted'
}

/**
 * The line under the disabled export buttons, null when the batch is ready.
 * With submissions in progress, the reasons with a count above zero, in REASONS
 * order («Not ready: 1 to extract · 14 in review»). With none, the batch
 * is empty or holds UNRESOLVABLE submissions only: the server's
 * flags say which only through *submissionCount*.
 */
export function exportBlockedLine(
  readiness: ExportReadiness,
  submissionCount: number,
): ExportBlocked | null {
  if (readiness.ready) return null
  const parts = REASONS.filter(([key]) => readiness[key] > 0).map(
    ([key, label]) => `${readiness[key]} ${label}`,
  )
  if (parts.length > 0) return { text: `Not ready: ${parts.join(' · ')}`, tone: 'warning' }
  if (submissionCount === 0) {
    return { text: 'Nothing to export yet: upload and review submissions first.', tone: 'muted' }
  }
  return { text: 'Nothing to export: no submission has been closed by a review.', tone: 'muted' }
}

/**
 * The error shown in the dialog for any failure other than NOT_EXPORT_READY,
 * e.g. «The export failed: HTTP 502. Try again.» A full stop is
 * added only when *reason* does not end with one already.
 */
export function exportFailedText(reason: string): string {
  const text = reason.trim()
  return `The export failed: ${text}${/[.!?]$/.test(text) ? '' : '.'} Try again.`
}
