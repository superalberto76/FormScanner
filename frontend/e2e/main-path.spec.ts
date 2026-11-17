// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Main path in the browser: batch, upload of F01, F04 and F11 from the kit,
// extraction, queue, review of F04 with the score in the header, F11's
// candidate corrected by hand, closing of all three, export of the summary
// CSV.
//
// Every expected value comes from the sources, never from a run:
//  * cases, queue and scores: scenarios F01, F04, F11 (backend/tests/
//    synthetic/scenarios.py: status, candidate, readings in review and their
//    reasons, score: F01 28, F04 18, F11 28 of 28);
//  * F04's actions and the scores they give, and F11's candidate: the review
//    actions flow (backend/tests/synthetic/test_review_actions_flow.py:
//    Q01 CONFIRM 18, Q02 key 19, M-RIGHE key 23, M-COLONNE key 27; F11's
//    MATRICOLA FLAG, then "100011" by hand);
//  * export: batch_export_service (columns, closed status, "Unreviewed
//    objects" = readings left unresolved at closing, upload order).
import { readFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'
import { COLL_MULTI, OWNER, kitPdf } from './env.ts'
import {
  act,
  confirmDialog,
  createBatch,
  headerValue,
  login,
  openProject,
  queueCell,
  queueRow,
  reviewObject,
  runExtraction,
  uploadPdfs,
} from './helpers.ts'

const BATCH = 'E2E'
const CASES = ['F01', 'F04', 'F11'] as const

// F04's key (scenarios.py CLEAN_A, specs.py): Q02 is option 3 ("C"); M-RIGHE
// the diagonal, M-COLONNE the anti-diagonal. F04 adds Q02 A, B, D, the cell
// R2 C1 to M-RIGHE and R3 C3 to M-COLONNE.
const F04_Q02_EXTRA = ['A · Opzione 1', 'B · Opzione 2', 'D · Opzione 4']
const F04_M_RIGHE_EXTRA = 'R2, C1'
const F04_M_COLONNE_EXTRA = 'R3, C3'

test('main path: upload, extraction, review, closing and export of the summary', async ({ page }) => {
  test.setTimeout(5 * 60_000)

  await test.step('create the batch on COLL-MULTI', async () => {
    await login(page, OWNER)
    await openProject(page, COLL_MULTI)
    await createBatch(page, BATCH)
  })
  const batchUrl = page.url()

  await test.step('upload F01, F04, F11: three MATCHED submissions', async () => {
    await uploadPdfs(page, CASES.map(kitPdf))
    await expect(page.getByText('3 submissions', { exact: true })).toBeVisible()
    const rows = page.getByRole('row').filter({ hasText: 'MATCHED' })
    await expect(rows).toHaveCount(3)
    await expect(page.getByRole('button', { name: 'To extract (3)' })).toBeVisible()
  })

  await test.step('run extraction: three SCANNED, readings in review as declared', async () => {
    await runExtraction(page, 3)
    await expect(page.getByRole('button', { name: 'To extract (0)' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'To review (3)' })).toBeVisible()

    // F01: in review only NOME and APERTA; F04: Q01, Q02, M-RIGHE, M-COLONNE
    // and the two free texts; F11: MATRICOLA and the two free texts.
    for (const [text, candidate, inReview] of [
      ['100001', '100001', '2'],
      ['100004', '100004', '6'],
      ['No candidate', '—⚠ No candidate', '3'],
    ] as const) {
      const row = queueRow(page, text)
      await expect(row).toHaveCount(1)
      await expect(queueCell(row, 'candidate')).toHaveText(candidate)
      await expect(queueCell(row, 'status')).toHaveText('SCANNED')
      await expect(queueCell(row, 'variant')).toHaveText('A')
      await expect(queueCell(row, 'pages')).toHaveText('3')
      await expect(queueCell(row, 'inReview')).toHaveText(inReview)
    }
  })

  await test.step('F04: readings, reasons and score in the header at every action', async () => {
    await queueRow(page, '100004').click()
    await expect(headerValue(page, 'Candidate')).toContainText('100004')
    await expect(headerValue(page, 'Variant')).toHaveText('A')
    await expect(page.getByText('6 readings need review')).toBeVisible()
    for (const label of ['Q01', 'Q02', 'M-RIGHE', 'M-COLONNE']) {
      await expect(reviewObject(page, label)).toContainText('Invalid answer')
    }
    for (const label of ['NOME', 'APERTA']) {
      await expect(reviewObject(page, label)).toContainText('Always reviewed')
    }
    await expectReview(page, '6 of 6 remaining', '18 / 28 (64.3%)')

    // TOO_MANY_ANSWERS confirmed: min(0, 0 + 1) = 0, as when pending.
    await act(page, 'Q01', 'Confirm')
    await expectReview(page, '5 of 6 remaining', '18 / 28 (64.3%)')

    await act(page, 'Q02', 'Replace…')
    for (const option of F04_Q02_EXTRA) await page.getByRole('checkbox', { name: option }).uncheck()
    await expect(page.getByRole('checkbox', { name: 'C · Opzione 3' })).toBeChecked()
    await page.getByRole('button', { name: 'Save replacement' }).click()
    await expectReview(page, '4 of 6 remaining', '19 / 28 (67.9%)')

    await act(page, 'M-RIGHE', 'Replace…')
    await page.getByRole('checkbox', { name: F04_M_RIGHE_EXTRA }).uncheck()
    await page.getByRole('button', { name: 'Save replacement' }).click()
    await expectReview(page, '3 of 6 remaining', '23 / 28 (82.1%)')

    await act(page, 'M-COLONNE', 'Replace…')
    await page.getByRole('checkbox', { name: F04_M_COLONNE_EXTRA }).uncheck()
    await page.getByRole('button', { name: 'Save replacement' }).click()
    await expectReview(page, '2 of 6 remaining', '27 / 28 (96.4%)')

    // The free texts score nothing.
    await act(page, 'NOME', 'Flag unresolvable')
    await expectReview(page, '1 of 6 remaining', '27 / 28 (96.4%)')
    await act(page, 'APERTA', 'Replace…')
    await page.getByRole('textbox', { name: 'Text' }).fill('Transcribed answer')
    await page.getByRole('button', { name: 'Save replacement' }).click()
    await expectReview(page, '0 of 6 remaining', '27 / 28 (96.4%)')

    // Nothing unresolved, a candidate, no missing page: closes without a dialog.
    await page.getByRole('button', { name: 'Close submission' }).click()
    await expectClosed(page, '0 of 6 at closing', '27 / 28 (96.4%)')
  })

  await test.step('F11: candidate corrected by hand, closed with two readings left', async () => {
    await page.goto(batchUrl)
    await queueRow(page, 'No candidate').click()
    await expect(headerValue(page, 'Candidate')).toContainText('No candidate')
    await expect(reviewObject(page, 'MATRICOLA')).toContainText('Missing digits')
    await expectReview(page, '3 of 3 remaining', '28 / 28 (100%)')

    // An incomplete respondent ID is not confirmable; FLAG never sets the candidate.
    await reviewObject(page, 'MATRICOLA').click()
    await expect(page.getByRole('button', { name: 'Confirm', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'Flag unresolvable', exact: true }).click()
    await expectReview(page, '2 of 3 remaining', '28 / 28 (100%)')
    await expect(headerValue(page, 'Candidate')).toContainText('No candidate')

    await headerValue(page, 'Candidate').getByRole('button', { name: 'Edit' }).click()
    await page.getByRole('textbox', { name: 'Candidate identifier' }).fill('100011')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(headerValue(page, 'Candidate')).toHaveText(/^100011/)
    await expect(headerValue(page, 'Candidate')).not.toContainText('No candidate')

    await closeWithUnresolved(page, '2 objects are still unresolved.')
    await expectClosed(page, '2 of 3 at closing', '28 / 28 (100%)')
  })

  await test.step('F01: closed with the two free texts left', async () => {
    await page.goto(batchUrl)
    await queueRow(page, '100001').click()
    await expect(reviewObject(page, 'NOME')).toContainText('Always reviewed')
    await expect(reviewObject(page, 'APERTA')).toContainText('Always reviewed')
    await expectReview(page, '2 of 2 remaining', '28 / 28 (100%)')
    await closeWithUnresolved(page, '2 objects are still unresolved.')
    await expectClosed(page, '2 of 2 at closing', '28 / 28 (100%)')
  })

  await test.step('queue: three REVIEWED, the batch is ready for export', async () => {
    await page.goto(batchUrl)
    for (const [candidate, inReview] of [
      ['100001', '2 unresolved'],
      ['100004', '—'],
      ['100011', '2 unresolved'],
    ] as const) {
      const row = queueRow(page, candidate)
      await expect(queueCell(row, 'status')).toHaveText('REVIEWED')
      await expect(queueCell(row, 'inReview')).toHaveText(inReview)
    }
    await expect(page.getByText('No candidate')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'To review (0)' })).toBeVisible()
    await expect(page.getByText(/^Not ready:|^Nothing to export/)).toHaveCount(0)
  })

  await test.step('export the summary as CSV: rows, status and points', async () => {
    await page.getByRole('button', { name: 'Export summary…' }).click()
    await page.getByRole('radio', { name: /CSV — comma separated/ }).check()
    const downloading = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Download' }).click()
    const download = await downloading
    expect(download.suggestedFilename()).toMatch(/\.csv$/)
    const csv = await readFile(await download.path(), 'utf-8')

    const [header, ...rows] = parseCsv(csv)
    expect(header).toEqual([
      'Submission', 'Candidate', 'Variant', 'Points', 'Max points', 'Percentage', 'Status',
      'Missing pages', 'Unreviewed objects', 'Batch', 'Scanned at (UTC)',
    ])
    // Upload order (F01, F04, F11); the submission id and the time are not
    // declared anywhere, only their shape.
    expect(rows.map(r => r.slice(1, 10))).toEqual([
      ['100001', 'A', '28', '28', '100.0', 'REVIEWED', '', '2', BATCH],
      ['100004', 'A', '27', '28', '96.4', 'REVIEWED', '', '0', BATCH],
      ['100011', 'A', '28', '28', '100.0', 'REVIEWED', '', '2', BATCH],
    ])
    for (const row of rows) {
      expect(row[0]).toMatch(/^[0-9a-f]{4}…[0-9a-f]{4}$/)
      expect(row[10]).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/)
    }
  })
})

/** "To review" and "Score" of the review header, while the submission is open. */
async function expectReview(page: Page, toReview: string, score: string): Promise<void> {
  await expect(headerValue(page, 'To review')).toHaveText(toReview)
  await expect(headerValue(page, 'Score')).toHaveText(score)
}

/** After closing: REVIEWED, read-only, the score saved as the last shown. */
async function expectClosed(page: Page, unresolved: string, score: string): Promise<void> {
  await expect(page.getByText('Submission closed.')).toBeVisible()
  await expect(page.getByText('This submission is closed. It is shown read-only.')).toBeVisible()
  await expect(headerValue(page, 'Unresolved')).toHaveText(unresolved)
  await expect(headerValue(page, 'Score')).toHaveText(score)
  await expect(page.getByRole('button', { name: 'Reopen submission' })).toBeVisible()
}

/** "Close submission", confirmed in the dialog that warns about *warning*. */
async function closeWithUnresolved(page: Page, warning: string): Promise<void> {
  await page.getByRole('button', { name: 'Close submission' }).click()
  const dialog = confirmDialog(page, 'Close submission')
  await expect(dialog).toContainText(warning)
  await dialog.getByRole('button', { name: 'Close submission' }).click()
}

/** The summary CSV (comma dialect, UTF-8 with BOM, CRLF): no quoted field holds
    a comma or a line break in this batch, so a plain split is enough. */
function parseCsv(text: string): string[][] {
  return text
    .replace(/^﻿/, '')
    .split('\r\n')
    .filter(line => line !== '')
    .map(line => line.split(',').map(cell => cell.replace(/^"(.*)"$/, '$1')))
}
