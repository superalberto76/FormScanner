// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Browser end-to-end tests. `npm run e2e`; prerequisites and variables in
// e2e/README.md. Not part of `npm test`.
//
// A backend (uvicorn) on the e2e database and storage, and the PRODUCTION
// build of the frontend served by `vite preview`, the API base URL baked in at
// build time. No change to the app's code or vite.config.ts.
import path from 'node:path'
import { defineConfig, devices } from '@playwright/test'
import {
  API_BASE_URL,
  BACKEND_DIR,
  BE_PORT,
  BE_URL,
  FE_BUILD_DIR,
  FE_PORT,
  FE_URL,
  FRONTEND_DIR,
  PYTHON,
  STORAGE_DIR,
  e2eDatabaseUrl,
} from './e2e/env.ts'

const storage = (name: string) => path.join(STORAGE_DIR, name)

export default defineConfig({
  testDir: './e2e',
  // The screenshots of the user guide are not a test: playwright.screenshots.config.ts.
  testIgnore: ['**/screenshots.spec.ts'],
  globalSetup: './e2e/global-setup.ts',
  outputDir: './e2e/.work/test-results',
  // One database for the whole run: the tests change it, one after another.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { outputFolder: './e2e/.work/report', open: 'never' }]],
  use: {
    baseURL: FE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      name: 'backend',
      command: `${PYTHON} -m uvicorn app.main:app --host 127.0.0.1 --port ${BE_PORT}`,
      cwd: BACKEND_DIR,
      url: `${BE_URL}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        DATABASE_URL: e2eDatabaseUrl(),
        CORS_ORIGINS: FE_URL,
        APP_ENV: 'e2e',
        APP_DEBUG: 'false',
        JWT_SECRET: 'e2e-only-secret',
        STORAGE_PATH: STORAGE_DIR,
        ORIGINAL_UPLOADS_DIR: storage('originals'),
        RENDERED_PAGES_DIR: storage('rendered_pages'),
        OBJECT_IMAGES_DIR: storage('object_images'),
        AVATAR_IMAGES_DIR: storage('avatar_images'),
        SUBMISSION_ORIGINALS_DIR: storage('submission_originals'),
        SUBMISSION_PAGES_DIR: storage('submission_pages'),
      },
    },
    {
      name: 'frontend',
      command:
        `npx vite build --outDir ${FE_BUILD_DIR} --emptyOutDir && ` +
        `npx vite preview --outDir ${FE_BUILD_DIR} --host 127.0.0.1 --port ${FE_PORT} --strictPort`,
      cwd: FRONTEND_DIR,
      url: FE_URL,
      reuseExistingServer: false,
      timeout: 180_000,
      env: { VITE_API_BASE_URL: API_BASE_URL },
    },
  ],
})
