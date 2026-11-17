// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the variant page rail: drag-reorder maths and the
// page-removal confirmation copy (N objects / M linked "show on all pages"). Kept
// free of React/JSX so they run on Node's built-in test runner (see pageRail.test.ts).

/** A page-removal impact summary: total objects on the page, and how many of them
 *  are LINKED members of a "show on all pages" group (only this page's copy is
 *  removed; the siblings on other pages survive). */
export interface PageRemovalImpact {
  /** Total objects on the page (N). */
  total: number
  /** Linked "show on all pages" members affected (M). */
  linked: number
}

/** Count the removal impact of a page from its objects (order-independent). A
 *  member counts as linked only when it is shared_across_pages AND still carries a
 *  shared_group_id (mirrors the BE's linked_members_removed tally). */
export function countPageRemovalImpact(
  objects: { shared_across_pages: boolean; shared_group_id: string | null }[],
): PageRemovalImpact {
  return {
    total: objects.length,
    linked: objects.filter(o => o.shared_across_pages && o.shared_group_id != null).length,
  }
}

/** Main confirmation message. When linked > 0 it
 *  explains that only this page's copies go and the siblings stay untouched. */
export function pageRemovalMessage({ total, linked }: PageRemovalImpact): string {
  if (linked > 0) {
    return (
      `This page contains ${total} objects. Only the copies on this page will be ` +
      `removed; the copies on the other pages are not touched. Removing the page ` +
      `cannot be undone.`
    )
  }
  return `This page contains ${total} objects. Removing the page cannot be undone.`
}

/** The "linked members affected" chip text, or null when there are none (M === 0). */
export function pageRemovalLinkedChip(linked: number): string | null {
  if (linked <= 0) return null
  return `🔗 ${linked} objects "shown on all pages" affected`
}

/**
 * Reorder `ids` by moving `dragId` to insertion slot `slot`, where `slot` is an
 * index into the ORIGINAL array (0 = before the first item, ids.length = after the
 * last). Returns a new array; returns `ids` unchanged when `dragId` is absent. The
 * slot is interpreted against the original positions, so it is adjusted for the
 * removal of the dragged item before insertion.
 */
export function reorderByDrop(ids: string[], dragId: string, slot: number): string[] {
  const from = ids.indexOf(dragId)
  if (from < 0) return ids
  const without = ids.filter((_, i) => i !== from)
  // `slot` counts the dragged item; once removed, every position at/after `from`
  // shifts left by one.
  let target = from < slot ? slot - 1 : slot
  target = Math.max(0, Math.min(target, without.length))
  without.splice(target, 0, dragId)
  return without
}

/** Shallow equality for two id arrays (used to skip no-op reorder requests). */
export function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((id, i) => id === b[i])
}
