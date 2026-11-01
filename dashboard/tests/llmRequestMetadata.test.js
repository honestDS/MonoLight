import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isMainDialogueRequestMetadata,
  mergeLlmRequestMetadata,
  normalizeLlmRequestMetadata,
  shouldReplaceLlmRequestMetadata
} from '../src/composables/chat/llmRequestMetadata.js'

test('estimated input metadata is normalized without a displayable input token value', () => {
  const metadata = normalizeLlmRequestMetadata({
    input_tokens: 500000,
    input_tokens_source: 'estimated',
    request_purpose: 'main_dialogue',
    context_window_tokens: 1000000,
    max_output_tokens: 2048
  })

  assert.equal(metadata.input_tokens, null)
  assert.equal(metadata.input_tokens_source, 'estimated')
  assert.equal(metadata.request_purpose, 'main_dialogue')
})

test('provider metadata keeps the confirmed input token value', () => {
  const metadata = normalizeLlmRequestMetadata({
    input_tokens: 300000,
    input_tokens_source: 'provider',
    request_purpose: 'main_dialogue',
    context_window_tokens: 1000000,
    max_output_tokens: 2048
  })

  assert.equal(metadata.input_tokens, 300000)
  assert.equal(metadata.input_tokens_source, 'provider')
  assert.equal(isMainDialogueRequestMetadata(metadata), true)
})

test('auxiliary request metadata is excluded from the main dialogue display', () => {
  const metadata = normalizeLlmRequestMetadata({
    input_tokens: 900000,
    input_tokens_source: 'provider',
    request_purpose: 'memory_recall',
    context_window_tokens: 1000000,
    max_output_tokens: 2048
  })

  assert.equal(isMainDialogueRequestMetadata(metadata), false)
})

test('estimated updates preserve the last confirmed provider input tokens', () => {
  const merged = mergeLlmRequestMetadata(
    {
      input_tokens: 300000,
      input_tokens_source: 'provider',
      total_output_tokens: 12
    },
    {
      input_tokens: null,
      input_tokens_source: 'estimated',
      context_window_tokens: 1000000,
      max_output_tokens: 2048
    }
  )

  assert.equal(merged.input_tokens, 300000)
  assert.equal(merged.input_tokens_source, 'provider')
  assert.equal(merged.total_output_tokens, 12)
})

test('metadata replacement follows work and event sequence ordering', () => {
  assert.equal(
    shouldReplaceLlmRequestMetadata(
      { work_sequence_no: 3, event_sequence_no: 4 },
      { work_sequence_no: 3, event_sequence_no: 3 }
    ),
    false
  )
  assert.equal(
    shouldReplaceLlmRequestMetadata(
      { work_sequence_no: 3, event_sequence_no: 4 },
      { work_sequence_no: 4, event_sequence_no: 1 }
    ),
    true
  )
})

test('normalizes cumulative totals separately from per-request input tokens', () => {
  const metadata = normalizeLlmRequestMetadata({
    input_tokens: 120,
    input_tokens_source: 'provider',
    total_input_tokens: 100120,
    total_cached_tokens: 0,
    total_output_tokens: 7,
    cache_hit_rate: 0,
    context_window_tokens: 1000000,
    max_output_tokens: 2048
  })

  assert.equal(metadata.input_tokens, 120)
  assert.equal(metadata.total_input_tokens, 100120)
  assert.equal(metadata.total_cached_tokens, 0)
  assert.equal(metadata.total_output_tokens, 7)
  assert.equal(metadata.cache_hit_rate, 0)
})

test('rejects invalid cumulative totals and impossible cache totals', () => {
  const validMetadata = {
    input_tokens: 120,
    input_tokens_source: 'provider',
    context_window_tokens: 4096,
    max_output_tokens: 512,
    total_input_tokens: 1000,
    total_cached_tokens: 250,
    total_output_tokens: 200
  }

  for (const field of ['total_input_tokens', 'total_cached_tokens', 'total_output_tokens']) {
    for (const value of [-1, Number.NaN, true]) {
      assert.equal(normalizeLlmRequestMetadata({ ...validMetadata, [field]: value }), null)
    }
  }

  assert.equal(
    normalizeLlmRequestMetadata({ ...validMetadata, total_cached_tokens: 1001 }),
    null
  )
})

test('normalizes totals-only persistent metadata without fabricated request fields', () => {
  const metadata = normalizeLlmRequestMetadata({
    total_input_tokens: 0,
    total_cached_tokens: 0,
    total_output_tokens: 0,
    cache_hit_rate: 0
  })

  assert.equal(metadata.total_input_tokens, 0)
  assert.equal(metadata.total_cached_tokens, 0)
  assert.equal(metadata.total_output_tokens, 0)
  assert.equal(metadata.cache_hit_rate, 0)
  assert.equal(Object.prototype.hasOwnProperty.call(metadata, 'input_tokens'), false)
  assert.equal(Object.prototype.hasOwnProperty.call(metadata, 'context_window_tokens'), false)
  assert.equal(Object.prototype.hasOwnProperty.call(metadata, 'max_output_tokens'), false)
  assert.equal(normalizeLlmRequestMetadata({}), null)
  assert.equal(normalizeLlmRequestMetadata({ total_cached_tokens: 0 }), null)
  assert.equal(normalizeLlmRequestMetadata({ input_tokens: 120 }), null)
})

test('merge retains cumulative totals omitted by estimated updates, including zero', () => {
  const merged = mergeLlmRequestMetadata(
    {
      input_tokens: 120,
      input_tokens_source: 'provider',
      total_input_tokens: 100120,
      total_cached_tokens: 0,
      total_output_tokens: 0,
      cache_hit_rate: 0
    },
    {
      input_tokens: null,
      input_tokens_source: 'estimated',
      context_window_tokens: 1000000,
      max_output_tokens: 2048
    }
  )

  assert.equal(merged.total_input_tokens, 100120)
  assert.equal(merged.total_cached_tokens, 0)
  assert.equal(merged.total_output_tokens, 0)
  assert.equal(merged.cache_hit_rate, 0)
})

test('merge keeps latest request fields while folding cumulative maxima', () => {
  const current = {
    input_tokens: 120,
    input_tokens_source: 'provider',
    context_window_tokens: 1000,
    max_output_tokens: 256,
    work_sequence_no: 8,
    event_sequence_no: 4,
    total_input_tokens: 100,
    total_cached_tokens: 20,
    total_output_tokens: 30,
    cache_hit_rate: 0.2
  }
  const older = {
    input_tokens: 80,
    input_tokens_source: 'provider',
    context_window_tokens: 2000,
    max_output_tokens: 512,
    work_sequence_no: 8,
    event_sequence_no: 3,
    total_input_tokens: 150,
    total_cached_tokens: 30,
    total_output_tokens: 45,
    cache_hit_rate: 0.99
  }

  const merged = mergeLlmRequestMetadata(current, older)

  assert.equal(merged.input_tokens, 120)
  assert.equal(merged.context_window_tokens, 1000)
  assert.equal(merged.max_output_tokens, 256)
  assert.equal(merged.work_sequence_no, 8)
  assert.equal(merged.event_sequence_no, 4)
  assert.equal(merged.total_input_tokens, 150)
  assert.equal(merged.total_cached_tokens, 30)
  assert.equal(merged.total_output_tokens, 45)
  assert.equal(merged.cache_hit_rate, 30 / 150)

  const smallerSnapshot = mergeLlmRequestMetadata(merged, {
    input_tokens: 240,
    input_tokens_source: 'provider',
    context_window_tokens: 4000,
    max_output_tokens: 1024,
    work_sequence_no: 9,
    event_sequence_no: 1,
    total_input_tokens: 120,
    total_cached_tokens: 25,
    total_output_tokens: 40,
    cache_hit_rate: 0.99
  })

  assert.equal(smallerSnapshot.total_input_tokens, 150)
  assert.equal(smallerSnapshot.total_cached_tokens, 30)
  assert.equal(smallerSnapshot.total_output_tokens, 45)
  assert.equal(smallerSnapshot.cache_hit_rate, 30 / 150)
})

test('totals-only updates preserve request context and work order', () => {
  const merged = mergeLlmRequestMetadata(
    {
      input_tokens: 120,
      input_tokens_source: 'provider',
      context_window_tokens: 1000,
      max_output_tokens: 256,
      work_sequence_no: 8,
      event_sequence_no: 4,
      total_input_tokens: 100,
      total_cached_tokens: 20,
      total_output_tokens: 30,
      cache_hit_rate: 0.2
    },
    {
      total_input_tokens: 120,
      total_cached_tokens: 30,
      total_output_tokens: 60,
      cache_hit_rate: 0.99
    }
  )

  assert.equal(merged.input_tokens, 120)
  assert.equal(merged.input_tokens_source, 'provider')
  assert.equal(merged.context_window_tokens, 1000)
  assert.equal(merged.max_output_tokens, 256)
  assert.equal(merged.work_sequence_no, 8)
  assert.equal(merged.event_sequence_no, 4)
  assert.equal(merged.total_input_tokens, 120)
  assert.equal(merged.total_cached_tokens, 30)
  assert.equal(merged.total_output_tokens, 60)
  assert.equal(merged.cache_hit_rate, 30 / 120)
})
