import { getSessionCacheHitRate } from './requestUsagePresentation.js'

export const MAIN_DIALOGUE_REQUEST_PURPOSE = 'main_dialogue'

const REQUEST_METADATA_FIELDS = [
  'input_tokens',
  'input_tokens_source',
  'context_window_tokens',
  'max_output_tokens'
]
const CUMULATIVE_TOTAL_FIELDS = [
  'total_input_tokens',
  'total_cached_tokens',
  'total_output_tokens'
]
const CONTEXT_IDENTITY_FIELDS = [
  'channel_id',
  'model_id',
  'protocol',
  'context_summary_revision',
  'context_content_revision'
]

const hasOwn = (value, field) => (
  value !== null && value !== undefined && Object.prototype.hasOwnProperty.call(value, field)
)

const isValidNonNegativeNumber = (value) => Number.isFinite(value) && value >= 0

const isTotalsOnlyLlmRequestMetadata = (metadata) => (
  (hasOwn(metadata, 'total_input_tokens') || hasOwn(metadata, 'total_output_tokens')) &&
  !REQUEST_METADATA_FIELDS.some((field) => hasOwn(metadata, field))
)

export const normalizeLlmRequestMetadata = (metadata) => {
  if (!metadata || typeof metadata !== 'object') return null

  const inputTokensSource = metadata?.input_tokens_source
  if (inputTokensSource !== undefined && !['estimated', 'provider'].includes(inputTokensSource)) return null

  const hasInputTokens = Number.isFinite(metadata?.input_tokens) && metadata.input_tokens >= 0
  const totalsOnly = isTotalsOnlyLlmRequestMetadata(metadata)
  if (!totalsOnly) {
    if (!Number.isFinite(metadata?.context_window_tokens) || metadata.context_window_tokens < 0) return null
    if (!Number.isFinite(metadata?.max_output_tokens) || metadata.max_output_tokens < 0) return null
    if (inputTokensSource !== 'estimated' && !hasInputTokens) return null
  }

  const normalizedMetadata = {}
  if (!totalsOnly) {
    normalizedMetadata.input_tokens = inputTokensSource === 'estimated' ? null : Math.trunc(metadata.input_tokens)
    normalizedMetadata.context_window_tokens = Math.trunc(metadata.context_window_tokens)
    normalizedMetadata.max_output_tokens = Math.trunc(metadata.max_output_tokens)
    if (inputTokensSource !== undefined) normalizedMetadata.input_tokens_source = inputTokensSource
  }
  if (Object.prototype.hasOwnProperty.call(metadata, 'request_purpose')) {
    if (typeof metadata.request_purpose !== 'string' || !metadata.request_purpose.trim()) return null
    normalizedMetadata.request_purpose = metadata.request_purpose.trim()
  }
  for (const field of ['output_tokens', 'cached_tokens', ...CUMULATIVE_TOTAL_FIELDS]) {
    if (!hasOwn(metadata, field)) continue
    if (!isValidNonNegativeNumber(metadata[field])) return null
    normalizedMetadata[field] = Math.trunc(metadata[field])
  }
  if (
    hasOwn(metadata, 'total_input_tokens') &&
    hasOwn(metadata, 'total_cached_tokens') &&
    metadata.total_cached_tokens > metadata.total_input_tokens
  ) return null
  if (hasOwn(metadata, 'cache_hit_rate')) {
    if (metadata.cache_hit_rate === null) {
      normalizedMetadata.cache_hit_rate = null
    } else {
      if (!Number.isFinite(metadata.cache_hit_rate) || metadata.cache_hit_rate < 0 || metadata.cache_hit_rate > 1) return null
      normalizedMetadata.cache_hit_rate = Number(metadata.cache_hit_rate)
    }
  }
  if (hasOwn(metadata, 'summary_trigger_tokens')) {
    if (
      metadata.summary_trigger_tokens !== null &&
      (!Number.isFinite(metadata.summary_trigger_tokens) ||
        !Number.isInteger(metadata.summary_trigger_tokens) ||
        metadata.summary_trigger_tokens <= 0)
    ) return null
    normalizedMetadata.summary_trigger_tokens = metadata.summary_trigger_tokens
  }
  if (hasOwn(metadata, 'channel_id')) {
    if (
      !Number.isFinite(metadata.channel_id) ||
      !Number.isInteger(metadata.channel_id) ||
      metadata.channel_id <= 0
    ) return null
    normalizedMetadata.channel_id = metadata.channel_id
  }
  for (const field of ['model_id', 'protocol']) {
    if (!hasOwn(metadata, field)) continue
    if (typeof metadata[field] !== 'string' || !metadata[field].trim()) return null
    normalizedMetadata[field] = metadata[field].trim()
  }
  for (const field of ['context_summary_revision', 'context_content_revision']) {
    if (!hasOwn(metadata, field)) continue
    if (
      !Number.isFinite(metadata[field]) ||
      !Number.isInteger(metadata[field]) ||
      metadata[field] < 0
    ) return null
    normalizedMetadata[field] = metadata[field]
  }
  if (Object.prototype.hasOwnProperty.call(metadata, 'response_id')) normalizedMetadata.response_id = metadata.response_id
  if (Object.prototype.hasOwnProperty.call(metadata, 'turn')) normalizedMetadata.turn = metadata.turn
  if (Object.prototype.hasOwnProperty.call(metadata, 'work_sequence_no')) {
    if (!Number.isFinite(metadata.work_sequence_no) || !Number.isInteger(metadata.work_sequence_no) || metadata.work_sequence_no <= 0) return null
    normalizedMetadata.work_sequence_no = metadata.work_sequence_no
  }
  if (Object.prototype.hasOwnProperty.call(metadata, 'event_sequence_no')) {
    if (!Number.isFinite(metadata.event_sequence_no) || !Number.isInteger(metadata.event_sequence_no) || metadata.event_sequence_no < 0) return null
    normalizedMetadata.event_sequence_no = metadata.event_sequence_no
  }
  return normalizedMetadata
}

export const shouldReplaceLlmRequestMetadata = (currentMetadata, incomingMetadata) => {
  if (!Object.prototype.hasOwnProperty.call(incomingMetadata, 'work_sequence_no')) return true
  if (!Object.prototype.hasOwnProperty.call(currentMetadata || {}, 'work_sequence_no')) return true
  if (incomingMetadata.work_sequence_no !== currentMetadata.work_sequence_no) {
    return incomingMetadata.work_sequence_no > currentMetadata.work_sequence_no
  }
  return (incomingMetadata.event_sequence_no ?? 0) >= (currentMetadata.event_sequence_no ?? 0)
}

export const isMainDialogueRequestMetadata = (metadata) => (
  !metadata?.request_purpose || metadata.request_purpose === MAIN_DIALOGUE_REQUEST_PURPOSE
)

const hasCompleteContextIdentity = (metadata) => {
  for (const field of CONTEXT_IDENTITY_FIELDS) {
    if (!hasOwn(metadata, field) || metadata[field] === null || metadata[field] === undefined) return false
  }
  return true
}

const hasMatchingContextIdentity = (currentMetadata, incomingMetadata) => {
  if (!hasCompleteContextIdentity(currentMetadata) || !hasCompleteContextIdentity(incomingMetadata)) return false
  for (const field of CONTEXT_IDENTITY_FIELDS) {
    if (currentMetadata[field] !== incomingMetadata[field]) return false
  }
  return true
}

export const mergeLlmRequestMetadata = (currentMetadata, metadata) => {
  const current = currentMetadata && typeof currentMetadata === 'object' ? currentMetadata : {}
  const incoming = metadata && typeof metadata === 'object' ? metadata : {}
  const totalsOnly = isTotalsOnlyLlmRequestMetadata(incoming)
  const shouldReplaceRequest = !totalsOnly && shouldReplaceLlmRequestMetadata(current, incoming)
  const nextMetadata = shouldReplaceRequest ? { ...incoming } : { ...current }

  for (const field of ['output_tokens', 'cached_tokens']) {
    if (!hasOwn(nextMetadata, field) && hasOwn(current, field)) nextMetadata[field] = current[field]
  }

  for (const field of CUMULATIVE_TOTAL_FIELDS) {
    const currentValue = current[field]
    const incomingValue = incoming[field]
    const hasCurrentValue = isValidNonNegativeNumber(currentValue)
    const hasIncomingValue = isValidNonNegativeNumber(incomingValue)
    if (!hasCurrentValue && !hasIncomingValue) continue
    if (hasCurrentValue && hasIncomingValue) {
      nextMetadata[field] = Math.trunc(Math.max(currentValue, incomingValue))
    } else if (hasCurrentValue) {
      nextMetadata[field] = Math.trunc(currentValue)
    } else {
      nextMetadata[field] = Math.trunc(incomingValue)
    }
  }

  nextMetadata.cache_hit_rate = getSessionCacheHitRate(nextMetadata)

  if (!totalsOnly && shouldReplaceRequest && incoming.input_tokens_source === 'estimated') {
    const canReuseProviderInput = (
      current.input_tokens_source === 'provider' &&
      isValidNonNegativeNumber(current.input_tokens) &&
      hasMatchingContextIdentity(current, incoming)
    )
    if (canReuseProviderInput) {
      nextMetadata.input_tokens = current.input_tokens
      nextMetadata.input_tokens_source = 'provider'
    } else {
      nextMetadata.input_tokens = null
      nextMetadata.input_tokens_source = 'estimated'
    }
  }
  return nextMetadata
}

export const invalidateLlmRequestInput = (currentMetadata, event) => {
  if (!currentMetadata || typeof currentMetadata !== 'object') return null
  if (!event || typeof event !== 'object') return currentMetadata
  if (!shouldReplaceLlmRequestMetadata(currentMetadata, event)) return currentMetadata

  const nextMetadata = {
    ...currentMetadata,
    input_tokens: null,
    input_tokens_source: 'estimated'
  }
  for (const field of ['work_sequence_no', 'event_sequence_no']) {
    if (hasOwn(event, field)) nextMetadata[field] = event[field]
  }
  return nextMetadata
}
