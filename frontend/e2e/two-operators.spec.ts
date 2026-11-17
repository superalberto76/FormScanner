// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Two operators on the same submission: the review lock in the browser.
// Operator 1 opens F04 ("🔒 You" in the queue); operator 2 finds it held
// ("In review by…", read-only) and takes it over;
// operator 1, back on the tab or refused with LOCK_LOST on a save, sees
// "… took over" and can no longer act; operator 1 takes it back.
//
// Expected values from the sources: the review lock rules (banners,
// read-only, "Edit" of the candidate no longer available, Take back); the
// operators' names from the synthetic builders ("Coll Owner", "Coll
// Collaborator"); F04's candidate 100004 from its scenario. The 409s behind
// it (LOCK_LOST, SUBMISSION_CHANGED) are covered by the synthetic flows
// (test_review_lock_flow.py): here only the interface.
import { expect, test, type Browser, type Page } from '@playwright/test'
import { COLLABORATOR, COLL_MULTI, OWNER, kitPdf } from './env.ts'
import {
  createBatch,
  headerValue,
  login,
  openProject,
  queueCell,
  queueRow,
  runExtraction,
  uploadPdfs,
} from './helpers.ts'

const F04_CANDIDATE = '100004'

// Known bug: back on the tab within 30 s of the last read of the submission,
// operator 1 does not see the take-over (the review query keeps the global
// staleTime of 30 s, so the focus refetches nothing). The steps up to the
// take-over are their own test, which must pass; the return to the tab is
// declared with test.fail (an expected failure: once the bug is fixed the
// test passes, Playwright reports it as a failure, and the declaration goes).
// Read-only and Take back are also reached by the other documented way, a
// save refused with LOCK_LOST (handleSaveError), in a test that must pass.
const KNOWN_BUG_TAKE_OVER_ON_TAB_RETURN =
  'Known bug #10: back on the tab within 30 s the take-over is not shown'

test('two operators: lock, "In review by", read-only, take over', async ({ browser }) => {
  test.setTimeout(5 * 60_000)
  await withOperators(browser, async (one, two) => {
    await lockAndTakeOver(one, two, 'E2E two operators')
  })
})

test('two operators: the first, back on the tab, is read-only and takes it back', async ({ browser }) => {
  test.fail(true, KNOWN_BUG_TAKE_OVER_ON_TAB_RETURN)
  test.setTimeout(5 * 60_000)
  await withOperators(browser, async (one, two) => {
    await lockAndTakeOver(one, two, 'E2E back on the tab')

    await test.step('operator 1, back on the tab: "took over", read-only, takes it back', async () => {
      await backOnTab(one)
      await expectTookOverThenTakeBack(one, 'Coll Collaborator')
    })

    await test.step('operator 2, back on the tab: Coll Owner took it back', async () => {
      await backOnTab(two)
      await expectTookOver(two, 'Coll Owner')
    })
  })
})

test('two operators: the first, refused with LOCK_LOST, is read-only and takes it back', async ({ browser }) => {
  test.setTimeout(5 * 60_000)
  await withOperators(browser, async (one, two) => {
    await lockAndTakeOver(one, two, 'E2E lock lost')

    await test.step('operator 1 acts on a stale page: refused, "took over", takes it back', async () => {
      // The page still offers the actions (the known bug above); the save is
      // refused with LOCK_LOST and the page refetches, nothing is written.
      await one.getByRole('button', { name: 'Confirm', exact: true }).click()
      await expectTookOverThenTakeBack(one, 'Coll Collaborator')
      await expect(headerValue(one, 'To review')).toHaveText('6 of 6 remaining')
    })

    await test.step('operator 2 acts on a stale page: refused, Coll Owner took it back', async () => {
      await two.getByRole('button', { name: 'Confirm', exact: true }).click()
      await expectTookOver(two, 'Coll Owner')
      await expect(headerValue(two, 'To review')).toHaveText('6 of 6 remaining')
    })
  })
})

/** Two operators, each in a browser context of its own; the contexts are
 *  closed whatever happens. */
async function withOperators(
  browser: Browser,
  body: (one: Page, two: Page) => Promise<void>,
): Promise<void> {
  const contexts = [await browser.newContext(), await browser.newContext()]
  try {
    const [one, two] = await Promise.all(contexts.map(c => c.newPage()))
    await login(one, OWNER)
    await login(two, COLLABORATOR)
    await body(one, two)
  } finally {
    await Promise.all(contexts.map(c => c.close()))
  }
}

/** The "… took over" banner naming *holder*, and the page read-only. */
async function expectTookOver(page: Page, holder: string): Promise<void> {
  const banner = page.getByRole('status').filter({ hasText: 'took over this submission' })
  await expect(banner).toContainText(`${holder} took over this submission`)
  await expect(banner).toContainText('Your view is now read-only.')
  await expectReadOnly(page)
}

/** Taken over by *holder*, read-only; Take back, and the page may act
 *  again. */
async function expectTookOverThenTakeBack(page: Page, holder: string): Promise<void> {
  await expectTookOver(page, holder)
  const banner = page.getByRole('status').filter({ hasText: 'took over this submission' })
  await banner.getByRole('button', { name: 'Take back' }).click()
  await expect(banner).toHaveCount(0)
  await expectCanAct(page)
}

/**
 * Operator 1 prepares the batch *batchName* with F04 and
 * opens it ("🔒 You" in the queue); operator 2 finds it held by Coll Owner,
 * read-only, and takes it over. Ends with operator 2 holding the lock and
 * operator 1 still on the submission page, as it was before the take-over.
 */
async function lockAndTakeOver(one: Page, two: Page, batchName: string): Promise<void> {

  await test.step('operator 1 prepares a batch with F04, extracted', async () => {
    await openProject(one, COLL_MULTI)
    await createBatch(one, batchName)
    await uploadPdfs(one, [kitPdf('F04')])
    await runExtraction(one, 1)
  })
  const batchUrl = one.url()

  await test.step('operator 1 opens F04: may act; the queue says "🔒 You"', async () => {
    await queueRow(one, F04_CANDIDATE).click()
    await expectCanAct(one)

    // The queue in a second tab of the same operator: opening it releases
    // nothing (only leaving the submission page does).
    const queue = await one.context().newPage()
    await queue.goto(batchUrl)
    await expect(queueCell(queueRow(queue, F04_CANDIDATE), 'inReviewBy')).toContainText('🔒You')
    await queue.close()
  })

  await test.step('operator 2: held by Coll Owner, read-only, takes over', async () => {
    await two.goto(batchUrl)
    await expect(queueCell(queueRow(two, F04_CANDIDATE), 'inReviewBy')).toContainText('🔒Coll Owner')
    await queueRow(two, F04_CANDIDATE).click()
    const banner = two.getByRole('status').filter({ hasText: 'In review by' })
    await expect(banner).toContainText('In review by Coll Owner')
    await expect(banner).toContainText('You can look, but not make changes.')
    await expectReadOnly(two)

    await banner.getByRole('button', { name: 'Take over' }).click()
    await expect(banner).toHaveCount(0)
    await expectCanAct(two)
  })
}

/**
 * The operator comes back to the tab. The review refetches on window focus,
 * never by polling (SubmissionReviewPage, refetchOnWindowFocus); TanStack
 * Query listens to visibilitychange on window, which a headless page never
 * fires on its own: dispatched on document and bubbling, as the browser does.
 */
async function backOnTab(page: Page): Promise<void> {
  await page.evaluate(() =>
    document.dispatchEvent(new Event('visibilitychange', { bubbles: true })))
}

/** The lock is ours: the reading's actions and the candidate's Edit. */
async function expectCanAct(page: Page): Promise<void> {
  await expect(page.getByRole('button', { name: 'Confirm', exact: true })).toBeVisible()
  await expect(headerValue(page, 'Candidate').getByRole('button', { name: 'Edit' })).toBeVisible()
}

/** Read-only: no action on the reading, no Edit of the candidate. */
async function expectReadOnly(page: Page): Promise<void> {
  await expect(headerValue(page, 'Candidate')).toContainText(F04_CANDIDATE)
  for (const action of ['Confirm', 'Replace…', 'Flag unresolvable']) {
    await expect(page.getByRole('button', { name: action, exact: true })).toHaveCount(0)
  }
  await expect(headerValue(page, 'Candidate').getByRole('button', { name: 'Edit' })).toHaveCount(0)
}
