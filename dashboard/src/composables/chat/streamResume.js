import {
  findAssistantResponseReplacementIndex,
  mergeAssistantResponseIntoList
} from '../../utils/assistantResponseIdentity.js'

const hasIdentity = value => value !== undefined && value !== null && value !== ''

export const applyResumedTurnEnd = (messages, data, requestId = null) => {
  if (!data || data.type !== 'turn_end') return messages

  const terminalMessage = {
    role: 'assistant',
    content: data.content,
    ...(hasIdentity(data.message_id) ? { db_id: data.message_id } : {}),
    ...(hasIdentity(data.response_id) ? { response_id: data.response_id } : {}),
    ...(hasIdentity(data.request_id ?? requestId) ? { request_id: data.request_id ?? requestId } : {}),
    ...(hasIdentity(data.work_id) ? { work_id: data.work_id } : {}),
    ...(hasIdentity(data.turn) ? { turn: data.turn } : {}),
    ...(hasIdentity(data.finish_reason) ? { finish_reason: data.finish_reason } : {}),
    ...(data.finish_details && typeof data.finish_details === 'object' ? { finish_details: data.finish_details } : {}),
    ...(hasIdentity(data.reasoning_content) ? { reasoning_content: data.reasoning_content } : {}),
    ...(hasIdentity(data.refusal) ? { refusal: data.refusal } : {}),
    ...(data.provider_metadata && typeof data.provider_metadata === 'object' ? { provider_metadata: data.provider_metadata } : {}),
    ...(data.message_provider_metadata && typeof data.message_provider_metadata === 'object' ? { message_provider_metadata: data.message_provider_metadata } : {})
  }

  if (findAssistantResponseReplacementIndex(messages, terminalMessage) === -1) return messages
  return mergeAssistantResponseIntoList(messages, terminalMessage)
}

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
