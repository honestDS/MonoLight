// 消息处理 composable：AI 响应解析与工具调用处理
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { isToolCall, isToolResult, normalizeMessageContent, getMessageDedupeKeys } from '../../utils'

const parseBackgroundSystemMessage = (item) => {
  if (item?.type !== 'background_result' && item?.role !== 'system') return null
  try {
    const payload = typeof item.content === 'string' ? JSON.parse(item.content) : item.content
    if (payload?.type === 'background_tool_result') return payload
  } catch {}
  return null
}

const findThinkingIndex = (messages, thinkingId, requestId) => {
  if (thinkingId) {
    const thinkingIndex = messages.findIndex(message => message.id === thinkingId && message.role === 'thinking')
    if (thinkingIndex !== -1) return thinkingIndex
  }
  if (requestId) {
    const requestThinkingIndex = messages.findLastIndex(message => message.role === 'thinking' && message.request_id === requestId)
    if (requestThinkingIndex !== -1) return requestThinkingIndex
  }
  const thinkingIndexes = messages
    .map((message, index) => message.role === 'thinking' ? index : -1)
    .filter(index => index !== -1)
  return thinkingIndexes.length === 1 ? thinkingIndexes[0] : -1
}

const insertBeforeThinking = (messagesRef, message, thinkingId, requestId) => {
  const thinkingIndex = findThinkingIndex(messagesRef.value, thinkingId, requestId)
  if (thinkingIndex === -1) return false
  messagesRef.value.splice(thinkingIndex, 0, message)
  return true
}

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
      removeThinkingForStream(messagesRef, thinkingId, requestId)
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
      removeThinkingForStream(messagesRef, thinkingId, requestId)
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

    if (replaceThinkingForStream(messagesRef, newMsg, thinkingId, requestId)) return

    // 2. 跨 Turn 增入：如果是新一轮的 Turn（responseId 变化），且由于没有 Thinking 占位符，
    // 我们应该将新消息插入到当前请求最新的一条相关消息（如 Tool 结果）之后，而不是去抢占其他请求的占位符
    if (requestId) {
      // 寻找该请求的最后一条消息位置，以便将新 Turn 消息插入到它后面
      let lastRelatedIdx = -1
      for (let i = messagesRef.value.length - 1; i >= 0; i--) {
        const m = messagesRef.value[i]
        if (m.role !== 'thinking' && (m.request_id === requestId || (m.role === 'tool' && m.id && String(m.id).includes(requestId)))) {
          lastRelatedIdx = i
          break
        }
      }
      
      if (lastRelatedIdx !== -1) {
        messagesRef.value.splice(lastRelatedIdx + 1, 0, {
          id: `assistant_${Date.now()}`,
          role: 'assistant',
          content: text,
          turn: turn,
          response_id: responseId,
          request_id: requestId,
          work_id: workId,
          created_at: Date.now() / 1000
        })
        return
      }
    }

    messagesRef.value.push(newMsg)
  }

  const removeThinkingForStream = (messagesRef, thinkingId, requestId) => {
    const thinkingIndex = findThinkingIndex(messagesRef.value, thinkingId, requestId)
    if (thinkingIndex !== -1) messagesRef.value.splice(thinkingIndex, 1)
  }

  const replaceThinkingForStream = (messagesRef, message, thinkingId, requestId) => {
    const thinkingIndex = findThinkingIndex(messagesRef.value, thinkingId, requestId)
    if (thinkingIndex === -1) return false
    const thinkingMessage = messagesRef.value[thinkingIndex]
    messagesRef.value[thinkingIndex] = { ...message, id: thinkingMessage.id }
    return true
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

    // 1. 跨 Turn 新建：如果后续正文已经先到达，工具调用必须插在正文之前
    if (requestId) {
      let lastRelatedIdx = -1
      for (let i = messagesRef.value.length - 1; i >= 0; i--) {
        if (messagesRef.value[i].role !== 'thinking' && messagesRef.value[i].request_id === requestId) {
          lastRelatedIdx = i
          break
        }
      }
      if (lastRelatedIdx !== -1) {
        const lastRelatedMessage = messagesRef.value[lastRelatedIdx]
        const insertAt = lastRelatedMessage.role === 'assistant' && !isToolCall(lastRelatedMessage)
          ? lastRelatedIdx
          : lastRelatedIdx + 1
        messagesRef.value.splice(insertAt, 0, newMsg)
        return
      }
    }

    // 2. 兜底追加
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
    messagesRef.value.push(newMsg)
  }

  // 处理流式下的业务错误事件
  const processStreamError = (messagesRef, errorMessage, thinkingId, requestId = null, workId = null, eventId = null) => {
    removeThinkingMessage(messagesRef, thinkingId)

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
      content: errorMessage,
      created_at: Date.now() / 1000,
      ...(requestId ? { request_id: requestId } : {}),
      ...(workId ? { work_id: workId } : {}),
      ...(eventId ? { event_id: eventId } : {})
    }
    if (requestId) {
      const lastRelatedIdx = messagesRef.value.findLastIndex(message => message.request_id === requestId)
      if (lastRelatedIdx !== -1) {
        messagesRef.value.splice(lastRelatedIdx + 1, 0, newMsg)
        return true
      }
    }
    messagesRef.value.push(newMsg)
    return true
  }
  
  // 处理完整的 AI 响应消息，WS 和 HTTP 共用
  const processAiResponse = (messagesRef, response, thinkingId, requestId = null) => {
    const workId = response.work_id
    if (workId && messagesRef.value.some(message => message.work_id === workId)) {
      removeThinkingMessage(messagesRef, thinkingId)
      return
    }

    const aiContent = response.choices?.[0]?.message?.content || ''
    const history = response.history || []
    const responseFiles = response.files || []
    const aiCreatedAt = response.choices?.[0]?.created_at || null
    const role = response.choices?.[0]?.message?.role || ''
    const finishReason = response.choices?.[0]?.finish_reason

    if (finishReason === 'queued') return

    const aiMessagesToInsert = []

    if (history.length > 0) {
      const historyMessages = history
        .map((item, idx) => {
          const backgroundPayload = parseBackgroundSystemMessage(item)
          if (backgroundPayload) {
            return {
              id: `history_${Date.now()}_${idx}`,
              role: 'background_system',
              content: JSON.stringify(backgroundPayload),
              created_at: item.created_at || null,
              ...(requestId ? { request_id: requestId } : {})
            }
          }
          const isToolRelated = (item.tool_calls && item.tool_calls.length > 0) || item.role === 'tool'
          return {
            id: `history_${Date.now()}_${idx}`,
            role: item.role,
            content: isToolRelated ? JSON.stringify(item) : item.content,
            created_at: item.created_at || null,
            ...(requestId ? { request_id: requestId } : {})
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

    if (auditConfirmation) {
      finalAiMsg = { id: `audit_confirmation_${auditConfirmation.audit_record_id || requestId || Date.now()}`, role: 'assistant', type: 'audit_confirmation', content: JSON.stringify(auditConfirmation), created_at: aiCreatedAt, ...(requestId ? { request_id: requestId } : {}) }
    } else if (isToolResult(tempMsg)) {
      finalAiMsg = { id: `tool_result_${requestId || Date.now()}`, role: 'tool', content: aiContent, created_at: aiCreatedAt, ...(requestId ? { request_id: requestId } : {}) }
    } else if (isToolCall(tempMsg)) {
      finalAiMsg = { id: `tool_call_${requestId || Date.now()}`, role: 'assistant', content: aiContent, created_at: aiCreatedAt, ...(requestId ? { request_id: requestId } : {}) }
    } else {
      finalAiMsg = { id: `assistant_${requestId || Date.now()}`, role: role, content: aiContent, created_at: aiCreatedAt, ...(requestId ? { request_id: requestId } : {}) }
    }

    if (responseFiles.length > 0) {
      finalAiMsg.files = responseFiles
    }
    if (workId) {
      finalAiMsg.work_id = workId
    }

    aiMessagesToInsert.push(finalAiMsg)
    _insertAiMessagesByThinking(messagesRef, aiMessagesToInsert, thinkingId, requestId)
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
  const removeThinkingMessage = (messagesRef, thinkingId) => {
    const thinkingIndex = messagesRef.value.findIndex(m => m.id === thinkingId && m.role === 'thinking')
    if (thinkingIndex !== -1) {
      messagesRef.value.splice(thinkingIndex, 1)
    }
  }

  // 按 thinking 位置插入 AI 消息
  const _insertAiMessagesByThinking = (messagesRef, aiMessages, thinkingId, requestId = null) => {
    if (!aiMessages || aiMessages.length === 0) return
    const existingKeys = new Set(messagesRef.value.flatMap(getToolMessageDedupeKeys))
    const dedupedMessages = []

    for (const message of aiMessages) {
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
    } else if (requestId) {
      const lastRelatedIdx = messagesRef.value.findLastIndex(message => message.request_id === requestId && message.role !== 'thinking')
      if (lastRelatedIdx !== -1) {
        messagesRef.value.splice(lastRelatedIdx + 1, 0, ...dedupedMessages)
      } else {
        messagesRef.value.push(...dedupedMessages)
      }
    } else {
      messagesRef.value.push(...dedupedMessages)
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
