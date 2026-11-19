import assert from 'node:assert/strict'

import {
  mergeLlmRequestMetadata,
  normalizeLlmRequestMetadata
} from '../../../src/composables/chat/llmRequestMetadata.js'

export const requestLimits = {
  context_window_tokens: 1000000,
  max_output_tokens: 2048
}

export const contextIdentity = {
  channel_id: 7,
  model_id: 'model-a',
  protocol: 'responses',
  context_summary_revision: 2,
  context_content_revision: 4
}

export const reduceLlmRequestMetadata = (currentMetadata, incomingMetadata) => {
  const normalizedMetadata = normalizeLlmRequestMetadata(incomingMetadata)
  assert.ok(normalizedMetadata)
  return mergeLlmRequestMetadata(currentMetadata, normalizedMetadata)
}
