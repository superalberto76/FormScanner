// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Avatar } from './Avatar'
import { useUserAvatar } from '../hooks/useUserAvatar'

interface Props {
  userId: string
  /** Display name — drives initials + colour when there is no photo. */
  name: string
  /** Whether this user has uploaded a photo; gates the fetch (no storms). */
  hasAvatar: boolean
  /** Optional email for initials fallback when `name` is blank. */
  email?: string
  size?: number
}

/**
 * Roster avatar: shows the user's real photo when they
 * have one, otherwise coloured initials. Wraps `Avatar` + `useUserAvatar` so
 * every roster surface (the project-list stack, the members modal, search
 * results) fetches a given user's photo at most once (shared react-query key).
 */
export function MemberAvatar({ userId, name, hasAvatar, email, size }: Props) {
  const { url } = useUserAvatar(userId, hasAvatar)
  return <Avatar name={name} email={email} photoUrl={url ?? null} size={size} />
}
