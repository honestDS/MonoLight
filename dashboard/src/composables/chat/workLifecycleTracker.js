const hasIdentity = value => value !== undefined && value !== null && value !== ''

const normalizeIdentity = value => hasIdentity(value) ? String(value) : null

const sameIdentity = (left, right) => {
  const normalizedLeft = normalizeIdentity(left)
  const normalizedRight = normalizeIdentity(right)
  return normalizedLeft !== null && normalizedLeft === normalizedRight
}

const getRequestIds = event => {
  if (!Array.isArray(event?.request_ids)) return new Set()
  return new Set(event.request_ids.map(normalizeIdentity).filter(Boolean))
}

export const shouldApplyOwnProactiveReply = (tracker, event, requestId) => {
  const normalizedRequestId = normalizeIdentity(requestId)
  if (!normalizedRequestId || !getRequestIds(event).has(normalizedRequestId)) return false

  const workId = normalizeIdentity(event?.work_id)
  if (!workId) return true

  return !tracker.isWorkTerminal(workId) || tracker.isAcceptedTerminalEvent(event)
}

const isThinkingForWork = (message, workId) =>
  message?.role === 'thinking' && sameIdentity(message.work_id, workId)

export const markInputQueued = (messages, event) => {
  const requestId = normalizeIdentity(event?.request_id)
  if (!requestId) return messages

  return messages.map(message => {
    if (message?.role !== 'user' || !sameIdentity(message.request_id, requestId)) return message
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
      !requestIds.has(normalizeIdentity(message.request_id))
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
        !requestIds.has(normalizeIdentity(message.request_id))
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

const getEventSequenceNo = event => (
  Number.isFinite(event?.event_sequence_no) ? event.event_sequence_no : null
)

const getEventTurn = event => (
  Number.isFinite(event?.turn) ? event.turn : null
)

const createWorkState = () => ({
  eventSequenceNo: null,
  turn: null,
  outputResponseIds: new Set(),
  startedResponseIds: new Set(),
  terminal: false
})

export function createWorkLifecycleTracker() {
  const workStates = new Map()
  const dequeuedRequestIds = new Set()
  let acceptedTerminalEvents = new WeakSet()

  const getWorkState = (event, create = true) => {
    const workId = normalizeIdentity(event?.work_id)
    if (!workId) return null
    if (!workStates.has(workId) && create) workStates.set(workId, createWorkState())
    return workStates.get(workId) || null
  }

  const acceptsWorkEvent = (state, event) => {
    const eventSequenceNo = getEventSequenceNo(event)
    if (eventSequenceNo !== null) {
      if (state.eventSequenceNo !== null && eventSequenceNo <= state.eventSequenceNo) return false
    }

    const turn = getEventTurn(event)
    if (turn !== null && state.turn !== null && turn < state.turn) return false

    if (eventSequenceNo !== null) state.eventSequenceNo = eventSequenceNo
    if (turn !== null && (state.turn === null || turn > state.turn)) state.turn = turn
    return true
  }

  return {
    markInputQueued(messages, event) {
      const requestId = normalizeIdentity(event?.request_id)
      if (!requestId || dequeuedRequestIds.has(requestId)) return messages

      const state = getWorkState(event)
      if (state && (state.terminal || !acceptsWorkEvent(state, event))) return messages
      return markInputQueued(messages, event)
    },

    markInputsDequeued(messages, event) {
      const requestIds = getRequestIds(event)
      if (requestIds.size === 0) return messages

      const state = getWorkState(event)
      if (state?.terminal) return messages
      if (state && !acceptsWorkEvent(state, event)) return messages

      requestIds.forEach(requestId => dequeuedRequestIds.add(requestId))
      return markInputsDequeued(messages, event)
    },

    startAgentLoop(messages, event) {
      const responseId = normalizeIdentity(event?.response_id)
      if (!responseId) return messages

      const state = getWorkState(event)
      if (!state || state.terminal || !acceptsWorkEvent(state, event)) return messages
      if (state.outputResponseIds.has(responseId) || state.startedResponseIds.has(responseId)) return messages

      state.startedResponseIds.add(responseId)
      return startAgentLoop(messages, event)
    },

    stopAgentLoop(messages, event) {
      const responseId = normalizeIdentity(event?.response_id)
      if (!responseId) return messages

      const state = getWorkState(event)
      if (!state || state.terminal || !acceptsWorkEvent(state, event)) return messages

      state.outputResponseIds.add(responseId)
      return stopAgentLoop(messages, event)
    },

    finishWorkLifecycle(messages, event) {
      const requestIds = getRequestIds(event)
      const state = getWorkState(event)
      if (state) {
        if (state.terminal || !acceptsWorkEvent(state, event)) return messages
        state.terminal = true
        if (event && typeof event === 'object') acceptedTerminalEvents.add(event)
      }

      requestIds.forEach(requestId => dequeuedRequestIds.add(requestId))
      return finishWorkLifecycle(messages, event)
    },

    isWorkTerminal(workId) {
      return workStates.get(normalizeIdentity(workId))?.terminal === true
    },

    isAcceptedTerminalEvent(event) {
      return Boolean(event && typeof event === 'object' && acceptedTerminalEvents.has(event))
    },

    resetWorkLifecycle(messages) {
      workStates.clear()
      dequeuedRequestIds.clear()
      acceptedTerminalEvents = new WeakSet()
      return resetWorkLifecycle(messages)
    }
  }
}
