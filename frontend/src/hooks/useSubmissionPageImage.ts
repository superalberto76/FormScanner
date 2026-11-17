// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { nd, type PageAlignmentStatus } from '../api/domain'
import { getStatus } from '../utils/apiError'

/**
 * Fetch the image of one scanned sheet the extraction read on (authenticated)
 * and expose a temporary object URL for an `<img src>` — the same pattern as
 * `useObjectImage`. Which file that is — the straightened image of a sheet
 * ALIGNED, the original otherwise — is the server's rule.
 *
 * Cached under ['submission-page-image', pageId, alignmentStatus] with
 * `staleTime: Infinity`: for one alignment status the image does not change,
 * so selecting another reading on the same sheet reuses the blob. When the
 * review read reports a new status (the first run aligned the sheet) the key
 * changes and the image is fetched again — no polling, no extra invalidation.
 * `notFound` separates "the file is gone" (a "Review anyway" placeholder may
 * point at one) from a real failure, so the caller can show a quiet note
 * rather than an error.
 */
export function useSubmissionPageImage(
  pageId: string,
  alignmentStatus: PageAlignmentStatus | null,
) {
  const q = useQuery({
    queryKey: ['submission-page-image', pageId, alignmentStatus],
    queryFn: () => nd.getSubmissionPageImageBlob(pageId),
    enabled: !!pageId,
    staleTime: Infinity,
    retry: false,
  })

  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!q.data) { setUrl(null); return }
    const objectUrl = URL.createObjectURL(q.data)
    setUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [q.data])

  return {
    url,
    isLoading: q.isLoading,
    isError: q.isError,
    notFound: getStatus(q.error) === 404,
    error: q.error,
  }
}
