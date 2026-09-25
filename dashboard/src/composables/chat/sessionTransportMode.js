export const resolveSessionTransportMode = session => (
  session?.source === 'http' ? 'http' : 'ws'
)

export const persistSessionTransportMode = async ({
  sessionId,
  mode,
  sessions,
  getCurrentSessionId,
  getCurrentTransportMode = () => mode,
  updateSessionSetting,
  applyTransportMode
}) => {
  const previousSession = sessions.find(session => session.session_id === sessionId)
  const previousMode = previousSession?.source

  try {
    await updateSessionSetting(sessionId, { transport_mode: mode })

    const session = sessions.find(item => item.session_id === sessionId)
    if (session) session.source = mode
    if (getCurrentSessionId() === sessionId) {
      await applyTransportMode(mode)
    }
  } catch (error) {
    const session = sessions.find(item => item.session_id === sessionId)
    if (session && previousMode !== undefined) {
      session.source = previousMode
    }
    if (getCurrentSessionId() === sessionId) {
      const restoreMode = previousMode === 'http' || previousMode === 'ws'
        ? previousMode
        : getCurrentTransportMode()
      await applyTransportMode(restoreMode)
    }
    throw error
  }
}

export const resumeSelectedSessionByTransport = async ({
  session,
  historyData,
  transportMode,
  getCurrentSessionId,
  sessions,
  processHttpSessionSnapshot,
  resumeStream
}) => {
  if (
    transportMode === 'http'
    && session?.session_id === getCurrentSessionId()
  ) {
    await processHttpSessionSnapshot(sessions)
    return 'http'
  }

  await resumeStream(session, historyData)
  return 'ws'
}
