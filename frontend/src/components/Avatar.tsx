// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { computeInitials, colorFromString } from './avatarUtils'

interface AvatarProps {
  /** Display name used to derive initials and the badge colour. */
  name: string
  /** Optional email — used for initials only when `name` is blank. */
  email?: string
  /**
   * Profile photo URL: when present the photo is shown instead of initials.
   */
  photoUrl?: string | null
  /** Diameter in px (badge is a circle). Defaults to 32. */
  size?: number
}

/**
 * Small reusable avatar. Renders a round photo when `photoUrl` is
 * given, otherwise a coloured badge with the user's initials. Decorative-only:
 * marked aria-hidden so screen readers rely on the surrounding label instead.
 */
export function Avatar({ name, email, photoUrl, size = 32 }: AvatarProps) {
  const box = { width: size, height: size }

  if (photoUrl) {
    return (
      <img
        className="avatar avatar-img"
        src={photoUrl}
        alt=""
        aria-hidden="true"
        style={box}
      />
    )
  }

  const initials = computeInitials(name, email)
  return (
    <span
      className="avatar avatar-initials"
      aria-hidden="true"
      style={{
        ...box,
        background: colorFromString(name || email || ''),
        fontSize: Math.round(size * 0.4),
      }}
    >
      {initials}
    </span>
  )
}
