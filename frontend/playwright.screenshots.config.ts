// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// The screenshots of the user guide: `npm run screenshots`. Same backend and
// frontend as the e2e tests (playwright.config.ts), its own preparation and a
// single script, e2e/screenshots.spec.ts, which writes the pictures of
// chapter 3 into docs/user-guide/images. Not a test: not part of `npm run e2e`
// nor of the continuous integration.
import { defineConfig, devices } from '@playwright/test'
import e2e from './playwright.config.ts'

export default defineConfig({
  ...e2e,
  testIgnore: [],
  testMatch: 'screenshots.spec.ts',
  globalSetup: './e2e/screenshots-setup.ts',
  outputDir: './e2e/.work/screenshots-results',
  reporter: [['list']],
  timeout: 10 * 60_000,
  use: {
    ...e2e.use,
    trace: 'retain-on-failure',
    screenshot: 'off',
    // A step that cannot be done fails at once, not at the end of the run.
    actionTimeout: 30_000,
  },
  // Fixed size and theme: the pictures stay the same from one run to the next.
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 800 },
        deviceScaleFactor: 1,
        colorScheme: 'light',
        locale: 'en-GB',
        timezoneId: 'UTC',
      },
    },
  ],
})
