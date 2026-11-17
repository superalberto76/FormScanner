// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Environment of the browser end-to-end tests: ports, paths, database and
// operators. Read by playwright.config.ts, the global setup and
// the tests; overridable through the E2E_* variables (see e2e/README.md).
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const E2E_DIR = path.dirname(fileURLToPath(import.meta.url))
export const FRONTEND_DIR = path.resolve(E2E_DIR, '..')
export const BACKEND_DIR = path.resolve(FRONTEND_DIR, '..', 'backend')

// Everything a run writes (kit, BE storage, FE build) lives here and is
// recreated at every run; ignored by git.
export const WORK_DIR = path.join(E2E_DIR, '.work')
export const KIT_DIR = path.join(WORK_DIR, 'kit')
export const STORAGE_DIR = path.join(WORK_DIR, 'storage')
export const FE_BUILD_DIR = path.join(WORK_DIR, 'dist')

const HOST = '127.0.0.1'
export const BE_PORT = Number(process.env.E2E_BE_PORT ?? 18000)
export const FE_PORT = Number(process.env.E2E_FE_PORT ?? 14173)
export const BE_URL = `http://${HOST}:${BE_PORT}`
export const FE_URL = `http://${HOST}:${FE_PORT}`
export const API_BASE_URL = `${BE_URL}/api/v1`

export const PYTHON = process.env.E2E_PYTHON ?? 'python'

/**
 * The e2e database: E2E_DATABASE_URL, otherwise TEST_DATABASE_URL with the
 * database renamed to formscanner_e2e (never pytest's own database). The
 * preparation empties it, and refuses any name not ending in "_e2e".
 */
export function e2eDatabaseUrl(): string {
  const explicit = process.env.E2E_DATABASE_URL
  if (explicit) return explicit
  const test = process.env.TEST_DATABASE_URL
  if (!test) {
    throw new Error('Set E2E_DATABASE_URL, or TEST_DATABASE_URL, to run the e2e tests')
  }
  const url = new URL(test)
  url.pathname = '/formscanner_e2e'
  return url.toString()
}

export interface Operator {
  email: string
  password: string
  name: string
}

// Created by tests.synthetic.e2e_env; named as in the synthetic tests.
export const PASSWORD = 'E2e-Password-1'
export const OWNER: Operator = { email: 'e2e-owner@example.com', password: PASSWORD, name: 'Coll Owner' }
export const COLLABORATOR: Operator = {
  email: 'e2e-collab@example.com',
  password: PASSWORD,
  name: 'Coll Collaborator',
}

// Project names from backend/tests/synthetic/specs.py.
export const COLL_MULTI = 'COLL-MULTI — Collaudo multi-variante'

/** A PDF case of the kit, from its COLL-MULTI folder (export_kit, BATCH_PDF). */
export function kitPdf(caseId: string): string {
  return path.join(KIT_DIR, '1-kit-pdf', `${caseId}.pdf`)
}

// ── Screenshots of the user guide (`npm run screenshots`) ──────────────────

// The pictures of chapter 3, committed with the guide.
export const GUIDE_IMAGES_DIR = path.resolve(FRONTEND_DIR, '..', 'docs', 'user-guide', 'images')
// The sheets and PDFs the run writes on the way.
export const SAMPLE_DIR = path.join(WORK_DIR, 'sample')

// From backend/tests/synthetic/sample_project.py: the administrator of a new
// installation (the values of .env.example) and the colleague created in the
// walkthrough, made-up names.
export const SAMPLE_ADMIN: Operator = { email: 'admin@example.com', password: 'ChangeMe!', name: 'Alex Example' }
export const SAMPLE_COLLEAGUE = {
  firstName: 'Sam',
  lastName: 'Example',
  email: 'sam@example.com',
  temporaryPassword: 'Sample-Password-1',
}
export const SAMPLE_PROJECT = 'Sample quiz'
export const SAMPLE_VARIANT = 'Version A'
// The respondents of SAMPLE_SHEETS, in upload order.
export const SAMPLE_SHEETS = ['jane-doe', 'john-smith', 'erika-example', 'max-sample'] as const
