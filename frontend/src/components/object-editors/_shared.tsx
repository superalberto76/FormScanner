// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReactNode } from 'react'
import { normalizeApiError } from '../../utils/apiError'

/** Inline, user-visible error for a failed editor mutation (422/409/etc.). */
export function InlineError({ error }: { error: unknown }) {
  if (!error) return null
  return <p className="nd-edit-error">{normalizeApiError(error)}</p>
}

/** Titled editor section. */
export function EditorSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="stack stack-8">
      <h4 className="nd-edit-title">{title}</h4>
      {children}
    </section>
  )
}
