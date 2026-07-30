export const getContextSummaryWorkKey = (data, requestId) => {
  if (data?.work_id !== undefined && data?.work_id !== null && data.work_id !== '') {
    return `work:${String(data.work_id)}`
  }
  if (data?.event_id !== undefined && data?.event_id !== null && data.event_id !== '') {
    return `event:${String(data.event_id)}`
  }
  if (requestId !== undefined && requestId !== null && requestId !== '') {
    return `request:${String(requestId)}`
  }
  return null
}

export const startContextSummaryWork = (activeKeys, requestKeys, data, requestId) => {
  const workKey = getContextSummaryWorkKey(data, requestId)
  if (!workKey) return
  activeKeys.add(workKey)
  if (requestId !== undefined && requestId !== null && requestId !== '') {
    const requestKeySet = requestKeys.get(requestId) || new Set()
    requestKeySet.add(workKey)
    requestKeys.set(requestId, requestKeySet)
  }
}

export const endContextSummaryWork = (activeKeys, requestKeys, data, requestId) => {
  const workKey = getContextSummaryWorkKey(data, requestId)
  if (!workKey) return
  activeKeys.delete(workKey)
  if (requestId !== undefined && requestId !== null && requestId !== '') {
    const requestKeySet = requestKeys.get(requestId)
    if (requestKeySet) {
      requestKeySet.delete(workKey)
      if (requestKeySet.size === 0) requestKeys.delete(requestId)
    }
  }
}

export const clearContextSummaryRequest = (activeKeys, requestKeys, requestId) => {
  if (requestId === undefined || requestId === null || requestId === '') return
  const requestKeySet = requestKeys.get(requestId)
  if (requestKeySet) {
    requestKeySet.forEach(workKey => activeKeys.delete(workKey))
    requestKeys.delete(requestId)
  }
}

export const clearAllContextSummaryWorks = (activeKeys, requestKeys) => {
  activeKeys.clear()
  requestKeys.clear()
}

export const shouldIgnoreExternalSessionEvent = (sequenceBySession, data, currentSessionId) => {
  if (!data) return false
  if (
    data.session_id !== undefined &&
    data.session_id !== null &&
    data.session_id !== '' &&
    currentSessionId !== undefined &&
    currentSessionId !== null &&
    currentSessionId !== '' &&
    String(data.session_id) !== String(currentSessionId)
  ) return true
  if (!Number.isFinite(data.event_sequence_no)) return false
  const sessionId = data.session_id || currentSessionId
  if (!sessionId) return false
  const scope = data.work_id !== undefined && data.work_id !== null && data.work_id !== ''
    ? `work:${String(sessionId)}:${String(data.work_id)}`
    : `session:${String(sessionId)}`
  const previousSequenceNo = sequenceBySession.get(scope)
  if (previousSequenceNo !== undefined && data.event_sequence_no <= previousSequenceNo) return true
  sequenceBySession.set(scope, data.event_sequence_no)
  return false
}

const getSummaryLifecycleKey = (data, requestId) => {
  const workKey = getContextSummaryWorkKey(data, requestId)
  if (!workKey) return null
  const sessionId = data?.session_id !== undefined && data?.session_id !== null && data.session_id !== ''
    ? String(data.session_id)
    : ''
  return `${sessionId}:${workKey}`
}

export function createContextSummaryTracker() {
  const sequenceByScope = new Map()
  const endedWorkKeys = new Set()

  return {
    shouldIgnoreExternalSessionEvent(data, currentSessionId) {
      return shouldIgnoreExternalSessionEvent(sequenceByScope, data, currentSessionId)
    },

    startContextSummaryWork(activeKeys, requestKeys, data, requestId) {
      const lifecycleKey = getSummaryLifecycleKey(data, requestId)
      if (!lifecycleKey || endedWorkKeys.has(lifecycleKey)) return
      startContextSummaryWork(activeKeys, requestKeys, data, requestId)
    },

    endContextSummaryWork(activeKeys, requestKeys, data, requestId) {
      const lifecycleKey = getSummaryLifecycleKey(data, requestId)
      if (!lifecycleKey) return
      endedWorkKeys.add(lifecycleKey)
      endContextSummaryWork(activeKeys, requestKeys, data, requestId)
    },

    clearContextSummaryRequest(activeKeys, requestKeys, requestId) {
      clearContextSummaryRequest(activeKeys, requestKeys, requestId)
    },

    clearAllContextSummaryWorks(activeKeys, requestKeys) {
      clearAllContextSummaryWorks(activeKeys, requestKeys)
      sequenceByScope.clear()
      endedWorkKeys.clear()
    }
  }
}
