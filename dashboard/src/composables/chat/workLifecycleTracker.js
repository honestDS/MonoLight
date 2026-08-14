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

const getMessageRequestIds = message => {
  const requestIds = getRequestIds(message)
  const requestId = normalizeIdentity(message?.request_id)
  if (requestId) requestIds.add(requestId)
  return requestIds
}

const getEventRequestIds = event => {
  const requestIds = getRequestIds(event)
  const requestId = normalizeIdentity(event?.request_id)
  if (requestId) requestIds.add(requestId)
  return requestIds
}

const hasRequestIdIntersection = (leftRequestIds, rightRequestIds) => (
  Array.from(leftRequestIds).some(requestId => rightRequestIds.has(requestId))
)

export const shouldApplyOwnProactiveReply = (tracker, event, requestId) => {
  const normalizedRequestId = normalizeIdentity(requestId)
  if (!normalizedRequestId || !getRequestIds(event).has(normalizedRequestId)) return false

  const workId = normalizeIdentity(event?.work_id)
  if (!workId) return true

  return !tracker.isWorkTerminal(workId) || tracker.isAcceptedTerminalEvent(event)
}

const isThinkingForWork = (message, workId) =>
  message?.role === 'thinking' && sameIdentity(message.work_id, workId)

const getUniqueThinkingId = (messages, requestId) => {
  const baseId = `thinking_request_${requestId}`
  if (!messages.some(message => sameIdentity(message?.id, baseId))) return baseId

  let suffix = 1
  let id = `${baseId}_${suffix}`
  while (messages.some(message => sameIdentity(message?.id, id))) {
    suffix += 1
    id = `${baseId}_${suffix}`
  }
  return id
}

export const startRequestLifecycle = (messages, event) => {
  const requestId = normalizeIdentity(event?.request_id)
  if (!requestId) return messages

  const existingMarkerIndex = messages.findIndex(message => (
    message?.role === 'thinking' && getMessageRequestIds(message).has(requestId)
  ))
  if (existingMarkerIndex !== -1) {
    const existingMarker = messages[existingMarkerIndex]
    const eventWorkId = normalizeIdentity(event?.work_id)
    const existingWorkId = normalizeIdentity(existingMarker.work_id)
    if (existingWorkId && (!eventWorkId || !sameIdentity(existingWorkId, eventWorkId))) return messages
    if (existingMarkerIndex === messages.length - 1) return messages

    return [
      ...messages.slice(0, existingMarkerIndex),
      ...messages.slice(existingMarkerIndex + 1),
      existingMarker
    ]
  }

  return [
    ...messages,
    {
      id: getUniqueThinkingId(messages, requestId),
      role: 'thinking',
      content: 'Thinking...',
      request_id: requestId,
      request_ids: [requestId]
    }
  ]
}

export const markInputQueued = (messages, event) => {
  const requestId = normalizeIdentity(event?.request_id)
  if (!requestId) return messages

  return messages
    .filter(message => !(
      message?.role === 'thinking'
      && !hasIdentity(message.work_id)
      && getMessageRequestIds(message).has(requestId)
    ))
    .map(message => {
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
    if (message?.role === 'thinking') {
      const markerWorkId = normalizeIdentity(message.work_id)
      if (workId && markerWorkId && sameIdentity(markerWorkId, workId)) {
        const markerRequestIds = new Set([
          ...getMessageRequestIds(message),
          ...requestIds
        ])
        return { ...message, request_ids: Array.from(markerRequestIds) }
      }

      if (
        !workId
        || markerWorkId
        || !hasRequestIdIntersection(getMessageRequestIds(message), requestIds)
      ) return message

      return { ...message, work_id: event.work_id }
    }

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

  const existingMarkerIndex = messages.findIndex(message => isThinkingForWork(message, workId))
  let markerIndex = existingMarkerIndex
  if (markerIndex === -1) {
    const eventRequestIds = getEventRequestIds(event)
    const unboundMarkerIndexes = messages.reduce((indexes, message, index) => {
      if (message?.role === 'thinking' && !hasIdentity(message.work_id)) indexes.push(index)
      return indexes
    }, [])

    if (eventRequestIds.size > 0) {
      markerIndex = unboundMarkerIndexes.find(index => (
        hasRequestIdIntersection(getMessageRequestIds(messages[index]), eventRequestIds)
      )) ?? -1
    } else if (unboundMarkerIndexes.length === 1) {
      markerIndex = unboundMarkerIndexes[0]
    }
  }

  const existingMarker = markerIndex === -1 ? null : messages[markerIndex]
  const existingMarkerFields = { ...(existingMarker || {}) }
  delete existingMarkerFields.request_id
  delete existingMarkerFields.request_ids
  const markerRequestIds = new Set([
    ...getMessageRequestIds(existingMarker),
    ...getEventRequestIds(event)
  ])
  const retainedMessages = messages.filter((message, index) => (
    !isThinkingForWork(message, workId) && index !== markerIndex
  ))

  retainedMessages.push({
    ...existingMarkerFields,
    id: existingMarker?.id || `thinking_${workId}_${responseId}`,
    role: 'thinking',
    content: 'Thinking...',
    work_id: event.work_id,
    response_id: event.response_id,
    ...(event.turn !== undefined ? { turn: event.turn } : {}),
    ...(markerRequestIds.size > 0 ? { request_ids: Array.from(markerRequestIds) } : {})
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
  const requestIds = getEventRequestIds(event)

  return messages
    .filter(message => {
      if (message?.role !== 'thinking') return true
      const markerWorkId = normalizeIdentity(message.work_id)
      if (workId && markerWorkId) return !sameIdentity(markerWorkId, workId)
      if (requestIds.size === 0) return true
      return !hasRequestIdIntersection(getMessageRequestIds(message), requestIds)
    })
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
  const terminalRequestIds = new Set()
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
    startRequestLifecycle(messages, event) {
      const requestId = normalizeIdentity(event?.request_id)
      if (!requestId || dequeuedRequestIds.has(requestId)) return messages

      const state = getWorkState(event)
      if (state?.terminal) return messages
      if (state && !acceptsWorkEvent(state, event)) return messages
      return startRequestLifecycle(messages, event)
    },

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
      if (hasRequestIdIntersection(getEventRequestIds(event), terminalRequestIds)) return messages

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
      const requestIds = getEventRequestIds(event)
      const state = getWorkState(event)
      if (state) {
        if (state.terminal || !acceptsWorkEvent(state, event)) return messages
        state.terminal = true
        if (event && typeof event === 'object') acceptedTerminalEvents.add(event)
      }

      requestIds.forEach(requestId => {
        dequeuedRequestIds.add(requestId)
        terminalRequestIds.add(requestId)
      })
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
      terminalRequestIds.clear()
      acceptedTerminalEvents = new WeakSet()
      return resetWorkLifecycle(messages)
    }
  }
}
