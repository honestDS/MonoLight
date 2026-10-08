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
  return messages.findIndex(message => {
    if (message?.db_id) return false
    if (getLocalMessageType(message) !== historyType) return false
    return JSON.stringify(normalizeMessageContent(message?.content)) === JSON.stringify(historyContent)
  })
}

export {
  normalizeHistoryMessage,
  getAuditConfirmationRecordId,
  parseAuditConfirmationResponse,
  getLocalMessageType,
  findTransientHistoryMessageIndex
}
