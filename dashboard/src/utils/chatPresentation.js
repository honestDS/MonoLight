const normalizeIdentity = value => value === undefined || value === null || value === '' ? null : String(value)

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
