const isValidNonNegativeNumber = (value) => Number.isFinite(value) && value >= 0

const isValidPositiveNumber = (value) => Number.isFinite(value) && value > 0

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

  const inputTokens = metadata?.input_tokens
  const triggerTokens = metadata?.summary_trigger_tokens
  if (
    metadata?.input_tokens_source !== 'provider' ||
    !isValidNonNegativeNumber(inputTokens) ||
    !isValidPositiveNumber(triggerTokens)
  ) {
    return emptyContextSummaryProgress('waiting')
  }

  const ratio = Math.min(Math.max(inputTokens / triggerTokens, 0), 1)
  const status = ratio >= 1 ? 'threshold_reached' : 'ready'
  const remainingTokens = Math.max(triggerTokens - inputTokens, 0)
  const remainingRatio = remainingTokens / triggerTokens

  return {
    status,
    inputTokens,
    triggerTokens,
    ratio,
    remainingRatio,
    remainingTokens,
    nearThreshold: ratio >= 0.8
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
