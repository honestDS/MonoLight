import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ensureAdvancedSettings,
  formatAdvancedSettings,
  mergeCustomHeaders,
  parseAdvancedSettingsDraft
} from '../src/utils/channelAdvancedSettings.js'

const translate = (key, params = {}) => ({ key, params })

const assertParseError = (draft, key, params = {}) => {
  const result = parseAdvancedSettingsDraft(draft, translate)

  assert.equal(result.value, null)
  assert.deepEqual(result.error, { key, params })
}

test('formats new custom headers and falls back to the legacy user agent', () => {
  assert.equal(
    formatAdvancedSettings({
      custom_headers: {
        'x-request-id': 'request-id',
        accept: 'application/json'
      },
      user_agent: 'legacy-agent'
    }),
    '{\n  "x-request-id": "request-id",\n  "accept": "application/json"\n}'
  )
  assert.equal(
    formatAdvancedSettings({ user_agent: 'legacy-agent' }),
    '{\n  "user-agent": "legacy-agent"\n}'
  )
})

test('normalizes header names and trims header values while parsing', () => {
  const result = parseAdvancedSettingsDraft(
    JSON.stringify({ 'X-Request-ID': '  request-id  ', ACCEPT: '\tapplication/json\n' }),
    translate
  )

  assert.deepEqual(result, {
    value: {
      'x-request-id': 'request-id',
      accept: 'application/json'
    },
    error: ''
  })
})

test('returns a translated error for invalid JSON and non-object JSON', () => {
  assertParseError('not-json', 'channels.custom_headers_json_object_error')

  for (const draft of ['null', '[]', '"text"', '42', 'true']) {
    assertParseError(draft, 'channels.custom_headers_json_object_error')
  }
})

test('rejects duplicate header names after case normalization', () => {
  assertParseError(
    JSON.stringify({ 'X-Request-ID': 'first', 'x-request-id': 'second' }),
    'channels.custom_headers_name_error'
  )
})

test('rejects reserved header names case-insensitively', () => {
  assertParseError(
    JSON.stringify({ Authorization: 'secret' }),
    'channels.custom_headers_reserved_header_error',
    { header: 'authorization' }
  )
})

test('rejects invalid header names', () => {
  assertParseError(
    JSON.stringify({ 'X Request ID': 'request-id' }),
    'channels.custom_headers_name_error'
  )
})

test('rejects non-string, control-character, and overlong header values', () => {
  assertParseError(
    JSON.stringify({ 'x-attempts': 3 }),
    'channels.custom_headers_value_error'
  )
  assertParseError(
    JSON.stringify({ 'x-message': 'valid\nvalue' }),
    'channels.custom_headers_value_error'
  )
  assert.equal(
    parseAdvancedSettingsDraft(
      JSON.stringify({ 'x-message': 'a'.repeat(4096) }),
      translate
    ).error,
    ''
  )
  assertParseError(
    JSON.stringify({ 'x-message': 'a'.repeat(4097) }),
    'channels.custom_headers_value_error'
  )
})

test('rejects more than 32 custom headers', () => {
  const headers = Object.fromEntries(
    Array.from({ length: 33 }, (_, index) => [`x-header-${index}`, 'value'])
  )

  assertParseError(
    JSON.stringify(headers),
    'channels.custom_headers_max_items_error'
  )
})

test('merges custom headers while preserving unknown settings and removing legacy user_agent', () => {
  const entry = {
    advanced_settings: {
      user_agent: 'legacy-agent',
      request_timeout_ms: 30000,
      unknown_setting: { enabled: true },
      custom_headers: { 'x-old': 'old' }
    }
  }

  const result = mergeCustomHeaders(entry, { 'x-new': 'new' })

  assert.deepEqual(result, {
    request_timeout_ms: 30000,
    unknown_setting: { enabled: true },
    custom_headers: { 'x-new': 'new' }
  })
  assert.equal(result, entry.advanced_settings)
})

test('removes custom headers when merging an empty set', () => {
  const entry = {
    advanced_settings: {
      user_agent: 'legacy-agent',
      custom_headers: { 'x-old': 'old' },
      unknown_setting: 'preserved'
    }
  }

  mergeCustomHeaders(entry, {})

  assert.deepEqual(entry.advanced_settings, { unknown_setting: 'preserved' })
})

test('replaces invalid advanced_settings with a plain object', () => {
  for (const invalidValue of [null, [], 'invalid', 42, false]) {
    const entry = { advanced_settings: invalidValue }

    ensureAdvancedSettings(entry)

    assert.deepEqual(entry.advanced_settings, {})
  }
})

test('leaves valid advanced_settings unchanged', () => {
  const settings = { request_timeout_ms: 30000 }
  const entry = { advanced_settings: settings }

  ensureAdvancedSettings(entry)

  assert.equal(entry.advanced_settings, settings)
})
