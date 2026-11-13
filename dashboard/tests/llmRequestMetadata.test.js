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
  formatTokenCount,
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

const reduceLlmRequestMetadata = (currentMetadata, incomingMetadata) => {
  const normalizedMetadata = normalizeLlmRequestMetadata(incomingMetadata)
  assert.ok(normalizedMetadata)
  return mergeLlmRequestMetadata(currentMetadata, normalizedMetadata)
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
  assert.equal(getContextSummaryProgress(invalidated).status, 'previous')
  assert.deepEqual(getContextSummaryProgress(invalidated), {
    status: 'previous',
    inputTokens: 80,
    triggerTokens: 100,
    ratio: 0.8,
    remainingRatio: 0.2,
    remainingTokens: 20,
    nearThreshold: false
  })

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
  assert.deepEqual(getContextSummaryProgress(staleConfirmation), {
    status: 'previous',
    inputTokens: 80,
    triggerTokens: 100,
    ratio: 0.8,
    remainingRatio: 0.2,
    remainingTokens: 20,
    nearThreshold: false
  })

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

test('reducer-to-presentation keeps original usage across identity and threshold changes', () => {
  const confirmed = reduceLlmRequestMetadata(null, {
    input_tokens: 80,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 1
  })

  const estimated = reduceLlmRequestMetadata(confirmed, {
    input_tokens: null,
    input_tokens_source: 'estimated',
    ...requestLimits,
    ...contextIdentity,
    channel_id: 8,
    model_id: 'model-b',
    summary_trigger_tokens: 200,
    work_sequence_no: 1,
    event_sequence_no: 2
  })

  assert.equal(estimated.input_tokens, null)
  assert.equal(estimated.input_tokens_source, 'estimated')
  assert.equal(estimated.channel_id, 8)
  assert.equal(estimated.model_id, 'model-b')
  assert.equal(estimated.summary_trigger_tokens, 200)
  assert.deepEqual(getContextSummaryProgress(estimated), {
    status: 'previous',
    inputTokens: 80,
    triggerTokens: 100,
    ratio: 0.8,
    remainingRatio: 0.2,
    remainingTokens: 20,
    nearThreshold: false
  })
})

test('reducer-to-presentation preserves the snapshot through estimated refreshes, totals-only updates, and old events', () => {
  let metadata = reduceLlmRequestMetadata(null, {
    input_tokens: 80,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 100,
    work_sequence_no: 2,
    event_sequence_no: 1,
    total_input_tokens: 100,
    total_cached_tokens: 20,
    total_output_tokens: 10
  })
  const confirmedSnapshot = {
    input_tokens: 80,
    input_tokens_source: 'provider',
    summary_trigger_tokens: 100,
    ...contextIdentity
  }
  const expectedProgress = {
    status: 'previous',
    inputTokens: 80,
    triggerTokens: 100,
    ratio: 0.8,
    remainingRatio: 0.2,
    remainingTokens: 20,
    nearThreshold: false
  }

  const updates = [
    {
      input_tokens: null,
      input_tokens_source: 'estimated',
      ...requestLimits,
      ...contextIdentity,
      summary_trigger_tokens: 200,
      work_sequence_no: 2,
      event_sequence_no: 2,
      total_input_tokens: 110,
      total_cached_tokens: 22,
      total_output_tokens: 12
    },
    {
      input_tokens: null,
      input_tokens_source: 'estimated',
      ...requestLimits,
      ...contextIdentity,
      summary_trigger_tokens: 300,
      work_sequence_no: 2,
      event_sequence_no: 2,
      total_input_tokens: 120,
      total_cached_tokens: 24,
      total_output_tokens: 14
    },
    {
      total_input_tokens: 130,
      total_cached_tokens: 26,
      total_output_tokens: 16
    },
    {
      input_tokens: null,
      input_tokens_source: 'estimated',
      ...requestLimits,
      ...contextIdentity,
      summary_trigger_tokens: 400,
      work_sequence_no: 2,
      event_sequence_no: 1,
      total_input_tokens: 125,
      total_cached_tokens: 25,
      total_output_tokens: 15
    }
  ]

  for (const update of updates) {
    metadata = reduceLlmRequestMetadata(metadata, update)
    assert.equal(metadata.input_tokens, null)
    assert.equal(metadata.input_tokens_source, 'estimated')
    assert.equal(metadata.work_sequence_no, 2)
    assert.equal(metadata.event_sequence_no, 2)
    assert.deepEqual(metadata.last_confirmed_context_usage, confirmedSnapshot)
    assert.deepEqual(getContextSummaryProgress(metadata), expectedProgress)
  }
})

test('reducer-to-presentation retains failed and same-version summaries, clears newer versions, and recovers only with new provider usage', () => {
  const confirmed = reduceLlmRequestMetadata(null, {
    input_tokens: 80,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 1
  })
  const previousProgress = {
    status: 'previous',
    inputTokens: 80,
    triggerTokens: 100,
    ratio: 0.8,
    remainingRatio: 0.2,
    remainingTokens: 20,
    nearThreshold: false
  }

  let metadata = invalidateLlmRequestInput(confirmed, {
    work_sequence_no: 1,
    event_sequence_no: 2,
    summary_status: 'failed'
  })
  assert.deepEqual(getContextSummaryProgress(metadata), previousProgress)

  metadata = invalidateLlmRequestInput(metadata, {
    work_sequence_no: 1,
    event_sequence_no: 3,
    context_summary_revision: contextIdentity.context_summary_revision,
    context_content_revision: contextIdentity.context_content_revision
  })
  assert.deepEqual(getContextSummaryProgress(metadata), previousProgress)

  metadata = invalidateLlmRequestInput(metadata, {
    work_sequence_no: 1,
    event_sequence_no: 4,
    context_summary_revision: 3,
    context_content_revision: 5
  })
  assert.equal(metadata.last_confirmed_context_usage, undefined)
  assert.equal(getContextSummaryProgress(metadata).status, 'waiting')

  const oldConfirmation = reduceLlmRequestMetadata(metadata, {
    input_tokens: 90,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 3
  })
  assert.equal(oldConfirmation.input_tokens, null)
  assert.equal(oldConfirmation.input_tokens_source, 'estimated')
  assert.equal(oldConfirmation.last_confirmed_context_usage, undefined)
  assert.equal(getContextSummaryProgress(oldConfirmation).status, 'waiting')

  const ready = reduceLlmRequestMetadata(oldConfirmation, {
    input_tokens: 80,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    context_summary_revision: 3,
    context_content_revision: 5,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 5
  })
  assert.equal(getContextSummaryProgress(ready).status, 'ready')
  assert.equal(getContextSummaryProgress(ready).inputTokens, 80)

  const thresholdReached = reduceLlmRequestMetadata(ready, {
    input_tokens: 120,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    context_summary_revision: 3,
    context_content_revision: 5,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 6
  })
  assert.equal(getContextSummaryProgress(thresholdReached).status, 'threshold_reached')
  assert.equal(getContextSummaryProgress(thresholdReached).inputTokens, 120)
})

test('reducer-to-presentation tolerates missing identity fields but rejects a missing snapshot revision', () => {
  const confirmed = reduceLlmRequestMetadata(null, {
    input_tokens: 80,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 1
  })

  const withoutChannel = reduceLlmRequestMetadata(confirmed, {
    input_tokens: null,
    input_tokens_source: 'estimated',
    ...requestLimits,
    model_id: 'model-a',
    protocol: 'responses',
    context_summary_revision: 2,
    context_content_revision: 4,
    summary_trigger_tokens: 200,
    work_sequence_no: 1,
    event_sequence_no: 2
  })
  assert.equal(withoutChannel.channel_id, undefined)
  assert.equal(withoutChannel.last_confirmed_context_usage.channel_id, 7)
  assert.deepEqual(getContextSummaryProgress(withoutChannel), {
    status: 'previous',
    inputTokens: 80,
    triggerTokens: 100,
    ratio: 0.8,
    remainingRatio: 0.2,
    remainingTokens: 20,
    nearThreshold: false
  })

  const identityWithoutSummaryRevision = {
    channel_id: contextIdentity.channel_id,
    model_id: contextIdentity.model_id,
    protocol: contextIdentity.protocol,
    context_content_revision: contextIdentity.context_content_revision
  }
  const confirmedWithoutRevision = reduceLlmRequestMetadata(null, {
    input_tokens: 80,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...identityWithoutSummaryRevision,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 1
  })
  const newerRevision = reduceLlmRequestMetadata(confirmedWithoutRevision, {
    input_tokens: null,
    input_tokens_source: 'estimated',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 200,
    work_sequence_no: 1,
    event_sequence_no: 2
  })
  assert.equal(newerRevision.last_confirmed_context_usage, undefined)
  assert.equal(getContextSummaryProgress(newerRevision).status, 'waiting')
})

test('reducer-to-presentation does not fabricate usage from missing input or thresholds and displays zero input', () => {
  assert.equal(
    normalizeLlmRequestMetadata({
      input_tokens_source: 'provider',
      ...requestLimits,
      summary_trigger_tokens: 100
    }),
    null
  )

  const estimatedWithoutInput = reduceLlmRequestMetadata(null, {
    input_tokens: null,
    input_tokens_source: 'estimated',
    ...requestLimits,
    summary_trigger_tokens: 100
  })
  assert.equal(getContextSummaryProgress(estimatedWithoutInput).status, 'waiting')

  const providerWithoutThreshold = reduceLlmRequestMetadata(null, {
    input_tokens: 20,
    input_tokens_source: 'provider',
    ...requestLimits
  })
  assert.deepEqual(getContextSummaryProgress(providerWithoutThreshold), {
    status: 'waiting',
    inputTokens: null,
    triggerTokens: null,
    ratio: null,
    remainingRatio: null,
    remainingTokens: null,
    nearThreshold: null
  })
  assert.equal(
    normalizeLlmRequestMetadata({
      input_tokens: 20,
      input_tokens_source: 'provider',
      ...requestLimits,
      summary_trigger_tokens: 0
    }),
    null
  )

  const zeroInput = reduceLlmRequestMetadata(null, {
    input_tokens: 0,
    input_tokens_source: 'provider',
    ...requestLimits,
    summary_trigger_tokens: 100
  })
  assert.deepEqual(getContextSummaryProgress(zeroInput), {
    status: 'ready',
    inputTokens: 0,
    triggerTokens: 100,
    ratio: 0,
    remainingRatio: 1,
    remainingTokens: 100,
    nearThreshold: false
  })
})

test('provider metadata without a threshold falls back to the complete confirmed usage record', () => {
  const confirmed = reduceLlmRequestMetadata(null, {
    input_tokens: 42,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    summary_trigger_tokens: 100,
    work_sequence_no: 1,
    event_sequence_no: 1
  })
  const providerWithoutThreshold = reduceLlmRequestMetadata(confirmed, {
    input_tokens: 84,
    input_tokens_source: 'provider',
    ...requestLimits,
    ...contextIdentity,
    work_sequence_no: 1,
    event_sequence_no: 2
  })

  assert.equal(providerWithoutThreshold.input_tokens, 84)
  assert.equal(providerWithoutThreshold.input_tokens_source, 'provider')
  assert.deepEqual(getContextSummaryProgress(providerWithoutThreshold), {
    status: 'previous',
    inputTokens: 42,
    triggerTokens: 100,
    ratio: 0.42,
    remainingRatio: 0.58,
    remainingTokens: 58,
    nearThreshold: false
  })
})

test('formats token counts with decimal compact units and fixed precision', () => {
  const cases = [
    [0, '0.00'],
    [801, '801.00'],
    [999, '999.00'],
    [1000, '1.00K'],
    [1024, '1.02K'],
    [20480, '20.48K'],
    [81520, '81.52K'],
    [1e6, '1.00M'],
    [1048000, '1.05M'],
    [1e9, '1.00B'],
    [1234567890, '1.23B']
  ]

  for (const [value, expected] of cases) {
    assert.equal(formatTokenCount(value), expected)
  }
})

test('rounding promotes token counts at compact unit boundaries', () => {
  const cases = [
    [999994, '999.99K'],
    [999995, '1.00M'],
    [999994999, '999.99M'],
    [999995000, '1.00B']
  ]

  for (const [value, expected] of cases) {
    assert.equal(formatTokenCount(value), expected)
  }
})

test('formats unknown token values as a placeholder', () => {
  for (const value of [
    null,
    undefined,
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
    '1000',
    true,
    false
  ]) {
    assert.equal(formatTokenCount(value), '-')
  }
})

test('formats the screenshot metadata while preserving raw summary progress values', () => {
  const metadata = Object.freeze({
    input_tokens: 23382,
    input_tokens_source: 'provider',
    summary_trigger_tokens: 513632,
    total_input_tokens: 81520,
    total_output_tokens: 801,
    context_window_tokens: 1048000,
    max_output_tokens: 20480
  })

  const progress = getContextSummaryProgress(metadata)
  const displayedCounts = [
    metadata.total_input_tokens,
    metadata.total_output_tokens,
    metadata.context_window_tokens,
    metadata.max_output_tokens,
    progress.inputTokens,
    progress.triggerTokens,
    progress.remainingTokens
  ].map(formatTokenCount)

  assert.deepEqual(displayedCounts, [
    '81.52K',
    '801.00',
    '1.05M',
    '20.48K',
    '23.38K',
    '513.63K',
    '490.25K'
  ])
  assert.equal(progress.inputTokens, 23382)
  assert.equal(progress.triggerTokens, 513632)
  assert.equal(progress.remainingTokens, 490250)
  assert.equal(progress.ratio, 23382 / 513632)
  assert.equal(progress.status, 'ready')
  assert.deepEqual(metadata, {
    input_tokens: 23382,
    input_tokens_source: 'provider',
    summary_trigger_tokens: 513632,
    total_input_tokens: 81520,
    total_output_tokens: 801,
    context_window_tokens: 1048000,
    max_output_tokens: 20480
  })
})
