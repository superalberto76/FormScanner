// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Decides which content-preview layer an object overlay renders on the authoring
// canvas. Kept as a pure, DOM-free function so PageOverlay stays
// declarative and the per-type selection is unit-testable without React.

import type { PageObjectListItem, PageObjectType } from '../api/domain'

/** The preview to draw for an object, or null when it should show geometry only. */
export type PreviewKind = PageObjectType | null

/**
 * Pick the preview layer for an object. Returns null when the object is hidden
 * (`visible=false` → geometry only, including IMAGE) or its type has no preview,
 * so the overlay renders just its box. Every current type maps to a preview.
 */
export function pickPreviewKind(
  obj: Pick<PageObjectListItem, 'visible' | 'object_type'>,
): PreviewKind {
  if (!obj.visible) return null
  switch (obj.object_type) {
    case 'TEXT':
    case 'CODE':
    case 'QUESTION':
    case 'MATRIX':
    case 'FREE_TEXT_AREA':
    case 'IMAGE':
      return obj.object_type
    default:
      return null
  }
}
