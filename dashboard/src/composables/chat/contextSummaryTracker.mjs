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
  if (!data || data.work_id !== undefined && data.work_id !== null && data.work_id !== '') return false
  if (!Number.isFinite(data.event_sequence_no)) return false
  const sessionId = data.session_id || currentSessionId
  if (!sessionId) return false
  const previousSequenceNo = sequenceBySession.get(sessionId)
  if (previousSequenceNo !== undefined && data.event_sequence_no <= previousSequenceNo) return true
  sequenceBySession.set(sessionId, data.event_sequence_no)
  return false
}
