// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Global setup of the e2e run: every run starts from a known state. Empties
// the work folder (kit and BE storage), then `python -m tests.synthetic.e2e_env`
// empties the e2e database, brings it to the Alembic head, creates the two
// operators, exports the kit and seeds the COLL-* projects (~90 s).
//
// Playwright starts the webServers BEFORE this setup: the backend does not
// touch the database at startup; it creates two storage folders
// (originals, rendered_pages), which this setup then empties — harmless,
// since every write creates its folder again.
import { spawnSync } from 'node:child_process'
import { mkdirSync, rmSync } from 'node:fs'
import {
  BACKEND_DIR,
  COLLABORATOR,
  KIT_DIR,
  OWNER,
  PASSWORD,
  PYTHON,
  STORAGE_DIR,
  e2eDatabaseUrl,
} from './env.ts'

export default function globalSetup(): void {
  for (const dir of [KIT_DIR, STORAGE_DIR]) {
    rmSync(dir, { recursive: true, force: true })
  }
  mkdirSync(STORAGE_DIR, { recursive: true })

  const result = spawnSync(
    PYTHON,
    [
      '-W', 'ignore',
      '-m', 'tests.synthetic.e2e_env',
      '--database-url', e2eDatabaseUrl(),
      '--kit-out', KIT_DIR,
      '--owner', OWNER.email,
      '--collaborator', COLLABORATOR.email,
      '--password', PASSWORD,
    ],
    { cwd: BACKEND_DIR, stdio: 'inherit', timeout: 10 * 60 * 1000 },
  )
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`tests.synthetic.e2e_env failed (exit ${result.status}): see its output above`)
  }
}
