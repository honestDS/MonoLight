import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { defaultModelEntry, normalizeModelEntry } from '../src/constants/index.js'
import enChannels from '../src/i18n/locales/en/channels.js'
import zhChannels from '../src/i18n/locales/zh/channels.js'

const channelModelItemFields = [
  'model_id',
  'usage',
  'protocol',
  'image_understanding',
  'audio_understanding',
  'video_understanding',
  'context_window_k',
  'temperature',
  'top_p',
  'max_tokens',
  'embedding_dimensions',
  'size',
  'quality',
  'embedding_timeout',
  'rerank_timeout',
  'is_enabled',
  'description',
  'advanced_settings'
].sort()

const componentSource = readFileSync(new URL('../src/components/ChannelModelEntry.vue', import.meta.url), 'utf8')

test('defaultModelEntry covers every ChannelModelItem field and its defaults', () => {
  const entry = defaultModelEntry()

  assert.deepEqual(Object.keys(entry).sort(), channelModelItemFields)
  assert.deepEqual(entry, {
    model_id: '',
    usage: 'CHAT',
    protocol: 'OPENAI',
    image_understanding: false,
    audio_understanding: false,
    video_understanding: false,
    context_window_k: 4,
    temperature: 0.7,
    top_p: 1,
    max_tokens: 2048,
    embedding_dimensions: null,
    embedding_timeout: 30,
    rerank_timeout: 15,
    is_enabled: true,
    size: '1024x1024',
    quality: 'auto',
    advanced_settings: {},
    description: ''
  })
})

test('normalizeModelEntry fills historical defaults and clones advanced_settings', () => {
  const historicalAdvancedSettings = { reasoning_effort: 'high' }
  const historicalEntry = {
    model_id: 'legacy-model',
    usage: 'CHAT',
    protocol: 'OPENAI',
    temperature: 0.2,
    advanced_settings: historicalAdvancedSettings
  }

  const normalized = normalizeModelEntry(historicalEntry)

  assert.deepEqual(normalized, {
    ...defaultModelEntry(),
    ...historicalEntry,
    advanced_settings: { ...historicalAdvancedSettings }
  })
  assert.notStrictEqual(normalized.advanced_settings, historicalAdvancedSettings)

  normalized.advanced_settings.reasoning_effort = 'low'
  assert.equal(historicalAdvancedSettings.reasoning_effort, 'high')
})

test('ChannelModelEntry contains enabled and usage-specific timeout controls', () => {
  assert.match(componentSource, /v-if="props\.showEnabled"[\s\S]*?<el-switch v-model="props\.entry\.is_enabled"/)
  assert.match(componentSource, /channels\.embedding_timeout[\s\S]*?v-model="props\.entry\.embedding_timeout"/)
  assert.match(componentSource, /channels\.rerank_timeout[\s\S]*?v-model="props\.entry\.rerank_timeout"/)
})

test('ChannelModelEntry keeps remove and enabled props enabled by default', () => {
  assert.match(componentSource, /showRemove:\s*\{\s*type:\s*Boolean,\s*default:\s*true\s*\}/)
  assert.match(componentSource, /showEnabled:\s*\{\s*type:\s*Boolean,\s*default:\s*true\s*\}/)
})

test('ChannelModelEntry exposes test result viewing from the header actions', () => {
  assert.match(componentSource, /\$t\('channels\.model_test_view_result'\)/)
  assert.match(componentSource, /const emit = defineEmits\([\s\S]*?'view-test-result'/)
  assert.match(componentSource, /@click="emit\('view-test-result'\)"/)
})

test('ChannelModelEntry no longer hides test results in a test collapse section', () => {
  for (const legacyMarker of [
    'model-entry-collapse--test',
    'testResultExpanded',
    'update:test-result-expanded'
  ]) {
    assert.equal(componentSource.includes(legacyMarker), false, `legacy marker remains: ${legacyMarker}`)
  }
})

test('new channel model labels exist in both locales', () => {
  for (const key of ['is_enabled', 'embedding_timeout', 'rerank_timeout', 'model_test_view_result']) {
    assert.equal(Object.prototype.hasOwnProperty.call(zhChannels, key), true, `missing zh key: ${key}`)
    assert.equal(Object.prototype.hasOwnProperty.call(enChannels, key), true, `missing en key: ${key}`)
    assert.equal(typeof zhChannels[key], 'string')
    assert.equal(typeof enChannels[key], 'string')
    assert.notEqual(zhChannels[key].trim(), '')
    assert.notEqual(enChannels[key].trim(), '')
  }
})
