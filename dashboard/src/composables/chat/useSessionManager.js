/**
 * 会话管理 composable
 * 封装会话列表、选择会话、新建会话等会话管理逻辑
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { useDeleteConfirm } from '../useDeleteConfirm'
import { PAGE_SIZE } from '../../constants'

export function useSessionManager() {
  // ==================== 状态定义 ====================
  
  // 会话相关状态
  const sessions = ref([])
  const sessionsLoading = ref(false)
  const currentSessionId = ref(null)
  const typingSessionId = ref(null)

  // 折叠面板状态
  const activeCollapse = ref([])

  // 分页相关
  const currentPage = ref(1)
  const hasMore = ref(true)
  const historyLoading = ref(false)
  const sessionCreating = ref(false)
  let isFirstLoad = true
  let loadedPageCount = 0

  // 加载历史记录的回调（由外部注入）
  let loadHistoryCallback = null

  // ==================== 会话管理方法 ====================
  
  /**
   * 设置加载历史记录的回调函数
   * @param {Function} callback - 回调函数，签名为 (sessionId, page, pageSize) => Promise
   */
  const setLoadHistoryCallback = (callback) => {
    loadHistoryCallback = callback
  }

  /**
   * 加载会话列表
   */
  const loadSessions = async () => {
    sessionsLoading.value = true
    try {
      const res = await chatApi.sessionsList()
      sessions.value = res.data.data || []
    } catch (err) {
      ElMessage.error(err.message || '获取会话列表失败')
    } finally {
      sessionsLoading.value = false
    }
  }

  // 使用删除确认组合式函数
  const { handleDelete: handleDeleteSession } = useDeleteConfirm(chatApi.deleteSession, loadSessions)

  /**
   * 选择会话
   * @param {Object} session - 会话对象
   * @param {boolean} disconnect - 是否断开连接，默认为 true
   * @param {Function} disconnectCallback - 断开连接的回调函数
   * @param {boolean} loadHistory - 是否重新加载历史记录
   */
  const selectSession = (session, disconnectCallback = null, disconnect = true, loadHistory = true) => {
    if (disconnect && disconnectCallback) {
      disconnectCallback()
    }    
    currentSessionId.value = session?.session_id
    if (!loadHistory){
      return
    }
    // 重置分页状态
    currentPage.value = 1
    hasMore.value = true
    historyLoading.value = false
    isFirstLoad = true
    loadedPageCount = 0
    
    // 触发历史记录加载（如果有回调）
    if (loadHistoryCallback) {
      loadHistoryCallback(2)
    }
  }

  /**
   * 加载会话历史记录
   * @param {number} pageCount - 页数
   */
  const loadSessionHistory = async (pageCount = 1) => {
    if (!currentSessionId.value || historyLoading.value || !hasMore.value) return
    
    historyLoading.value = true
    try {
      const totalSize = PAGE_SIZE * pageCount
      const res = await chatApi.sessionsHistory(currentSessionId.value, 1, totalSize)
      const historyData = res.data?.data || []
      
      if (historyData.length > 0) {
        loadedPageCount += pageCount
        if (historyData.length < totalSize) {
          hasMore.value = false
        }
      } else {
        hasMore.value = false
      }
      return historyData
    } catch (err) {
      ElMessage.error(err.message || '获取历史记录失败')
      return []
    } finally {
      historyLoading.value = false
    }
  }

  /**
   * 新建会话
   * @param {Function} disconnectCallback - 断开连接的回调函数
   */
  const createNewSession = (disconnectCallback = null) => {
    // 断开连接（如果有回调）
    if (disconnectCallback) {
      disconnectCallback()
    }
    currentSessionId.value = null
    // 设置新建会话状态为 true
    sessionCreating.value = true
  }

  /**
   * 获取按最后活跃时间排序的会话列表
   * @returns {Array} 排序后的会话列表
   */
  const getSortedSessions = () => {
    return [...sessions.value].sort((a, b) =>
      new Date(b.last_active) - new Date(a.last_active)
    )
  }

  /**
   * 重置分页状态
   */
  const resetPagination = () => {
    currentPage.value = 1
    hasMore.value = true
    historyLoading.value = false
    isFirstLoad = true
    loadedPageCount = 0
  }

  /**
   * 检查是否还有更多历史记录
   * @returns {boolean}
   */
  const checkHasMore = () => {
    return loadedPageCount >= 2 ? (hasMore.value = false, false) : hasMore.value
  }

  /**
   * 异步请求生成并更新会话标题
   * @param {string} sessionId - 会话ID
   * @param {string} firstMessage - 第一条消息内容
   */
  const updateSessionTitle = async (sessionId, firstMessage) => {
    if (!sessionId || !firstMessage) return
    
    try {
      const res = await chatApi.generateTitle({
        session_id: sessionId,
        first_message: firstMessage
      })
      
      const newTitle = res.data?.data?.title
      if (newTitle) {
        // 更新本地会话列表中的标题
        const sessionIdx = sessions.value.findIndex(s => s.session_id === sessionId)
        if (sessionIdx !== -1) {
          // 如果是正在进行的会话，启用打字机效果
          const session = sessions.value[sessionIdx]
          
          // 逐字更新逻辑
          typingSessionId.value = sessionId
          const targetTitle = newTitle
          session.title = '' // 先清空，准备打字机效果
          
          let i = 0
          const timer = setInterval(() => {
            if (i < targetTitle.length) {
              session.title += targetTitle[i]
              i++
            } else {
              clearInterval(timer)
              typingSessionId.value = null
            }
          }, 100) // 每100ms出一个字
        }
      }
    } catch (err) {
      console.error('Failed to generate session title:', err)
      typingSessionId.value = null
    }
  }

  return {
    // 状态
    sessions,
    sessionsLoading,
    currentSessionId,
    typingSessionId,
    activeCollapse,
    hasMore,
    historyLoading,
    sessionCreating,
    // 方法
    setLoadHistoryCallback,
    loadSessions,
    selectSession,
    createNewSession,
    handleDeleteSession,
    loadSessionHistory,
    getSortedSessions,
    resetPagination,
    checkHasMore,
    updateSessionTitle
  }
}
