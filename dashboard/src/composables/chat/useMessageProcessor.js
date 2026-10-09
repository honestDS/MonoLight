// 消息处理 composable：AI 响应解析与工具调用处理
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import i18n from '../../i18n'
import { findAssistantResponseReplacementIndex, getMessageDedupeKeys, isAssistantResponse, isToolCall, isToolResult, normalizeMessageContent } from '../../utils'
import { truncateErrorMessage } from '../../utils/errorMessage.js'
import { appendStreamReasoning, finalizeStreamReasoning as finalizeReasoning } from './reasoningTracker.js'
import { insertMessageBeforeThinking, removeThinkingMessageByIdentity } from './thinkingTracker.js'
import { insertTerminalHistory, processStreamError, processStreamToolStart } from './terminalHistory.js'
import { applyResumedTurnEnd } from './streamResume.js'

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

  const streamEventsByIdentity = new Map()
  const getStreamEventScope = (responseId, workId, turn, requestId) => {
    const stableResponseId = normalizeStableId(responseId)
    if (stableResponseId) return `response:${stableResponseId}`
    const stableWorkId = normalizeStableId(workId)
    if (stableWorkId && turn !== undefined && turn !== null) return `work:${stableWorkId}:${String(turn)}`
    const stableRequestId = normalizeStableId(requestId)
    if (stableRequestId && turn !== undefined && turn !== null) return `request:${stableRequestId}:${String(turn)}`
    return null
  }
  const shouldSkipRepeatedStreamEvent = (kind, text, responseId, workId, turn, requestId, eventId) => {
    const stableEventId = normalizeStableId(eventId)
    const scope = getStreamEventScope(responseId, workId, turn, requestId)
    if (!scope && !stableEventId) return false

    const scopeKey = scope || 'event'
    const eventKey = `${kind}:${stableEventId ? `event:${stableEventId}` : `text:${text}`}`
    const stableRequestId = normalizeStableId(requestId)
    const requestKey = stableRequestId || 'unknown'
    let eventsByRequest = streamEventsByIdentity.get(scopeKey)
    if (!eventsByRequest) {
      eventsByRequest = new Map()
      streamEventsByIdentity.set(scopeKey, eventsByRequest)
    }
    const requestKeys = eventsByRequest.get(eventKey)
    if (stableEventId && requestKeys) return true

    const isReplayFromAnotherRequest = Boolean(
      requestKeys
      && stableRequestId
      && [...requestKeys].some(key => key !== requestKey && key !== 'unknown')
    )
    const nextRequestKeys = requestKeys || new Set()
    nextRequestKeys.add(requestKey)
    eventsByRequest.set(eventKey, nextRequestKeys)
    return isReplayFromAnotherRequest
  }
  const hasFinalizedStreamMessage = (messages, responseId, workId, turn, requestId) => findStreamMessageIndexes(
    messages,
    responseId,
    workId,
    turn,
    requestId,
    message => message.role === 'assistant'
      && (message._stream_finalized === true || normalizeStableId(message.db_id))
  ).length > 0
  const resetStreamState = () => {
    streamEventsByIdentity.clear()
  }

  // 处理流式的增量文本推送事件
  const processStreamReasoning = (messagesRef, text, turn, responseId, requestId, workId, eventId) => {
    if (typeof text !== 'string' || !text) return
    if (hasFinalizedStreamMessage(messagesRef.value, responseId, workId, turn, requestId)) return
    if (shouldSkipRepeatedStreamEvent('reasoning', text, responseId, workId, turn, requestId, eventId)) return
    messagesRef.value = appendStreamReasoning(messagesRef.value, text, { turn, responseId, requestId, workId })
  }

  const finalizeStreamReasoning = (messagesRef, reasoningContent, turn, responseId, requestId, workId) => {
    messagesRef.value = finalizeReasoning(messagesRef.value, reasoningContent, { turn, responseId, requestId, workId })
  }

  const processStreamContent = (messagesRef, text, turn, thinkingId, finishReason, responseId, requestId, workId, eventId) => {
    if (typeof text !== 'string' || !text) return

    // 识别排队状态
    if (finishReason === 'queued') {
      return
    }

    if (hasFinalizedStreamMessage(messagesRef.value, responseId, workId, turn, requestId)) return
    if (shouldSkipRepeatedStreamEvent('content', text, responseId, workId, turn, requestId, eventId)) return

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

  const processStreamTurnEnd = (messagesRef, data, requestId = null) => {
    messagesRef.value = applyResumedTurnEnd(messagesRef.value, data, requestId, resolveAssistantDisplayContent)
    streamEventsByIdentity.delete(getStreamEventScope(
      data?.response_id,
      data?.work_id,
      data?.turn,
      data?.request_id ?? requestId
    ))
  }

  // 处理完整的 AI 响应消息，WS 和 HTTP 共用
  const processAiResponse = (messagesRef, response, thinkingId, requestId = null) => {
    const workId = response.work_id

    const choice = response.choices?.[0]
    const choiceMessage = choice?.message
    const finishReason = choice?.finish_reason ?? response.finish_reason
    const refusal = choiceMessage?.refusal ?? response.refusal
    const reasoningContent = choiceMessage?.reasoning_content ?? response.reasoning_content
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
    if (typeof reasoningContent === 'string' && reasoningContent.trim()) {
      finalAiMsg.reasoning_content = reasoningContent
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

    if (isAssistantResponse(finalAiMsg)) {
      finalAiMsg._stream_finalized = true
      const hasReasoning = message => typeof message?.reasoning_content === 'string' && message.reasoning_content.trim()
      const hasRealResponseIdentity = message => {
        const responseId = normalizeStableId(message?.response_id)
        const workId = normalizeStableId(message?.work_id)
        return Boolean(responseId && (!workId || responseId !== `session-reply-work:${workId}`))
      }
      const isWeakOrdinaryResponse = !isToolCall(finalAiMsg) && !hasRealResponseIdentity(finalAiMsg)
      if (isWeakOrdinaryResponse) {
        const ordinaryResponseCandidates = messagesRef.value
          .filter(message => isAssistantResponse(message) && !isToolCall(message) && hasRealResponseIdentity(message))
        const existingResponseIndex = findAssistantResponseReplacementIndex(ordinaryResponseCandidates, finalAiMsg)
        const existingResponse = existingResponseIndex === -1
          ? null
          : ordinaryResponseCandidates[existingResponseIndex]
        if (existingResponse) {
          finalAiMsg.response_id = existingResponse.response_id
          if (finalAiMsg.turn === undefined || finalAiMsg.turn === null) {
            finalAiMsg.turn = existingResponse.turn
          }
        }
      }
      const hasWeakResponseId = !hasRealResponseIdentity(finalAiMsg)
      const existingAssistantIndex = findAssistantResponseReplacementIndex(messagesRef.value, finalAiMsg)
      const existingAssistant = existingAssistantIndex === -1 ? null : messagesRef.value[existingAssistantIndex]
      if (
        !hasReasoning(finalAiMsg)
        && hasReasoning(existingAssistant)
        && (!isWeakOrdinaryResponse || !isToolCall(existingAssistant))
      ) {
        finalAiMsg.reasoning_content = existingAssistant.reasoning_content
      }

      const reasoningCandidates = messagesRef.value
        .filter(message => message.role === 'reasoning')
        .filter(candidate => !isWeakOrdinaryResponse || !messagesRef.value.some(otherMessage => (
          isToolCall(otherMessage)
          && matchesStreamIdentity(
            otherMessage,
            candidate.response_id,
            candidate.work_id,
            candidate.turn,
            candidate.request_id
          )
        )))
        .map(message => ({ ...message, role: 'assistant' }))
      const reasoningIndex = findAssistantResponseReplacementIndex(reasoningCandidates, finalAiMsg)
      const temporaryReasoning = reasoningIndex === -1 ? null : reasoningCandidates[reasoningIndex]
      const hasRealTemporaryResponse = hasRealResponseIdentity(temporaryReasoning)
      if (hasRealTemporaryResponse && hasWeakResponseId) {
        finalAiMsg.response_id = temporaryReasoning.response_id
        if (finalAiMsg.turn === undefined || finalAiMsg.turn === null) {
          finalAiMsg.turn = temporaryReasoning.turn
        }
      }

      if (!hasReasoning(finalAiMsg) && hasReasoning(temporaryReasoning)) {
        finalAiMsg.reasoning_content = temporaryReasoning.reasoning_content
      }
    }

    aiMessagesToInsert.push(finalAiMsg)
    _insertAiMessagesByThinking(messagesRef, aiMessagesToInsert, thinkingId, requestId, workId)
    for (const incoming of aiMessagesToInsert) {
      if (!isAssistantResponse(incoming)) continue
      const idx = findAssistantResponseReplacementIndex(messagesRef.value, incoming)
      if (idx === -1) continue
      const canonical = messagesRef.value[idx]
      messagesRef.value = finalizeReasoning(messagesRef.value, canonical.reasoning_content, {
        turn: canonical.turn,
        responseId: canonical.response_id,
        requestId: canonical.request_id,
        workId: canonical.work_id
      })
      streamEventsByIdentity.delete(getStreamEventScope(
        canonical.response_id,
        canonical.work_id,
        canonical.turn,
        canonical.request_id
      ))
    }
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
    messagesRef.value.push({ id: thinkingId, role: 'thinking', content: '' })
    return thinkingId
  }

  // 移除 thinking 占位符消息
  const removeThinkingMessage = (messagesRef, thinkingId, requestId = null) =>
    removeThinkingMessageByIdentity(messagesRef.value, thinkingId, requestId)

  // 按 thinking 位置插入 AI 消息
  const _insertAiMessagesByThinking = (messagesRef, aiMessages, thinkingId, requestId = null, workId = null) =>
    insertTerminalHistory(messagesRef, aiMessages, thinkingId, requestId, workId, resolveAssistantDisplayContent)

  return {
    processStreamContent,
    processStreamReasoning,
    finalizeStreamReasoning,
    processStreamToolStart,
    processStreamToolEnd,
    processStreamTurnEnd,
    processStreamError,
    processAiResponse,
    resetStreamState,
    handleToolCallMessage,
    handleNewSession,
    cleanupThinkingMessage,
    addUserMessage,
    addThinkingMessage,
    removeThinkingMessage
  }
}
