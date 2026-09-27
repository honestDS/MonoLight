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
