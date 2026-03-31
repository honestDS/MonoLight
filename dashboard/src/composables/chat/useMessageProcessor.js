/**
 * 消息处理 composable
 * 封装 AI 响应解析、工具调用处理等消息处理逻辑
 */
import { nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { isToolCall, isToolResult } from '../../utils'

export function useMessageProcessor() {
  // ==================== 消息处理方法 ====================
  
  /**
   * 处理完整的 AI 响应消息（WS 和 HTTP 共用）
   * @param {Object} messagesRef - 消息列表 ref
   * @param {Object} response - API 响应数据
   * @param {number|string} thinkingId - thinking 消息 ID
   * @param {Function} scrollToBottom - 滚动到底部回调
   */
  const processAiResponse = (messagesRef, response, thinkingId, scrollToBottom) => {
    const aiContent = response.choices?.[0]?.message?.content || ''
    const history = response.history || []
    const aiCreatedAt = response.choices?.[0]?.created_at || null
    const role = response.choices?.[0]?.message?.role || ''

    // 清理 thinking 占位符（先精确匹配，再模糊匹配）
    let thinkingIndex = messagesRef.value.findIndex(m => m.id === thinkingId)
    if (thinkingIndex === -1) {
      thinkingIndex = messagesRef.value.findIndex(m => m.role === 'thinking')
    }

    // 处理 history（只包含工具调用类消息）
    if (history.length > 0) {
      const historyMessages = history.map((item, idx) => ({
        id: `history_${Date.now()}_${idx}`,
        role: item.role,
        content: JSON.stringify(item),
        created_at: item.created_at || null
      }))

      if (thinkingIndex !== -1) {
        messagesRef.value.splice(thinkingIndex, 1, ...historyMessages)
      } else {
        messagesRef.value.push(...historyMessages)
      }
    } else if (thinkingIndex !== -1) {
      messagesRef.value.splice(thinkingIndex, 1)
    } else {
      console.warn('未找到 Thinking 占位符，尝试清理残留消息')
      const residualIndex = messagesRef.value.findIndex(m => m.role === 'thinking')
      if (residualIndex !== -1) {
        messagesRef.value.splice(residualIndex, 1)
      }
    }

    // 处理 AI 响应，使用工具函数判断 role
    const tempMsg = { content: aiContent }

    if (isToolResult(tempMsg)) {
      messagesRef.value.push({
        id: thinkingId,
        role: 'tool',
        content: aiContent,
        created_at: aiCreatedAt
      })
    } else if (isToolCall(tempMsg)) {
      messagesRef.value.push({
        id: thinkingId,
        role: 'assistant',
        content: aiContent,
        created_at: aiCreatedAt
      })
    } else {
      messagesRef.value.push({
        id: thinkingId,
        role: role,
        content: aiContent,
        created_at: aiCreatedAt
      })
    }

    if (scrollToBottom) {
      nextTick(() => scrollToBottom())
    }
  }

  /**
   * 处理工具调用消息
   * @param {Object} messagesRef - 消息列表 ref
   * @param {Object} toolCall - 工具调用对象
   * @param {Function} scrollToBottom - 滚动到底部回调
   */
  const handleToolCallMessage = (messagesRef, toolCall, scrollToBottom) => {
    const lastMsg = messagesRef.value[messagesRef.value.length - 1]
    if (lastMsg && lastMsg.role === 'tool_call') {
      // 追加到现有工具调用
      lastMsg.content = { ...lastMsg.content, ...toolCall }
    } else {
      // 新建工具调用消息
      messagesRef.value.push({
        id: Date.now(),
        role: 'tool_call',
        content: toolCall
      })
    }
    if (scrollToBottom) {
      nextTick(() => scrollToBottom())
    }
  }

  /**
   * 处理新会话创建
   * @param {Object} sessionsRef - 会话列表 ref
   * @param {Function} selectSession - 选择会话回调
   * @param {number|string} thinkingId - thinking 消息 ID
   * @param {boolean} disconnect - 是否断开连接
   */
  const handleNewSession = async (sessionsRef, selectSession, thinkingId, disconnect = true) => {
    // 重新加载会话列表
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

  /**
   * 清理残留 thinking 消息
   * @param {Object} messagesRef - 消息列表 ref
   */
  const cleanupThinkingMessage = (messagesRef) => {
    const thinkingMsgIndex = messagesRef.value.findIndex(m => m.role === 'thinking')
    if (thinkingMsgIndex !== -1) {
      messagesRef.value.splice(thinkingMsgIndex, 1)
    }
  }

  /**
   * 添加用户消息
   * @param {Object} messagesRef - 消息列表 ref
   * @param {string} content - 消息内容
   * @returns {number} 生成的消息 ID
   */
  const addUserMessage = (messagesRef, content) => {
    const userMsgId = Date.now()
    messagesRef.value.push({
      id: userMsgId,
      role: 'user',
      content: content,
      created_at: Date.now() / 1000
    })
    return userMsgId
  }

  /**
   * 添加 thinking 占位符消息
   * @param {Object} messagesRef - 消息列表 ref
   * @returns {number} 生成的消息 ID
   */
  const addThinkingMessage = (messagesRef) => {
    const thinkingId = Date.now() + 1
    messagesRef.value.push({
      id: thinkingId,
      role: 'thinking',
      content: 'Thinking...'
    })
    return thinkingId
  }

  /**
   * 移除 thinking 占位符消息
   * @param {Object} messagesRef - 消息列表 ref
   * @param {number|string} thinkingId - thinking 消息 ID
   */
  const removeThinkingMessage = (messagesRef, thinkingId) => {
    const thinkingIndex = messagesRef.value.findIndex(m => m.id === thinkingId)
    if (thinkingIndex !== -1) {
      messagesRef.value.splice(thinkingIndex, 1)
    }
  }

  return {
    // 方法
    processAiResponse,
    handleToolCallMessage,
    handleNewSession,
    cleanupThinkingMessage,
    addUserMessage,
    addThinkingMessage,
    removeThinkingMessage
  }
}