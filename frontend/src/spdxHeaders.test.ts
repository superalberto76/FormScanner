// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Every TypeScript and CSS file of the frontend carries the license header:
// the two REUSE lines at the top of the file. Only the license line is
// checked, so that a new file without it fails here rather than slipping into
// a release.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const FRONTEND_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// Only the first lines count: the header belongs at the top of the file.
const HEADER_LINES = 5
const SKIPPED_DIRS = new Set(['node_modules', 'dist'])

function licenseLine(file: string): string | null {
  if (/\.tsx?$/.test(file)) return '// SPDX-License-Identifier: AGPL-3.0-or-later'
  if (file.endsWith('.css')) return '/* SPDX-License-Identifier: AGPL-3.0-or-later */'
  return null
}

/** The source files under *dir*, relative to the frontend folder. Hidden
 *  folders (e2e/.work among them), node_modules and dist are skipped. */
function sourceFiles(dir: string = FRONTEND_DIR): string[] {
  const found: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith('.') || SKIPPED_DIRS.has(entry.name)) continue
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) found.push(...sourceFiles(full))
    else if (licenseLine(entry.name) !== null) found.push(path.relative(FRONTEND_DIR, full))
  }
  return found.sort()
}

test('the frontend has source files', () => {
  // Guards the test below against passing vacuously on a wrong FRONTEND_DIR.
  assert.ok(sourceFiles().includes(path.join('src', 'main.tsx')))
})

test('every TypeScript and CSS file has the SPDX license header', () => {
  const missing = sourceFiles().filter(file => {
    const head = readFileSync(path.join(FRONTEND_DIR, file), 'utf-8').split('\n').slice(0, HEADER_LINES)
    return !head.includes(licenseLine(file) as string)
  })
  assert.deepEqual(missing, [])
})
