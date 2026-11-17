// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Maps project/variant statuses onto the existing badge palette (badge-draft /
// badge-published / badge-archived) so we don't add a new design vocabulary.

type AnyStatus =
  | 'DRAFT' | 'ACTIVE' | 'CLOSED' | 'ARCHIVED' | 'PUBLISHED'

const CLASS_BY_STATUS: Record<AnyStatus, string> = {
  DRAFT: 'badge-draft',
  ACTIVE: 'badge-published',
  PUBLISHED: 'badge-published',
  CLOSED: 'badge-archived',
  ARCHIVED: 'badge-archived',
}

export function StatusBadge({ status }: { status: string }) {
  const cls = CLASS_BY_STATUS[status as AnyStatus] ?? 'badge-draft'
  return <span className={`badge ${cls}`}>{status}</span>
}
