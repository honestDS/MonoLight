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

  return {
    // 状态
    sessions,
    sessionsLoading,
    currentSessionId,
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
    checkHasMore
  }
}