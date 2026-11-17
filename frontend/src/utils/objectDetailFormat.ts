// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Formatting helpers for ObjectDetailPanel — centralizes the repeated nullish
// handling and boolean/label formatting so the panel's JSX stays declarative.

/** Placeholder shown for empty / null / undefined values. */
export const DASH = '—'

/** Display a scalar, falling back to DASH for null/undefined/blank strings. */
export function fmt(value: string | number | null | undefined): string | number {
  if (value === null || value === undefined) return DASH
  if (typeof value === 'string' && value.trim() === '') return DASH
  return value
}

/** Human-friendly boolean. */
export function yesNo(value: boolean): string {
  return value ? 'Yes' : 'No'
}

/** "bold, italic" | "bold" | "italic" | "normal". */
export function textStyleSummary(bold: boolean, italic: boolean): string {
  const parts = [bold ? 'bold' : null, italic ? 'italic' : null].filter(Boolean) as string[]
  return parts.length ? parts.join(', ') : 'normal'
}

/** Comma-joined labels ordered by order_index (DASH when empty). */
export function labelsByOrder<T extends { order_index: number; label: string }>(items: T[]): string {
  if (items.length === 0) return DASH
  return [...items].sort((a, b) => a.order_index - b.order_index).map(i => i.label).join(', ')
}
