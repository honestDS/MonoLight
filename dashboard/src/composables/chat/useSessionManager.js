// 会话管理 composable：列表、选择与新建会话
import { inject, onScopeDispose, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { useDeleteConfirm } from '../useDeleteConfirm'
import { PAGE_SIZE } from '../../constants'
import i18n from '../../i18n'
import { resolveHistoryRequest } from './historyPagination.js'
import { createSessionActivityController, formatSessionActivityTime } from './sessionActivity.js'
import { SESSION_ACTIVITY_KEY } from './useSessionTasks.js'

const t = (key, ...args) => i18n.global.t(key, ...args)

export function useSessionManager() {
  const activityService = inject(SESSION_ACTIVITY_KEY, null)

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
  let sessionsUpdatedCallback = null
  let activityUpdatedCallback = null
  let disposed = false

  // ==================== 会话管理方法 ====================
  
  // 设置加载历史记录回调；callback 签名为 (sessionId, page, pageSize) => Promise
  const setLoadHistoryCallback = (callback) => {
    loadHistoryCallback = callback
  }

  const setSessionsUpdatedCallback = (callback) => {
    sessionsUpdatedCallback = callback
  }

  const setSessionActivityUpdatedCallback = (callback) => {
    activityUpdatedCallback = callback
  }

  const fetchSessions = async () => {
    const res = await chatApi.sessionsList()
    return res.data.data || []
  }

  const fetchActivity = async () => {
    return activityService?.snapshot?.value?.activities || []
  }

  const sessionActivityController = createSessionActivityController({
    fetchSessions,
    fetchActivity,
    getSessions: () => sessions.value,
    setSessions: value => {
      sessions.value = value
    },
    getCurrentSessionId: () => currentSessionId.value,
    nextSequence: activityService?.nextSequence,
    getActivitySequence: () => activityService?.snapshot?.value?.sequence ?? 0,
    onSessionsUpdated: value => sessionsUpdatedCallback
      ? sessionsUpdatedCallback(value)
      : undefined,
    onActivityUpdated: value => activityUpdatedCallback
      ? activityUpdatedCallback(value)
      : undefined
  })

  const stopActivityWatch = activityService
    ? watch(activityService.snapshot, snapshot => {
      if (snapshot) void sessionActivityController.refreshActivity().catch(console.error)
    }, { flush: 'sync' })
    : null

  /**
   * 按需加载完整会话列表，活动字段由全局活动快照同步。
   */
  const loadSessions = async () => {
    sessionsLoading.value = true
    try {
      return await sessionActivityController.refreshSessions()
    } catch (err) {
      if (!disposed) {
        ElMessage.error(err.message || t('chat.load_sessions_failed'))
      }
      return sessions.value
    } finally {
      sessionsLoading.value = false
    }
  }

  const refreshSessionLoadingState = () => sessionActivityController.refreshSessions().catch(err => {
    console.error(err)
    return sessions.value
  })

  onScopeDispose(() => {
    disposed = true
    sessionsUpdatedCallback = null
    activityUpdatedCallback = null
    stopActivityWatch?.()
    sessionActivityController.dispose()
  })

  // 使用删除确认组合式函数
  const { handleDelete: handleDeleteSession } = useDeleteConfirm(chatApi.deleteSession, loadSessions)

  // 选择会话；disconnect 控制是否断开连接，loadHistory 控制是否重载历史
  const selectSession = (session, disconnectCallback = null, disconnect = true, loadHistory = true) => {
    sessionActivityController.resetReplyObservation()
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
      const request = resolveHistoryRequest(currentPage.value, pageCount, PAGE_SIZE)
      const res = await chatApi.sessionsHistory(requestedSessionId, request.page, request.size)
      if (requestedGeneration !== sessionGeneration || requestedSessionId !== currentSessionId.value) {
        return []
      }

      const historyData = res.data?.data || []
      if (historyData.length > 0) {
        currentPage.value = request.nextPage
      }
      if (historyData.length < request.size) {
        hasMore.value = false
      }
      return historyData
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
    sessionActivityController.resetReplyObservation()
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
    
    const lastActive = formatSessionActivityTime()

    try {
      const res = await chatApi.generateTitle({
        session_id: sessionId,
        first_message: firstMessage
      })
      
      const newTitle = res.data?.data?.title || t('chat.default_title')
      await loadSessions()
      
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
      typingSessionId.value = sessionId
      const targetTitle = newTitle
      sessions.value[sessionIdx].title = ''
      
      let i = 0
      const timer = setInterval(() => {
        if (disposed) {
          clearInterval(timer)
          if (typingSessionId.value === sessionId) typingSessionId.value = null
          return
        }

        const session = sessions.value.find(s => s.session_id === sessionId)
        if (!session) {
          clearInterval(timer)
          if (typingSessionId.value === sessionId) typingSessionId.value = null
          return
        }

        if (i < targetTitle.length) {
          session.title = targetTitle.slice(0, i + 1)
          i++
        } else {
          clearInterval(timer)
          if (typingSessionId.value === sessionId) typingSessionId.value = null
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
    setSessionsUpdatedCallback,
    setSessionActivityUpdatedCallback,
    loadSessions,
    refreshSessionLoadingState,
    selectSession,
    createNewSession,
    handleDeleteSession,
    loadSessionHistory,
    getSortedSessions,
    resetPagination,
    updateSessionTitle
  }
}
