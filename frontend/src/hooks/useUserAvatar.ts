// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/endpoints'

/**
 * Fetch ANY user's served avatar (authenticated) and expose a temporary object
 * URL for an `<img src>`. Per-user clone of `useAvatarImage`: rendering
 * through `<img>` — never injecting the SVG inline — means
 * a served SVG cannot execute scripts.
 *
 * Keyed by ['user-avatar', userId], so the SAME user appearing in several rows
 * (avatar stack + modal) is fetched ONCE and shared (react-query dedupes). Pass
 * `hasAvatar=false` (the member carries this flag) to skip the request entirely,
 * so users without a photo never fetch and never 404-spam. On 404/empty → null.
 */
export function useUserAvatar(
  userId: string,
  hasAvatar: boolean,
): { url: string | null } {
  const q = useQuery({
    queryKey: ['user-avatar', userId],
    queryFn: () => api.getUserAvatar(userId),
    enabled: hasAvatar,
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

  return { url }
}
