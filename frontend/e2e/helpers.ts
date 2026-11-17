// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared steps of the e2e tests. Selectors by role and text on the English
// UI.
import { expect, type Locator, type Page } from '@playwright/test'
import type { Operator } from './env.ts'

/** Log in from the login page and land on the project list. */
export async function login(page: Page, operator: Operator): Promise<void> {
  await page.goto('/login')
  await page.getByLabel('Email').fill(operator.email)
  await page.getByLabel('Password').fill(operator.password)
  await page.getByRole('button', { name: 'Login' }).click()
  await expect(page).toHaveURL(/\/projects$/)
  await expect(page.getByRole('heading', { name: 'Projects', level: 1 })).toBeVisible()
}

/** From the project list, open the project named *name*. */
export async function openProject(page: Page, name: string): Promise<void> {
  await page.getByRole('row', { name: new RegExp(escapeRegExp(name)) }).click()
  await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible()
}

// ── Batch ───────────────────────────────────────────────────────────────────

/** From an open project, create the batch *name* and land on its page. */
export async function createBatch(page: Page, name: string): Promise<void> {
  await page.getByRole('link', { name: 'Scanning' }).click()
  await page.getByRole('button', { name: '+ New batch' }).click()
  await page.getByLabel('Name').fill(name)
  await page.getByRole('button', { name: 'Create batch' }).click()
  await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible()
}

/** Upload *files* (PDFs) into the open batch; one submission each. */
export async function uploadPdfs(page: Page, files: readonly string[]): Promise<void> {
  await page.locator('input[type="file"]').setInputFiles([...files])
  const label = files.length === 1 ? 'Upload 1 file' : `Upload ${files.length} files`
  await page.getByRole('button', { name: label }).click()
  await expect(page.getByText('✓ Submission created')).toHaveCount(files.length)
}

/** "Run extraction" on the open batch, until the run reports *count* ready
 *  for review (the number of results is declared by no source: shape only). */
export async function runExtraction(page: Page, count: number): Promise<void> {
  const submissions = count === 1 ? '1 submission' : `${count} submissions`
  await page.getByRole('button', { name: 'Run extraction' }).click()
  await expect(
    page.getByText(
      new RegExp(`^Last run: ${submissions} processed · \\d+ results created · ${count} ready for review$`),
    ),
  ).toBeVisible({ timeout: 90_000 })
}

// ── Queue of a batch ────────────────────────────────────────────────────────

/** The queue's columns when no row can be ticked (no MATCH_FAILED in view). */
export const QUEUE_COLUMN = {
  candidate: 1,
  status: 2,
  variant: 3,
  pages: 4,
  inReview: 5,
  inReviewBy: 7,
} as const

/** The queue row whose text contains *text* (a candidate, "No candidate"). */
export function queueRow(page: Page, text: string): Locator {
  return page.getByRole('row').filter({ hasText: text })
}

export function queueCell(row: Locator, column: keyof typeof QUEUE_COLUMN): Locator {
  return row.getByRole('cell').nth(QUEUE_COLUMN[column])
}

// ── Review page ─────────────────────────────────────────────────────────────

/** A value of the review header ("Score", "Candidate", "To review", …). */
export function headerValue(page: Page, term: string): Locator {
  return page
    .getByRole('term')
    .filter({ hasText: new RegExp(`^${escapeRegExp(term)}$`) })
    .locator('xpath=following-sibling::dd[1]')
}

/** The row of the reading labelled *label* in the review list. */
export function reviewObject(page: Page, label: string): Locator {
  return page.getByRole('button').filter({ has: page.getByText(label, { exact: true }) })
}

/** Select the reading *label*, then click the focus panel's *action*. */
export async function act(page: Page, label: string, action: string): Promise<void> {
  await reviewObject(page, label).click()
  await page.getByRole('button', { name: action, exact: true }).click()
}

/**
 * The confirmation dialog titled *title*. ConfirmDialog has no dialog role:
 * its box is found by its heading.
 */
export function confirmDialog(page: Page, title: string): Locator {
  return page.locator('.modal-box').filter({ has: page.getByRole('heading', { name: title }) })
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
