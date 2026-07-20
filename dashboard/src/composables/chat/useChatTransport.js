// 聊天通信 composable：封装 HTTP 与 WebSocket 通信
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { useWebSocket } from '../useWebSocket'
import i18n from '../../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)

const resolveErrorMessage = (data, fallback) => data?.message || data?.content || data?.detail || data?.error || fallback

const isWsErrorPayload = (data) => {
  if (data?.type === 'error' || data?.error || data?.detail) return true
  const choice = Array.isArray(data?.choices) ? data.choices[0] : null
  return choice?.message?.role === 'err'
}

export function useChatTransport() {
  // ==================== 通信模式管理 ====================

  // 通信模式: 'http' - 普通HTTP模式, 'ws' - WebSocket流式模式
  const transportMode = ref('ws')  // 默认使用流式模式
  const wsConnected = ref(false)     // WebSocket连接状态

  // WebSocket 管理
  const wsManager = useWebSocket()

  // 并发请求回调映射管理: requestId -> callbacks
  const callbacksMap = new Map()
  let sessionEventCallbacks = null

  // 注册唯一持久的消息分发器，支持多请求并行分发
  wsManager.onMessage((data) => {
    const requestId = data.request_id || 'default'
    const callbacks = callbacksMap.get(requestId)
    if (callbacks) {
      if (data.session_id && callbacks.sessionId && data.session_id !== callbacks.sessionId) return
      handleWsMessage(data, callbacks)
    } else if (
      data.type === 'proactive_reply' ||
      data.type === 'proactive_reply_error' ||
      data.type === 'audit_confirmation_status' ||
      data.type === 'context_summary_start' ||
      data.type === 'context_summary_end' ||
      isWsErrorPayload(data)
    ) {
      if (sessionEventCallbacks) {
        if (data.session_id && sessionEventCallbacks.sessionId && data.session_id !== sessionEventCallbacks.sessionId) return
        handleWsMessage(data, sessionEventCallbacks)
      }
    }
  })

  // ==================== WebSocket 相关方法 ====================

  // 处理 WebSocket 消息
  const handleWsMessage = (data, options = {}) => {
    const {
      onContent,
      onToolStart,
      onToolEnd,
      onComplete,
      onError,
      onSessionId,
      onProactiveReply,
      onProactiveReplyError,
      onAuditConfirmationStatus,
      onContextSummaryStart,
      onContextSummaryEnd,
      scrollToBottom,
      setLoading,
      thinkingId,
      requestId: currentRequestId
    } = options

    // 忽略心跳响应
    if (data.type === 'pong' || data.type === 'ping') return

    const type = data.type
    const requestId = data.request_id || currentRequestId

    if (type === 'context_summary_start') {
      if (onContextSummaryStart) onContextSummaryStart(data)
      return
    }

    if (type === 'context_summary_end') {
      if (onContextSummaryEnd) onContextSummaryEnd(data)
      return
    }

    // 1. 处理增量文本推送
    if (type === 'content') {
      if (onContent) {
        onContent(data.content, data.turn, thinkingId, data.finish_reason, data.response_id, requestId, data.work_id, data.event_id)
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      return
    }

    // 1.5 处理每一轮结束（覆盖最终清洗后的文本，清理占位符）
    if (type === 'turn_end') {
      if (onComplete) {
        // 调用基于 response_id 的覆盖逻辑
        onComplete(data, thinkingId, requestId, 'turn_end')
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      return
    }

    // 2. 处理工具调用开始
    if (type === 'tool_start') {
      if (onToolStart) {
        onToolStart({
          id: data.tool_call_id,
          name: data.name,
          arguments: data.arguments
        }, thinkingId, data.response_id, requestId, data.work_id)
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      return
    }

    // 3. 处理工具调用结束
    if (type === 'tool_end') {
      if (onToolEnd) {
        onToolEnd({
          tool_call_id: data.tool_call_id,
          name: data.name,
          result: data.result
        }, data.response_id, requestId, data.work_id)
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      return
    }

    // 4. 处理对话结束
    if (type === 'done') {
      if (onComplete) {
        onComplete(data, thinkingId, requestId)
      }
      if (setLoading) {
        setLoading(false)
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      if (requestId) {
        callbacksMap.delete(requestId)
      }
      return
    }

    // 5. 处理会话 ID 同步
    if (type === 'session_id') {
      if (onSessionId) {
        onSessionId(data.session_id)
      }
      return
    }

    if (type === 'proactive_reply') {
      if (onProactiveReply) {
        onProactiveReply(data)
      } else if (onComplete) {
        onComplete(data, thinkingId, requestId, 'proactive_reply')
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      return
    }

    if (type === 'proactive_reply_error') {
      if (onProactiveReplyError) {
        onProactiveReplyError(data)
      } else if (onError) {
        onError(resolveErrorMessage(data, 'Background proactive reply failed'), thinkingId, requestId, data)
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      return
    }

    if (type === 'audit_confirmation_status') {
      if (onAuditConfirmationStatus) onAuditConfirmationStatus(data)
      return
    }

    // 处理任务开始确认
    if (type === 'task_start') {
      if (options.onTaskStart) {
        options.onTaskStart()
      }
      return
    }

    // 6. 处理异常通知
    if (type === 'error' || data.error || data.detail) {
      const errorMessage = resolveErrorMessage(data, t('chat.stream_error'))
      console.error('WebSocket业务错误:', errorMessage)
      if (onError) {
        onError(errorMessage, thinkingId, requestId, data)
      }
      if (setLoading) {
        setLoading(false)
      }
      if (requestId) {
        callbacksMap.delete(requestId)
      }
      return
    }

    // 7. 兼容逻辑处理（LLMResponse 格式）
    if (data.choices && Array.isArray(data.choices)) {
      const choice = data.choices[0]
      if (!choice) return

      const content = resolveErrorMessage(choice.message, '')
      const finishReason = choice.finish_reason

      if (choice.message?.role === 'err') {
        if (onError) onError(content, thinkingId, requestId, data)
        if (setLoading) setLoading(false)
        if (requestId) callbacksMap.delete(requestId)
        return
      }

      if (finishReason) {
        if (onComplete) onComplete(data, thinkingId, requestId)
        if (setLoading) setLoading(false)
        if (scrollToBottom) scrollToBottom()
        if (requestId) callbacksMap.delete(requestId)
      }
    }
  }

  // 断开 WebSocket 连接
  const disconnectWebSocket = () => {
    wsManager.disconnect()
    wsConnected.value = false
    callbacksMap.clear()
    sessionEventCallbacks = null
  }

  // ==================== 发送方法 ====================

  const httpSend = async ({ message, sessionId, attachments, callbacks = {} }) => {
    const payload = {
      message,
      session_id: sessionId || null,
      attachments: attachments || null
    }
    if (!sessionId) {
      const res = await chatApi.completions({ ...payload, stream: false })
      return res.data
    }
    return chatApi.completionsStream(payload, event => handleWsMessage(event, callbacks))
  }

  const wsSend = async ({ message, sessionId, attachments, requestId, callbacks = {} }) => {
    const token = localStorage.getItem('token')
    if (!token) throw new Error(t('chat.not_logged_in'))

    const finalCallbacks = { ...callbacks, requestId, sessionId }
    sessionEventCallbacks = finalCallbacks
    if (requestId) {
      callbacksMap.set(requestId, finalCallbacks)
    }

    if (!wsManager.isConnected.value) {
      try {
        await wsManager.connect(token)
        wsConnected.value = true
      } catch (e) {
        console.error('WebSocket连接失败:', e)
        ElMessage.error(t('chat.ws_connect_failed'))
        transportMode.value = 'http'
        if (requestId) callbacksMap.delete(requestId)
        return false
      }
    }

    const wsData = {
      type: 'chat',
      message,
      session_id: sessionId || null,
      attachments: attachments || null,
      request_id: requestId
    }

    if (!wsManager.sendMessage(wsData)) {
      ElMessage.error(t('chat.ws_message_send_failed'))
      if (requestId) callbacksMap.delete(requestId)
      return false
    }
    return true
  }

  const send = async (options) => {
    if (transportMode.value === 'ws') {
      return wsSend(options)
    } else {
      return httpSend(options)
    }
  }

  const setTransportMode = async (mode, disconnectCallback = null) => {
    if (mode === 'ws' && transportMode.value !== 'ws') {
      transportMode.value = mode
    } else if (mode === 'http' && transportMode.value !== 'http') {
      if (wsConnected.value) {
        if (disconnectCallback) disconnectCallback()
        else disconnectWebSocket()
      }
      transportMode.value = mode
    }
  }

  const initWebSocket = async () => {
    const token = localStorage.getItem('token')
    if (token) {
      try {
        await wsManager.connect(token)
        wsConnected.value = true
        return true
      } catch (e) {
        console.error('Init WS failed:', e)
        return false
      }
    }
    return false
  }

  return {
    transportMode,
    wsConnected,
    disconnectWebSocket,
    httpSend,
    wsSend,
    send,
    setTransportMode,
    initWebSocket,
    wsManager
  }
}
