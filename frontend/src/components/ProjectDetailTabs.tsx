// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Link } from 'react-router-dom'

export type ProjectDetailTab = 'variants' | 'scanning'

/**
 * Verbatim microcopy for the gated Scanning tab. Used in two places, which is
 * why it lives here as a single constant: (a) the `title` on the disabled tab,
 * (b) the in-content message when landing directly on /scanning on a DRAFT
 * project — the only status the tab is locked for.
 */
export const SCANNING_REQUIRES_ACTIVE = "Requires an active project — use ‘Activate’ above"

interface Props {
  projectId: string
  activeTab: ProjectDetailTab
  /**
   * True for ACTIVE, CLOSED and ARCHIVED projects (a concluded project's batches
   * stay reachable for review and export); false for DRAFT and while the
   * project is loading.
   */
  scanningEnabled: boolean
}

/**
 * Tab bar of the project detail page (Variants / Scanning). Deliberately local
 * and presentational — NOT a generic Tabs abstraction; there is exactly one
 * caller.
 *
 * Tabs are <Link>s so back/forward, ctrl-click and open-in-new-tab work for
 * free. The Scanning tab is always rendered, but when it is disabled it is a
 * plain <span> — not a link — so it cannot be activated by click or keyboard.
 */
export function ProjectDetailTabs({ projectId, activeTab, scanningEnabled }: Props) {
  const scanningClasses = [
    'nd-tab',
    activeTab === 'scanning' ? 'is-active' : '',
    scanningEnabled ? '' : 'is-disabled',
  ].filter(Boolean).join(' ')

  return (
    <nav className="nd-tabs" aria-label="Project sections">
      <Link
        to={`/projects/${projectId}/variants`}
        className={`nd-tab${activeTab === 'variants' ? ' is-active' : ''}`}
        aria-current={activeTab === 'variants' ? 'page' : undefined}
      >
        Variants
      </Link>
      {scanningEnabled ? (
        <Link
          to={`/projects/${projectId}/scanning`}
          className={scanningClasses}
          aria-current={activeTab === 'scanning' ? 'page' : undefined}
        >
          Scanning
        </Link>
      ) : (
        <span className={scanningClasses} aria-disabled="true" title={SCANNING_REQUIRES_ACTIVE}>
          <span aria-hidden="true">🔒</span> Scanning
        </span>
      )}
    </nav>
  )
}
