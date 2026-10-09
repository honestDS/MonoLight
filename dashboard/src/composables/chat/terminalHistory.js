import {
  findAssistantResponseReplacementIndex,
  isAssistantResponse,
  isPlainAssistantResponse,
  mergeAssistantResponse,
  mergeAssistantResponseIntoList
} from '../../utils/assistantResponseIdentity.js'
import { truncateErrorMessage } from '../../utils/errorMessage.js'
import { findThinkingIndex, insertMessageBeforeThinking } from './thinkingTracker.js'

const normalizeMessageContent = (content) => {
  if (typeof content === 'string') {
    try {
      return JSON.parse(content)
    } catch {
      return content
    }
  }
  return content
}

const getToolMessageDedupeKeys = (message) => {
  const content = normalizeMessageContent(message?.content)
  const keys = new Set()
  const toolCalls = content?.tool_calls || message?.tool_calls || []
  for (const toolCall of toolCalls) {
    const toolCallId = toolCall?.id || toolCall?.function?.id
    if (toolCallId) keys.add(`tool_call:${toolCallId}`)
  }

  const toolCallId = content?.tool_call_id || message?.tool_call_id
  if (toolCallId) keys.add(`tool_result:${toolCallId}`)
  return [...keys]
}

const isToolCall = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'object' && content !== null) {
      return content.role === 'assistant' && content.tool_calls && content.tool_calls.length > 0
    }
    if (typeof content === 'string') {
      const parsed = JSON.parse(content)
      return parsed.role === 'assistant' && parsed.tool_calls && parsed.tool_calls.length > 0
    }
    return false
  } catch {
    return false
  }
}

const normalizeStableId = value => value === undefined || value === null || value === '' ? null : String(value)

const findLastRelatedStreamMessageIndex = (messages, workId, requestId) => {
  const stableWorkId = normalizeStableId(workId)
  if (stableWorkId) {
    const workMessageIdx = messages.findLastIndex(message =>
      message.role !== 'thinking' && normalizeStableId(message.work_id) === stableWorkId
    )
    if (workMessageIdx !== -1) return workMessageIdx
  }

  const stableRequestId = normalizeStableId(requestId)
  if (!stableRequestId) return -1
  return messages.findLastIndex(message =>
    message.role !== 'thinking' && normalizeStableId(message.request_id) === stableRequestId
  )
}

export const processStreamError = (messagesRef, errorMessage, thinkingId, requestId = null, workId = null, eventId = null, messageId = null) => {
  const normalizedMessageId = Number(messageId)
  const hasMessageId = Number.isSafeInteger(normalizedMessageId) && normalizedMessageId > 0
  const alreadyHandled = messagesRef.value.some(message =>
    message.role === 'err' && (
      (hasMessageId && Number(message.db_id) === normalizedMessageId) ||
      (eventId && message.event_id === eventId) ||
      (workId && message.work_id === workId)
    )
  )
  if (alreadyHandled) return false

  const newMsg = {
    id: `err_${requestId || Date.now()}`,
    role: 'err',
    content: truncateErrorMessage(errorMessage),
    created_at: Date.now() / 1000,
    ...(requestId ? { request_id: requestId } : {}),
    ...(workId ? { work_id: workId } : {}),
    ...(eventId ? { event_id: eventId } : {}),
    ...(hasMessageId ? { db_id: normalizedMessageId } : {})
  }
  const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
  if (lastRelatedIdx !== -1) {
    messagesRef.value.splice(lastRelatedIdx + 1, 0, newMsg)
    return true
  }
  messagesRef.value.push(newMsg)
  return true
}

const findToolCallIndex = (messages, toolCallId) => {
  if (!toolCallId) return -1
  return messages.findIndex(message => getToolMessageDedupeKeys(message).includes(`tool_call:${toolCallId}`))
}

export const insertTerminalHistory = (
  messagesRef,
  aiMessages,
  thinkingId,
  requestId = null,
  workId = null,
  resolveDisplayContent = content => content ?? ''
) => {
  if (!aiMessages || aiMessages.length === 0) return

  let orderedMessages = []
  const pendingToolKeys = new Set()
  for (const message of aiMessages) {
    if (isAssistantResponse(message)) {
      const pendingReplacementIndex = findAssistantResponseReplacementIndex(orderedMessages, message)
      if (pendingReplacementIndex !== -1) {
        orderedMessages = mergeAssistantResponseIntoList(orderedMessages, message)
        continue
      }

      if (isPlainAssistantResponse(message)) {
        const displayContent = resolveDisplayContent(
          message.content,
          message.refusal,
          message.finish_reason
        )
        const hasDisplayContent = typeof displayContent === 'string'
          ? Boolean(displayContent.trim())
          : displayContent !== undefined && displayContent !== null
        const hasFiles = Array.isArray(message.files) && message.files.length > 0
        const hasReasoning = typeof message.reasoning_content === 'string' && Boolean(message.reasoning_content.trim())
        if (
          !hasDisplayContent &&
          !hasFiles &&
          !hasReasoning &&
          findAssistantResponseReplacementIndex(messagesRef.value, message) === -1
        ) continue
      }
    }

    const messageKeys = getToolMessageDedupeKeys(message)
    if ([...messageKeys].some(key => pendingToolKeys.has(key))) continue
    messageKeys.forEach(key => pendingToolKeys.add(key))
    orderedMessages.push(message)
  }

  if (orderedMessages.length === 0) return

  const findExistingMessageIndex = message => {
    const messageKeys = getToolMessageDedupeKeys(message)
    if (messageKeys.length > 0) {
      return messagesRef.value.findIndex(existingMessage => {
        const existingKeys = getToolMessageDedupeKeys(existingMessage)
        return messageKeys.some(key => existingKeys.includes(key))
      })
    }

    if (isAssistantResponse(message)) {
      return findAssistantResponseReplacementIndex(messagesRef.value, message)
    }
    return -1
  }

  const findNextExistingMessageIndex = startIndex => {
    for (let index = startIndex; index < orderedMessages.length; index += 1) {
      const existingIndex = findExistingMessageIndex(orderedMessages[index])
      if (existingIndex !== -1) return existingIndex
    }
    return -1
  }

  let insertionCursor = null
  for (let index = 0; index < orderedMessages.length; index += 1) {
    const message = orderedMessages[index]
    let existingIndex = findExistingMessageIndex(message)
    if (existingIndex !== -1) {
      const existingMessage = messagesRef.value[existingIndex]
      const messageKeys = getToolMessageDedupeKeys(message)
      if (isAssistantResponse(message)) {
        const mergedMessage = messageKeys.length > 0
          ? mergeAssistantResponse(existingMessage, message)
          : message
        if (messageKeys.length > 0) {
          messagesRef.value[existingIndex] = mergedMessage
          if (findAssistantResponseReplacementIndex(messagesRef.value, mergedMessage) !== -1) {
            messagesRef.value = mergeAssistantResponseIntoList(messagesRef.value, mergedMessage)
          }
        } else {
          messagesRef.value = mergeAssistantResponseIntoList(messagesRef.value, mergedMessage)
        }
        existingIndex = findExistingMessageIndex(mergedMessage)
      } else {
        messagesRef.value[existingIndex] = {
          ...existingMessage,
          ...message,
          id: existingMessage.id ?? message.id,
          response_id: existingMessage.response_id ?? message.response_id,
          request_id: existingMessage.request_id ?? message.request_id,
          work_id: existingMessage.work_id ?? message.work_id,
          turn: existingMessage.turn ?? message.turn
        }
        existingIndex = findExistingMessageIndex(message)
      }
      if (existingIndex !== -1) insertionCursor = existingIndex + 1
      continue
    }

    const nextExistingIndex = findNextExistingMessageIndex(index + 1)
    let insertAt = nextExistingIndex
    if (insertAt === -1 && insertionCursor !== null) {
      insertAt = insertionCursor
    }
    if (insertAt === -1) {
      insertAt = findThinkingIndex(messagesRef.value, thinkingId, requestId)
    }
    if (insertAt === -1) {
      const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
      insertAt = lastRelatedIdx !== -1 ? lastRelatedIdx + 1 : messagesRef.value.length
    }

    messagesRef.value.splice(insertAt, 0, message)
    insertionCursor = insertAt + 1
  }
}

export const processStreamToolStart = (messagesRef, toolCall, thinkingId, responseId, requestId, workId) => {
  const existingIdx = findToolCallIndex(messagesRef.value, toolCall.id)
  if (existingIdx !== -1) {
    const existingMsg = messagesRef.value[existingIdx]
    const existingContent = normalizeMessageContent(existingMsg.content)
    const existingToolCalls = Array.isArray(existingContent?.tool_calls) ? existingContent.tool_calls : []
    messagesRef.value[existingIdx] = {
      ...existingMsg,
      content: JSON.stringify({
        ...existingContent,
        role: 'assistant',
        tool_calls: existingToolCalls.map(existingToolCall => {
          const existingToolCallId = existingToolCall?.id || existingToolCall?.function?.id
          if (existingToolCallId !== toolCall.id) return existingToolCall
          return {
            ...existingToolCall,
            id: toolCall.id,
            name: toolCall.name,
            arguments: toolCall.arguments
          }
        })
      }),
      response_id: existingMsg.response_id || responseId,
      request_id: existingMsg.request_id || requestId,
      work_id: existingMsg.work_id || workId
    }
    return
  }

  const sameResponseToolCallIdx = responseId
    ? messagesRef.value.findIndex(message =>
      message.response_id === responseId &&
      message.role === 'assistant' &&
      isToolCall(message)
    )
    : -1
  if (sameResponseToolCallIdx !== -1) {
    const existingMsg = messagesRef.value[sameResponseToolCallIdx]
    const existingContent = normalizeMessageContent(existingMsg.content)
    const existingToolCalls = Array.isArray(existingContent?.tool_calls) ? existingContent.tool_calls : []
    messagesRef.value[sameResponseToolCallIdx] = {
      ...existingMsg,
      content: JSON.stringify({
        ...existingContent,
        role: 'assistant',
        tool_calls: [...existingToolCalls, {
          id: toolCall.id,
          name: toolCall.name,
          arguments: toolCall.arguments
        }]
      }),
      request_id: existingMsg.request_id || requestId,
      work_id: existingMsg.work_id || workId
    }
    return
  }

  const streamedContentIdx = responseId
    ? messagesRef.value.findIndex(message =>
      message.response_id === responseId &&
      message.role === 'assistant' &&
      !isToolCall(message)
    )
    : -1
  const streamedContentMsg = streamedContentIdx !== -1
    ? messagesRef.value[streamedContentIdx]
    : null

  const contentObj = {
    role: 'assistant',
    content: streamedContentMsg?.content || undefined,
    tool_calls: [{
      id: toolCall.id,
      name: toolCall.name,
      arguments: toolCall.arguments
    }]
  }

  const newMsg = {
    ...(streamedContentMsg || {}),
    id: streamedContentMsg?.id || `tool_call_${toolCall.id || Date.now()}`,
    role: 'assistant',
    content: JSON.stringify(contentObj),
    response_id: streamedContentMsg?.response_id || responseId,
    request_id: streamedContentMsg?.request_id || requestId,
    work_id: streamedContentMsg?.work_id || workId,
    created_at: streamedContentMsg?.created_at || Date.now() / 1000
  }

  // 工具调用与同轮流式正文属于同一条助手消息，合并后可避免后续事件将正文覆盖掉。
  if (streamedContentIdx !== -1) {
    newMsg.id = streamedContentMsg.id
    newMsg.created_at = streamedContentMsg.created_at || newMsg.created_at
    messagesRef.value[streamedContentIdx] = newMsg
    return
  }

  if (insertMessageBeforeThinking(messagesRef.value, newMsg, thinkingId, requestId)) return

  const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
  if (lastRelatedIdx !== -1) {
    messagesRef.value.splice(lastRelatedIdx + 1, 0, newMsg)
    return
  }

  messagesRef.value.push(newMsg)
}
