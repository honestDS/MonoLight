const isValidNonNegativeNumber = (value) => Number.isFinite(value) && value >= 0

const isValidPositiveNumber = (value) => Number.isFinite(value) && value > 0

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

export const getCacheUsagePresentation = (metadata) => {
  const reportedInputTokens = metadata?.cache_reported_input_tokens
  const reportedCachedTokens = metadata?.cache_reported_cached_tokens
  const totalInputTokens = metadata?.total_input_tokens

  const hasValidCounts = (
    isValidNonNegativeNumber(reportedInputTokens) &&
    isValidNonNegativeNumber(reportedCachedTokens) &&
    isValidNonNegativeNumber(totalInputTokens) &&
    reportedCachedTokens <= reportedInputTokens &&
    reportedInputTokens <= totalInputTokens
  )
  if (!hasValidCounts) return { hitRate: null, coverage: null }

  let hitRate = null
  if (reportedInputTokens > 0) hitRate = reportedCachedTokens / reportedInputTokens

  let coverage = null
  if (totalInputTokens > 0) coverage = reportedInputTokens / totalInputTokens

  return { hitRate, coverage }
}
