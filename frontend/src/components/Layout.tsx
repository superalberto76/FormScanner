// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { UserMenu } from './UserMenu'

export function Layout({ children }: { children: ReactNode }) {
  return (
    <>
      <header className="header">
        <div className="header-inner">
          <Link to="/projects" className="header-brand">FormScanner</Link>
          <div className="header-spacer" />
          <UserMenu />
        </div>
      </header>
      <main className="page">{children}</main>
    </>
  )
}
