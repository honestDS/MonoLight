import { getMessageDbId } from '../../utils/assistantResponseIdentity.js'
import { filterToolOutputMessages, isToolOutputMessage } from '../../utils/toolOutputVisibility.js'

const parseContent = (content) => {
  if (typeof content === 'string') {
    try {
      const parsed = JSON.parse(content)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
    } catch {
      return null
    }
  }
  return content && typeof content === 'object' && !Array.isArray(content) ? content : null
}

const getComparableId = (value) => {
  if (value === null || value === undefined || value === '') return null
  return String(value)
}

const hasValidStatus = (status) => typeof status === 'string' && status.trim() !== ''

const getToolCallId = (message) => {
  const serializedMessage = parseContent(message?.content)
  const toolCallId = serializedMessage?.tool_call_id ?? message?.tool_call_id
  return toolCallId === null || toolCallId === undefined || toolCallId === '' ? null : String(toolCallId)
}

const mergeAuditToolResultIntoList = (messages, remoteMessage) => {
  const normalizedRemoteMessage = {
    ...remoteMessage,
    db_id: remoteMessage.db_id ?? remoteMessage.id
  }
  const remoteDbId = getMessageDbId(normalizedRemoteMessage)
  const remoteToolCallId = getToolCallId(normalizedRemoteMessage)
  let messageIndex = remoteDbId === null
    ? -1
    : messages.findIndex(message => getMessageDbId(message) === remoteDbId)

  if (messageIndex === -1 && remoteToolCallId !== null) {
    messageIndex = messages.findIndex(message => getToolCallId(message) === remoteToolCallId)
  }
  if (messageIndex === -1) return [...messages, normalizedRemoteMessage]

  return messages.map((message, index) => {
    if (index !== messageIndex) return message
    return {
      ...message,
      ...normalizedRemoteMessage,
      id: message?.id ?? normalizedRemoteMessage.id,
      response_id: message?.response_id ?? normalizedRemoteMessage.response_id,
      request_id: message?.request_id ?? normalizedRemoteMessage.request_id,
      work_id: message?.work_id ?? normalizedRemoteMessage.work_id,
      turn: message?.turn ?? normalizedRemoteMessage.turn
    }
  })
}

const matchesMessageId = (message, messageId) => {
  const persistedIds = [message?.db_id, message?.message_id]
  if (persistedIds.some(value => getComparableId(value) === messageId)) return true

  const localId = message?.id
  return (typeof localId === 'number' || (typeof localId === 'string' && /^\d+$/.test(localId)))
    && getComparableId(localId) === messageId
}

export const applyAuditConfirmationStatusToMessages = (messages, data) => {
  if (!Array.isArray(messages) || !data || typeof data !== 'object') {
    return { messages, updated: false }
  }

  const messageId = getComparableId(data.message_id)
  const auditRecordId = getComparableId(data.audit_record_id)
  const incomingContent = parseContent(data.content)
  const hasIncomingContent = incomingContent !== null
  const hasMessageIdUpdate = messageId !== null
  const hasStatusUpdate = hasValidStatus(data.status)
  if (!hasIncomingContent && !hasMessageIdUpdate && !hasStatusUpdate) {
    return { messages, updated: false }
  }

  let messageIndex = -1
  if (messageId !== null) {
    messageIndex = messages.findIndex(message =>
      message?.type === 'audit_confirmation' && matchesMessageId(message, messageId)
    )
  }
  if (messageIndex === -1 && auditRecordId !== null) {
    messageIndex = messages.findIndex(message => {
      if (message?.type !== 'audit_confirmation') return false
      return getComparableId(parseContent(message.content)?.audit_record_id) === auditRecordId
    })
  }
  if (messageIndex === -1) return { messages, updated: false }

  const message = messages[messageIndex]
  const mergedContent = {
    ...(parseContent(message.content) || {}),
    ...(incomingContent || {}),
    ...(hasStatusUpdate ? { status: data.status } : {})
  }
  const updatedMessage = {
    ...message,
    ...(hasMessageIdUpdate ? { db_id: data.message_id } : {}),
    content: JSON.stringify(mergedContent)
  }

  return {
    messages: messages.map((item, index) => index === messageIndex ? updatedMessage : item),
    updated: true
  }
}

export const applyAuditToolResultsUpdateToMessages = (messages, data, showToolCalls = true) => {
  if (!Array.isArray(messages) || !data || typeof data !== 'object' || !Array.isArray(data.messages)) {
    return { messages, updated: false }
  }

  const remoteMessages = filterToolOutputMessages(data.messages, showToolCalls)
  let updatedMessages = messages
  let updated = false
  for (const remoteMessage of remoteMessages) {
    if (
      !remoteMessage ||
      typeof remoteMessage !== 'object' ||
      Array.isArray(remoteMessage) ||
      !isToolOutputMessage(remoteMessage)
    ) continue
    updatedMessages = mergeAuditToolResultIntoList(updatedMessages, remoteMessage)
    updated = true
  }

  return { messages: updatedMessages, updated }
}
