// 消息处理 composable：AI 响应解析与工具调用处理
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import i18n from '../../i18n'
import { findAssistantResponseReplacementIndex, getMessageDedupeKeys, isAssistantResponse, isPlainAssistantResponse, isToolCall, isToolResult, mergeAssistantResponseIntoList, normalizeMessageContent } from '../../utils'
import { truncateErrorMessage } from '../../utils/errorMessage.js'
import { findThinkingIndex, insertMessageBeforeThinking, removeThinkingMessageByIdentity } from './thinkingTracker.js'

const t = (key, ...args) => i18n.global.t(key, ...args)

export const resolveAssistantDisplayContent = (content, refusal, finishReason) => {
  if (typeof content === 'string' ? content.trim() : content !== undefined && content !== null) return content
  if (typeof refusal === 'string' && refusal.trim()) return refusal
  if (finishReason === 'length') return t('chat.response_output_limit')
  if (finishReason === 'content_filter' || finishReason === 'refusal') return t('chat.response_refused')
  if (finishReason === 'incomplete') return t('chat.response_incomplete')
  return ''
}

const parseBackgroundSystemMessage = (item) => {
  if (item?.type !== 'background_result' && item?.role !== 'system') return null
  try {
    const payload = typeof item.content === 'string' ? JSON.parse(item.content) : item.content
    if (payload?.type === 'background_tool_result') return payload
  } catch {}
  return null
}

const insertBeforeThinking = (messagesRef, message, thinkingId, requestId) =>
  insertMessageBeforeThinking(messagesRef.value, message, thinkingId, requestId)

const findToolCallIndex = (messages, toolCallId) => {
  if (!toolCallId) return -1
  return messages.findIndex(message => getMessageDedupeKeys(message).has(`tool_call:${toolCallId}`))
}

const findToolResultIndex = (messages, toolCallId) => {
  if (!toolCallId) return -1
  return messages.findIndex(message => getMessageDedupeKeys(message).has(`tool_result:${toolCallId}`))
}

const getToolMessageDedupeKeys = (message) => {
  return [...getMessageDedupeKeys(message)].filter(key => key.startsWith('tool_call:') || key.startsWith('tool_result:'))
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

const matchesStreamIdentity = (message, responseId, workId, turn, requestId) => {
  const messageResponseId = normalizeStableId(message?.response_id)
  const stableResponseId = normalizeStableId(responseId)
  if (stableResponseId) {
    if (messageResponseId) return messageResponseId === stableResponseId
    const messageWorkId = normalizeStableId(message?.work_id)
    const stableWorkId = normalizeStableId(workId)
    if (stableWorkId && messageWorkId === stableWorkId && turn !== undefined && turn !== null) {
      return message.turn === turn
    }
    return Boolean(requestId && message?.request_id === requestId)
  }

  const stableWorkId = normalizeStableId(workId)
  const messageWorkId = normalizeStableId(message?.work_id)
  if (stableWorkId && messageWorkId === stableWorkId && turn !== undefined && turn !== null) {
    return message.turn === turn
  }
  return Boolean(requestId && message?.request_id === requestId)
}

const findStreamMessageIndexes = (messages, responseId, workId, turn, requestId, predicate) => messages
  .map((message, index) => ({ message, index }))
  .filter(({ message }) => matchesStreamIdentity(message, responseId, workId, turn, requestId) && predicate(message))

const removeMessageIndexes = (messagesRef, indexes) => {
  if (indexes.length === 0) return
  const indexesToRemove = new Set(indexes)
  messagesRef.value = messagesRef.value.filter((_, index) => !indexesToRemove.has(index))
}

export function useMessageProcessor() {
  // ==================== 消息处理方法 ====================

  const seenContentEvents = new Map()
  const shouldSkipRepeatedContentEvent = (text, responseId, workId, turn, requestId, eventId) => {
    const stableId = normalizeStableId(responseId) || (normalizeStableId(workId) && turn !== undefined && turn !== null
      ? `work:${normalizeStableId(workId)}:${turn}`
      : null)
    if (!stableId) return false

    const eventKey = eventId
      ? `event:${eventId}`
      : `content:${stableId}:${text}`
    const requestKey = normalizeStableId(requestId) || 'unknown'
    const requestKeys = seenContentEvents.get(eventKey)
    if (requestKeys?.has(requestKey)) {
      return Boolean(eventId)
    }

    const isReplayFromAnotherRequest = Boolean(requestKeys && requestId && [...requestKeys].some(key => key !== requestKey && key !== 'unknown'))
    const nextRequestKeys = requestKeys || new Set()
    nextRequestKeys.add(requestKey)
    seenContentEvents.set(eventKey, nextRequestKeys)
    if (seenContentEvents.size > 2000) {
      seenContentEvents.delete(seenContentEvents.keys().next().value)
    }
    return isReplayFromAnotherRequest
  }

  // 处理流式的增量文本推送事件
  const processStreamContent = (messagesRef, text, turn, thinkingId, finishReason, responseId, requestId, workId, eventId) => {
    // 识别排队状态
    if (finishReason === 'queued') {
      return
    }

    if (shouldSkipRepeatedContentEvent(text, responseId, workId, turn, requestId, eventId)) return

    // 1. 优先复用当前轮次已有的正文消息，避免同一 response_id 被工具消息抢占后重复创建正文
    const matchingMessages = findStreamMessageIndexes(
      messagesRef.value,
      responseId,
      workId,
      turn,
      requestId,
      message => message.role === 'assistant' && !isToolCall(message)
    )
    let targetIdx = matchingMessages.length > 0
      ? matchingMessages.reduce((best, item) => String(item.message.content || '').length > String(messagesRef.value[best].content || '').length ? item.index : best, matchingMessages[0].index)
      : -1
    if (targetIdx === -1 && requestId) {
      targetIdx = messagesRef.value.findLastIndex(message =>
        message.request_id === requestId &&
        message.role === 'assistant' &&
        !isToolCall(message) &&
        (!normalizeStableId(responseId) || !normalizeStableId(message.response_id)) &&
        (turn === undefined || turn === null || message.turn === turn)
      )
    }

    if (targetIdx !== -1) {
      const targetMsg = messagesRef.value[targetIdx]
      targetMsg.content = (targetMsg.content || '') + text
      messagesRef.value[targetIdx] = {
        ...targetMsg,
        response_id: targetMsg.response_id || responseId,
        request_id: targetMsg.request_id || requestId,
        work_id: targetMsg.work_id || workId,
        turn: targetMsg.turn ?? turn
      }
      removeMessageIndexes(messagesRef, matchingMessages.filter(item => item.index !== targetIdx).map(item => item.index))
      return
    }

    const matchingToolMessages = findStreamMessageIndexes(
      messagesRef.value,
      responseId,
      workId,
      turn,
      requestId,
      message => message.role === 'assistant' && isToolCall(message)
    )
    if (matchingToolMessages.length > 0) {
      const targetItem = matchingToolMessages[0]
      const targetContent = normalizeMessageContent(targetItem.message.content)
      messagesRef.value[targetItem.index] = {
        ...targetItem.message,
        content: JSON.stringify({
          ...(targetContent && typeof targetContent === 'object' ? targetContent : {}),
          content: `${targetContent?.content || ''}${text}`
        }),
        response_id: targetItem.message.response_id || responseId,
        request_id: targetItem.message.request_id || requestId,
        work_id: targetItem.message.work_id || workId,
        turn: targetItem.message.turn ?? turn
      }
      removeMessageIndexes(messagesRef, matchingToolMessages.slice(1).map(item => item.index))
      return
    }

    const newMsg = {
      id: `assistant_${responseId || requestId || Date.now()}`,
      role: 'assistant',
      content: text,
      turn: turn,
      response_id: responseId,
      request_id: requestId,
      work_id: workId,
      created_at: Date.now() / 1000
    }

    if (insertBeforeThinking(messagesRef, newMsg, thinkingId, requestId)) return

    const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
    if (lastRelatedIdx !== -1) {
      messagesRef.value.splice(lastRelatedIdx + 1, 0, newMsg)
      return
    }

    messagesRef.value.push(newMsg)
  }

  // 处理流式下的工具调用开始，推送 tool_call 占位
  const processStreamToolStart = (messagesRef, toolCall, thinkingId, responseId, requestId, workId) => {
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
      id: `tool_call_${toolCall.id || Date.now()}`,
      role: 'assistant', 
      content: JSON.stringify(contentObj),
      response_id: responseId,
      request_id: requestId,
      work_id: workId,
      created_at: Date.now() / 1000
    }

    // 工具调用与同轮流式正文属于同一条助手消息，合并后可避免后续事件将正文覆盖掉。
    if (streamedContentIdx !== -1) {
      newMsg.id = streamedContentMsg.id
      newMsg.created_at = streamedContentMsg.created_at || newMsg.created_at
      messagesRef.value[streamedContentIdx] = newMsg
      return
    }

    if (insertBeforeThinking(messagesRef, newMsg, thinkingId, requestId)) return

    const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
    if (lastRelatedIdx !== -1) {
      messagesRef.value.splice(lastRelatedIdx + 1, 0, newMsg)
      return
    }

    messagesRef.value.push(newMsg)
  }

  // 处理流式下的工具调用结束，推送 tool 返回结果
  const processStreamToolEnd = (messagesRef, toolEnd, responseId, requestId, workId) => {
    const existingIdx = findToolResultIndex(messagesRef.value, toolEnd.tool_call_id)
    if (existingIdx !== -1) {
      const existingMsg = messagesRef.value[existingIdx]
      messagesRef.value[existingIdx] = {
        ...existingMsg,
        content: JSON.stringify({
          role: 'tool',
          tool_call_id: toolEnd.tool_call_id,
          content: toolEnd.result
        }),
        response_id: existingMsg.response_id || responseId,
        request_id: existingMsg.request_id || requestId,
        work_id: existingMsg.work_id || workId
      }
      return
    }

    const contentObj = {
      role: 'tool',
      tool_call_id: toolEnd.tool_call_id,
      content: toolEnd.result
    }

    // 将工具返回结果与 requestId 关联
    const newMsg = {
      id: `tool_res_${toolEnd.tool_call_id || Date.now()}`,
      role: 'tool',
      content: JSON.stringify(contentObj),
      response_id: responseId,
      request_id: requestId,
      work_id: workId,
      created_at: Date.now() / 1000
    }

    const toolCallIdx = findToolCallIndex(messagesRef.value, toolEnd.tool_call_id)
    if (toolCallIdx !== -1) {
      let insertAt = toolCallIdx + 1
      while (
        insertAt < messagesRef.value.length &&
        messagesRef.value[insertAt].role === 'tool' &&
        messagesRef.value[insertAt].response_id === responseId &&
        (!workId || messagesRef.value[insertAt].work_id === workId)
      ) {
        insertAt += 1
      }
      messagesRef.value.splice(insertAt, 0, newMsg)
      return
    }

    if (insertBeforeThinking(messagesRef, newMsg, null, requestId)) return
    const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
    if (lastRelatedIdx !== -1) {
      messagesRef.value.splice(lastRelatedIdx + 1, 0, newMsg)
      return
    }
    messagesRef.value.push(newMsg)
  }

  // 处理流式下的业务错误事件
  const processStreamError = (messagesRef, errorMessage, thinkingId, requestId = null, workId = null, eventId = null) => {
    const alreadyHandled = messagesRef.value.some(message =>
      message.role === 'err' && (
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
      ...(eventId ? { event_id: eventId } : {})
    }
    const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
    if (lastRelatedIdx !== -1) {
      messagesRef.value.splice(lastRelatedIdx + 1, 0, newMsg)
      return true
    }
    messagesRef.value.push(newMsg)
    return true
  }
  
  // 处理完整的 AI 响应消息，WS 和 HTTP 共用
  const processAiResponse = (messagesRef, response, thinkingId, requestId = null) => {
    const workId = response.work_id

    const choice = response.choices?.[0]
    const choiceMessage = choice?.message
    const finishReason = choice?.finish_reason ?? response.finish_reason
    const refusal = choiceMessage?.refusal ?? response.refusal
    const finishDetails = choice?.finish_details ?? response.finish_details
    const providerMetadata = choice?.provider_metadata ?? response.provider_metadata
    const messageProviderMetadata = choiceMessage?.provider_metadata ?? response.message_provider_metadata
    let aiContent = resolveAssistantDisplayContent(
      choiceMessage ? choiceMessage.content : response.content,
      refusal,
      finishReason
    )
    const history = response.history || []
    const responseFiles = response.files || []
    const aiCreatedAt = choice?.created_at || response.created_at || null
    const role = choiceMessage?.role || response.role || 'assistant'
    if (role === 'err') aiContent = truncateErrorMessage(aiContent)

    if (finishReason === 'queued') return

    const aiMessagesToInsert = []

    if (history.length > 0) {
      const historyMessages = history
        .map((item, idx) => {
          const id = `history_${Date.now()}_${idx}`
          const dbId = item?.id ?? item?.db_id ?? item?.message_id
          const requestIdForMessage = item?.request_id ?? requestId
          const backgroundPayload = parseBackgroundSystemMessage(item)
          if (backgroundPayload) {
            return {
              ...item,
              id,
              ...(dbId !== null && dbId !== undefined && dbId !== '' ? { db_id: dbId } : {}),
              role: 'background_system',
              content: JSON.stringify(backgroundPayload),
              created_at: item.created_at || null,
              ...(requestIdForMessage ? { request_id: requestIdForMessage } : {})
            }
          }
          const isToolRelated = (item.tool_calls && item.tool_calls.length > 0) || item.role === 'tool'
          return {
            ...item,
            id,
            ...(dbId !== null && dbId !== undefined && dbId !== '' ? { db_id: dbId } : {}),
            role: item.role,
            content: isToolRelated ? JSON.stringify(item) : item.content,
            created_at: item.created_at || null,
            ...(requestIdForMessage ? { request_id: requestIdForMessage } : {})
          }
        })
        .filter((item, idx) => {
          if (idx === history.length - 1 && item.role === 'assistant' && !isToolCall({ content: item.content })) {
             return false
          }
          return true
        })

      aiMessagesToInsert.push(...historyMessages)
    }

    const tempMsg = { content: aiContent }
    let finalAiMsg = null
    let auditConfirmation = null
    try {
      const parsed = typeof aiContent === 'string' ? JSON.parse(aiContent) : aiContent
      if (parsed?.type === 'audit_confirmation') auditConfirmation = parsed
    } catch {}

    const responseRequestId = response.request_id ?? requestId
    const responseDbId = response.message_id ?? response.db_id
    if (auditConfirmation) {
      finalAiMsg = { id: `audit_confirmation_${auditConfirmation.audit_record_id || responseRequestId || Date.now()}`, role: 'assistant', type: 'audit_confirmation', content: JSON.stringify(auditConfirmation), created_at: aiCreatedAt, ...(responseRequestId ? { request_id: responseRequestId } : {}) }
    } else if (isToolResult(tempMsg)) {
      finalAiMsg = { id: `tool_result_${responseRequestId || Date.now()}`, role: 'tool', content: aiContent, created_at: aiCreatedAt, ...(responseRequestId ? { request_id: responseRequestId } : {}) }
    } else if (isToolCall(tempMsg)) {
      finalAiMsg = { id: `tool_call_${responseRequestId || Date.now()}`, role: 'assistant', content: aiContent, created_at: aiCreatedAt, ...(responseRequestId ? { request_id: responseRequestId } : {}) }
    } else {
      finalAiMsg = { id: `assistant_${responseRequestId || Date.now()}`, role: role, content: aiContent, created_at: aiCreatedAt, ...(responseRequestId ? { request_id: responseRequestId } : {}) }
    }

    if (responseFiles.length > 0) {
      finalAiMsg.files = responseFiles
    }
    if (workId) {
      finalAiMsg.work_id = workId
    }
    if (response.response_id) {
      finalAiMsg.response_id = response.response_id
    }
    if (responseDbId !== null && responseDbId !== undefined && responseDbId !== '') {
      finalAiMsg.db_id = responseDbId
    }
    if (typeof finishReason === 'string' && finishReason) {
      finalAiMsg.finish_reason = finishReason
    }
    if (finishDetails && typeof finishDetails === 'object' && Object.keys(finishDetails).length > 0) {
      finalAiMsg.finish_details = finishDetails
    }
    if (typeof refusal === 'string' && refusal) {
      finalAiMsg.refusal = refusal
    }
    if (providerMetadata && typeof providerMetadata === 'object' && Object.keys(providerMetadata).length > 0) {
      finalAiMsg.provider_metadata = providerMetadata
    }
    if (messageProviderMetadata && typeof messageProviderMetadata === 'object' && Object.keys(messageProviderMetadata).length > 0) {
      finalAiMsg.message_provider_metadata = messageProviderMetadata
    }

    aiMessagesToInsert.push(finalAiMsg)
    _insertAiMessagesByThinking(messagesRef, aiMessagesToInsert, thinkingId, requestId, workId)
  }

  // 处理工具调用消息
  const handleToolCallMessage = (messagesRef, toolCall) => {
    const lastMsg = messagesRef.value[messagesRef.value.length - 1]
    if (lastMsg && lastMsg.role === 'tool_call') {
      lastMsg.content = { ...lastMsg.content, ...toolCall }
    } else {
      messagesRef.value.push({ id: Date.now(), role: 'tool_call', content: toolCall })
    }
  }

  // 处理新会话创建
  const handleNewSession = async (sessionsRef, selectSession, thinkingId, disconnect = true) => {
    const res = await chatApi.sessionsList()
    sessionsRef.value = res.data.data || []
    
    if (sessionsRef.value.length > 0) {
      const sortedSessions = [...sessionsRef.value].sort((a, b) =>
        new Date(b.last_active) - new Date(a.last_active)
      )
      const sortedSession = sortedSessions[0]
      selectSession(sortedSession, null, disconnect)
    }
  }

  // 清理残留 thinking 消息
  const cleanupThinkingMessage = (messagesRef) => {
    for (let i = messagesRef.value.length - 1; i >= 0; i--) {
      if (messagesRef.value[i].role === 'thinking') {
        messagesRef.value.splice(i, 1)
      }
    }
  }

  // 添加用户消息
  const addUserMessage = (messagesRef, content) => {
    const userMsgId = Date.now()
    messagesRef.value.push({ id: userMsgId, role: 'user', content: content, created_at: Date.now() / 1000 })
    return userMsgId
  }

  // 添加 thinking 占位符消息
  const addThinkingMessage = (messagesRef) => {
    const thinkingId = Date.now() + 1
    messagesRef.value.push({ id: thinkingId, role: 'thinking', content: 'Thinking...' })
    return thinkingId
  }

  // 移除 thinking 占位符消息
  const removeThinkingMessage = (messagesRef, thinkingId, requestId = null) =>
    removeThinkingMessageByIdentity(messagesRef.value, thinkingId, requestId)

  // 按 thinking 位置插入 AI 消息
  const _insertAiMessagesByThinking = (messagesRef, aiMessages, thinkingId, requestId = null, workId = null) => {
    if (!aiMessages || aiMessages.length === 0) return
    const existingKeys = new Set(messagesRef.value.flatMap(getToolMessageDedupeKeys))
    let dedupedMessages = []

    for (const message of aiMessages) {
      if (isAssistantResponse(message)) {
        const replacementIndex = findAssistantResponseReplacementIndex(messagesRef.value, message)
        if (replacementIndex !== -1) {
          messagesRef.value = mergeAssistantResponseIntoList(messagesRef.value, message)
          continue
        }

        const pendingReplacementIndex = findAssistantResponseReplacementIndex(dedupedMessages, message)
        if (pendingReplacementIndex !== -1) {
          dedupedMessages = mergeAssistantResponseIntoList(dedupedMessages, message)
          continue
        }

        if (isPlainAssistantResponse(message)) {
          const displayContent = resolveAssistantDisplayContent(
            message.content,
            message.refusal,
            message.finish_reason
          )
          const hasDisplayContent = typeof displayContent === 'string'
            ? Boolean(displayContent.trim())
            : displayContent !== undefined && displayContent !== null
          const hasFiles = Array.isArray(message.files) && message.files.length > 0
          if (!hasDisplayContent && !hasFiles) continue

          dedupedMessages.push(message)
          continue
        }
      }

      const messageKeys = getToolMessageDedupeKeys(message)
      if ([...messageKeys].some(key => existingKeys.has(key))) continue
      messageKeys.forEach(key => existingKeys.add(key))
      dedupedMessages.push(message)
    }

    if (dedupedMessages.length === 0) {
      return
    }

    const insertAt = findThinkingIndex(messagesRef.value, thinkingId, requestId)

    if (insertAt !== -1) {
      messagesRef.value.splice(insertAt, 0, ...dedupedMessages)
    } else {
      const lastRelatedIdx = findLastRelatedStreamMessageIndex(messagesRef.value, workId, requestId)
      if (lastRelatedIdx !== -1) {
        messagesRef.value.splice(lastRelatedIdx + 1, 0, ...dedupedMessages)
      } else {
        messagesRef.value.push(...dedupedMessages)
      }
    }
  }

  return {
    processStreamContent,
    processStreamToolStart,
    processStreamToolEnd,
    processStreamError,
    processAiResponse,
    handleToolCallMessage,
    handleNewSession,
    cleanupThinkingMessage,
    addUserMessage,
    addThinkingMessage,
    removeThinkingMessage
  }
}
