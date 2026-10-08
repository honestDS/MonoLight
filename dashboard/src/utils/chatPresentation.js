const normalizeIdentity = value => value === undefined || value === null || value === '' ? null : String(value)
const MESSAGE_LIST_BOTTOM_TOLERANCE = 24

export const getMessageListScrollState = (metrics = {}) => {
  const { scrollHeight, clientHeight, scrollTop } = metrics || {}
  if (![scrollHeight, clientHeight, scrollTop].every(Number.isFinite) || scrollHeight < 0 || clientHeight <= 0) {
    return null
  }

  const maxScrollTop = Math.max(0, scrollHeight - clientHeight)
  const bottomDistance = Math.max(0, maxScrollTop - Math.max(0, scrollTop))
  return {
    hasOverflow: maxScrollTop > 0,
    atBottom: bottomDistance <= MESSAGE_LIST_BOTTOM_TOLERANCE,
    bottomDistance,
  }
}

export const getReasoningCollapseName = (message) => {
  const responseId = normalizeIdentity(message?.response_id)
  if (responseId) return `reasoning:response:${responseId}`

  const workId = normalizeIdentity(message?.work_id)
  const turn = normalizeIdentity(message?.turn)
  if (workId && turn) return `reasoning:work:${workId}:turn:${turn}`

  const dbId = normalizeIdentity(message?.db_id ?? message?.message_id)
  if (dbId) return `reasoning:db:${dbId}`

  const messageId = normalizeIdentity(message?.id)
  return `reasoning:message:${messageId || 'unknown'}`
}

export const resolveChatActivityNotice = ({ contextSummarizing = false, thinking = false, historyLoading = false } = {}) => {
  if (contextSummarizing) return 'context_summary'
  if (thinking) return 'thinking'
  if (historyLoading) return 'history_loading'
  return null
}

export const isFollowableLlmOutput = message => (
  message?.type === 'tool_group' || ['assistant', 'tool', 'reasoning'].includes(message?.role)
)
