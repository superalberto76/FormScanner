// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// The backend steps of the user guide's screenshots
// (backend/tests/synthetic/sample_project.py), run with the backend's Python.
import { spawnSync } from 'node:child_process'
import { BACKEND_DIR, PYTHON } from './env.ts'

export function sampleStep(args: readonly string[]): void {
  const result = spawnSync(
    PYTHON,
    ['-W', 'ignore', '-m', 'tests.synthetic.sample_project', ...args],
    { cwd: BACKEND_DIR, stdio: 'inherit', timeout: 5 * 60 * 1000 },
  )
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`sample_project ${args[0]} failed (exit ${result.status}): see its output above`)
  }
}
