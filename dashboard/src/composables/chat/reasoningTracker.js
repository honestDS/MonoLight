const normalizeIdentity = value => value === undefined || value === null || value === '' ? null : String(value)

const matchesThinking = (message, responseId, requestId, workId) => {
  if (message?.role !== 'thinking') return false
  const stableResponseId = normalizeIdentity(responseId)
  const stableWorkId = normalizeIdentity(workId)
  if (stableResponseId && normalizeIdentity(message.response_id) === stableResponseId) return true
  if (stableWorkId && normalizeIdentity(message.work_id) === stableWorkId) return true
  return Boolean(requestId && (message.request_id === requestId || message.request_ids?.includes(requestId)))
}

const matchesReasoning = (message, responseId, requestId, workId, turn) => {
  if (message?.role !== 'reasoning') return false
  const stableResponseId = normalizeIdentity(responseId)
  if (stableResponseId && normalizeIdentity(message.response_id) === stableResponseId) return true
  const stableWorkId = normalizeIdentity(workId)
  if (stableWorkId && normalizeIdentity(message.work_id) === stableWorkId && (turn === undefined || turn === null || message.turn === turn)) return true
  return Boolean(requestId && message.request_id === requestId && (turn === undefined || turn === null || message.turn === turn))
}

const matchesAssistant = (message, responseId, workId, turn) => {
  if (message?.role !== 'assistant') return false
  const stableResponseId = normalizeIdentity(responseId)
  if (stableResponseId && normalizeIdentity(message.response_id) === stableResponseId) return true
  const stableWorkId = normalizeIdentity(workId)
  return Boolean(
    stableWorkId
    && normalizeIdentity(message.work_id) === stableWorkId
    && (turn === undefined || turn === null || message.turn === turn)
  )
}

export const appendStreamReasoning = (messages, text, { turn, responseId, requestId, workId } = {}) => {
  if (typeof text !== 'string' || !text) return messages
  const nextMessages = [...messages]
  const reasoningIndex = nextMessages.findLastIndex(message => matchesReasoning(message, responseId, requestId, workId, turn))
  if (reasoningIndex !== -1) {
    const reasoning = nextMessages[reasoningIndex]
    nextMessages[reasoningIndex] = {
      ...reasoning,
      reasoning_content: `${reasoning.reasoning_content || ''}${text}`
    }
    return nextMessages
  }

  const assistantIndex = nextMessages.findLastIndex(message => matchesAssistant(message, responseId, workId, turn))
  if (assistantIndex !== -1) {
    const assistant = nextMessages[assistantIndex]
    nextMessages[assistantIndex] = {
      ...assistant,
      reasoning_content: `${assistant.reasoning_content || ''}${text}`
    }
    return nextMessages
  }

  const reasoning = {
    id: `reasoning_${responseId || requestId || Date.now()}`,
    role: 'reasoning',
    content: '',
    reasoning_content: text,
    response_id: responseId,
    request_id: requestId,
    work_id: workId,
    turn,
    created_at: Date.now() / 1000
  }
  const thinkingIndex = nextMessages.findLastIndex(message => matchesThinking(message, responseId, requestId, workId))
  if (thinkingIndex !== -1) nextMessages.splice(thinkingIndex, 0, reasoning)
  else nextMessages.push(reasoning)
  return nextMessages
}

export const finalizeStreamReasoning = (messages, reasoningContent, { turn, responseId, requestId, workId } = {}) => {
  const nextMessages = [...messages]
  const reasoningIndex = nextMessages.findLastIndex(message => matchesReasoning(message, responseId, requestId, workId, turn))
  const streamedReasoning = reasoningIndex === -1 ? '' : String(nextMessages[reasoningIndex].reasoning_content || '')
  const finalReasoning = typeof reasoningContent === 'string' && reasoningContent.trim()
    ? reasoningContent
    : streamedReasoning

  const assistantIndex = nextMessages.findLastIndex(message => matchesAssistant(message, responseId, workId, turn))
  if (assistantIndex !== -1 && finalReasoning.trim()) {
    nextMessages[assistantIndex] = {
      ...nextMessages[assistantIndex],
      reasoning_content: finalReasoning
    }
  } else if (assistantIndex === -1 && finalReasoning.trim()) {
    const assistant = {
      id: `assistant_${responseId || requestId || Date.now()}`,
      role: 'assistant',
      content: '',
      reasoning_content: finalReasoning,
      response_id: responseId,
      request_id: requestId,
      work_id: workId,
      turn,
      created_at: Date.now() / 1000
    }
    const insertAt = reasoningIndex === -1
      ? nextMessages.findLastIndex(message => matchesThinking(message, responseId, requestId, workId))
      : reasoningIndex
    nextMessages.splice(insertAt === -1 ? nextMessages.length : insertAt, 0, assistant)
  }

  return nextMessages.filter(message => !matchesReasoning(message, responseId, requestId, workId, turn))
}
