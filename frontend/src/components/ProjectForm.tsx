// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import type { FormProjectCreate, FormProjectType } from '../api/domain'

const PROJECT_TYPES: FormProjectType[] = ['EXAM', 'SURVEY', 'QUESTIONNAIRE', 'GENERIC_FORM']

interface Props {
  onSubmit: (data: FormProjectCreate) => void | Promise<void>
  onCancel: () => void
  submitting?: boolean
}

/** Create form for a FormProject. Pure: the parent owns the mutation. */
export function ProjectForm({ onSubmit, onCancel, submitting = false }: Props) {
  const [type, setType] = useState<FormProjectType>('EXAM')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [eventDate, setEventDate] = useState('')

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    void onSubmit({
      type,
      name: name.trim(),
      description: description.trim() || null,
      // <input type=date> gives YYYY-MM-DD; backend field is datetime → send midnight.
      event_date: eventDate ? `${eventDate}T00:00:00` : null,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="stack stack-12">
      <div className="form-group">
        <label htmlFor="p-type">Type</label>
        <select id="p-type" value={type} onChange={e => setType(e.target.value as FormProjectType)}>
          {PROJECT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      <div className="form-group">
        <label htmlFor="p-name">Name</label>
        <input id="p-name" required value={name} onChange={e => setName(e.target.value)}
               placeholder="e.g. Math Final 2026" />
      </div>
      <div className="form-group">
        <label htmlFor="p-desc">Description</label>
        <textarea id="p-desc" rows={2} value={description} onChange={e => setDescription(e.target.value)} />
      </div>
      <div className="form-group">
        <label htmlFor="p-date">Event date</label>
        <input id="p-date" type="date" value={eventDate} onChange={e => setEventDate(e.target.value)} />
      </div>
      <div className="row row-8">
        <button type="submit" className="btn btn-primary" disabled={submitting || !name.trim()}>
          {submitting ? 'Creating…' : 'Create project'}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      </div>
    </form>
  )
}
