import assert from 'node:assert/strict'
import test from 'node:test'

import {
  formatTokenCount,
  getContextSummaryProgress,
  getSessionCacheHitRate
} from '../../../src/composables/chat/requestUsagePresentation.js'

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
