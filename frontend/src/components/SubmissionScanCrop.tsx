// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type CSSProperties } from 'react'
import type {
  PageAlignmentStatus,
  SubmissionReviewGeometry,
  SubmissionReviewTemplatePage,
} from '../api/domain'
import { useSubmissionPageImage } from '../hooks/useSubmissionPageImage'
import { normalizeApiError } from '../utils/apiError'
import { cropFrame, fullSheetFrame, type ScanFrame } from './submissionReview'
import type { PercentRect } from '../utils/viewerCoords'

interface Props {
  /** The SubmissionPage the reading is on. */
  pageId: string
  /** The sheet's alignment status: part of the image's cache key. */
  alignmentStatus: PageAlignmentStatus | null
  geometry: SubmissionReviewGeometry
  templatePage: SubmissionReviewTemplatePage
  objectTitle: string
}

/** Tallest the frame may grow, per view, in CSS px. */
const CROP_MAX_HEIGHT = 420
const FULL_MAX_HEIGHT = 700

function boxStyle(rect: PercentRect): CSSProperties {
  return {
    left: `${rect.leftPct}%`,
    top: `${rect.topPct}%`,
    width: `${rect.widthPct}%`,
    height: `${rect.heightPct}%`,
  }
}

/**
 * The scan around the selected reading: a crop with some context and
 * the object's box outlined, or — on demand — the whole sheet with the same box.
 *
 * The image is the one the extraction read on: for a sheet
 * aligned, its straightened copy, already in the template's frame and the
 * right way up; otherwise the original scan. It is stretched to the TEMPLATE's
 * aspect ratio (width and height set independently), which is how the
 * extractors map geometry onto it, per axis. The box therefore sits on exactly
 * the pixels the reading came from: on an aligned sheet exactly the answer; on
 * a sheet not aligned it is off exactly as the reading was, and the full-sheet
 * view lets the operator find the real answer.
 *
 * The toggle is local state; the focus keys this component by result, so a
 * new selection opens on the crop again.
 */
export function SubmissionScanCrop({ pageId, alignmentStatus, geometry, templatePage, objectTitle }: Props) {
  const [fullSheet, setFullSheet] = useState(false)
  const { url, isLoading, isError, notFound, error } = useSubmissionPageImage(pageId, alignmentStatus)

  if (notFound) return <p className="nd-muted nd-scan-msg">Scan image not available.</p>
  if (isError) {
    return <div className="error-msg">{normalizeApiError(error, 'Could not load the scan.')}</div>
  }

  const frame: ScanFrame = fullSheet ? fullSheetFrame(geometry, templatePage) : cropFrame(geometry, templatePage)
  const maxHeight = fullSheet ? FULL_MAX_HEIGHT : CROP_MAX_HEIGHT
  const frameStyle: CSSProperties = {
    aspectRatio: String(frame.aspectRatio),
    maxWidth: `${maxHeight * frame.aspectRatio}px`,
  }

  return (
    <div className="nd-scan">
      <div className="nd-scan-frame" style={frameStyle}>
        {isLoading || !url ? (
          <span className="nd-muted nd-scan-loading">Loading scan…</span>
        ) : (
          <>
            <img
              className="nd-scan-img"
              src={url}
              alt={fullSheet ? 'Full scanned sheet' : `Scan of ${objectTitle}`}
              style={boxStyle(frame.image)}
              draggable={false}
            />
            {frame.highlight && (
              <div className="nd-scan-highlight" style={boxStyle(frame.highlight)} aria-hidden="true" />
            )}
          </>
        )}
      </div>
      <button type="button" className="nd-link-btn nd-scan-toggle" onClick={() => setFullSheet(v => !v)}>
        {fullSheet ? 'Show crop' : 'Show full sheet'}
      </button>
    </div>
  )
}
