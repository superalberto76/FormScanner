// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Global setup of `npm run screenshots`: empties the backend storage and the
// sample folder, then `python -m tests.synthetic.sample_project prepare`
// empties the e2e database, brings it to the Alembic head and creates the
// administrator of the user guide.
import { mkdirSync, rmSync } from 'node:fs'
import { SAMPLE_DIR, STORAGE_DIR, e2eDatabaseUrl } from './env.ts'
import { sampleStep } from './screenshots-steps.ts'

export default function globalSetup(): void {
  for (const dir of [SAMPLE_DIR, STORAGE_DIR]) {
    rmSync(dir, { recursive: true, force: true })
  }
  mkdirSync(STORAGE_DIR, { recursive: true })
  mkdirSync(SAMPLE_DIR, { recursive: true })
  sampleStep(['prepare', '--database-url', e2eDatabaseUrl()])
}
