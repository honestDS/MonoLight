const isValidNonNegativeNumber = (value) => Number.isFinite(value) && value >= 0

const isValidPositiveNumber = (value) => Number.isFinite(value) && value > 0

const isValidContextRevision = (value) => (
  Number.isFinite(value) && Number.isInteger(value) && value >= 0
)

const hasUsableProviderContextUsage = (metadata) => (
  metadata?.input_tokens_source === 'provider' &&
  isValidNonNegativeNumber(metadata?.input_tokens) &&
  isValidPositiveNumber(metadata?.summary_trigger_tokens)
)

const getUsablePreviousContextUsage = (metadata) => {
  const snapshot = metadata?.last_confirmed_context_usage
  if (!hasUsableProviderContextUsage(snapshot)) return null

  for (const field of ['context_summary_revision', 'context_content_revision']) {
    const metadataRevision = metadata?.[field]
    if (
      isValidContextRevision(metadataRevision) &&
      (!isValidContextRevision(snapshot[field]) || snapshot[field] < metadataRevision)
    ) {
      return null
    }
  }

  return snapshot
}

const tokenNumberFormatter = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  compactDisplay: 'short',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2
})

export const formatTokenCount = (value) =>
  Number.isFinite(value) ? tokenNumberFormatter.format(value) : '-'

const emptyContextSummaryProgress = (status) => ({
  status,
  inputTokens: null,
  triggerTokens: null,
  ratio: null,
  remainingRatio: null,
  remainingTokens: null,
  nearThreshold: null
})

export const getContextSummaryProgress = (metadata, contextSummarizing = false) => {
  if (contextSummarizing) return emptyContextSummaryProgress('summarizing')

  let contextUsage
  let usingPreviousContextUsage = false
  if (hasUsableProviderContextUsage(metadata)) {
    contextUsage = metadata
  } else {
    contextUsage = getUsablePreviousContextUsage(metadata)
    usingPreviousContextUsage = true
  }
  if (!contextUsage) {
    return emptyContextSummaryProgress('waiting')
  }

  const inputTokens = contextUsage.input_tokens
  const triggerTokens = contextUsage.summary_trigger_tokens
  const ratio = Math.min(Math.max(inputTokens / triggerTokens, 0), 1)
  let status
  if (usingPreviousContextUsage) {
    status = 'previous'
  } else if (ratio >= 1) {
    status = 'threshold_reached'
  } else {
    status = 'ready'
  }
  const remainingTokens = Math.max(triggerTokens - inputTokens, 0)
  const remainingRatio = remainingTokens / triggerTokens

  return {
    status,
    inputTokens,
    triggerTokens,
    ratio,
    remainingRatio,
    remainingTokens,
    nearThreshold: !usingPreviousContextUsage && ratio >= 0.8
  }
}

export const getSessionCacheHitRate = (metadata) => {
  const totalInputTokens = metadata?.total_input_tokens
  const totalCachedTokens = metadata?.total_cached_tokens

  if (
    !isValidPositiveNumber(totalInputTokens) ||
    !isValidNonNegativeNumber(totalCachedTokens) ||
    totalCachedTokens > totalInputTokens
  ) {
    return null
  }

  return totalCachedTokens / totalInputTokens
}
