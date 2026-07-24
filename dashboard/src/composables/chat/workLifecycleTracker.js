const hasIdentity = value => value !== undefined && value !== null && value !== ''

const normalizeIdentity = value => hasIdentity(value) ? String(value) : null

const sameIdentity = (left, right) => {
  const normalizedLeft = normalizeIdentity(left)
  const normalizedRight = normalizeIdentity(right)
  return normalizedLeft !== null && normalizedLeft === normalizedRight
}

const getRequestIds = event => {
  if (!Array.isArray(event?.request_ids)) return []
  return new Set(event.request_ids.filter(hasIdentity))
}

const isThinkingForWork = (message, workId) =>
  message?.role === 'thinking' && sameIdentity(message.work_id, workId)

export const markInputQueued = (messages, event) => {
  const requestId = normalizeIdentity(event?.request_id)
  if (!requestId) return messages

  return messages.map(message => {
    if (message?.role !== 'user' || message.request_id !== event.request_id) return message
    const nextMessage = { ...message, status: 'queued' }
    if (hasIdentity(event?.work_id)) nextMessage.work_id = event.work_id
    return nextMessage
  })
}

export const markInputsDequeued = (messages, event) => {
  const requestIds = getRequestIds(event)
  const workId = normalizeIdentity(event?.work_id)
  if (requestIds.size === 0) return messages

  return messages.map(message => {
    if (
      message?.role !== 'user' ||
      ![...requestIds].some(requestId => sameIdentity(message.request_id, requestId))
    ) return message

    const shouldClearStatus = message.status === 'queued'
    const shouldUpdateWorkId = workId && !sameIdentity(message.work_id, event.work_id)
    if (!shouldClearStatus && !shouldUpdateWorkId) return message

    const nextMessage = { ...message }
    if (shouldClearStatus) delete nextMessage.status
    if (shouldUpdateWorkId) nextMessage.work_id = event.work_id
    return nextMessage
  })
}

export const startAgentLoop = (messages, event) => {
  const workId = normalizeIdentity(event?.work_id)
  const responseId = normalizeIdentity(event?.response_id)
  if (!workId || !responseId) return messages

  const existingMarker = messages.find(message => isThinkingForWork(message, workId))
  const existingMarkerFields = { ...(existingMarker || {}) }
  delete existingMarkerFields.request_id
  delete existingMarkerFields.request_ids
  const retainedMessages = messages.filter(message => !isThinkingForWork(message, workId))

  retainedMessages.push({
    ...existingMarkerFields,
    id: existingMarker?.id || `thinking_${workId}_${responseId}`,
    role: 'thinking',
    content: 'Thinking...',
    work_id: event.work_id,
    response_id: event.response_id,
    ...(event.turn !== undefined ? { turn: event.turn } : {})
  })
  return retainedMessages
}

export const stopAgentLoop = (messages, event) => {
  const workId = normalizeIdentity(event?.work_id)
  const responseId = normalizeIdentity(event?.response_id)
  if (!workId || !responseId) return messages

  return messages.filter(message => !(
    isThinkingForWork(message, workId) && sameIdentity(message.response_id, responseId)
  ))
}

export const finishWorkLifecycle = (messages, event) => {
  const workId = normalizeIdentity(event?.work_id)
  const requestIds = getRequestIds(event)

  return messages
    .filter(message => !(workId && isThinkingForWork(message, workId)))
    .map(message => {
      if (
        message?.role !== 'user' ||
        message.status !== 'queued' ||
        !requestIds.has(message.request_id)
      ) return message

      const { status, ...messageWithoutStatus } = message
      return messageWithoutStatus
    })
}

export const resetWorkLifecycle = messages => messages
  .filter(message => message?.role !== 'thinking')
  .map(message => {
    if (message?.role !== 'user' || message.status !== 'queued') return message
    const { status, ...messageWithoutStatus } = message
    return messageWithoutStatus
  })
