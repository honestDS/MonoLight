const getParsedContent = (content) => {
  if (typeof content !== 'string') return content
  try {
    return JSON.parse(content)
  } catch {
    return null
  }
}

const hasIdentity = (value) => value !== null && value !== undefined && value !== ''

const hasToolCalls = (message, content) => (
  (Array.isArray(message?.tool_calls) && message.tool_calls.length > 0)
  || (Array.isArray(content?.tool_calls) && content.tool_calls.length > 0)
)

const mergeRemoteMessage = (localMessage, remoteMessage) => ({
  ...localMessage,
  ...remoteMessage,
  id: localMessage?.id ?? remoteMessage?.id,
  response_id: localMessage?.response_id ?? remoteMessage?.response_id,
  request_id: localMessage?.request_id ?? remoteMessage?.request_id,
  work_id: localMessage?.work_id ?? remoteMessage?.work_id,
  turn: localMessage?.turn ?? remoteMessage?.turn
})

export const getMessageDbId = (message) => {
  const dbId = [
    message?.db_id,
    message?.message_id,
    typeof message?.id === 'number' ? message.id : null
  ].find(hasIdentity)
  return dbId === undefined ? null : String(dbId)
}

export const isPlainAssistantResponse = (message) => {
  const content = getParsedContent(message?.content)
  return message?.role === 'assistant'
    && message.type !== 'audit_confirmation'
    && message.type !== 'tool_call'
    && message.type !== 'tool_result'
    && message.role !== 'tool'
    && content?.role !== 'tool'
    && content?.type !== 'audit_confirmation'
    && content?.type !== 'tool_call'
    && content?.type !== 'tool_result'
    && !hasToolCalls(message, content)
}

export const findAssistantResponseReplacementIndex = (messages, incomingMessage) => {
  if (!isPlainAssistantResponse(incomingMessage)) return -1

  const incomingDbId = getMessageDbId(incomingMessage)
  const incomingResponseId = incomingMessage?.response_id
  const incomingWorkId = incomingMessage?.work_id
  const hasStrongerIdentity = hasIdentity(incomingResponseId) || hasIdentity(incomingWorkId)
  const canUseWorkFallback = incomingDbId !== null || !hasIdentity(incomingResponseId)
  const identities = [
    ['db_id', incomingDbId],
    ['response_id', incomingResponseId],
    ...(canUseWorkFallback ? [['work_id', incomingWorkId]] : []),
    ...(incomingDbId === null && !hasStrongerIdentity ? [['request_id', incomingMessage?.request_id]] : [])
  ]

  for (const [field, value] of identities) {
    if (!hasIdentity(value)) continue
    const stableValue = String(value)
    const findIndex = field === 'work_id' || field === 'request_id'
      ? messages.findLastIndex.bind(messages)
      : messages.findIndex.bind(messages)
    const replacementIndex = findIndex(message => {
      if (!isPlainAssistantResponse(message)) return false
      const messageDbId = getMessageDbId(message)
      if (incomingDbId !== null && field !== 'db_id' && messageDbId !== null && messageDbId !== incomingDbId) return false
      const messageValue = field === 'db_id' ? messageDbId : message?.[field]
      return hasIdentity(messageValue) && String(messageValue) === stableValue
    })
    if (replacementIndex !== -1) return replacementIndex
  }

  return -1
}

export const mergeAssistantResponse = (localMessage, remoteMessage) => {
  const remoteDbId = getMessageDbId(remoteMessage)
  const localDbId = getMessageDbId(localMessage)
  const remoteHasContent = typeof remoteMessage?.content === 'string'
    ? Boolean(remoteMessage.content.trim())
    : remoteMessage?.content !== undefined && remoteMessage?.content !== null
  const normalizedRemoteMessage = {
    ...remoteMessage,
    ...(remoteDbId ? { db_id: remoteDbId } : {})
  }
  const mergedMessage = mergeRemoteMessage(localMessage, normalizedRemoteMessage)

  return {
    ...mergedMessage,
    ...(!remoteHasContent && localMessage?.content !== undefined ? { content: localMessage.content } : {}),
    ...(remoteDbId ? { db_id: remoteDbId } : localDbId ? { db_id: localDbId } : {}),
    ...(localMessage?.response_id == null && remoteMessage?.response_id != null ? { response_id: remoteMessage.response_id } : {}),
    ...(localMessage?.work_id == null && remoteMessage?.work_id != null ? { work_id: remoteMessage.work_id } : {}),
    ...(localMessage?.request_id == null && remoteMessage?.request_id != null ? { request_id: remoteMessage.request_id } : {})
  }
}

export const mergeAssistantResponseIntoList = (messages, remoteMessage) => {
  const replacementIndex = findAssistantResponseReplacementIndex(messages, remoteMessage)
  if (replacementIndex === -1) return [...messages, remoteMessage]
  return messages.map((message, index) => (
    index === replacementIndex ? mergeAssistantResponse(message, remoteMessage) : message
  ))
}
