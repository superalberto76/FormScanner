// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/endpoints'

/**
 * Fetch the current user's served avatar (authenticated) and expose a temporary
 * object URL for an `<img src>`. Rendering through `<img>` — never injecting the
 * SVG inline — means a served SVG cannot execute scripts. Clones the exact
 * object-URL lifecycle of `useObjectImage`.
 *
 * The blob is cached by react-query under ['avatar-me']; invalidate that key
 * after a (re-)upload or delete to refresh. Pass `enabled=false` (i.e. when the
 * user has no avatar) to skip the request entirely. On 404/empty → url = null.
 *
 * Both the ProfilePage and the UserMenu call this with the SAME key, so the
 * photo is fetched once and shared (react-query dedupes).
 */
export function useAvatarImage(enabled: boolean): { url: string | null; isLoading: boolean } {
  const q = useQuery({
    queryKey: ['avatar-me'],
    queryFn: () => api.getMyAvatar(),
    enabled,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!q.data) { setUrl(null); return }
    const objectUrl = URL.createObjectURL(q.data)
    setUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [q.data])

  return { url, isLoading: q.isLoading }
}
