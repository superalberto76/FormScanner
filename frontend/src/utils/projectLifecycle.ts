// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Project lifecycle graph — the SINGLE front-end source of truth for which
// status moves are legal. It mirrors the backend graph EXACTLY
// (_ALLOWED_TRANSITIONS in app/services/form_project_service.py); the BE stays
// authoritative (every transition is re-validated server-side, illegal/forbidden
// moves return 409/403). The FE only uses this to decide which buttons to offer.
import type { FormProjectStatus } from '../api/domain'

// The ONLY legal status moves. ARCHIVED is terminal (no outgoing edges).
export const PROJECT_TRANSITIONS: Record<FormProjectStatus, FormProjectStatus[]> = {
  DRAFT:    ['ACTIVE', 'CLOSED'],
  ACTIVE:   ['CLOSED', 'DRAFT'],
  CLOSED:   ['DRAFT', 'ARCHIVED'],
  ARCHIVED: [],
}

// Per-target button metadata: the action label and whether the move is
// destructive (drives a danger style + danger confirm). Keyed by the TARGET
// status — e.g. moving *to* DRAFT is "Reopen", *to* ARCHIVED "Archive".
export interface TransitionMeta {
  label: string
  danger: boolean
}
export const TRANSITION_META: Record<FormProjectStatus, TransitionMeta> = {
  ACTIVE:   { label: 'Activate', danger: false },
  CLOSED:   { label: 'Close',    danger: false },
  DRAFT:    { label: 'Reopen',   danger: false },
  ARCHIVED: { label: 'Archive',  danger: true  },
}

/** The allowed outgoing transitions from *status* (empty for ARCHIVED). */
export function allowedTransitions(status: FormProjectStatus): FormProjectStatus[] {
  return PROJECT_TRANSITIONS[status]
}
