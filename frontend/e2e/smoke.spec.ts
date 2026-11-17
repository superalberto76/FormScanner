// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Smoke e2e test: the environment works end to end — prepared database,
// backend, production build of the frontend, login.
import { expect, test } from '@playwright/test'
import { COLL_MULTI, OWNER } from './env.ts'
import { login, openProject } from './helpers.ts'

test('the owner logs in and opens COLL-MULTI', async ({ page }) => {
  await login(page, OWNER)
  await openProject(page, COLL_MULTI)
  await expect(page.getByRole('link', { name: 'Scanning' })).toBeVisible()
})
