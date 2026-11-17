// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Per-type visual vocabulary for PageObject overlays / list / detail.
// Colors: TEXT gray, CODE blue, FREE_TEXT_AREA orange,
// QUESTION green, MATRIX purple.

import type { PageObjectListItem, PageObjectType } from '../api/domain'

export interface ObjectTypeStyle {
  /** Outline / accent color. */
  color: string
  /** Faint fill used behind the overlay. */
  fill: string
  /** Short human label. */
  label: string
}

export const OBJECT_TYPE_STYLE: Record<PageObjectType, ObjectTypeStyle> = {
  TEXT: { color: '#475569', fill: 'rgba(71, 85, 105, 0.06)', label: 'Text' },
  CODE: { color: '#2563eb', fill: 'rgba(37, 99, 235, 0.08)', label: 'Code' },
  FREE_TEXT_AREA: { color: '#ea580c', fill: 'rgba(234, 88, 12, 0.08)', label: 'Free text' },
  QUESTION: { color: '#16a34a', fill: 'rgba(22, 163, 74, 0.08)', label: 'Question' },
  MATRIX: { color: '#7c3aed', fill: 'rgba(124, 58, 237, 0.08)', label: 'Matrix' },
  IMAGE: { color: '#d946ef', fill: 'rgba(217, 70, 239, 0.08)', label: 'Image' },
}

/** Label shown on the overlay chip / list row: the user label, else the type name. */
export function objectDisplayLabel(o: Pick<PageObjectListItem, 'label' | 'object_type'>): string {
  const trimmed = o.label?.trim()
  return trimmed || OBJECT_TYPE_STYLE[o.object_type].label
}
