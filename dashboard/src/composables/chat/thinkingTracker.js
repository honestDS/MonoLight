const hasIdentity = value => value !== undefined && value !== null && value !== ''

const getRelatedRequestIds = (relatedRequestIds) => {
  if (!relatedRequestIds) return []
  if (typeof relatedRequestIds === 'string') return hasIdentity(relatedRequestIds) ? [relatedRequestIds] : []
  try {
    if (typeof relatedRequestIds[Symbol.iterator] !== 'function') return []
    return [...relatedRequestIds].filter(hasIdentity)
  } catch {
    return []
  }
}

export const findThinkingIndex = (messages, thinkingId, requestId, allowUnscopedFallback = true) => {
  if (hasIdentity(thinkingId)) {
    const thinkingIndex = messages.findIndex(message => message.id === thinkingId && message.role === 'thinking')
    if (thinkingIndex !== -1) return thinkingIndex
  }

  if (hasIdentity(requestId)) {
    const requestThinkingIndex = messages.findLastIndex(message =>
      message.role === 'thinking' && (
        message.request_id === requestId ||
        Array.isArray(message.request_ids) && message.request_ids.includes(requestId)
      )
    )
    if (requestThinkingIndex !== -1) return requestThinkingIndex
  }

  if (hasIdentity(thinkingId) || hasIdentity(requestId)) return -1

  const thinkingIndexes = messages
    .map((message, index) => message.role === 'thinking' ? index : -1)
    .filter(index => index !== -1)
  return allowUnscopedFallback && thinkingIndexes.length === 1 ? thinkingIndexes[0] : -1
}

export const ensureActiveThinkingMessage = (messages, newThinkingId, requestId, relatedRequestIds = null) => {
  const thinkingMessages = messages.filter(message => message.role === 'thinking')
  const requestIds = new Set()
  for (const message of thinkingMessages) {
    if (hasIdentity(message.request_id)) requestIds.add(message.request_id)
    if (Array.isArray(message.request_ids)) {
      message.request_ids.filter(hasIdentity).forEach(id => requestIds.add(id))
    }
  }
  getRelatedRequestIds(relatedRequestIds).forEach(id => requestIds.add(id))
  if (hasIdentity(requestId)) requestIds.add(requestId)
  if (!thinkingMessages.length) {
    messages.push({
      id: newThinkingId,
      role: 'thinking',
      content: 'Thinking...',
      request_id: requestId,
      request_ids: [...requestIds]
    })
    return newThinkingId
  }

  const activeThinking = thinkingMessages[0]
  if (hasIdentity(requestId)) {
    activeThinking.request_id = requestId
    requestIds.add(requestId)
  }
  activeThinking.request_ids = [...requestIds]

  for (let index = messages.length - 1; index >= 0; index--) {
    if (messages[index].role === 'thinking') messages.splice(index, 1)
  }
  messages.push(activeThinking)
  return activeThinking.id
}

export const clearThinkingRequestCallbacks = (callbacksMap, requestId, thinkingId, relatedRequestIds = null) => {
  if (requestId) callbacksMap.delete(requestId)
  getRelatedRequestIds(relatedRequestIds).forEach(relatedRequestId => callbacksMap.delete(relatedRequestId))
  if (thinkingId === undefined || thinkingId === null || thinkingId === '') return
  for (const [callbackRequestId, callbacks] of callbacksMap) {
    if (callbacks.thinkingId === thinkingId) callbacksMap.delete(callbackRequestId)
  }
}

export const insertMessageBeforeThinking = (messages, message, thinkingId, requestId) => {
  const thinkingIndex = findThinkingIndex(messages, thinkingId, requestId)
  if (thinkingIndex === -1) return false
  messages.splice(thinkingIndex, 0, message)
  return true
}

export const removeThinkingMessageByIdentity = (messages, thinkingId, requestId = null) => {
  const thinkingIndex = findThinkingIndex(messages, thinkingId, requestId, false)
  if (thinkingIndex === -1) return false
  messages.splice(thinkingIndex, 1)
  return true
}
