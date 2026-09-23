export const shouldResumeSessionStream = ({ session, transportMode }) => {
  if (transportMode !== 'ws' || !session?.session_id) return false
  return !session.source || session.source === 'http' || session.source === 'ws'
}

export const getInitialResumeLoading = ({ session, transportMode }) =>
  shouldResumeSessionStream({ session, transportMode }) && session?.is_loading === true

export const resumeSessionStream = async ({
  session, latestSession = session, transportMode, isCurrentSession, setLoading, resume
}) => {
  if (!isCurrentSession()) return
  if (!shouldResumeSessionStream({ session: latestSession || session, transportMode })) {
    setLoading(false)
    return
  }

  if (session?.is_loading || latestSession?.is_loading) setLoading(true)
  let resumed = false
  try {
    resumed = await resume()
  } finally {
    if (!resumed && isCurrentSession()) setLoading(false)
  }
}

export const createSessionReconnectHandler = ({
  getCurrentSessionId, getSession, getHistoryMessages, resumeSession
}) => async () => {
  const sessionId = getCurrentSessionId()
  if (!sessionId) return false

  const session = getSession(sessionId) || { session_id: sessionId }
  const historyData = getHistoryMessages()
    .filter(message => typeof message?.db_id === 'number'
      && Number.isSafeInteger(message.db_id)
      && message.db_id >= 0)
    .map(message => ({ id: message.db_id }))

  return resumeSession(session, historyData)
}
