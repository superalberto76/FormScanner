// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useToast } from './ToastProvider'
import {
  nd,
  type PageObjectType,
  type TextPageObjectCreate,
  type CodePageObjectCreate,
  type FreeTextAreaPageObjectCreate,
  type QuestionPageObjectCreate,
  type MatrixPageObjectCreate,
} from '../api/domain'
import { CodeForm, FreeTextAreaForm, MatrixForm, QuestionForm, TextForm } from './ObjectForms'

interface Props {
  pageId: string
  readOnly: boolean
}

const TYPE_OPTIONS: PageObjectType[] = ['TEXT', 'CODE', 'FREE_TEXT_AREA', 'QUESTION', 'MATRIX']

/** Create panel: pick an object type, fill the typed form, POST it. */
export function ObjectCreatePanel({ pageId, readOnly }: Props) {
  const [type, setType] = useState<PageObjectType>('TEXT')
  const toast = useToast()
  const qc = useQueryClient()

  const onCreated = () => {
    toast.success('Object created')
    void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
    void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
  }
  const onError = (err: unknown) => toast.error(err)

  const textM = useMutation({ mutationFn: (p: TextPageObjectCreate) => nd.createTextObject(pageId, p), onSuccess: onCreated, onError })
  const codeM = useMutation({ mutationFn: (p: CodePageObjectCreate) => nd.createCodeObject(pageId, p), onSuccess: onCreated, onError })
  const freeM = useMutation({ mutationFn: (p: FreeTextAreaPageObjectCreate) => nd.createFreeTextAreaObject(pageId, p), onSuccess: onCreated, onError })
  const questionM = useMutation({ mutationFn: (p: QuestionPageObjectCreate) => nd.createQuestionObject(pageId, p), onSuccess: onCreated, onError })
  const matrixM = useMutation({ mutationFn: (p: MatrixPageObjectCreate) => nd.createMatrixObject(pageId, p), onSuccess: onCreated, onError })

  if (readOnly) {
    return (
      <div className="nd-panel-section">
        <p className="info-msg">Variant is read-only — object creation is disabled.</p>
      </div>
    )
  }

  return (
    <div className="nd-panel-section">
      <h3 className="mb-16">Add object</h3>
      <div className="form-group mb-16">
        <label>Object type</label>
        <select value={type} onChange={e => setType(e.target.value as PageObjectType)}>
          {TYPE_OPTIONS.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      {type === 'TEXT' && <TextForm onSubmit={textM.mutate} submitting={textM.isPending} />}
      {type === 'CODE' && <CodeForm onSubmit={codeM.mutate} submitting={codeM.isPending} />}
      {type === 'FREE_TEXT_AREA' && <FreeTextAreaForm onSubmit={freeM.mutate} submitting={freeM.isPending} />}
      {type === 'QUESTION' && <QuestionForm onSubmit={questionM.mutate} submitting={questionM.isPending} />}
      {type === 'MATRIX' && <MatrixForm onSubmit={matrixM.mutate} submitting={matrixM.isPending} />}
    </div>
  )
}
