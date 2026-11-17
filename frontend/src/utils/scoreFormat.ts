// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helper for showing a score (also used for the submission score).
// Scores are floats on the server and are never rounded there: the
// rounding is the display's job. Kept free of React/JSX so it runs on Node's
// built-in test runner (see scoreFormat.test.ts).

import type { SubmissionScore } from '../api/domain'

/** A score with at most two decimals and no trailing zeros, with a dot as the
 *  decimal separator (UI strings are English): 18 → "18", 17.5 → "17.5",
 *  1/3 → "0.33". A value that rounds to zero shows as "0", never "-0". */
export function formatScore(score: number): string {
  // Binary floats round some exact halves down (1.005 → "1", not "1.01"):
  // harmless for scores, which are authored with at most a couple of decimals.
  const rounded = Math.round(score * 100) / 100
  return String(rounded === 0 ? 0 : rounded)
}

/** The score of a submission as its triple:
 *  "12 / 18 (66.7%)". The percentage has one decimal, trailing zero dropped
 *  ("50%"), and shows as "—" when the server sends none (maximum 0). A
 *  negative total keeps its sign in both parts. */
export function formatScoreTriple(score: SubmissionScore): string {
  const points = `${formatScore(score.raw_score)} / ${formatScore(score.max_possible_score)}`
  if (score.percentage === null) return `${points} (—)`
  // Rounded on the absolute value, sign put back: Math.round sends halves
  // towards +∞, which would show -12.25 as "-12.2%" next to "12.3%".
  const rounded = Math.sign(score.percentage) * Math.round(Math.abs(score.percentage) * 10) / 10
  return `${points} (${rounded === 0 ? 0 : rounded}%)`
}
