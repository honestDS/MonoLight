// 会话管理 composable：列表、选择与新建会话
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { useDeleteConfirm } from '../useDeleteConfirm'
import { PAGE_SIZE } from '../../constants'
import i18n from '../../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)

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
  let sessionGeneration = 0

  // 加载历史记录的回调（由外部注入）
  let loadHistoryCallback = null

  // ==================== 会话管理方法 ====================
  
  // 设置加载历史记录回调；callback 签名为 (sessionId, page, pageSize) => Promise
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
      ElMessage.error(err.message || t('chat.load_sessions_failed'))
    } finally {
      sessionsLoading.value = false
    }
  }

  // 使用删除确认组合式函数
  const { handleDelete: handleDeleteSession } = useDeleteConfirm(chatApi.deleteSession, loadSessions)

  // 选择会话；disconnect 控制是否断开连接，loadHistory 控制是否重载历史
  const selectSession = (session, disconnectCallback = null, disconnect = true, loadHistory = true) => {
    if (disconnect && disconnectCallback) {
      disconnectCallback()
    }    
    sessionGeneration += 1
    currentSessionId.value = session?.session_id
    if (!loadHistory){
      return
    }
    // 重置分页状态
    currentPage.value = 1
    hasMore.value = true
    historyLoading.value = false
    
    // 触发历史记录加载（如果有回调）
    if (loadHistoryCallback) {
      loadHistoryCallback(2)
    }
  }

  // 加载会话历史记录；pageCount 为本次连续加载的页数
  const loadSessionHistory = async (pageCount = 1) => {
    if (!currentSessionId.value || historyLoading.value || !hasMore.value) return []

    const requestedSessionId = currentSessionId.value
    const requestedGeneration = sessionGeneration
    historyLoading.value = true
    try {
      const pages = []
      const pagesToLoad = Math.max(1, pageCount)

      for (let index = 0; index < pagesToLoad && hasMore.value; index += 1) {
        const page = currentPage.value
        const res = await chatApi.sessionsHistory(requestedSessionId, page, PAGE_SIZE)
        if (requestedGeneration !== sessionGeneration || requestedSessionId !== currentSessionId.value) {
          return []
        }

        const historyData = res.data?.data || []
        if (historyData.length > 0) {
          pages.unshift(historyData)
          currentPage.value = page + 1
        }
        if (historyData.length < PAGE_SIZE) {
          hasMore.value = false
        }
      }

      return pages.flat()
    } catch (err) {
      if (requestedGeneration === sessionGeneration && requestedSessionId === currentSessionId.value) {
        ElMessage.error(err.message || t('chat.load_history_failed'))
      }
      return []
    } finally {
      if (requestedGeneration === sessionGeneration) {
        historyLoading.value = false
      }
    }
  }

  // 新建会话，可传入断开连接回调
  const createNewSession = (disconnectCallback = null) => {
    // 断开连接（如果有回调）
    if (disconnectCallback) {
      disconnectCallback()
    }
    sessionGeneration += 1
    currentSessionId.value = null
    // 设置新建会话状态为 true
    sessionCreating.value = true
  }

  // 获取按最后活跃时间排序的会话列表
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
  }


  // 异步生成并更新会话标题
  const updateSessionTitle = async (
    sessionId, // 会话 ID
    firstMessage // 第一条消息内容
  ) => {
    if (!sessionId) return
    
    // 格式化当前时间为 YYYY-MM-DD HH:mm:ss (与后端保持一致)
    const now = new Date()
    const lastActive = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`

    try {
      const res = await chatApi.generateTitle({
        session_id: sessionId,
        first_message: firstMessage
      })
      
      const newTitle = res.data?.data?.title || t('chat.default_title')
      
      // 更新本地会话列表中的标题
      let sessionIdx = sessions.value.findIndex(s => s.session_id === sessionId)
      
      // 如果在列表中找不到（新会话），则立即插入初始项
      if (sessionIdx === -1) {
        const newSession = {
          session_id: sessionId,
          title: '',
          last_active: lastActive
        }
        sessions.value.unshift(newSession)
        sessionIdx = 0
      }

      // 执行打字机效果
      const session = sessions.value[sessionIdx]
      typingSessionId.value = sessionId
      const targetTitle = newTitle
      session.title = ''
      
      let i = 0
      const timer = setInterval(() => {
        if (i < targetTitle.length) {
          session.title += targetTitle[i]
          i++
        } else {
          clearInterval(timer)
          typingSessionId.value = null
        }
      }, 100)
    } catch (err) {
      console.error('Failed to generate session title:', err)
      // 出错兜底：确保会话项至少存在于列表中
      if (sessions.value.findIndex(s => s.session_id === sessionId) === -1) {
        sessions.value.unshift({
          session_id: sessionId,
          title: t('chat.default_title'),
          last_active: lastActive
        })
      }
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
    updateSessionTitle
  }
}
