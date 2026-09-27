export const MAIN_DIALOGUE_REQUEST_PURPOSE = 'main_dialogue'

export const normalizeLlmRequestMetadata = (metadata) => {
  const inputTokensSource = metadata?.input_tokens_source
  if (inputTokensSource !== undefined && !['estimated', 'provider'].includes(inputTokensSource)) return null

  const hasInputTokens = Number.isFinite(metadata?.input_tokens) && metadata.input_tokens >= 0
  if (!Number.isFinite(metadata?.context_window_tokens) || metadata.context_window_tokens < 0) return null
  if (!Number.isFinite(metadata?.max_output_tokens) || metadata.max_output_tokens < 0) return null
  if (inputTokensSource !== 'estimated' && !hasInputTokens) return null

  const normalizedMetadata = {
    input_tokens: inputTokensSource === 'estimated' ? null : Math.trunc(metadata.input_tokens),
    context_window_tokens: Math.trunc(metadata.context_window_tokens),
    max_output_tokens: Math.trunc(metadata.max_output_tokens)
  }
  if (inputTokensSource !== undefined) normalizedMetadata.input_tokens_source = inputTokensSource
  if (Object.prototype.hasOwnProperty.call(metadata, 'request_purpose')) {
    if (typeof metadata.request_purpose !== 'string' || !metadata.request_purpose.trim()) return null
    normalizedMetadata.request_purpose = metadata.request_purpose.trim()
  }
  for (const field of ['output_tokens', 'cached_tokens', 'total_output_tokens']) {
    if (!Object.prototype.hasOwnProperty.call(metadata, field)) continue
    if (!Number.isFinite(metadata[field]) || metadata[field] < 0) return null
    normalizedMetadata[field] = Math.trunc(metadata[field])
  }
  if (Object.prototype.hasOwnProperty.call(metadata, 'cache_hit_rate')) {
    if (!Number.isFinite(metadata.cache_hit_rate) || metadata.cache_hit_rate < 0 || metadata.cache_hit_rate > 1) return null
    normalizedMetadata.cache_hit_rate = Number(metadata.cache_hit_rate)
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

export const mergeLlmRequestMetadata = (currentMetadata, metadata) => {
  const nextMetadata = { ...metadata }
  for (const field of ['output_tokens', 'cached_tokens', 'cache_hit_rate', 'total_output_tokens']) {
    if (!Object.prototype.hasOwnProperty.call(nextMetadata, field) && Object.prototype.hasOwnProperty.call(currentMetadata || {}, field)) {
      nextMetadata[field] = currentMetadata[field]
    }
  }

  if (metadata.input_tokens_source === 'estimated') {
    const confirmedInputTokens = currentMetadata?.input_tokens_source !== 'estimated' && Number.isFinite(currentMetadata.input_tokens)
      ? currentMetadata.input_tokens
      : null
    nextMetadata.input_tokens = confirmedInputTokens
    nextMetadata.input_tokens_source = confirmedInputTokens === null ? 'estimated' : 'provider'
  }
  return nextMetadata
}
