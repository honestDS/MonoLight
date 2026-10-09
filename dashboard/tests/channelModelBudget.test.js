import test from 'node:test'
import assert from 'node:assert/strict'
import { createI18n } from 'vue-i18n'

import enChannels from '../src/i18n/locales/en/channels.js'
import zhChannels from '../src/i18n/locales/zh/channels.js'
import { applyOpenRouterModelMetadata } from '../src/utils/channelModelMetadata.js'
import { defaultModelEntry } from '../src/constants/index.js'

test('adjusts the default output limit for a one-kilotoken context', () => {
  const entry = defaultModelEntry()
  const result = applyOpenRouterModelMetadata(entry, { context_length: 1000 })

  assert.equal(entry.context_window_k, 1)
  assert.equal(entry.max_tokens, 743)
  assert.equal(result.fields.includes('context_window_k'), true)
  assert.equal(result.fields.includes('max_tokens'), true)
})

test('prefers top-provider context length when deriving the adjusted budget', () => {
  const entry = defaultModelEntry()
  const result = applyOpenRouterModelMetadata(entry, {
    context_length: 128000,
    top_provider: { context_length: 12000 }
  })

  assert.equal(entry.context_window_k, 12)
  assert.equal(entry.max_tokens, 11743)
  assert.equal(result.fields.includes('max_tokens'), true)
})

test('keeps legal custom output limits when metadata reduces the context', () => {
  for (const maxTokens of [128, 512, 0]) {
    const entry = defaultModelEntry()
    entry.max_tokens = maxTokens

    const result = applyOpenRouterModelMetadata(entry, { context_length: 1000 })

    assert.equal(entry.context_window_k, 1)
    assert.equal(entry.max_tokens, maxTokens)
    assert.equal(result.fields.includes('max_tokens'), false)
  }
})

test('preserves invalid max_tokens values during metadata budget detection', () => {
  const invalidMaxTokens = [
    -1,
    1.5,
    '20480',
    NaN,
    Infinity,
    -Infinity,
    Number.MAX_SAFE_INTEGER + 1,
    true,
    false
  ]

  for (const maxTokens of invalidMaxTokens) {
    const entry = defaultModelEntry()
    entry.max_tokens = maxTokens
    const result = applyOpenRouterModelMetadata(entry, { context_length: 1000 })

    assert.equal(entry.context_window_k, 1)
    assert.equal(result.fields.includes('context_window_k'), true)
    assert.equal(result.fields.includes('max_tokens'), false)
    if (Number.isNaN(maxTokens)) {
      assert.equal(Object.is(entry.max_tokens, maxTokens), true)
    } else {
      assert.equal(entry.max_tokens, maxTokens)
    }
  }
})

test('uses the default output limit when max_tokens is missing or null', () => {
  const entries = [defaultModelEntry(), defaultModelEntry()]
  delete entries[0].max_tokens
  entries[1].max_tokens = null

  for (const entry of entries) {
    const result = applyOpenRouterModelMetadata(entry, { context_length: 1000 })

    assert.equal(entry.context_window_k, 1)
    assert.equal(entry.max_tokens, 743)
    assert.equal(result.fields.includes('max_tokens'), true)
  }
})

test('leaves the existing context and output limit when metadata has no valid context', () => {
  const invalidMetadata = [
    {},
    { context_length: 0 },
    { context_length: NaN },
    { context_length: Infinity },
    { context_length: '1000' }
  ]

  for (const model of invalidMetadata) {
    const entry = defaultModelEntry()
    entry.context_window_k = 12
    entry.max_tokens = 512

    const result = applyOpenRouterModelMetadata(entry, model)

    assert.equal(entry.context_window_k, 12)
    assert.equal(entry.max_tokens, 512)
    assert.equal(result.fields.includes('context_window_k'), false)
    assert.equal(result.fields.includes('max_tokens'), false)
  }
})

test('does not adjust max_tokens for non-chat entries', () => {
  const entry = defaultModelEntry()
  entry.usage = 'EMBEDDING'

  const result = applyOpenRouterModelMetadata(entry, { context_length: 1000 })

  assert.equal(entry.context_window_k, 1)
  assert.equal(entry.max_tokens, 20480)
  assert.equal(result.fields.includes('context_window_k'), true)
  assert.equal(result.fields.includes('max_tokens'), false)
})

test('adjusts a one-kilotoken budget with exactly zero remaining capacity', () => {
  const entry = defaultModelEntry()
  entry.max_tokens = 744

  const result = applyOpenRouterModelMetadata(entry, { context_length: 1000 })

  assert.equal(entry.context_window_k, 1)
  assert.equal(entry.max_tokens, 743)
  assert.equal(result.fields.includes('max_tokens'), true)
})

test('does not report or lower max_tokens on a repeated metadata application', () => {
  const entry = defaultModelEntry()
  const model = { context_length: 1000 }

  const first = applyOpenRouterModelMetadata(entry, model)
  const second = applyOpenRouterModelMetadata(entry, model)

  assert.equal(first.fields.includes('max_tokens'), true)
  assert.equal(second.fields.includes('max_tokens'), false)
  assert.equal(entry.max_tokens, 743)
})

test('preserves unrelated model entry settings during budget adjustment', () => {
  const entry = defaultModelEntry()
  entry.model_id = 'model-to-keep'
  entry.temperature = 0.15
  entry.advanced_settings = { custom_headers: { 'x-model': 'keep' } }
  entry.reasoning_efforts = ['manual']
  entry.description = 'Existing description'

  applyOpenRouterModelMetadata(entry, { context_length: 1000 })

  assert.equal(entry.model_id, 'model-to-keep')
  assert.equal(entry.temperature, 0.15)
  assert.deepEqual(entry.advanced_settings, { custom_headers: { 'x-model': 'keep' } })
  assert.deepEqual(entry.reasoning_efforts, ['manual'])
  assert.equal(entry.description, 'Existing description')
})

test('renders the automatic budget adjustment message in Chinese and English', () => {
  const entry = defaultModelEntry()
  applyOpenRouterModelMetadata(entry, { context_length: 1000 })
  const i18n = createI18n({
    legacy: false,
    locale: 'zh',
    fallbackLocale: 'en',
    messages: { zh: zhChannels, en: enChannels },
    missingWarn: false,
    fallbackWarn: false
  })

  for (const locale of ['zh', 'en']) {
    i18n.global.locale.value = locale
    const rendered = i18n.global.t('model_metadata_max_tokens_adjusted', {
      context_window_k: entry.context_window_k,
      max_tokens: entry.max_tokens
    })

    assert.match(rendered, /1K/)
    assert.match(rendered, /743/)
    assert.ok(rendered.replace(/1K|743/g, '').trim().length > 0)
    assert.doesNotMatch(rendered, /\{[^{}]*\}/)
  }
})
