// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// The screenshots of chapter 3 of the user guide (`npm run screenshots`,
// playwright.screenshots.config.ts). Not a test: it walks through the
// chapter in the browser and saves one picture per step into
// docs/user-guide/images, named 03-NN-<step>.png in the order of the chapter.
//
// The browser does what the chapter shows being done: the colleague's
// account, the project, the variant, one question drawn on the page,
// publishing, the PDF, activation, the members, the batch, the upload, the
// extraction, the review and the export. The rest of the form and the
// filled-in sheets come from backend/tests/synthetic/sample_project.py
// (`fill`, `sheets`), whose test checks that the sheets read as the chapter
// says.
//
// The scores the chapter quotes are checked on the way: the script stops if a
// step does not give what the chapter says.
//
// Change the interface, run it again and commit the pictures of the steps the
// change touches: every run also changes the others a little (submission ids,
// upload times).
import path from 'node:path'
import { expect, test, type Locator, type Page } from '@playwright/test'
import {
  GUIDE_IMAGES_DIR,
  SAMPLE_ADMIN,
  SAMPLE_COLLEAGUE,
  SAMPLE_DIR,
  SAMPLE_PROJECT,
  SAMPLE_SHEETS,
  SAMPLE_VARIANT,
  e2eDatabaseUrl,
} from './env.ts'
import { act, confirmDialog, headerValue, queueRow, reviewObject, runExtraction } from './helpers.ts'
import { sampleStep } from './screenshots-steps.ts'

const BATCH = 'Morning session'

// The first question of the sample (sample_project.FIRST_QUESTION), drawn in
// the browser at its place on the page (page pixels, A4 at 96 dpi).
const FIRST_QUESTION = {
  label: 'Question 1',
  text: 'What is the capital of France?',
  options: ['Berlin', 'Madrid', 'Paris', 'Rome'],
  correct: 2, // "Paris", 0-based
  box: { x: 60, y: 330, w: 330, h: 90 },
}
const PAGE_WIDTH_PX = 794

/** Save the picture *name* of the window (the whole page with *fullPage*), or of *target* only. */
async function shot(
  page: Page,
  name: string,
  target?: Locator,
  { fullPage = false }: { fullPage?: boolean } = {},
): Promise<void> {
  // Toasts come and go: never in a picture.
  for (const close of await page.locator('.toast-close').all()) {
    await close.click().catch(() => undefined)
  }
  await expect(page.locator('.toast')).toHaveCount(0)
  await page.mouse.move(0, 0)
  // A page scrolled by a click would draw the sticky top bar halfway down a full-page picture.
  if (fullPage) await page.evaluate(() => window.scrollTo(0, 0))
  const file = path.join(GUIDE_IMAGES_DIR, `${name}.png`)
  if (target) await target.screenshot({ path: file, animations: 'disabled' })
  else await page.screenshot({ path: file, animations: 'disabled', fullPage })
}

/** Render page 1 of *pdf* as the picture *name* (the browser shows no PDF headless). */
function renderPdf(pdf: string, name: string): void {
  sampleStep(['render', '--pdf', pdf, '--png', path.join(GUIDE_IMAGES_DIR, `${name}.png`)])
}

test('user guide, chapter 3: screenshots', async ({ page }) => {
  await test.step('sign in as the administrator', async () => {
    await page.goto('/login')
    await page.getByLabel('Email').fill(SAMPLE_ADMIN.email)
    await page.getByLabel('Password').fill(SAMPLE_ADMIN.password)
    await shot(page, '03-01-sign-in')
    await page.getByRole('button', { name: 'Login' }).click()
    await expect(page.getByRole('heading', { name: 'Projects', level: 1 })).toBeVisible()
  })

  await test.step("create the colleague's account", async () => {
    await page.getByRole('button', { name: /^User menu/ }).click()
    await page.getByRole('menuitem', { name: /Add user/ }).click()
    await page.getByLabel('First name').fill(SAMPLE_COLLEAGUE.firstName)
    await page.getByLabel('Last name').fill(SAMPLE_COLLEAGUE.lastName)
    await page.getByLabel('Email').fill(SAMPLE_COLLEAGUE.email)
    await page.getByLabel('Temporary password').fill(SAMPLE_COLLEAGUE.temporaryPassword)
    await shot(page, '03-02-add-user')
    await page.getByRole('button', { name: 'Create user' }).click()
    await expect(page.getByText(`User ${SAMPLE_COLLEAGUE.email} created`)).toBeVisible()
  })

  await test.step('create the project', async () => {
    await page.getByRole('link', { name: 'FormScanner' }).click()
    await page.getByRole('button', { name: '+ New project' }).click()
    await page.getByLabel('Name').fill(SAMPLE_PROJECT)
    await page.getByLabel('Description').fill('A short general-knowledge quiz.')
    await page.getByLabel('Event date').fill('2026-12-15')
    await shot(page, '03-03-new-project')
    await page.getByRole('button', { name: 'Create project' }).click()
    await page.getByRole('row', { name: new RegExp(SAMPLE_PROJECT) }).click()
    await expect(page.getByRole('heading', { name: SAMPLE_PROJECT, level: 1 })).toBeVisible()
  })

  await test.step('create the variant', async () => {
    await page.getByRole('button', { name: '+ New variant' }).click()
    await page.getByLabel('Name').fill(SAMPLE_VARIANT)
    await shot(page, '03-04-new-variant')
    await page.getByRole('button', { name: 'Create variant' }).click()
    await expect(page).toHaveURL(/\/variants\//)
    await expect(page.getByText('No objects on this page — pick a tool above and draw a box.')).toBeVisible()
  })

  await test.step('draw the first question', async () => {
    await page.locator('select.nd-toolbar-select').first().selectOption('ADD_QUESTION')
    const surface = page.locator('.nd-page-surface')
    const box = await surface.boundingBox()
    if (!box) throw new Error('the page is not on screen')
    const k = box.width / PAGE_WIDTH_PX
    const q = FIRST_QUESTION.box
    await page.mouse.move(box.x + q.x * k, box.y + q.y * k)
    await page.mouse.down()
    await page.mouse.move(box.x + (q.x + q.w / 2) * k, box.y + (q.y + q.h / 2) * k, { steps: 5 })
    await page.mouse.move(box.x + (q.x + q.w) * k, box.y + (q.y + q.h) * k, { steps: 5 })
    await page.mouse.up()

    const modal = page.locator('.modal-box').filter({ hasText: 'Add QUESTION object' })
    await expect(modal).toBeVisible()
    await modal.locator('.form-group').filter({ hasText: /^label/ }).locator('input').fill(FIRST_QUESTION.label)
    await modal.locator('.form-group').filter({ hasText: 'Question text' }).locator('textarea').fill(FIRST_QUESTION.text)
    await modal.getByRole('button', { name: '+ Add option' }).click()
    await modal.getByRole('button', { name: '+ Add option' }).click()
    for (const [i, text] of FIRST_QUESTION.options.entries()) {
      await modal.getByPlaceholder(`Option ${i + 1}`, { exact: true }).fill(text)
    }
    await modal.getByRole('checkbox', { name: '✓' }).nth(FIRST_QUESTION.correct).check()
    await shot(page, '03-05-question-form')
    await modal.getByRole('button', { name: 'Add QUESTION object' }).click()
    await expect(modal).toHaveCount(0)
    // The score of the correct option, in the panel (the create form has none).
    await objectInList(page, FIRST_QUESTION.label).click()
    await expect(page.getByText('Edit · QUESTION')).toBeVisible()
    await page.getByPlaceholder('score').nth(FIRST_QUESTION.correct).fill('1')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText('Max score: 1')).toBeVisible()
    await shot(page, '03-06-question-on-page', undefined, { fullPage: true })
  })

  await test.step('the complete form', async () => {
    sampleStep(['fill', '--database-url', e2eDatabaseUrl()])
    await page.reload()
    await objectInList(page, 'Student number').click()
    await expect(page.getByText('This object is the')).toBeVisible()
    await expect(page.getByText('Max score: 7')).toBeVisible()
    await shot(page, '03-07-complete-form', undefined, { fullPage: true })
  })

  await test.step('publish the variant and download the PDF', async () => {
    await page.getByRole('button', { name: 'Publish', exact: true }).click()
    const dialog = confirmDialog(page, 'Publish variant')
    await shot(page, '03-08-publish')
    await dialog.getByRole('button', { name: 'Publish' }).click()
    await expect(page.getByText(/This variant is PUBLISHED/)).toBeVisible()

    const downloading = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Download Student PDF' }).click()
    const download = await downloading
    const pdf = path.join(SAMPLE_DIR, download.suggestedFilename())
    await download.saveAs(pdf)
    renderPdf(pdf, '03-09-printable-pdf')
  })

  await test.step('activate the project', async () => {
    // The editor's breadcrumb names the project "Project".
    await page.getByRole('link', { name: 'Project', exact: true }).click()
    await page.getByRole('button', { name: 'Activate', exact: true }).click()
    const dialog = confirmDialog(page, 'Activate project')
    await shot(page, '03-10-activate')
    await dialog.getByRole('button', { name: 'Activate' }).click()
    await expect(page.getByText('ACTIVE').first()).toBeVisible()
  })

  await test.step('add the colleague to the project', async () => {
    await page.getByRole('link', { name: 'FormScanner' }).click()
    await page.getByRole('button', { name: 'Show the project members' }).click()
    await page.getByRole('button', { name: 'Add collaborators' }).click()
    await page.getByPlaceholder('Search by name or email (min 2 characters)…').fill('sam')
    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByText('Collaborator', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()
    await shot(page, '03-11-members', page.locator('.modal-box'))
    await page.getByRole('button', { name: '✕' }).click()
  })

  await test.step('the filled-in sheets', async () => {
    sampleStep(['sheets', '--database-url', e2eDatabaseUrl(), '--out', SAMPLE_DIR])
    renderPdf(path.join(SAMPLE_DIR, 'john-smith.pdf'), '03-12-scanned-sheet')
  })

  await test.step('create the batch and upload the sheets', async () => {
    await page.getByRole('row', { name: new RegExp(SAMPLE_PROJECT) }).click()
    await page.getByRole('link', { name: 'Scanning' }).click()
    await page.getByRole('button', { name: '+ New batch' }).click()
    await page.getByLabel('Name').fill(BATCH)
    await shot(page, '03-13-new-batch')
    await page.getByRole('button', { name: 'Create batch' }).click()
    await expect(page.getByRole('heading', { name: BATCH, level: 1 })).toBeVisible()

    await page
      .locator('input[type="file"]')
      .setInputFiles(SAMPLE_SHEETS.map(stem => path.join(SAMPLE_DIR, `${stem}.pdf`)))
    await shot(page, '03-14-upload')
    await page.getByRole('button', { name: `Upload ${SAMPLE_SHEETS.length} files` }).click()
    await expect(page.getByText('✓ Submission created')).toHaveCount(SAMPLE_SHEETS.length)
    await page.reload()
    await expect(page.getByRole('button', { name: `To extract (${SAMPLE_SHEETS.length})` })).toBeVisible()
    await shot(page, '03-15-uploaded')
  })
  const batchUrl = page.url()

  await test.step('run extraction', async () => {
    await runExtraction(page, SAMPLE_SHEETS.length)
    await shot(page, '03-16-extracted')
  })

  await test.step('review John Smith: a light mark, two bubbles, the name', async () => {
    await queueRow(page, '20452').click()
    await reviewObject(page, 'Question 2').click()
    await expect(page.getByText('Why it is here')).toBeVisible()
    await expect(headerValue(page, 'Score')).toContainText('3 / 7')
    await shot(page, '03-17-review')
    await act(page, 'Question 2', 'Confirm')
    await expect(headerValue(page, 'To review')).toContainText('2 of 3 remaining')
    await expect(headerValue(page, 'Score')).toContainText('4 / 7')
    await act(page, 'Question 4', 'Confirm')
    await expect(headerValue(page, 'To review')).toContainText('1 of 3 remaining')
    await replaceName(page, 'John Smith')
    await expect(headerValue(page, 'To review')).toContainText('0 of 3 remaining')
    await expect(headerValue(page, 'Score')).toContainText('4 / 7')
    await page.getByRole('button', { name: 'Close submission' }).click()
    await expect(page.getByText('This submission is closed. It is shown read-only.')).toBeVisible()
    await shot(page, '03-18-closed')
  })

  await test.step('review Erika Example: the student number', async () => {
    await page.goto(batchUrl)
    await queueRow(page, 'No candidate').click()
    await act(page, 'Student number', 'Replace…')
    await page.getByRole('textbox', { name: 'Identifier' }).fill('20453')
    await shot(page, '03-19-replace-identifier')
    await page.getByRole('button', { name: 'Save replacement' }).click()
    await expect(headerValue(page, 'Candidate')).toContainText('20453')
    await replaceName(page, 'Erika Example')
    await expect(headerValue(page, 'Score')).toContainText('6 / 7')
    await page.getByRole('button', { name: 'Close submission' }).click()
    await expect(page.getByText('This submission is closed. It is shown read-only.')).toBeVisible()
  })

  const lastTwo = [
    ['20451', 'Jane Doe', '7 / 7'],
    ['20454', 'Max Sample', '5 / 7'],
  ] as const
  for (const [candidate, name, score] of lastTwo) {
    await test.step(`review ${name}: the name`, async () => {
      await page.goto(batchUrl)
      await queueRow(page, candidate).click()
      await replaceName(page, name)
      await expect(headerValue(page, 'Score')).toContainText(score)
      await page.getByRole('button', { name: 'Close submission' }).click()
      await expect(page.getByText('This submission is closed. It is shown read-only.')).toBeVisible()
    })
  }

  await test.step('export the results', async () => {
    await page.goto(batchUrl)
    await expect(page.getByRole('button', { name: `Reviewed (${SAMPLE_SHEETS.length})` })).toBeVisible()
    await shot(page, '03-20-reviewed')
    await page.getByRole('button', { name: 'Export summary…' }).click()
    await page.getByRole('radio', { name: /Excel workbook/ }).check()
    await shot(page, '03-21-export')
    await page.getByRole('button', { name: 'Cancel' }).click()
  })
})

/** An object in the editor's list of objects. */
function objectInList(page: Page, label: string): Locator {
  return page.getByRole('listitem').filter({ hasText: label })
}

/** Replace the OCR reading of the name with *name*, and wait for the save. */
async function replaceName(page: Page, name: string): Promise<void> {
  await act(page, 'Name', 'Replace…')
  await page.getByRole('textbox', { name: 'Text' }).fill(name)
  await page.getByRole('button', { name: 'Save replacement' }).click()
  await expect(page.getByRole('button', { name: 'Save replacement' })).toHaveCount(0)
  await expect(reviewObject(page, 'Name')).toContainText('✎')
}
