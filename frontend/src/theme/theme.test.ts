// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Behaviour tests for the theme logic. Runs on Node's built-in test
// runner with native TS type-stripping (see package.json "test": node --test).
// The module touches the DOM only inside functions, so we stub window/document/
// localStorage on globalThis per-test and restore them afterwards.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  resolve,
  prefersDark,
  applyResolved,
  getStoredMode,
  setStoredMode,
  THEME_KEY,
} from './theme.ts'

/** Run `fn` with a stubbed window.matchMedia reporting `matches`. */
function withSystemDark(matches: boolean, fn: () => void) {
  const prev = globalThis.window
  globalThis.window = {
    matchMedia: (q: string) => ({ matches, media: q }) as unknown as MediaQueryList,
  } as unknown as Window & typeof globalThis
  try {
    fn()
  } finally {
    globalThis.window = prev
  }
}

test('resolve(LIGHT)/resolve(DARK) force the palette regardless of the OS', () => {
  // No window stub on purpose: explicit modes must not consult the OS.
  assert.equal(resolve('LIGHT'), 'light')
  assert.equal(resolve('DARK'), 'dark')
})

test('resolve(SYSTEM) follows matchMedia', () => {
  withSystemDark(true, () => assert.equal(resolve('SYSTEM'), 'dark'))
  withSystemDark(false, () => assert.equal(resolve('SYSTEM'), 'light'))
})

test('prefersDark reflects the OS preference', () => {
  withSystemDark(true, () => assert.equal(prefersDark(), true))
  withSystemDark(false, () => assert.equal(prefersDark(), false))
})

test('prefersDark is false when matchMedia is unavailable', () => {
  const prev = globalThis.window
  globalThis.window = {} as unknown as Window & typeof globalThis
  try {
    assert.equal(prefersDark(), false)
  } finally {
    globalThis.window = prev
  }
})

test('applyResolved writes data-theme on <html>', () => {
  const prev = globalThis.document
  const el = { dataset: {} as DOMStringMap }
  globalThis.document = { documentElement: el } as unknown as Document
  try {
    applyResolved('dark')
    assert.equal(el.dataset.theme, 'dark')
    applyResolved('light')
    assert.equal(el.dataset.theme, 'light')
  } finally {
    globalThis.document = prev
  }
})

test('getStoredMode round-trips and defaults to SYSTEM', () => {
  const store = new Map<string, string>()
  const prev = globalThis.localStorage
  globalThis.localStorage = {
    getItem: (k: string) => (store.has(k) ? store.get(k) : null),
    setItem: (k: string, v: string) => {
      store.set(k, v)
    },
    removeItem: (k: string) => {
      store.delete(k)
    },
  } as unknown as Storage
  try {
    assert.equal(getStoredMode(), 'SYSTEM') // unset → default
    setStoredMode('DARK')
    assert.equal(store.get(THEME_KEY), 'DARK')
    assert.equal(getStoredMode(), 'DARK')
    store.set(THEME_KEY, 'BLUE') // unrecognised → default
    assert.equal(getStoredMode(), 'SYSTEM')
  } finally {
    globalThis.localStorage = prev
  }
})
