// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { useToast } from './ToastProvider'
import {
  nd,
  type CodePageObjectCreate,
  type FreeTextAreaPageObjectCreate,
  type ImagePageObjectCreate,
  type MatrixPageObjectCreate,
  type PageObjectDetail,
  type PageObjectType,
  type QuestionPageObjectCreate,
  type TextPageObjectCreate,
} from '../api/domain'
import type { Rect } from '../utils/viewerCoords'
import { CodeForm, FreeTextAreaForm, ImageForm, MatrixForm, QuestionForm, TextForm } from './ObjectForms'
import type { GeometryState } from './ObjectForms'

interface Props {
  pageId: string
  type: PageObjectType
  /** Geometry from the drawn box; prefills the form (still editable). */
  rect: Rect
  onClose: () => void
  onCreated: (objectId: string) => void
}

/** Modal that hosts the typed create form, prefilled with the drawn geometry. */
export function ObjectCreateModal({ pageId, type, rect, onClose, onCreated }: Props) {
  const toast = useToast()
  const qc = useQueryClient()
  const initialGeometry: Partial<GeometryState> = {
    x: rect.x, y: rect.y, width: rect.width, height: rect.height,
  }

  function handleSuccess(obj: PageObjectDetail) {
    toast.success('Object created')
    void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
    void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
    onCreated(obj.id)
    onClose()
  }
  const onError = (err: unknown) => toast.error(err)

  const textM = useMutation({ mutationFn: (p: TextPageObjectCreate) => nd.createTextObject(pageId, p), onSuccess: handleSuccess, onError })
  const codeM = useMutation({ mutationFn: (p: CodePageObjectCreate) => nd.createCodeObject(pageId, p), onSuccess: handleSuccess, onError })
  const freeM = useMutation({ mutationFn: (p: FreeTextAreaPageObjectCreate) => nd.createFreeTextAreaObject(pageId, p), onSuccess: handleSuccess, onError })
  const questionM = useMutation({ mutationFn: (p: QuestionPageObjectCreate) => nd.createQuestionObject(pageId, p), onSuccess: handleSuccess, onError })
  const matrixM = useMutation({ mutationFn: (p: MatrixPageObjectCreate) => nd.createMatrixObject(pageId, p), onSuccess: handleSuccess, onError })

  // IMAGE is a two-step create: create the object (geometry + fit), then upload
  // the file. If the upload fails the object still exists, so we land the user
  // in the editor to re-upload rather than silently leaving an empty object.
  const imageM = useMutation({
    mutationFn: async ({ data, file }: { data: ImagePageObjectCreate; file: File }) => {
      const created = await nd.createImageObject(pageId, data)
      try {
        const withImage = await nd.uploadObjectImage(created.id, file)
        return { obj: withImage, uploadError: null as unknown }
      } catch (uploadError) {
        return { obj: created, uploadError }
      }
    },
    onSuccess: ({ obj, uploadError }) => {
      void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
      void qc.invalidateQueries({ queryKey: ['object-image', obj.id] })
      if (uploadError) {
        toast.error(uploadError, 'Object created, but the image upload failed — re-upload it from the editor.')
      } else {
        toast.success('Object created')
      }
      onCreated(obj.id)
      onClose()
    },
    onError,  // the create itself failed
  })

  // Portal to <body> so it covers the canvas overlays/handles.
  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-box nd-modal-scroll" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Add {type} object</h2>
          <button className="btn btn-secondary btn-sm" onClick={onClose}>✕</button>
        </div>
        <p className="nd-muted mb-16">Position and size are taken from the box you drew — just fill in the details below.</p>

        {type === 'TEXT' && <TextForm initialGeometry={initialGeometry} onSubmit={textM.mutate} submitting={textM.isPending} />}
        {type === 'CODE' && <CodeForm initialGeometry={initialGeometry} onSubmit={codeM.mutate} submitting={codeM.isPending} />}
        {type === 'FREE_TEXT_AREA' && <FreeTextAreaForm initialGeometry={initialGeometry} onSubmit={freeM.mutate} submitting={freeM.isPending} />}
        {type === 'QUESTION' && <QuestionForm initialGeometry={initialGeometry} onSubmit={questionM.mutate} submitting={questionM.isPending} />}
        {type === 'MATRIX' && <MatrixForm initialGeometry={initialGeometry} onSubmit={matrixM.mutate} submitting={matrixM.isPending} onCancel={onClose} error={matrixM.error} />}
        {type === 'IMAGE' && <ImageForm initialGeometry={initialGeometry} onSubmit={(data, file) => imageM.mutate({ data, file })} submitting={imageM.isPending} error={imageM.error} />}
      </div>
    </div>,
    document.body,
  )
}
