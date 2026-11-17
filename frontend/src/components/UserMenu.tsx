// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { clearAuth, useAuth } from '../auth/authStore'
import { useAvatarImage } from '../hooks/useAvatarImage'
import { Avatar } from './Avatar'
import { canAddUser, canManageUsers, getDisplayName } from './userMenu'

const MENU_ID = 'user-menu-dropdown'

/**
 * Top-right user menu: a single avatar button that opens an
 * accessible dropdown. Renders nothing when logged out.
 *
 * Accessibility (no menu library): trigger has aria-haspopup/expanded/controls;
 * dropdown is role="menu"; focus moves to the first enabled item on open; Escape
 * closes and restores focus to the trigger; ArrowUp/Down cycle enabled items;
 * a click or focus outside closes it.
 */
export function UserMenu() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  // Profile photo: shared ['avatar-me'] query with the
  // ProfilePage (react-query dedupes), gated on has_avatar. Falls back to
  // initials when absent. Called before the early `return null` so the hook
  // order stays stable; disabled (no fetch) when logged out.
  const { url: avatarUrl } = useAvatarImage(!!user?.has_avatar)

  const containerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close on click / focus outside the menu.
  useEffect(() => {
    if (!open) return
    function onOutside(e: MouseEvent | FocusEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onOutside)
    document.addEventListener('focusin', onOutside)
    return () => {
      document.removeEventListener('mousedown', onOutside)
      document.removeEventListener('focusin', onOutside)
    }
  }, [open])

  // Move focus to the first enabled item when the menu opens.
  useEffect(() => {
    if (!open) return
    enabledItems()[0]?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  if (!user) return null

  const displayName = getDisplayName(user)

  /** The currently rendered, non-disabled menu items, in DOM order. */
  function enabledItems(): HTMLElement[] {
    return Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>(
        '[role="menuitem"]:not([aria-disabled="true"])',
      ) ?? [],
    )
  }

  function close(focusTrigger = true) {
    setOpen(false)
    if (focusTrigger) triggerRef.current?.focus()
  }

  function handleSignOut() {
    // Reuse the existing secure-logout path: clearAuth() removes the persisted
    // token (fs_token) AND the in-memory state, so reopening the browser requires
    // a fresh login. Also drop cached query data so nothing leaks across sessions.
    clearAuth()
    queryClient.clear()
    navigate('/login', { replace: true })
  }

  function handleAddUser() {
    // Admin gating only mirrors the BE: require_admin stays authoritative.
    setOpen(false)
    navigate('/admin/users/new')
  }

  function handleManageUsers() {
    // Admin-only "Manage users" list. BE require_admin + AdminRoute
    // stay authoritative; the menu gating below only mirrors them.
    setOpen(false)
    navigate('/admin/users')
  }

  function handleProfile() {
    setOpen(false)
    navigate('/profile')
  }

  function handleAppearance() {
    setOpen(false)
    navigate('/appearance')
  }

  function onMenuKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.preventDefault()
      close()
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const items = enabledItems()
    if (items.length === 0) return
    const current = items.indexOf(document.activeElement as HTMLElement)
    const next =
      e.key === 'ArrowDown'
        ? (current + 1) % items.length
        : (current - 1 + items.length) % items.length
    items[next].focus()
  }

  function onTriggerKeyDown(e: ReactKeyboardEvent<HTMLButtonElement>) {
    if (!open && (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault()
      setOpen(true)
    } else if (open && e.key === 'Escape') {
      e.preventDefault()
      close()
    }
  }

  return (
    <div className="user-menu" ref={containerRef}>
      <button
        ref={triggerRef}
        type="button"
        className="user-menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={MENU_ID}
        aria-label={`User menu — ${displayName}`}
        onClick={() => setOpen(o => !o)}
        onKeyDown={onTriggerKeyDown}
      >
        <Avatar name={displayName} email={user.email} photoUrl={avatarUrl ?? null} />
      </button>

      {open && (
        <div
          id={MENU_ID}
          ref={menuRef}
          className="user-menu-dropdown"
          role="menu"
          aria-label="User menu"
          onKeyDown={onMenuKeyDown}
        >
          {/* 1) Read-only identity header — not focusable, not clickable. */}
          <div className="user-menu-headerrow" role="presentation">
            <Avatar name={displayName} email={user.email} photoUrl={avatarUrl ?? null} />
            <div className="user-menu-identity">
              <span className="user-menu-name" title={displayName}>{displayName}</span>
              <span className="user-menu-status">Signed in</span>
            </div>
          </div>

          <div className="user-menu-divider" role="separator" />

          {/* 2) Profile — self-service identity + password. */}
          <button
            type="button"
            role="menuitem"
            className="user-menu-item"
            onClick={handleProfile}
          >
            <span>Profile</span>
          </button>

          {/* 3) Appearance — UI theme personalization. */}
          <button
            type="button"
            role="menuitem"
            className="user-menu-item"
            onClick={handleAppearance}
          >
            <span>Appearance</span>
          </button>

          {/* 4) Manage users — admin only; the paginated user list. */}
          {canManageUsers(user) && (
            <button
              type="button"
              role="menuitem"
              className="user-menu-item"
              onClick={handleManageUsers}
            >
              <span>Manage users</span>
              <span className="user-menu-tag">admin only</span>
            </button>
          )}

          {/* 5) Add user — admin only; opens the create-user screen. */}
          {canAddUser(user) && (
            <button
              type="button"
              role="menuitem"
              className="user-menu-item"
              onClick={handleAddUser}
            >
              <span>Add user</span>
              <span className="user-menu-tag">admin only</span>
            </button>
          )}

          <div className="user-menu-divider" role="separator" />

          {/* 6) Sign out — danger style; reuses clearAuth() then routes to /login. */}
          <button
            type="button"
            role="menuitem"
            className="user-menu-item user-menu-danger"
            onClick={handleSignOut}
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  )
}
