import {
  findAssistantResponseReplacementIndex,
  mergeAssistantResponse,
  mergeAssistantResponseIntoList
} from '../../utils/assistantResponseIdentity.js'
import { finalizeStreamReasoning } from './reasoningTracker.js'

const hasIdentity = value => value !== undefined && value !== null && value !== ''

export const applyResumedTurnEnd = (
  messages,
  data,
  requestId = null,
  resolveDisplayContent
) => {
  if (!data || data.type !== 'turn_end') return messages

  const hasValue = value => typeof value === 'string' ? Boolean(value.trim()) : hasIdentity(value)
  const resolvedRequestId = data.request_id ?? requestId
  const resolveDisplay = typeof resolveDisplayContent === 'function'
    ? resolveDisplayContent
    : (content, refusal) => hasValue(content) ? content : hasValue(refusal) ? refusal : ''
  const terminalMessage = {
    role: 'assistant',
    content: data.content,
    ...(hasIdentity(data.message_id) ? { db_id: String(data.message_id) } : {}),
    ...(hasIdentity(data.response_id) ? { response_id: data.response_id } : {}),
    ...(hasIdentity(resolvedRequestId) ? { request_id: resolvedRequestId } : {}),
    ...(hasIdentity(data.work_id) ? { work_id: data.work_id } : {}),
    ...(hasIdentity(data.turn) ? { turn: data.turn } : {}),
    ...(hasIdentity(data.finish_reason) ? { finish_reason: data.finish_reason } : {}),
    ...(data.finish_details && typeof data.finish_details === 'object' ? { finish_details: data.finish_details } : {}),
    ...(hasIdentity(data.reasoning_content) ? { reasoning_content: data.reasoning_content } : {}),
    ...(hasIdentity(data.refusal) ? { refusal: data.refusal } : {}),
    ...(data.provider_metadata && typeof data.provider_metadata === 'object' ? { provider_metadata: data.provider_metadata } : {}),
    ...(data.message_provider_metadata && typeof data.message_provider_metadata === 'object' ? { message_provider_metadata: data.message_provider_metadata } : {}),
    _stream_finalized: true
  }
  const hasExisting = findAssistantResponseReplacementIndex(messages, terminalMessage) !== -1
  if (!hasExisting) {
    const identitySeed = hasIdentity(data.response_id)
      ? data.response_id
      : hasIdentity(data.work_id) && hasIdentity(data.turn)
        ? `${data.work_id}:${data.turn}`
        : hasIdentity(resolvedRequestId) && hasIdentity(data.turn)
          ? `${resolvedRequestId}:${data.turn}`
          : hasIdentity(data.message_id)
            ? data.message_id
            : Date.now()
    terminalMessage.id = `assistant_${String(identitySeed)}`
    terminalMessage.created_at = Date.now() / 1000
  }

  let mergedMessages = mergeAssistantResponseIntoList(
    Array.isArray(messages) ? messages : [],
    hasExisting ? terminalMessage : mergeAssistantResponse({}, terminalMessage)
  )
  let targetIndex = findAssistantResponseReplacementIndex(mergedMessages, terminalMessage)
  if (targetIndex === -1) targetIndex = mergedMessages.length - 1

  const parseContent = content => {
    if (typeof content !== 'string') return content
    try {
      return JSON.parse(content)
    } catch {
      return null
    }
  }
  const getMessageContent = message => {
    const parsedContent = parseContent(message?.content)
    if (
      parsedContent
      && typeof parsedContent === 'object'
      && !Array.isArray(parsedContent)
      && ('content' in parsedContent || Array.isArray(parsedContent.tool_calls))
    ) return parsedContent.content
    return message?.content
  }
  const targetMessage = mergedMessages[targetIndex]
  const parsedTargetContent = parseContent(targetMessage?.content)
  const hasTargetToolCalls = Array.isArray(targetMessage?.tool_calls) && targetMessage.tool_calls.length > 0
    || Array.isArray(parsedTargetContent?.tool_calls) && parsedTargetContent.tool_calls.length > 0
  const hasSnapshotDisplay = hasValue(data.content) || hasValue(data.refusal)
  const hasTargetDisplay = hasValue(getMessageContent(targetMessage)) || hasTargetToolCalls
  if (targetMessage && (hasSnapshotDisplay || !hasTargetDisplay)) {
    const resolvedContent = resolveDisplay(data.content, data.refusal, data.finish_reason)
    if (resolvedContent !== undefined && resolvedContent !== null) {
      mergedMessages = mergeAssistantResponseIntoList(mergedMessages, {
        ...terminalMessage,
        content: resolvedContent
      })
    }
  }

  targetIndex = findAssistantResponseReplacementIndex(mergedMessages, terminalMessage)
  if (targetIndex === -1) targetIndex = mergedMessages.length - 1
  const canonicalMessage = mergedMessages[targetIndex] || terminalMessage
  const canonicalResponseId = canonicalMessage.response_id ?? data.response_id
  const canonicalWorkId = canonicalMessage.work_id ?? data.work_id
  const canonicalTurn = canonicalMessage.turn ?? data.turn
  const canonicalRequestId = canonicalMessage.request_id ?? resolvedRequestId
  const reasoningContent = hasValue(data.reasoning_content)
    ? data.reasoning_content
    : hasValue(canonicalMessage.reasoning_content)
      ? canonicalMessage.reasoning_content
      : null

  return finalizeStreamReasoning(mergedMessages, reasoningContent, {
    turn: canonicalTurn,
    responseId: canonicalResponseId,
    requestId: canonicalRequestId,
    workId: canonicalWorkId
  })
}

export const shouldResumeSessionStream = ({ session, transportMode }) => {
  if (transportMode !== 'ws' || !session?.session_id) return false
  return !session.source || session.source === 'http' || session.source === 'ws'
}

export const getInitialResumeLoading = ({ session, transportMode }) =>
  shouldResumeSessionStream({ session, transportMode }) && session?.is_reply_running === true

export const getHistoryMessageCursor = messages => {
  if (!Array.isArray(messages)) return 0

  const databaseIds = messages
    .map(message => Number(message?.db_id))
    .filter(messageId => Number.isSafeInteger(messageId) && messageId >= 0)
  const candidateIds = databaseIds.length > 0
    ? databaseIds
    : messages
        .map(message => Number(message?.id))
        .filter(messageId => Number.isSafeInteger(messageId) && messageId >= 0)

  return candidateIds.reduce((latestId, messageId) => Math.max(latestId, messageId), 0)
}

export const resumeSessionStream = async ({
  session, latestSession = session, transportMode, isCurrentSession, setLoading, resume
}) => {
  if (!isCurrentSession()) return
  if (!shouldResumeSessionStream({ session: latestSession || session, transportMode })) {
    setLoading(false)
    return
  }

  if (session?.is_reply_running || latestSession?.is_reply_running) setLoading(true)
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
    .filter(message => {
      const dbId = message?.db_id
      return (typeof dbId === 'number' || (typeof dbId === 'string' && dbId.trim() !== ''))
        && Number.isSafeInteger(Number(dbId))
        && Number(dbId) >= 0
    })
    .map(message => ({ id: Number(message.db_id) }))

  return resumeSession(session, historyData)
}
