import {
  isToolCall,
  isToolResult,
  normalizeMessageContent
} from '../../utils'

const normalizeHistoryMessage = (message) => {
  const normalizedMessage = {
    ...message,
    db_id: message?.db_id ?? message?.id
  }
  const content = normalizeMessageContent(message?.content)
  if (message?.type === 'background_result' && content?.type === 'background_tool_result') {
    return {
      ...normalizedMessage,
      role: 'background_system',
      content: JSON.stringify(content)
    }
  }
  return normalizedMessage
}

const getAuditConfirmationRecordId = (message) => {
  if (message?.type !== 'audit_confirmation') return null
  try {
    const payload = typeof message.content === 'string' ? JSON.parse(message.content) : message.content
    return payload?.audit_record_id ? String(payload.audit_record_id) : null
  } catch {
    return null
  }
}

const parseAuditConfirmationResponse = (response) => {
  for (const content of [response?.choices?.[0]?.message?.content, response?.content]) {
    try {
      const payload = typeof content === 'string' ? JSON.parse(content) : content
      if (payload?.type === 'audit_confirmation') return payload
    } catch {}
  }
  return null
}

const getLocalMessageType = (message) => {
  if (message?.type === 'audit_decision' && message.role === 'user') return 'user'
  if (message?.type && message.type !== 'text') return message.type
  if (isToolCall(message)) return 'tool_call'
  if (isToolResult(message)) return 'tool_result'
  return message?.role || message?.type || 'message'
}

const findTransientHistoryMessageIndex = (messages, historyMessage) => {
  const historyContent = normalizeMessageContent(historyMessage?.content)
  const historyType = getLocalMessageType(historyMessage)
  const parseSafePositiveInteger = value => {
    const parsed = Number(value)
    return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
  }
  const incomingId = historyType === 'user'
    ? parseSafePositiveInteger(historyMessage?.db_id ?? historyMessage?.id)
    : null

  return messages.findIndex((message, index) => {
    if (message?.db_id) return false
    if (getLocalMessageType(message) !== historyType) return false
    if (JSON.stringify(normalizeMessageContent(message?.content)) !== JSON.stringify(historyContent)) return false
    if (JSON.stringify(message?.attachments ?? []) !== JSON.stringify(historyMessage?.attachments ?? [])) return false

    const messageRequestId = message?.request_id
    const historyRequestId = historyMessage?.request_id
    if (
      messageRequestId != null && messageRequestId !== '' &&
      historyRequestId != null && historyRequestId !== '' &&
      messageRequestId !== historyRequestId
    ) return false

    if (historyType !== 'user' || incomingId === null) return true

    const hasEarlierUser = messages.slice(0, index).some(otherMessage => {
      if (otherMessage?.role !== 'user') return false
      const boundaryId = parseSafePositiveInteger(otherMessage?.db_id ?? otherMessage?.message_id)
      return boundaryId !== null && boundaryId >= incomingId
    })
    const hasLaterUser = messages.slice(index + 1).some(otherMessage => {
      if (otherMessage?.role !== 'user') return false
      const boundaryId = parseSafePositiveInteger(otherMessage?.db_id ?? otherMessage?.message_id)
      return boundaryId !== null && boundaryId <= incomingId
    })
    return !hasEarlierUser && !hasLaterUser
  })
}

export {
  normalizeHistoryMessage,
  getAuditConfirmationRecordId,
  parseAuditConfirmationResponse,
  getLocalMessageType,
  findTransientHistoryMessageIndex
}
