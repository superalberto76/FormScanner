// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { normalizeApiError } from '../utils/apiError'

export type ToastKind = 'success' | 'error' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  text: string
}

interface ToastApi {
  /** Generic push. */
  notify: (kind: ToastKind, text: string) => void
  success: (text: string) => void
  info: (text: string) => void
  /** Accepts a string or any thrown value (normalized via normalizeApiError). */
  error: (errOrText: unknown, fallback?: string) => void
  dismiss: (id: number) => void
}

const ToastContext = createContext<ToastApi | null>(null)

const AUTO_DISMISS_MS: Record<ToastKind, number> = {
  success: 4000,
  info: 4000,
  error: 7000,
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts(prev => prev.filter(t => t.id !== id))
  }, [])

  const notify = useCallback((kind: ToastKind, text: string) => {
    const id = nextId.current++
    setToasts(prev => [...prev, { id, kind, text }])
    window.setTimeout(() => dismiss(id), AUTO_DISMISS_MS[kind])
  }, [dismiss])

  const success = useCallback((text: string) => notify('success', text), [notify])
  const info = useCallback((text: string) => notify('info', text), [notify])
  const error = useCallback(
    (errOrText: unknown, fallback?: string) =>
      notify('error', typeof errOrText === 'string' ? errOrText : normalizeApiError(errOrText, fallback)),
    [notify],
  )

  const api: ToastApi = { notify, success, info, error, dismiss }

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map(t => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            <span className="toast-text">{t.text}</span>
            <button className="toast-close" onClick={() => dismiss(t.id)} aria-label="Dismiss">✕</button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within a ToastProvider')
  return ctx
}
