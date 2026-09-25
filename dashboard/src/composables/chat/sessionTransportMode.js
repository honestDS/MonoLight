export const resolveSessionTransportMode = session => (
  session?.source === 'http' ? 'http' : 'ws'
)

export const persistSessionTransportMode = async ({
  sessionId,
  mode,
  sessions,
  updateSessionSetting
}) => {
  const session = sessions.find(item => item.session_id === sessionId)
  const previousMode = session?.source

  try {
    await updateSessionSetting(sessionId, { transport_mode: mode })
    if (session) session.source = mode
  } catch (error) {
    if (session && previousMode !== undefined) {
      session.source = previousMode
    }
    throw error
  }
}

export const activateSelectedSessionTransportMode = async ({
  session,
  mode,
  historyData,
  applyTransportMode,
  resumeStream
}) => {
  await applyTransportMode(mode)
  if (mode === 'ws' && session?.session_id) {
    await resumeStream(session, historyData)
  }
  return mode
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
