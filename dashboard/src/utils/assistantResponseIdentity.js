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

const getContentValue = (message, parsedContent = getParsedContent(message?.content)) => {
  if (
    parsedContent
    && typeof parsedContent === 'object'
    && !Array.isArray(parsedContent)
    && ('content' in parsedContent || Array.isArray(parsedContent.tool_calls))
  ) {
    return parsedContent.content
  }
  return message?.content
}

const hasContentValue = (value) => (
  typeof value === 'string' ? Boolean(value.trim()) : value !== undefined && value !== null
)

const getToolCallDetails = (message) => {
  const parsedContent = getParsedContent(message?.content)
  const messageToolCalls = Array.isArray(message?.tool_calls) && message.tool_calls.length > 0
    ? message.tool_calls
    : null
  const contentToolCalls = Array.isArray(parsedContent?.tool_calls) && parsedContent.tool_calls.length > 0
    ? parsedContent.tool_calls
    : null

  return {
    parsedContent,
    messageToolCalls,
    contentToolCalls,
    toolCalls: messageToolCalls || contentToolCalls
  }
}

const getMessageIdentity = (message, field) => {
  if (hasIdentity(message?.[field])) return message[field]
  const parsedContent = getParsedContent(message?.content)
  return parsedContent?.[field]
}

const isSyntheticWorkResponseId = (responseId, workId) => (
  hasIdentity(responseId)
  && hasIdentity(workId)
  && String(responseId) === `session-reply-work:${String(workId)}`
)

const isWeakResponseIdentity = (responseId, workId) => (
  !hasIdentity(responseId) || isSyntheticWorkResponseId(responseId, workId)
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

export const isAssistantResponse = (message) => {
  const content = getParsedContent(message?.content)
  return message?.role === 'assistant'
    && message.type !== 'audit_confirmation'
    && message.type !== 'tool_result'
    && message.role !== 'tool'
    && content?.role !== 'tool'
    && content?.type !== 'audit_confirmation'
    && content?.type !== 'tool_result'
}

// 保留现有导出，供只需无工具调用响应的调用方使用。
export const isPlainAssistantResponse = (message) => {
  const content = getParsedContent(message?.content)
  return isAssistantResponse(message)
    && message.type !== 'tool_call'
    && content?.type !== 'tool_call'
    && !hasToolCalls(message, content)
}

const getAssistantResponseReplacementIndices = (messages, incomingMessage) => {
  if (!isAssistantResponse(incomingMessage)) return []

  const incomingDbId = getMessageDbId(incomingMessage)
  const incomingResponseId = getMessageIdentity(incomingMessage, 'response_id')
  const incomingWorkId = getMessageIdentity(incomingMessage, 'work_id')
  const incomingTurn = getMessageIdentity(incomingMessage, 'turn')
  const incomingRequestId = getMessageIdentity(incomingMessage, 'request_id')
  const syntheticWorkResponse = isSyntheticWorkResponseId(incomingResponseId, incomingWorkId)
  const replacementIndices = []
  const addReplacementIndex = (replacementIndex) => {
    if (replacementIndex !== -1 && !replacementIndices.includes(replacementIndex)) {
      replacementIndices.push(replacementIndex)
    }
  }

  const canUseCandidate = (message, allowDifferentDbId = false) => {
    if (!isAssistantResponse(message)) return false
    if (allowDifferentDbId || incomingDbId === null) return true
    const messageDbId = getMessageDbId(message)
    return messageDbId === null || messageDbId === incomingDbId
  }

  if (incomingDbId !== null) {
    const replacementIndex = messages.findIndex(message => (
      canUseCandidate(message, true) && getMessageDbId(message) === incomingDbId
    ))
    addReplacementIndex(replacementIndex)
  }

  if (hasIdentity(incomingResponseId) && !syntheticWorkResponse) {
    const stableResponseId = String(incomingResponseId)
    const replacementIndex = messages.findIndex(message => (
      canUseCandidate(message)
      && hasIdentity(getMessageIdentity(message, 'response_id'))
      && String(getMessageIdentity(message, 'response_id')) === stableResponseId
    ))
    addReplacementIndex(replacementIndex)
  }

  // 合成会话回复曾复用 work id，只能回退到最后一个匹配回合；真实 response id 可
  // 收敛同一回合较早的合成占位消息。
  if (hasIdentity(incomingWorkId) && (isWeakResponseIdentity(incomingResponseId, incomingWorkId) || hasIdentity(incomingResponseId))) {
    const stableWorkId = String(incomingWorkId)
    const replacementIndex = messages.findLastIndex(message => {
      if (!canUseCandidate(message)) return false
      const messageWorkId = getMessageIdentity(message, 'work_id')
      if (!hasIdentity(messageWorkId) || String(messageWorkId) !== stableWorkId) return false
      if (hasIdentity(incomingTurn)) {
        const messageTurn = getMessageIdentity(message, 'turn')
        if (!hasIdentity(messageTurn) || String(messageTurn) !== String(incomingTurn)) return false
      }
      if (syntheticWorkResponse || !hasIdentity(incomingResponseId)) return true
      const messageTurn = getMessageIdentity(message, 'turn')
      return isWeakResponseIdentity(getMessageIdentity(message, 'response_id'), stableWorkId)
        && (!hasIdentity(incomingTurn) || (hasIdentity(messageTurn) && String(messageTurn) === String(incomingTurn)))
    })
    addReplacementIndex(replacementIndex)
  }

  if (incomingDbId === null && !hasIdentity(incomingResponseId) && !hasIdentity(incomingWorkId) && hasIdentity(incomingRequestId)) {
    const stableRequestId = String(incomingRequestId)
    const replacementIndex = messages.findLastIndex(message => (
      canUseCandidate(message)
      && hasIdentity(getMessageIdentity(message, 'request_id'))
      && String(getMessageIdentity(message, 'request_id')) === stableRequestId
    ))
    addReplacementIndex(replacementIndex)
  }

  return replacementIndices
}

export const findAssistantResponseReplacementIndex = (messages, incomingMessage) => (
  getAssistantResponseReplacementIndices(messages, incomingMessage)[0] ?? -1
)

export const mergeAssistantResponse = (localMessage, remoteMessage) => {
  const remoteDbId = getMessageDbId(remoteMessage)
  const localDbId = getMessageDbId(localMessage)
  const remoteToolDetails = getToolCallDetails(remoteMessage)
  const localToolDetails = getToolCallDetails(localMessage)
  const hasAnyToolCalls = Boolean(remoteToolDetails.toolCalls || localToolDetails.toolCalls)
  const remoteContent = getContentValue(remoteMessage, remoteToolDetails.parsedContent)
  const localContent = getContentValue(localMessage, localToolDetails.parsedContent)
  const remoteHasContent = hasContentValue(remoteContent)
  const normalizedRemoteMessage = {
    ...remoteMessage,
    ...(remoteDbId ? { db_id: remoteDbId } : {})
  }
  const mergedMessage = mergeRemoteMessage(localMessage, normalizedRemoteMessage)

  if (hasAnyToolCalls) {
    const toolCalls = remoteToolDetails.toolCalls || localToolDetails.toolCalls
    const finalContent = remoteHasContent ? remoteContent : localContent
    const isAssistantToolContent = content => (
      content
      && typeof content === 'object'
      && !Array.isArray(content)
      && (content.role === 'assistant' || content.type === 'tool_call' || Array.isArray(content.tool_calls))
    )
    const sourceContent = remoteToolDetails.contentToolCalls
      ? remoteToolDetails.parsedContent
      : localToolDetails.contentToolCalls
        ? localToolDetails.parsedContent
        : isAssistantToolContent(remoteToolDetails.parsedContent)
          ? remoteToolDetails.parsedContent
          : isAssistantToolContent(localToolDetails.parsedContent)
            ? localToolDetails.parsedContent
            : null
    const topLevelToolCalls = remoteToolDetails.messageToolCalls || localToolDetails.messageToolCalls
    return {
      ...mergedMessage,
      ...(topLevelToolCalls ? { tool_calls: topLevelToolCalls } : {}),
      content: JSON.stringify({
        ...(sourceContent ? sourceContent : {}),
        role: 'assistant',
        tool_calls: toolCalls,
        content: finalContent ?? ''
      })
    }
  }

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
  const replacementIndices = getAssistantResponseReplacementIndices(messages, remoteMessage)
  if (replacementIndices.length === 0) return [...messages, remoteMessage]

  const replacementIndex = replacementIndices[0]
  const mergedMessage = replacementIndices
    .slice(1)
    .reduce((primary, index) => {
      const merged = mergeAssistantResponse(messages[index], primary)
      const getPreferredField = field => {
        const primaryValue = getMessageIdentity(primary, field)
        return hasIdentity(primaryValue) ? primaryValue : getMessageIdentity(merged, field)
      }
      const id = hasIdentity(primary?.id) ? primary.id : merged?.id
      const dbId = hasIdentity(primary?.db_id) ? primary.db_id : merged?.db_id
      const messageId = hasIdentity(primary?.message_id) ? primary.message_id : merged?.message_id
      const responseId = getPreferredField('response_id')
      const requestId = getPreferredField('request_id')
      const workId = getPreferredField('work_id')
      const turn = getPreferredField('turn')

      return {
        ...merged,
        ...(hasIdentity(id) ? { id } : {}),
        ...(hasIdentity(dbId) ? { db_id: dbId } : {}),
        ...(hasIdentity(messageId) ? { message_id: messageId } : {}),
        ...(hasIdentity(responseId) ? { response_id: responseId } : {}),
        ...(hasIdentity(requestId) ? { request_id: requestId } : {}),
        ...(hasIdentity(workId) ? { work_id: workId } : {}),
        ...(hasIdentity(turn) ? { turn } : {})
      }
    }, messages[replacementIndex])
  const finalMessage = mergeAssistantResponse(mergedMessage, remoteMessage)
  const mergedIndices = new Set(replacementIndices.slice(1))

  return messages.map((message, index) => (
    index === replacementIndex ? finalMessage : message
  )).filter((message, index) => index === replacementIndex || !mergedIndices.has(index))
}
