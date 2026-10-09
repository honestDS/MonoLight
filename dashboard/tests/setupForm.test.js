import assert from 'node:assert/strict'
import test from 'node:test'

import { defaultModelEntry } from '../src/constants/index.js'
import {
  addSetupDetectedModels,
  buildSetupRequest
} from '../src/utils/setupForm.js'

const createModel = (overrides = {}) => ({
  ...defaultModelEntry(),
  ...overrides
})

test('adds detected models in order and only reports entries it filled or added', () => {
  const manual = createModel({
    model_id: 'manual-model',
    protocol: 'OPENAI_RESPONSES',
    temperature: 0.15,
    advanced_settings: { source: 'manual' }
  })
  const empty = createModel({
    protocol: 'OPENAI_RESPONSES',
    temperature: 0.25,
    advanced_settings: { source: 'empty' }
  })
  const manualBefore = structuredClone(manual)
  const entries = [manual, empty]

  const touched = addSetupDetectedModels(entries, [' model-a ', 'model-b'])

  assert.deepEqual(entries.map(entry => entry.model_id), [
    'manual-model',
    'model-a',
    'model-b'
  ])
  assert.strictEqual(touched[0], empty)
  assert.strictEqual(touched[1], entries[2])
  assert.equal(touched.includes(manual), false)
  assert.equal(empty.protocol, 'OPENAI_RESPONSES')
  assert.equal(empty.temperature, 0.25)
  assert.deepEqual(empty.advanced_settings, { source: 'empty' })
  assert.deepEqual(manual, manualBefore)
})

test('creates independent default model entries for multiple detected models', () => {
  const entries = []

  const touched = addSetupDetectedModels(entries, ['first-model', 'second-model'])

  assert.deepEqual(touched.map(entry => entry.model_id), ['first-model', 'second-model'])
  assert.notStrictEqual(entries[0].reasoning_efforts, entries[1].reasoning_efforts)
  assert.notStrictEqual(entries[0].advanced_settings, entries[1].advanced_settings)

  entries[0].reasoning_efforts.push('low')
  entries[0].advanced_settings.source = 'first'
  assert.deepEqual(entries[1].reasoning_efforts, [])
  assert.deepEqual(entries[1].advanced_settings, {})
})

test('deduplicates trimmed ids, is idempotent, and ignores invalid input', () => {
  const entries = [createModel({ model_id: ' existing-model ' })]

  const touched = addSetupDetectedModels(entries, [
    ' existing-model ',
    ' first-model ',
    'first-model',
    '',
    '   ',
    null,
    42,
    'second-model'
  ])

  assert.deepEqual(entries.map(entry => entry.model_id), [
    ' existing-model ',
    'first-model',
    'second-model'
  ])
  assert.deepEqual(touched.map(entry => entry.model_id), ['first-model', 'second-model'])

  const afterFirstSelection = structuredClone(entries)
  assert.deepEqual(addSetupDetectedModels(entries, [' first-model ', 'second-model']), [])
  assert.deepEqual(addSetupDetectedModels(entries, []), [])
  assert.deepEqual(entries, afterFirstSelection)

  for (const values of [null, undefined, {}, '', [null, undefined, 0, '', '  ']]) {
    const before = structuredClone(entries)
    assert.deepEqual(addSetupDetectedModels(entries, values), [])
    assert.deepEqual(entries, before)
  }
  assert.deepEqual(addSetupDetectedModels(null, ['new-model']), [])
  assert.deepEqual(addSetupDetectedModels({}, ['new-model']), [])
})

test('does not delete existing entries and re-adds a removed id without changing survivors', () => {
  const first = createModel({ model_id: 'first-model' })
  const second = createModel({
    model_id: 'second-model',
    protocol: 'OPENAI_RESPONSES',
    temperature: 0.3,
    advanced_settings: { preserved: true }
  })
  const entries = [first, second]
  const before = structuredClone(entries)

  assert.deepEqual(addSetupDetectedModels(entries, ['first-model']), [])
  assert.deepEqual(addSetupDetectedModels(entries, []), [])
  assert.deepEqual(entries, before)

  entries.splice(0, 1)
  const touched = addSetupDetectedModels(entries, [' first-model '])

  assert.deepEqual(entries.map(entry => entry.model_id), ['second-model', 'first-model'])
  assert.strictEqual(touched.length, 1)
  assert.strictEqual(touched[0], entries[1])
  assert.deepEqual(entries[0], before[1])
})

test('builds an independent complete request for each configured model', () => {
  const first = createModel({
    model_id: '  model-a  ',
    protocol: 'OPENAI_RESPONSES',
    image_understanding: true,
    audio_understanding: true,
    video_understanding: false,
    context_window_k: 128,
    temperature: 0.15,
    top_p: 0.82,
    reasoning_efforts: ['low', 'high'],
    reasoning_effort: 'legacy-a',
    max_tokens: 4096,
    description: 'first model description',
    advanced_settings: { budget: 900, headers: { source: 'first' } },
    usage: 'CHAT',
    embedding_dimensions: 1536,
    embedding_timeout: 30,
    is_enabled: false,
    size: '1024x1024',
    quality: 'hd'
  })
  const second = createModel({
    model_id: ' model-b ',
    protocol: 'OPENAI',
    image_understanding: false,
    audio_understanding: false,
    video_understanding: true,
    context_window_k: 64,
    temperature: 0.65,
    top_p: 0.97,
    reasoning_efforts: ['medium', 'xhigh'],
    reasoning_effort: 'legacy-b',
    max_tokens: 2048,
    description: 'second model description',
    advanced_settings: { cache: false },
    usage: 'IMAGE_GENERATION',
    embedding_dimensions: 768,
    rerank_timeout: 15,
    size: '512x512',
    quality: 'standard'
  })
  const form = {
    admin: {
      username: '  admin-user  ',
      password: ' pass word '
    },
    channel: {
      name: '  Main channel  ',
      base_url: ' https://example.test/v1  ',
      api_key: '  key with spaces  ',
      http_proxy: ' http://proxy.test:8080/ ',
      model_id: 'flat-model-should-not-be-used',
      model_ids: [first, second]
    },
    profile: { name: '  Main profile  ' },
    ui_only: true
  }
  const before = structuredClone(form)

  const request = buildSetupRequest(form)

  assert.deepEqual(request.admin, {
    username: 'admin-user',
    password: ' pass word '
  })
  assert.equal(request.channel.name, 'Main channel')
  assert.equal(request.channel.base_url, 'https://example.test/v1')
  assert.equal(request.channel.api_key, '  key with spaces  ')
  assert.equal(request.channel.http_proxy, 'http://proxy.test:8080/')
  assert.deepEqual(request.profile, { name: 'Main profile' })
  assert.deepEqual(request.channel.model_ids, [
    {
      model_id: 'model-a',
      protocol: 'OPENAI_RESPONSES',
      image_understanding: true,
      audio_understanding: true,
      video_understanding: false,
      context_window_k: 128,
      temperature: 0.15,
      top_p: 0.82,
      reasoning_efforts: ['low', 'high'],
      max_tokens: 4096,
      description: 'first model description',
      advanced_settings: { budget: 900, headers: { source: 'first' } }
    },
    {
      model_id: 'model-b',
      protocol: 'OPENAI',
      image_understanding: false,
      audio_understanding: false,
      video_understanding: true,
      context_window_k: 64,
      temperature: 0.65,
      top_p: 0.97,
      reasoning_efforts: ['medium', 'xhigh'],
      max_tokens: 2048,
      description: 'second model description',
      advanced_settings: { cache: false }
    }
  ])

  const serializedKeys = [
    'model_id',
    'protocol',
    'image_understanding',
    'audio_understanding',
    'video_understanding',
    'context_window_k',
    'temperature',
    'top_p',
    'reasoning_efforts',
    'max_tokens',
    'description',
    'advanced_settings'
  ]
  for (const model of request.channel.model_ids) {
    assert.deepEqual(Object.keys(model).sort(), [...serializedKeys].sort())
    assert.equal(Object.hasOwn(model, 'reasoning_effort'), false)
    assert.equal(Object.hasOwn(model, 'size'), false)
    assert.equal(Object.hasOwn(model, 'quality'), false)
  }
  assert.notStrictEqual(request.channel.model_ids[0].advanced_settings, first.advanced_settings)
  assert.notStrictEqual(request.channel.model_ids[1].advanced_settings, second.advanced_settings)
  assert.deepEqual(form, before)
  assert.deepEqual(buildSetupRequest(form), request)
  assert.deepEqual(form, before)
})

test('returns no models when model_ids is missing, non-array, or empty', () => {
  const cases = [
    ['missing', { model_id: 'flat-model' }],
    ['null', { model_ids: null, model_id: 'flat-model' }],
    ['object', { model_ids: { model_id: 'flat-model' } }],
    ['string', { model_ids: 'flat-model' }],
    ['empty', { model_ids: [] }]
  ]

  for (const [name, channel] of cases) {
    assert.deepEqual(
      buildSetupRequest({ channel }).channel.model_ids,
      [],
      `${name} model_ids should not fall back to flat fields`
    )
  }
})
