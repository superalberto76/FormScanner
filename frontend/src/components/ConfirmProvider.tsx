// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import { ConfirmDialog } from './ConfirmDialog'

export interface ConfirmOptions {
  title: string
  message: ReactNode
  /** Optional block rendered below the message (e.g. a chip/note). */
  details?: ReactNode
  confirmLabel?: string
  danger?: boolean
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<ConfirmFn | null>(null)

/**
 * Imperative confirmation. `const confirm = useConfirm()` then
 * `if (await confirm({ title, message })) { ...do it... }`.
 *
 * Centralizes destructive-action confirmation (delete object/option/row/col/cell,
 * publish, clone) so each call site is one line instead of bespoke dialog state.
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<{ opts: ConfirmOptions; resolve: (v: boolean) => void } | null>(null)

  const confirm = useCallback<ConfirmFn>(
    (opts) => new Promise<boolean>((resolve) => setPending({ opts, resolve })),
    [],
  )

  function settle(result: boolean) {
    pending?.resolve(result)
    setPending(null)
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && (
        <ConfirmDialog
          title={pending.opts.title}
          message={pending.opts.message}
          details={pending.opts.details}
          confirmLabel={pending.opts.confirmLabel}
          danger={pending.opts.danger}
          onConfirm={() => settle(true)}
          onCancel={() => settle(false)}
        />
      )}
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext)
  if (!ctx) throw new Error('useConfirm must be used within a ConfirmProvider')
  return ctx
}
