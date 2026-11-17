// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { QuestionType } from '../api/domain'

/** Options carrying just the correct flag (creation drafts or persisted reads). */
type CorrectFlag = { is_correct: boolean }

/**
 * SINGLE_CHOICE allows at most one correct option. Returns true when the option
 * at `index` must have its "correct" checkbox DISABLED because another option is
 * already correct — so the user can't select a second one and must clear the
 * current pick first. The already-correct option itself is never locked, so it
 * can be unchecked. No-op for MULTIPLE_CHOICE.
 *
 * Mirrors the MATRIX isCorrectLocked pattern for QUESTION.
 */
export function isCorrectLocked(
  questionType: QuestionType,
  options: readonly CorrectFlag[],
  index: number,
): boolean {
  if (questionType !== 'SINGLE_CHOICE') return false
  if (options[index]?.is_correct) return false
  return options.some((o, i) => i !== index && o.is_correct)
}
