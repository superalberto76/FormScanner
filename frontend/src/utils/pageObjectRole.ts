// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Special scanning roles (identification): the small typed constants
// + helpers shared by the ObjectEditPanel "Special role" section, the
// Identification summary and the PageObjectList badge. Colocated with the other
// display-utility modules (see objectDisplay.ts).

import type { PageObjectDetail, PageObjectRole, PageObjectRoleBinding, PageObjectType } from '../api/domain'

// Which object types may hold each role. FE-side static policy (NOT derived from
// a backend field): a VARIANT_IDENTIFIER must be a CODE; a RESPONDENT_IDENTIFIER
// may be a CODE, MATRIX or FREE_TEXT_AREA. TEXT / QUESTION / IMAGE are eligible
// for neither, so the whole role section is omitted for them.
export const ROLE_ELIGIBLE_TYPES: Record<PageObjectRole, PageObjectType[]> = {
  VARIANT_IDENTIFIER: ['CODE'],
  RESPONDENT_IDENTIFIER: ['CODE', 'MATRIX', 'FREE_TEXT_AREA'],
}

// Human labels for badges, tooltips and the summary rows.
export const ROLE_LABELS: Record<PageObjectRole, string> = {
  VARIANT_IDENTIFIER: 'Variant identifier',
  RESPONDENT_IDENTIFIER: 'Respondent identifier',
}

// Fixed display order (VARIANT_IDENTIFIER first) for the panel controls and the
// Identification summary — kept explicit so iteration never depends on object-key
// ordering.
export const ALL_ROLES: PageObjectRole[] = ['VARIANT_IDENTIFIER', 'RESPONDENT_IDENTIFIER']

/** Roles a given object type may hold, in fixed display order (possibly empty). */
export function eligibleRolesFor(objectType: PageObjectType): PageObjectRole[] {
  return ALL_ROLES.filter(role => ROLE_ELIGIBLE_TYPES[role].includes(objectType))
}

/** A bound object's display label, falling back to its raw id when unlabeled. */
export function roleBindingLabel(binding: PageObjectRoleBinding): string {
  return binding.object_label?.trim() || binding.page_object_id
}

/**
 * Whether a CODE object may enable "include page number": it holds
 * VARIANT_IDENTIFIER itself, OR a shared sibling (same non-null shared_group_id)
 * holds it in this variant's role bindings. An object with no shared_group_id can
 * only qualify via its own role. Pure boolean — computed on render, not state.
 */
export function holdsVariantIdentifier(
  obj: Pick<PageObjectDetail, 'role' | 'shared_group_id'>,
  bindings: PageObjectRoleBinding[],
): boolean {
  if (obj.role === 'VARIANT_IDENTIFIER') return true
  if (obj.shared_group_id == null) return false
  return bindings.some(b => b.role === 'VARIANT_IDENTIFIER' && b.shared_group_id === obj.shared_group_id)
}
