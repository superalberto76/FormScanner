// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useToast } from './ToastProvider'
import { useConfirm } from './ConfirmProvider'
import { nd, type FormProjectRead, type FormProjectStatus } from '../api/domain'
import { allowedTransitions, TRANSITION_META } from '../utils/projectLifecycle'

interface Props {
  project: FormProjectRead
  canManage: boolean
}

// Per-target confirmation copy, tuned to what each move does so the
// owner/admin understands the consequence before committing. Keyed by TARGET.
const CONFIRM_MESSAGE: Record<FormProjectStatus, string> = {
  ACTIVE:
    "Activate the project? While the project is active, the content of its variants is frozen (it cannot be edited).",
  CLOSED:
    "Close the project? Variants can no longer be published; export stays available.",
  DRAFT:
    "Reopen the project as a draft? Variants already published stay published.",
  ARCHIVED:
    "Archive the project? Published variants become ARCHIVED and everything becomes read-only. This cannot be undone.",
}

/**
 * Owner/admin-only lifecycle transition buttons — one per allowed outgoing edge
 * of the project's current state (mirrors the BE graph; see projectLifecycle.ts).
 * Collaborators/non-members see nothing (the status badge alone communicates
 * state). The BE remains authoritative: illegal or forbidden moves (e.g. activate
 * without a PUBLISHED variant) surface verbatim as an error toast.
 */
export function ProjectLifecycleActions({ project, canManage }: Props) {
  const toast = useToast()
  const confirm = useConfirm()
  const qc = useQueryClient()

  const transitionM = useMutation({
    mutationFn: (target: FormProjectStatus) => nd.transitionProject(project.id, target),
    onSuccess: (_data, target) => {
      toast.success(`Status changed to ${target}`)
      void qc.invalidateQueries({ queryKey: ['project', project.id] })
      void qc.invalidateQueries({ queryKey: ['projects'] })            // list badge
      void qc.invalidateQueries({ queryKey: ['variants', project.id] }) // archive cascade
    },
    onError: (err) => toast.error(err),  // BE 409/403 (e.g. activate gate) verbatim
  })

  // Collaborators / non-members see no transition controls; ARCHIVED is terminal.
  if (!canManage) return null
  const targets = allowedTransitions(project.status)
  if (targets.length === 0) return null

  return (
    <>
      {targets.map((target) => {
        const meta = TRANSITION_META[target]
        return (
          <button
            key={target}
            className="btn btn-secondary btn-sm"
            style={meta.danger ? { color: 'var(--danger)' } : undefined}
            disabled={transitionM.isPending}
            title={`${meta.label} → ${target}`}
            onClick={async () => {
              const ok = await confirm({
                title: `${meta.label} project`,
                message: CONFIRM_MESSAGE[target],
                confirmLabel: meta.label,
                danger: meta.danger,
              })
              if (ok) transitionM.mutate(target)
            }}
          >
            {meta.label}
          </button>
        )
      })}
    </>
  )
}
