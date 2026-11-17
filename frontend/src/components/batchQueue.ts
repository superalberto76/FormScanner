// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the review queue's header toolbar: the result
// line of a manual extraction run and the confidence-threshold percent format.
// Same split as batchSubmissions.ts: a plain .ts module (no JSX) so the wording
// and the parsing can be unit-tested with Node's native runner (`node --test`)
// — see batchQueue.test.ts. Only `import type` from the app runtime:
// BatchExtractionControl.tsx and BatchThresholdControl.tsx hold the markup.
import type { ExtractionRunSummary } from '../api/domain'

const NUMBER = new Intl.NumberFormat('en-US')

/**
 * "1,140 results" / "1 result". Not batchUpload's `pluralize`: that one prints
 * the bare number, and a run over a large batch creates results in the
 * thousands, which need the separator to be read at a glance.
 */
function countOf(n: number, singular: string, plural = `${singular}s`): string {
  return `${NUMBER.format(n)} ${n === 1 ? singular : plural}`
}

export interface ExtractionResultLines {
  /** What the run did, always present. */
  main: string
  /** The failures and what to do about them, or null when there were none. */
  errors: string | null
}

/**
 * The two lines shown under "Run extraction" after a run in this session.
 *
 * A run that read nothing and failed nothing says so explicitly: "0 results
 * created" alone reads like something went wrong, while the truth is that the
 * batch was already fully read. Errors get their own line because they call for
 * an action (run again) and are styled as a problem.
 */
export function extractionResultLines(summary: ExtractionRunSummary): ExtractionResultLines {
  let main =
    `Last run: ${countOf(summary.submissions_processed, 'submission')} processed · ` +
    `${countOf(summary.results_created, 'result')} created · ` +
    `${NUMBER.format(summary.submissions_scanned)} ready for review`
  if (summary.results_created === 0 && summary.errors === 0) {
    main += ' — nothing new to read.'
  }
  const errors = summary.errors > 0
    ? `${countOf(summary.errors, 'error')} — those submissions stay in “To extract”; run again to retry.`
    : null
  return { main, errors }
}

/**
 * "80%", "82.5%" — a threshold fraction as the percent an operator thinks in.
 * At most one decimal, trailing zero dropped: the BE stores any fraction, and
 * an override set elsewhere (e.g. 0.825) must not be rounded into a value the
 * batch does not actually use.
 */
export function formatThresholdPercent(fraction: number): string {
  return `${Math.round(fraction * 1000) / 10}%`
}

/**
 * The threshold the edit input describes, as the FRACTION the BE takes
 * ("85" → 0.85), or null when the input is not an integer 0–100 inclusive.
 * Integers only on purpose: whole percent steps are all the triage needs, and
 * a decimal input would invite values nobody can tell apart.
 */
export function parseThresholdPercentInput(raw: string): number | null {
  const trimmed = raw.trim()
  if (!/^\d{1,3}$/.test(trimmed)) return null
  const percent = Number(trimmed)
  return percent <= 100 ? percent / 100 : null
}
