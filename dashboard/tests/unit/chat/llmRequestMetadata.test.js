import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isMainDialogueRequestMetadata,
  mergeLlmRequestMetadata,
  normalizeLlmRequestMetadata,
  shouldReplaceLlmRequestMetadata
} from '../../../src/composables/chat/llmRequestMetadata.js'
import { getContextSummaryProgress } from '../../../src/composables/chat/requestUsagePresentation.js'
import { contextIdentity, requestLimits } from '../../support/chat/llmRequestMetadata.js'

test('estimated input metadata is normalized without a displayable input token value', () => {
  const metadata = normalizeLlmRequestMetadata({
    input_tokens: 500000,
    input_tokens_source: 'estimated',
    request_purpose: 'main_dialogue',
    ...requestLimits
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
    ...requestLimits
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
    ...requestLimits
  })

  assert.equal(isMainDialogueRequestMetadata(metadata), false)
})

test('estimated updates keep input unknown and preserve confirmed usage as a display snapshot', () => {
  const confirmed = {
    input_tokens: 300000,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 500000,
    work_sequence_no: 3,
    event_sequence_no: 4,
    total_output_tokens: 12
  }
  const estimated = {
    input_tokens: null,
    input_tokens_source: 'estimated',
    ...requestLimits,
    ...contextIdentity,
    work_sequence_no: 3,
    event_sequence_no: 5
  }

  const merged = mergeLlmRequestMetadata(confirmed, estimated)

  assert.equal(merged.input_tokens, null)
  assert.equal(merged.input_tokens_source, 'estimated')
  assert.equal(merged.total_output_tokens, 12)
  assert.deepEqual(merged.last_confirmed_context_usage, {
    input_tokens: 300000,
    input_tokens_source: 'provider',
    summary_trigger_tokens: 500000,
    ...contextIdentity
  })
  assert.deepEqual(getContextSummaryProgress(merged), {
    status: 'previous',
    inputTokens: 300000,
    triggerTokens: 500000,
    ratio: 0.6,
    remainingRatio: 0.4,
    remainingTokens: 200000,
    nearThreshold: false
  })
})

test('estimated updates keep input unknown and discard the display snapshot on newer revisions', () => {
  const confirmed = {
    input_tokens: 300000,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 500000,
    work_sequence_no: 3,
    event_sequence_no: 4
  }
  const estimated = {
    input_tokens: null,
    input_tokens_source: 'estimated',
    ...requestLimits,
    ...contextIdentity,
    work_sequence_no: 3,
    event_sequence_no: 5
  }

  const changedIdentity = [
    ['channel_id', 8],
    ['model_id', 'model-b'],
    ['protocol', 'chat_completions'],
    ['context_summary_revision', 3],
    ['context_content_revision', 5]
  ]

  for (const [field, value] of changedIdentity) {
    const merged = mergeLlmRequestMetadata(confirmed, { ...estimated, [field]: value })
    assert.equal(merged.input_tokens, null)
    assert.equal(merged.input_tokens_source, 'estimated')
    if (field === 'context_summary_revision' || field === 'context_content_revision') {
      assert.equal(merged.last_confirmed_context_usage, undefined)
      assert.equal(getContextSummaryProgress(merged).status, 'waiting')
    } else {
      assert.deepEqual(merged.last_confirmed_context_usage, {
        input_tokens: 300000,
        input_tokens_source: 'provider',
        summary_trigger_tokens: 500000,
        ...contextIdentity
      })
      assert.deepEqual(getContextSummaryProgress(merged), {
        status: 'previous',
        inputTokens: 300000,
        triggerTokens: 500000,
        ratio: 0.6,
        remainingRatio: 0.4,
        remainingTokens: 200000,
        nearThreshold: false
      })
    }
  }
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

test('normalizes cumulative totals and cache hit rate', () => {
  const metadata = normalizeLlmRequestMetadata({
    input_tokens: 120,
    input_tokens_source: 'provider',
    total_input_tokens: 100120,
    total_cached_tokens: 0,
    total_output_tokens: 7,
    cache_hit_rate: 0,
    ...requestLimits
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
    ...requestLimits,
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
    cache_hit_rate: null
  })

  assert.equal(metadata.total_input_tokens, 0)
  assert.equal(metadata.total_cached_tokens, 0)
  assert.equal(metadata.total_output_tokens, 0)
  assert.equal(metadata.cache_hit_rate, null)
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
      total_output_tokens: 0
    },
    {
      input_tokens: null,
      input_tokens_source: 'estimated',
      ...requestLimits
    }
  )

  assert.equal(merged.input_tokens, null)
  assert.equal(merged.input_tokens_source, 'estimated')
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
    ...contextIdentity,
    work_sequence_no: 8,
    event_sequence_no: 4,
    total_input_tokens: 100,
    total_cached_tokens: 20,
    total_output_tokens: 30
  }
  const older = {
    input_tokens: 80,
    input_tokens_source: 'provider',
    context_window_tokens: 2000,
    max_output_tokens: 512,
    ...contextIdentity,
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
      total_output_tokens: 30
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
