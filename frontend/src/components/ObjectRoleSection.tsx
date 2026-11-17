// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { nd, type PageObjectDetail, type PageObjectRole } from '../api/domain'
import { eligibleRolesFor, roleBindingLabel, ROLE_LABELS } from '../utils/pageObjectRole'
import { EditorSection } from './object-editors/_shared'
import { useToast } from './ToastProvider'

interface Props {
  obj: PageObjectDetail
  variantId: string
  pageId: string
}

/**
 * "Special role" section of the object editor.
 *
 * An IMMEDIATE, non-draft mutation surface — the closest analogue is "Shared
 * across pages": it mutates instantly (independent of the panel's Save/Reset
 * flow) and reconciles via query invalidation, so it stays interactive even
 * while other draft fields are dirty.
 *
 * State is derived from `obj.role` / `obj.object_type` / `obj.shared_across_pages`
 * plus this component's own ['role-bindings', variantId] query (deduped by
 * react-query with the Identification summary's identical subscription — no
 * prop-drilling). Assign/remove are pessimistic (button disabled while pending,
 * no optimistic local state); on settle we always refetch the role bindings so a
 * concurrently-assigned role corrects the locally-computed "occupied" state.
 * The section is omitted entirely for object types that can hold no role.
 */
export function ObjectRoleSection({ obj, variantId, pageId }: Props) {
  const qc = useQueryClient()
  const toast = useToast()

  const roleBindingsQ = useQuery({
    queryKey: ['role-bindings', variantId],
    queryFn: () => nd.getVariantRoleBindings(variantId),
    enabled: !!variantId,
  })

  // Invalidate the three touched caches: role bindings (summary +
  // this section), the page-object list (role rides on the list rows) and this
  // object's detail (drives obj.role here). Role bindings are refetched on BOTH
  // success and error (onSettled) as a race-condition safety net.
  function invalidateRoleBindings() {
    void qc.invalidateQueries({ queryKey: ['role-bindings', variantId] })
  }
  function invalidateObject() {
    void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
    void qc.invalidateQueries({ queryKey: ['page-object', obj.id] })
    // A respondent-ID role takes an evaluative MATRIX out of the maximum.
    void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
  }

  const assignM = useMutation({
    mutationFn: (role: PageObjectRole) => nd.setPageObjectRole(obj.id, role),
    onSuccess: (_data, role) => {
      invalidateObject()
      toast.success(`Assigned as ${ROLE_LABELS[role]}`)
    },
    onError: (err) => toast.error(err),
    onSettled: invalidateRoleBindings,
  })

  const removeM = useMutation({
    mutationFn: () => nd.removePageObjectRole(obj.id),
    onSuccess: () => {
      invalidateObject()
      toast.success('Role removed')
    },
    onError: (err) => toast.error(err),
    onSettled: invalidateRoleBindings,
  })

  const eligibleRoles = eligibleRolesFor(obj.object_type)
  // Omit the section entirely for types that can never hold a role (TEXT /
  // QUESTION / IMAGE).
  if (eligibleRoles.length === 0) return null

  const busy = assignM.isPending || removeM.isPending
  const bindings = roleBindingsQ.data ?? []

  return (
    <EditorSection title="Special role">
      {obj.role ? (
        // State 1 — this object already holds a role: badge + Remove only (no
        // reassignment shortcut; the user must remove first, mirroring the BE 409).
        <>
          <p className="nd-muted nd-edit-hint">
            🏷 This object is the <strong>{ROLE_LABELS[obj.role]}</strong> for this variant.
          </p>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => removeM.mutate()}>
            {removeM.isPending ? 'Removing…' : 'Remove role'}
          </button>
        </>
      ) : (
        // State 2 — no role yet: one assign control per eligible role, each
        // independently enabled/disabled with its own microcopy.
        <div className="stack stack-8">
          {eligibleRoles.map(role => {
            // Since obj.role is null this object holds no binding; any binding for
            // `role` is therefore held by another object (the id guard is belt-and-
            // suspenders).
            const occupiedBy = bindings.find(b => b.role === role && b.page_object_id !== obj.id)
            const needsShare = role === 'VARIANT_IDENTIFIER' && !obj.shared_across_pages
            const disabled = busy || roleBindingsQ.isLoading || !!occupiedBy || needsShare
            return (
              <div key={role} className="stack stack-4">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={disabled}
                  onClick={() => assignM.mutate(role)}
                >
                  {assignM.isPending ? 'Assigning…' : `Set as ${ROLE_LABELS[role]}`}
                </button>
                {occupiedBy ? (
                  <p className="nd-muted nd-edit-hint">
                    Already assigned to '{roleBindingLabel(occupiedBy)}' — p. {occupiedBy.page_index}
                  </p>
                ) : needsShare ? (
                  <p className="nd-muted nd-edit-hint">Requires 'Show on all pages' enabled</p>
                ) : null}
              </div>
            )
          })}
        </div>
      )}
    </EditorSection>
  )
}
