// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the batch upload area.
// Kept in a plain .ts module (no JSX) so the classification and the advisory
// limits can be unit-tested with Node's native test runner (`node --test`)
// without rendering React — see batchUpload.test.ts. Deliberately free of
// app-runtime imports (only `import type`): the component (BatchUploadArea.tsx)
// holds the wiring, the rules live here.
import type { SubmissionBatchIngestResult } from '../api/domain'

/** The only PDF mime the ingestion path accepts. */
export const PDF_MIME = 'application/pdf'

/** Scanned sheets come off a scanner as JPEG or PNG — nothing else is ingested. */
export const IMAGE_MIMES = ['image/png', 'image/jpeg']

/** `accept` for the file input: both ingestion paths behind one dropzone. */
export const SUBMISSION_UPLOAD_ACCEPT = [PDF_MIME, ...IMAGE_MIMES].join(',')

/* Advisory client-side ceilings for ONE upload round. Neither is a rule — the
   BE stays authoritative per file (413/422) — they only warn before a selection
   that is going to be a long, failure-prone round trip. */
export const MAX_SELECTED_FILES = 50
export const MAX_SELECTED_BYTES = 300 * 1024 * 1024

export interface ClassifiedFiles {
  pdfFiles: File[]
  imageFiles: File[]
  /** Dropped files that are neither PDF nor JPEG/PNG. Ignored, never blocking. */
  ignoredCount: number
}

/** Same file dropped twice (name + size + mtime) — a re-drop, not a second sheet. */
function fileKey(f: File): string {
  return `${f.name}::${f.size}::${f.lastModified}`
}

/**
 * Split a drop / file-input selection into the two ingestion paths, keeping the
 * order the files arrived in (for images that order is the last-resort page
 * order, so it must not be reshuffled).
 *
 * `already` is the current selection: its files are kept as-is at the front and
 * anything re-selected is dropped, so dropping the same folder twice does not
 * upload it twice. Unsupported types are counted and silently excluded — the
 * dropzone already states what it takes, and one stray .docx must not block a
 * selection that is otherwise fine.
 */
export function classifyFiles(
  incoming: File[],
  already: ClassifiedFiles = { pdfFiles: [], imageFiles: [], ignoredCount: 0 },
): ClassifiedFiles {
  const seen = new Set([...already.pdfFiles, ...already.imageFiles].map(fileKey))
  const pdfFiles = [...already.pdfFiles]
  const imageFiles = [...already.imageFiles]
  let ignoredCount = already.ignoredCount

  for (const file of incoming) {
    if (file.type === PDF_MIME) {
      if (!seen.has(fileKey(file))) { seen.add(fileKey(file)); pdfFiles.push(file) }
    } else if (IMAGE_MIMES.includes(file.type)) {
      if (!seen.has(fileKey(file))) { seen.add(fileKey(file)); imageFiles.push(file) }
    } else {
      ignoredCount++
    }
  }
  return { pdfFiles, imageFiles, ignoredCount }
}

/** Total bytes of a selection (both paths). */
export function totalBytes(files: ClassifiedFiles): number {
  return [...files.pdfFiles, ...files.imageFiles].reduce((sum, f) => sum + f.size, 0)
}

/** Total file count of a selection (both paths). */
export function totalCount(files: ClassifiedFiles): number {
  return files.pdfFiles.length + files.imageFiles.length
}

/**
 * Advisory warning for an oversized selection, or null when it is within both
 * ceilings. ADVISORY ONLY: the caller keeps the Upload button enabled — the
 * user may well know better, and the BE is the authority on what it accepts.
 */
export function selectionWarning(files: ClassifiedFiles): string | null {
  const count = totalCount(files)
  const bytes = totalBytes(files)
  const overCount = count > MAX_SELECTED_FILES
  const overBytes = bytes > MAX_SELECTED_BYTES
  if (!overCount && !overBytes) return null

  const mb = Math.round(bytes / (1024 * 1024))
  const maxMb = Math.round(MAX_SELECTED_BYTES / (1024 * 1024))
  const parts: string[] = []
  if (overCount) parts.push(`${count} files (suggested max ${MAX_SELECTED_FILES})`)
  if (overBytes) parts.push(`${mb} MB (suggested max ${maxMb} MB)`)
  return `Large upload: ${parts.join(' and ')}. It may take a while, and single files can still be refused by the server. Remove some files to stay under the limit, or upload in more rounds.`
}

/** "3 files" / "1 file" — the Upload button's label needs both. */
export function pluralize(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`
}

/**
 * The counters line under a completed image upload, in this order:
 * what the call opened, what it extended, what it could not group, what it
 * refused. Always all four — a silent shortfall is exactly what an operator
 * must not get, so a zero is stated rather than omitted.
 */
export function ingestSummaryLine(result: SubmissionBatchIngestResult): string {
  return [
    pluralize(result.submissions_created.length, 'new submission'),
    `${result.submissions_updated.length} updated`,
    `${result.unassigned_count} unassigned`,
    `${result.rejected.length} rejected`,
  ].join(' · ')
}
