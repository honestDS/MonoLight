/**
 * 聊天通信 composable
 * 封装 HTTP 和 WebSocket 两种通信模式
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { useWebSocket } from '../useWebSocket'

export function useChatTransport() {
  // ==================== 通信模式管理 ====================
  
  // 通信模式: 'http' - 普通HTTP模式, 'ws' - WebSocket流式模式
  const transportMode = ref('ws')  // 默认使用流式模式
  const wsConnected = ref(false)     // WebSocket连接状态
  let currentThinkingId = null
  
  // WebSocket 管理
  const wsManager = useWebSocket()

  // 存储当前活跃的流式回调
  let activeCallbacks = null

  // 注册唯一持久的消息分发器，解决连接重用时回调失效问题
  wsManager.onMessage((data) => {
    if (activeCallbacks) {
      handleWsMessage(data, activeCallbacks)
    }
  })

  // ==================== WebSocket 相关方法 ====================
  
  /**
   * 处理 WebSocket 消息
   * @param {Object} data - WebSocket 消息数据
   * @param {Object} options - 处理选项
   * @param {Function} options.onToolCall - 工具调用处理回调
   * @param {Function} options.onResponse - AI 响应处理回调
   * @param {Function} options.onComplete - 完成处理回调
   * @param {Function} options.onSessionId - 会话 ID 同步回调
   * @param {Function} options.scrollToBottom - 滚动到底部回调
   * @param {Function} options.setLoading - 设置 loading 状态回调
   */
  const handleWsMessage = (data, options = {}) => {
    const { onContent, onToolStart, onToolEnd, onComplete, onError, onSessionId, scrollToBottom, setLoading } = options

    // 忽略心跳响应
    if (data.type === 'pong' || data.type === 'ping') return

    const type = data.type

    // 1. 处理增量文本推送
    if (type === 'content') {
      if (onContent) {
        onContent(data.content, data.turn, currentThinkingId, data.finish_reason)
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
          name: data.name, // 后端推送的 key 是 name，非 tool_name
          arguments: data.arguments
        })
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
          name: data.name, // 后端推送的 key 是 name，非 tool_name
          result: data.result
        })
      }
      if (scrollToBottom) {
        scrollToBottom()
      }
      return
    }

    // 4. 处理对话结束
    if (type === 'done') {
      if (onComplete) {
        onComplete(data, currentThinkingId)
      }
      if (setLoading) {
        setLoading(false)
      }
      if (scrollToBottom) {
        scrollToBottom()
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

    // 6. 处理异常通知
    if (type === 'error') {
      console.error('WebSocket业务错误:', data.message)
      if (onError) {
        onError(data.message, currentThinkingId)
      }
      if (setLoading) {
        setLoading(false)
      }
      return
    }

    // 兼容老的数据结构（如异常直接以 LLMResponse 格式返回）
    if (data.choices && Array.isArray(data.choices)) {
      const choice = data.choices[0]
      if (!choice) return
      
      const content = choice.message?.content || ''
      const finishReason = choice.finish_reason

      // 异常角色
      if (choice.message?.role === 'err') {
        if (onError) {
          onError(content, currentThinkingId)
        }
        if (setLoading) {
          setLoading(false)
        }
        return
      }

      if (finishReason) {
        if (onComplete) {
          onComplete(data, currentThinkingId)
        }
        if (setLoading) {
          setLoading(false)
        }
        if (scrollToBottom) {
          scrollToBottom()
        }
      }
      return
    }
  }

  /**
   * 断开 WebSocket 连接
   */
  const disconnectWebSocket = () => {
    wsManager.disconnect()
    wsConnected.value = false
  }

  // ==================== 发送方法 ====================
  
  /**
   * HTTP 方式发送消息
   * @param {Object} options - 发送选项
   * @param {string} options.message - 消息内容
   * @param {string|null} options.sessionId - 会话 ID
   * @returns {Promise<Object>} API 响应
   */
  const httpSend = async ({ message, sessionId }) => {
    const res = await chatApi.completions({
      message,
      session_id: sessionId || null,
      stream: false
    })
    return res.data
  }

  /**
   * WebSocket 方式发送消息 (懒连接)
   * @param {Object} options - 发送选项
   * @param {string} options.message - 消息内容
   * @param {string|null} options.sessionId - 会话 ID
   * @param {Object} options.callbacks - 流式事件处理回调对象
   * @returns {Promise<boolean>} 是否发送成功
   */
  const wsSend = async ({ message, sessionId, callbacks = {} }) => {
    const token = localStorage.getItem('token')
    if (!token) {
      throw new Error('未登录')
    }
    
    // 更新最新消息的回调引用
    activeCallbacks = callbacks
    
    // 懒连接：只有在发送消息时才会尝试连接WebSocket
    if (!wsManager.isConnected.value) {
      try {
        await wsManager.connect(token)
        wsConnected.value = true
      } catch (e) {
        console.error('WebSocket连接失败:', e)
        ElMessage.error('WebSocket 连接失败，将使用 HTTP 模式')
        // 回退到HTTP模式
        transportMode.value = 'http'
        return false
      }
    }
    
    // 通过 WebSocket 发送
    const wsData = {
      type: 'chat',
      message,
      session_id: sessionId || null
    }
    
    if (!wsManager.sendMessage(wsData)) {
      ElMessage.error('WebSocket 消息发送失败')
      return false
    }
    return true
  }

  /**
   * 根据当前通信模式发送消息
   * @param {Object} options - 发送选项
   * @returns {Promise} 发送结果
   */
  const send = async (options) => {
    if (transportMode.value === 'ws') {
      return wsSend(options)
    } else {
      return httpSend(options)
    }
  }

  /**
   * 切换通信模式
   * @param {string} mode - 通信模式，'http' 或 'ws'
   * @param {Function} disconnectCallback - 断开连接回调
   */
  const setTransportMode = async (mode, disconnectCallback = null) => {
    if (mode === 'ws' && transportMode.value !== 'ws') {
      // 切换到WebSocket模式（懒连接，稍后发送消息时才连接）
      transportMode.value = mode
    } else if (mode === 'http' && transportMode.value !== 'http') {
      // 切换到HTTP模式，先断开WebSocket（如果已连接）
      if (wsConnected.value) {
        if (disconnectCallback) {
          disconnectCallback()
        } else {
          disconnectWebSocket()
        }
      }
      transportMode.value = mode
    }
  }

  /**
   * 获取当前 thinking ID
   * @returns {number} thinking ID
   */
  const getCurrentThinkingId = () => currentThinkingId

  /**
   * 设置当前 thinking ID
   * @param {number} id - thinking ID
   */
  const setCurrentThinkingId = (id) => {
    currentThinkingId = id
  }

  /**
   * 初始化 WebSocket
   * @returns {Promise} 连接结果
   */
  const initWebSocket = async () => {
    const token = localStorage.getItem('token')
    if (token) {
      await wsManager.connect(token)
      wsConnected.value = true
      return true
    }
    return false
  }

  return {
    // 状态
    transportMode,
    wsConnected,
    // 方法
    handleWsMessage,
    disconnectWebSocket,
    httpSend,
    wsSend,
    send,
    setTransportMode,
    getCurrentThinkingId,
    setCurrentThinkingId,
    initWebSocket,
    // WebSocket 管理器（供外部使用）
    wsManager
  }
}