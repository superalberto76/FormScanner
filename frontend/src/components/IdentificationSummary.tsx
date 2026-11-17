// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { nd, type PageObjectRole } from '../api/domain'
import { normalizeApiError } from '../utils/apiError'
import { ALL_ROLES, roleBindingLabel, ROLE_LABELS } from '../utils/pageObjectRole'

interface Props {
  variantId: string
}

/** One labelled row inside a `.detail-grid` (local equivalent of ObjectDetailPanel's Field). */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (<><dt>{label}</dt><dd>{children}</dd></>)
}

/**
 * Read-only "Identification" summary: the variant's
 * current role assignments. ALWAYS visible in the workspace right column,
 * independent of object selection. Two fixed rows (Variant identifier /
 * Respondent identifier); each shows "{object} — p. {page}" when assigned, else
 * "Not assigned".
 *
 * Uses its own ['role-bindings', variantId] query — react-query dedupes it with
 * the ObjectEditPanel role section's identical subscription, so a role mutation
 * there refreshes this summary for free (no prop-drilling).
 */
export function IdentificationSummary({ variantId }: Props) {
  const bindingsQ = useQuery({
    queryKey: ['role-bindings', variantId],
    queryFn: () => nd.getVariantRoleBindings(variantId),
    enabled: !!variantId,
  })

  const bindingFor = (role: PageObjectRole) => bindingsQ.data?.find(b => b.role === role)

  return (
    <div className="nd-panel-section">
      <h3 className="mb-16">Identification</h3>
      {bindingsQ.isError ? (
        <p className="error-msg">{normalizeApiError(bindingsQ.error)}</p>
      ) : (
        <dl className="detail-grid">
          {ALL_ROLES.map(role => {
            const binding = bindingFor(role)
            return (
              <Row key={role} label={ROLE_LABELS[role]}>
                {binding
                  ? `${roleBindingLabel(binding)} — p. ${binding.page_index}`
                  : <span className="nd-muted">Not assigned</span>}
              </Row>
            )
          })}
        </dl>
      )}
    </div>
  )
}
