// 消息处理 composable：AI 响应解析与工具调用处理
import { nextTick } from 'vue'
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

export function useMessageProcessor() {
  // ==================== 消息处理方法 ====================

  // 处理流式的增量文本推送事件
  const processStreamContent = (messagesRef, text, turn, thinkingId, finishReason, responseId, requestId) => {
    // 识别排队状态
    if (finishReason === 'queued') {
      removeThinkingMessage(messagesRef, thinkingId)
      return
    }

    // 1. 优先寻找已绑定 responseId 的消息进行增量追加
    let targetIdx = -1
    if (responseId) {
      targetIdx = messagesRef.value.findIndex(m => m.response_id === responseId && m.role === 'assistant')
    }

    if (targetIdx !== -1) {
      const targetMsg = messagesRef.value[targetIdx]
      // 只有在非工具调用且 role 是 assistant 时才追加文字
      if (targetMsg.role === 'assistant' && !isToolCall(targetMsg)) {
        targetMsg.content = (targetMsg.content || '') + text
        messagesRef.value[targetIdx] = { ...targetMsg }
        return
      }
    }

    // 2. 索取机制：若未绑定，寻找与当前请求绑定的 Thinking 占位符进行转换
    let thinkingIdx = -1
    if (thinkingId) {
      // 必须精确匹配当前请求的 thinkingId，绝不抢占其他并发请求的占位符
      thinkingIdx = messagesRef.value.findIndex(m => m.id === thinkingId && m.role === 'thinking')
    }

    // 若精准匹配未找到，且存在任何 thinking 占位符，兜底匹配最后一个占位符以支持用户连续发送时的流式追加
    if (thinkingIdx === -1) {
      thinkingIdx = messagesRef.value.findLastIndex(m => m.role === 'thinking')
    }

    if (thinkingIdx !== -1) {
      const originalThinkingMsg = messagesRef.value[thinkingIdx]
      // 转换：Thinking -> Assistant 并绑定 responseId 和 requestId
      messagesRef.value[thinkingIdx] = {
        id: originalThinkingMsg.id, // 必须使用它自己的 id，防止与前一轮响应发生重复 key 冲突导致渲染失效
        role: 'assistant',
        content: text,
        turn: turn,
        response_id: responseId,
        request_id: requestId,
        created_at: Date.now() / 1000
      }
      return
    }

    // 3. 跨 Turn 增入：如果是新一轮的 Turn（responseId 变化），且由于没有 Thinking 占位符，
    // 我们应该将新消息插入到当前请求最新的一条相关消息（如 Tool 结果）之后，而不是去抢占其他请求的占位符
    if (requestId) {
      // 寻找该请求的最后一条消息位置，以便将新 Turn 消息插入到它后面
      let lastRelatedIdx = -1
      for (let i = messagesRef.value.length - 1; i >= 0; i--) {
        const m = messagesRef.value[i]
        if (m.request_id === requestId || (m.role === 'tool' && m.id && String(m.id).includes(requestId))) {
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
          created_at: Date.now() / 1000
        })
        return
      }
    }

    // 4. 兜底：直接追加新消息到末尾（如果有 thinking 消息，插在第一个 thinking 之前，保证 thinking 在最末尾）
    const newMsg = {
      id: thinkingId || Date.now(),
      role: 'assistant',
      content: text,
      turn: turn,
      response_id: responseId,
      request_id: requestId,
      created_at: Date.now() / 1000
    }
    const firstThinkingIdx = messagesRef.value.findIndex(m => m.role === 'thinking')
    if (firstThinkingIdx !== -1) {
      messagesRef.value.splice(firstThinkingIdx, 0, newMsg)
    } else {
      messagesRef.value.push(newMsg)
    }
  }

  // 处理流式下的工具调用开始，推送 tool_call 占位
  const processStreamToolStart = (messagesRef, toolCall, thinkingId, responseId, requestId) => {
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
        request_id: existingMsg.request_id || requestId
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
        request_id: existingMsg.request_id || requestId
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
      created_at: Date.now() / 1000
    }

    // 工具调用与同轮流式正文属于同一条助手消息，合并后可避免后续事件将正文覆盖掉。
    if (streamedContentIdx !== -1) {
      newMsg.id = streamedContentMsg.id
      newMsg.created_at = streamedContentMsg.created_at || newMsg.created_at
      messagesRef.value[streamedContentIdx] = newMsg
      return
    }

    // 1. 优先替换匹配当前请求的占位符
    let thinkingIdx = -1
    if (thinkingId) {
      thinkingIdx = messagesRef.value.findIndex(m => m.id === thinkingId && m.role === 'thinking')
    }

    // 若精准匹配未找到，且存在任何 thinking 占位符，兜底匹配最后一个占位符以支持用户连续发送时的流式追加
    if (thinkingIdx === -1) {
      thinkingIdx = messagesRef.value.findLastIndex(m => m.role === 'thinking')
    }

    if (thinkingIdx !== -1) {
      const originalThinkingMsg = messagesRef.value[thinkingIdx]
      newMsg.id = originalThinkingMsg.id // 保持 id 一致防止重复 key
      messagesRef.value[thinkingIdx] = newMsg
      return
    }

    // 2. 跨 Turn 新建：如果后续正文已经先到达，工具调用必须插在正文之前
    if (requestId) {
      let lastRelatedIdx = -1
      for (let i = messagesRef.value.length - 1; i >= 0; i--) {
        if (messagesRef.value[i].request_id === requestId) {
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

    // 3. 兜底追加
    const firstThinkingIdx = messagesRef.value.findIndex(m => m.role === 'thinking')
    if (firstThinkingIdx !== -1) {
      messagesRef.value.splice(firstThinkingIdx, 0, newMsg)
    } else {
      messagesRef.value.push(newMsg)
    }
  }

  // 处理流式下的工具调用结束，推送 tool 返回结果
  const processStreamToolEnd = (messagesRef, toolEnd, responseId, requestId) => {
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
        request_id: existingMsg.request_id || requestId
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
      created_at: Date.now() / 1000
    }

    const toolCallIdx = findToolCallIndex(messagesRef.value, toolEnd.tool_call_id)
    if (toolCallIdx !== -1) {
      let insertAt = toolCallIdx + 1
      while (
        insertAt < messagesRef.value.length &&
        messagesRef.value[insertAt].role === 'tool' &&
        messagesRef.value[insertAt].response_id === responseId
      ) {
        insertAt += 1
      }
      messagesRef.value.splice(insertAt, 0, newMsg)
      return
    }

    const firstThinkingIdx = messagesRef.value.findIndex(m => m.role === 'thinking')
    if (firstThinkingIdx !== -1) {
      messagesRef.value.splice(firstThinkingIdx, 0, newMsg)
    } else {
      messagesRef.value.push(newMsg)
    }
  }

  // 处理流式下的业务错误事件
  const processStreamError = (messagesRef, errorMessage, thinkingId) => {
    removeThinkingMessage(messagesRef, thinkingId)

    const newMsg = {
      id: thinkingId || Date.now(),
      role: 'err',
      content: errorMessage,
      created_at: Date.now() / 1000
    }
    const firstThinkingIdx = messagesRef.value.findIndex(m => m.role === 'thinking')
    if (firstThinkingIdx !== -1) {
      messagesRef.value.splice(firstThinkingIdx, 0, newMsg)
    } else {
      messagesRef.value.push(newMsg)
    }
  }
  
  // 处理完整的 AI 响应消息，WS 和 HTTP 共用
  const processAiResponse = (messagesRef, response, thinkingId, scrollToBottom) => {
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
              created_at: item.created_at || null
            }
          }
          const isToolRelated = (item.tool_calls && item.tool_calls.length > 0) || item.role === 'tool'
          return {
            id: `history_${Date.now()}_${idx}`,
            role: item.role,
            content: isToolRelated ? JSON.stringify(item) : item.content,
            created_at: item.created_at || null
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

    if (isToolResult(tempMsg)) {
      finalAiMsg = { id: thinkingId || Date.now(), role: 'tool', content: aiContent, created_at: aiCreatedAt }
    } else if (isToolCall(tempMsg)) {
      finalAiMsg = { id: thinkingId || Date.now(), role: 'assistant', content: aiContent, created_at: aiCreatedAt }
    } else {
      finalAiMsg = { id: thinkingId || Date.now(), role: role, content: aiContent, created_at: aiCreatedAt }
    }

    if (responseFiles.length > 0) {
      finalAiMsg.files = responseFiles
    }
    if (workId) {
      finalAiMsg.work_id = workId
    }

    aiMessagesToInsert.push(finalAiMsg)
    _insertAiMessagesByThinking(messagesRef, aiMessagesToInsert, thinkingId)
    
    if (scrollToBottom) {
      nextTick(() => scrollToBottom())
    }
  }

  // 处理工具调用消息
  const handleToolCallMessage = (messagesRef, toolCall, scrollToBottom) => {
    const lastMsg = messagesRef.value[messagesRef.value.length - 1]
    if (lastMsg && lastMsg.role === 'tool_call') {
      lastMsg.content = { ...lastMsg.content, ...toolCall }
    } else {
      messagesRef.value.push({ id: Date.now(), role: 'tool_call', content: toolCall })
    }
    if (scrollToBottom) {
      nextTick(() => scrollToBottom())
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
  const _insertAiMessagesByThinking = (messagesRef, aiMessages, thinkingId) => {
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
      removeThinkingMessage(messagesRef, thinkingId)
      return
    }

    let insertAt = -1
    if (thinkingId) {
      insertAt = messagesRef.value.findIndex(m => m.id === thinkingId)
    }
    if (insertAt === -1) {
      insertAt = messagesRef.value.findIndex(m => m.role === 'thinking')
    }

    if (insertAt !== -1) {
      messagesRef.value.splice(insertAt, 1, ...dedupedMessages)
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
