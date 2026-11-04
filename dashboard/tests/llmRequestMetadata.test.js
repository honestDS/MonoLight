import assert from 'node:assert/strict'
import test from 'node:test'

import {
  invalidateLlmRequestInput,
  isMainDialogueRequestMetadata,
  mergeLlmRequestMetadata,
  normalizeLlmRequestMetadata,
  shouldReplaceLlmRequestMetadata
} from '../src/composables/chat/llmRequestMetadata.js'
import {
  getSessionCacheHitRate,
  getContextSummaryProgress
} from '../src/composables/chat/requestUsagePresentation.js'

const requestLimits = {
  context_window_tokens: 1000000,
  max_output_tokens: 2048
}

const contextIdentity = {
  channel_id: 7,
  model_id: 'model-a',
  protocol: 'responses',
  context_summary_revision: 2,
  context_content_revision: 4
}

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

test('estimated updates reuse confirmed input only for the complete matching context identity', () => {
  const confirmed = {
    input_tokens: 300000,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
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

  assert.equal(merged.input_tokens, 300000)
  assert.equal(merged.input_tokens_source, 'provider')
  assert.equal(merged.total_output_tokens, 12)
})

test('estimated updates do not reuse confirmed input when context identity changes', () => {
  const confirmed = {
    input_tokens: 300000,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
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

test('session cache hit rate uses cumulative totals and rejects invalid values', () => {
  for (const recentRequest of [
    { input_tokens: 120, cached_tokens: 12 },
    { input_tokens: 800000, cached_tokens: 0 }
  ]) {
    assert.equal(
      getSessionCacheHitRate({
        ...recentRequest,
        total_input_tokens: 1000000,
        total_cached_tokens: 600000
      }),
      0.6
    )
  }
  assert.equal(
    getSessionCacheHitRate({
      input_tokens: 80,
      cached_tokens: 0,
      total_input_tokens: 100,
      total_cached_tokens: 0
    }),
    0
  )
  assert.equal(getSessionCacheHitRate({ total_input_tokens: 0, total_cached_tokens: 0 }), null)
  assert.equal(getSessionCacheHitRate({ total_input_tokens: 100 }), null)
  assert.equal(
    getSessionCacheHitRate({ total_input_tokens: 100, total_cached_tokens: 101 }),
    null
  )

  for (const field of ['total_input_tokens', 'total_cached_tokens']) {
    for (const value of [-1, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      assert.equal(
        getSessionCacheHitRate({
          total_input_tokens: 100,
          total_cached_tokens: 25,
          [field]: value
        }),
        null
      )
    }
  }
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

test('context summary progress uses only the latest confirmed input, not cumulative input', () => {
  const progress = getContextSummaryProgress({
    input_tokens: 42,
    input_tokens_source: 'provider',
    summary_trigger_tokens: 100,
    total_input_tokens: 900000
  })

  assert.deepEqual(progress, {
    status: 'ready',
    inputTokens: 42,
    triggerTokens: 100,
    ratio: 0.42,
    remainingRatio: 0.58,
    remainingTokens: 58,
    nearThreshold: false
  })
})

test('context summary progress reaches the threshold at and above the trigger', () => {
  for (const inputTokens of [100, 140]) {
    const progress = getContextSummaryProgress({
      input_tokens: inputTokens,
      input_tokens_source: 'provider',
      summary_trigger_tokens: 100
    })

    assert.equal(progress.status, 'threshold_reached')
    assert.equal(progress.inputTokens, inputTokens)
    assert.equal(progress.ratio, 1)
    assert.equal(progress.remainingRatio, 0)
    assert.equal(progress.remainingTokens, 0)
    assert.equal(progress.nearThreshold, true)
  }
})

test('context summary progress reports summarizing and waits without upstream input', () => {
  const metadata = {
    input_tokens: 40,
    input_tokens_source: 'provider',
    summary_trigger_tokens: 100
  }

  assert.deepEqual(getContextSummaryProgress(metadata, true), {
    status: 'summarizing',
    inputTokens: null,
    triggerTokens: null,
    ratio: null,
    remainingRatio: null,
    remainingTokens: null,
    nearThreshold: null
  })
  assert.deepEqual(getContextSummaryProgress({
    input_tokens: null,
    input_tokens_source: 'estimated',
    summary_trigger_tokens: 100
  }), {
    status: 'waiting',
    inputTokens: null,
    triggerTokens: null,
    ratio: null,
    remainingRatio: null,
    remainingTokens: null,
    nearThreshold: null
  })
})

test('context summary invalidation keeps cumulative usage and rejects duplicate or old input events', () => {
  const confirmed = {
    input_tokens: 80,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 100,
    work_sequence_no: 3,
    event_sequence_no: 10,
    total_input_tokens: 500,
    total_cached_tokens: 120,
    total_output_tokens: 40,
    cache_hit_rate: 120 / 500
  }

  const invalidated = invalidateLlmRequestInput(confirmed, {
    work_sequence_no: 3,
    event_sequence_no: 11
  })

  assert.equal(invalidated.input_tokens, null)
  assert.equal(invalidated.input_tokens_source, 'estimated')
  assert.equal(invalidated.event_sequence_no, 11)
  assert.equal(invalidated.total_input_tokens, 500)
  assert.equal(invalidated.total_cached_tokens, 120)
  assert.equal(invalidated.total_output_tokens, 40)
  assert.equal(invalidated.cache_hit_rate, 120 / 500)
  assert.equal(getContextSummaryProgress(invalidated).status, 'waiting')

  const duplicate = invalidateLlmRequestInput(invalidated, {
    work_sequence_no: 3,
    event_sequence_no: 11
  })
  assert.equal(duplicate.input_tokens, null)
  assert.equal(duplicate.input_tokens_source, 'estimated')

  const oldEvent = invalidateLlmRequestInput(duplicate, {
    work_sequence_no: 3,
    event_sequence_no: 10
  })
  assert.equal(oldEvent, duplicate)

  const staleConfirmation = mergeLlmRequestMetadata(duplicate, {
    ...confirmed,
    event_sequence_no: 10
  })
  assert.equal(staleConfirmation.input_tokens, null)
  assert.equal(staleConfirmation.input_tokens_source, 'estimated')
  assert.equal(staleConfirmation.event_sequence_no, 11)
  assert.equal(getContextSummaryProgress(staleConfirmation).status, 'waiting')

  const freshConfirmation = mergeLlmRequestMetadata(staleConfirmation, {
    ...confirmed,
    input_tokens: 90,
    event_sequence_no: 12
  })
  assert.equal(freshConfirmation.input_tokens, 90)
  assert.equal(freshConfirmation.input_tokens_source, 'provider')
  assert.equal(freshConfirmation.event_sequence_no, 12)
  assert.equal(freshConfirmation.total_input_tokens, 500)
  assert.equal(freshConfirmation.cache_hit_rate, 120 / 500)
  assert.equal(getContextSummaryProgress(freshConfirmation).status, 'ready')
  assert.equal(getContextSummaryProgress(freshConfirmation).ratio, 0.9)
})
