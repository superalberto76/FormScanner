// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { markSessionExpired } from '../auth/authStore'

const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '/api/v1'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    // Set only for a STRUCTURED detail ({code, message, ...}, e.g. the 409s):
    // the machine-readable code a caller branches on, and the whole object for
    // its extras (e.g. who took the lock). `detail` is then the message.
    public readonly code?: string,
    public readonly data?: Record<string, unknown>,
  ) {
    super(detail)
    this.name = 'ApiError'
  }
}

interface RequestOpts {
  method?: string
  json?: unknown
  formData?: FormData
  token?: string | null
}

/**
 * The ApiError of a failed response: the message from `detail` (a string, a
 * FastAPI validation list, or a STRUCTURED `{code, message, ...}` whose `code`
 * and extras become `code` / `data`). A 401 on an authenticated route marks
 * the session expired. Shared by apiFetch and the raw-fetch downloads (batch
 * export), so `getErrorCode` works on both.
 */
export async function apiErrorFromResponse(res: Response, path: string): Promise<ApiError> {
  let detail = `HTTP ${res.status}`
  let code: string | undefined
  let data: Record<string, unknown> | undefined
  try {
    const j = await res.json()
    if (typeof j.detail === 'string') {
      detail = j.detail
    } else if (Array.isArray(j.detail)) {
      detail = (j.detail as Array<{ msg: string }>).map(d => d.msg).join('; ')
    } else if (
      j.detail && typeof j.detail === 'object' && typeof j.detail.message === 'string'
    ) {
      // A structured detail: `code` is the contract, `message` is for humans.
      detail = j.detail.message
      if (typeof j.detail.code === 'string') code = j.detail.code
      data = j.detail as Record<string, unknown>
    }
  } catch { /* ignore parse errors */ }
  // A 401 on an authenticated route means the session is dead — clear it so
  // guarded routes redirect to /login. The login endpoint itself returns 401
  // for bad credentials, which must NOT trigger the session-expired flow.
  if (res.status === 401 && !path.startsWith('/auth/login')) {
    markSessionExpired()
  }
  return new ApiError(res.status, detail, code, data)
}

export async function apiFetch<T>(path: string, opts: RequestOpts = {}): Promise<T> {
  const headers: Record<string, string> = {}
  if (opts.token) headers['Authorization'] = `Bearer ${opts.token}`

  let body: BodyInit | undefined
  if (opts.formData) {
    // Don't set Content-Type — browser adds it with correct boundary
    body = opts.formData
  } else if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(opts.json)
  }

  const res = await fetch(`${BASE}${path}`, {
    method: opts.method ?? 'GET',
    headers,
    body,
  })

  if (!res.ok) throw await apiErrorFromResponse(res, path)

  if (res.status === 204) return undefined as T

  const ct = res.headers.get('content-type') ?? ''
  if (ct.startsWith('image/') || ct.startsWith('application/octet-stream')) {
    return res.blob() as unknown as T
  }
  return res.json() as Promise<T>
}
