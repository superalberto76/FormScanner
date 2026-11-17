// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { nd } from '../api/domain'

/**
 * Fetch an IMAGE object's served binary (authenticated) and expose a temporary
 * object URL for an `<img src>`. Rendering through `<img>` — never injecting the
 * SVG inline — means a served SVG cannot execute scripts.
 *
 * The blob is cached by react-query under ['object-image', objectId]; invalidate
 * that key after a (re-)upload to refresh. Pass `enabled=false` (e.g. before any
 * file exists) to skip the request entirely.
 */
export function useObjectImage(objectId: string, enabled = true) {
  const q = useQuery({
    queryKey: ['object-image', objectId],
    queryFn: () => nd.getObjectImageBlob(objectId),
    enabled: enabled && !!objectId,
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

  return { url, isLoading: q.isLoading, isError: q.isError }
}
