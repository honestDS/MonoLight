import {
  createSessionReconnectHandler,
  getHistoryMessageCursor,
  resumeSessionStream
} from './streamResume.js'

export function useChatStreamResume({
  chatState,
  sessionManager,
  transport,
  isSessionScopeActive,
  createStreamCallbacks
}) {
  const resumeSelectedSessionStream = async (session, historyData = []) => {
    const sessionId = session?.session_id
    const latestSession = sessionManager.sessions.value.find(item => item.session_id === sessionId) || session
    const isCurrentSession = () => (
      isSessionScopeActive()
      && sessionId === sessionManager.currentSessionId.value
    )
    if (!isCurrentSession()) return

    const callbacks = createStreamCallbacks({
      isCurrentSession,
      deferLoadingUntilResumeComplete: true
    })

    try {
      await resumeSessionStream({
        session,
        latestSession,
        transportMode: transport.transportMode.value,
        isCurrentSession,
        setLoading: value => { chatState.loading.value = value },
        resume: () => transport.resumeSession({
          sessionId,
          historyMessageId: getHistoryMessageCursor(historyData),
          callbacks
        })
      })
    } catch (err) {
      console.error('WebSocket会话恢复失败:', err)
    }
  }

  transport.setReconnectHandler(createSessionReconnectHandler({
    getCurrentSessionId: () => sessionManager.currentSessionId.value,
    getSession: sessionId => sessionManager.sessions.value.find(item => item.session_id === sessionId),
    getHistoryMessages: () => chatState.messages.value,
    resumeSession: resumeSelectedSessionStream
  }))

  return { resumeSelectedSessionStream }
}
