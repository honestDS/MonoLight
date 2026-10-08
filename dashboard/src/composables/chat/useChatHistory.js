import { nextTick, ref, watch } from 'vue'
import { createHistoryMergeTracker } from './historyMergeTracker.js'
import { getIncrementalHistoryCursor, syncIncrementalHistory } from './historyIncrementalSync.js'
import { createHttpHistorySyncController } from './httpHistorySync.js'
import {
  findTransientHistoryMessageIndex,
  getAuditConfirmationRecordId,
  normalizeHistoryMessage
} from './chatMessageHelpers.js'
import {
  findAssistantResponseReplacementIndex,
  findMessageReplacementIndex,
  getMessageDedupeKeys,
  isAssistantResponse,
  isPlainAssistantResponse,
  mergeAssistantResponseIntoList,
  mergeRemoteMessage
} from '../../utils'
import { filterToolOutputMessages } from '../../utils/toolOutputVisibility'

const HTTP_HISTORY_FAST_SYNC_INTERVAL_MS = 2000
const HTTP_HISTORY_INCREMENTAL_PAGE_SIZE = 50
const HTTP_HISTORY_INCREMENTAL_MAX_PAGES = 4

export function useChatHistory({
  chatState,
  sessionManager,
  transport,
  isCurrentSessionReadOnly,
  currentSessionShowToolCalls,
  isSessionScopeActive,
  api,
  createHistorySyncController = createHttpHistorySyncController
}) {
  const historyMergeTracker = createHistoryMergeTracker()
  const initialHistoryLoaded = ref(true)
  const incrementalHistoryCursors = new Map()
  let restoringHistoryScroll = false

  const filterNewMessages = (messages) => filterToolOutputMessages(
    messages,
    currentSessionShowToolCalls.value
  )

  const loadInitialSessionHistory = async (pageCount) => {
    const requestedSessionId = sessionManager.currentSessionId.value
    const historyData = await sessionManager.loadSessionHistory(pageCount)
    if (requestedSessionId !== sessionManager.currentSessionId.value) return

    restoringHistoryScroll = true
    try {
      const visibleHistoryData = filterNewMessages(historyData)
      if (visibleHistoryData.length > 0) {
        // 插入到消息列表开头
        chatState.insertMessage(0, visibleHistoryData.map(normalizeHistoryMessage), true)
      }
      initialHistoryLoaded.value = true
      await nextTick()
      if (visibleHistoryData.length > 0) {
        await chatState.scrollToBottom('auto')
      }
    } finally {
      requestAnimationFrame(() => {
        restoringHistoryScroll = false
      })
    }
    return historyData
  }

  sessionManager.setLoadHistoryCallback((pageCount) => loadInitialSessionHistory(pageCount))

  const reloadCurrentSessionHistory = async () => {
    if (!sessionManager.currentSessionId.value) return
    initialHistoryLoaded.value = false
    chatState.clearMessages()
    sessionManager.resetPagination()
    await loadInitialSessionHistory(2)
  }

  const mergeSessionHistoryPage = historyData => {
    if (!Array.isArray(historyData) || historyData.length === 0) return

    const existingKeys = new Set(chatState.messages.value.flatMap(m => [...getMessageDedupeKeys(m)]))
    let mergedMessages = [...chatState.messages.value]
    let changed = false
    for (const item of historyData) {
      const message = normalizeHistoryMessage({ ...item, db_id: item.id })
      if (isAssistantResponse(message)) {
        const replacementIndex = findAssistantResponseReplacementIndex(mergedMessages, message)
        if (replacementIndex !== -1) {
          mergedMessages = mergeAssistantResponseIntoList(mergedMessages, message)
          getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
          changed = true
          continue
        }
        if (isPlainAssistantResponse(message)) {
          mergedMessages.push(message)
          getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
          changed = true
          continue
        }
      }

      const auditRecordId = getAuditConfirmationRecordId(message)
      if (auditRecordId) {
        const existingIndex = mergedMessages.findIndex(existing => getAuditConfirmationRecordId(existing) === auditRecordId)
        if (existingIndex !== -1) {
          mergedMessages[existingIndex] = mergeRemoteMessage(mergedMessages[existingIndex], message)
          getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
          changed = true
          continue
        }
      }
      const replacementIndex = findMessageReplacementIndex(mergedMessages, message)
      if (replacementIndex !== -1) {
        mergedMessages[replacementIndex] = mergeRemoteMessage(mergedMessages[replacementIndex], message)
        getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
        changed = true
        continue
      }
      const transientIndex = findTransientHistoryMessageIndex(mergedMessages, message)
      if (transientIndex !== -1) {
        const localMessage = mergedMessages[transientIndex]
        mergedMessages[transientIndex] = mergeRemoteMessage(localMessage, message)
        getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
        changed = true
        continue
      }
      const messageKeys = getMessageDedupeKeys(message)
      if ([...messageKeys].some(key => existingKeys.has(key))) continue
      mergedMessages.push(message)
      messageKeys.forEach(key => existingKeys.add(key))
      changed = true
    }
    if (changed) {
      chatState.messages.value = mergedMessages
    }
  }

  const mergeLatestSessionHistory = async (sessionId = sessionManager.currentSessionId.value) => {
    if (!sessionId || sessionId !== sessionManager.currentSessionId.value || !initialHistoryLoaded.value) return

    const requestId = historyMergeTracker.begin()
    const response = await api.sessionsHistory(sessionId, 1, 20)
    if (
      sessionId !== sessionManager.currentSessionId.value
      || !historyMergeTracker.isLatest(requestId)
    ) return
    mergeSessionHistoryPage(filterNewMessages(response.data?.data || []))
  }

  const ensureIncrementalHistoryCursor = sessionId => {
    if (!sessionId || incrementalHistoryCursors.has(sessionId)) return
    incrementalHistoryCursors.set(
      sessionId,
      getIncrementalHistoryCursor(chatState.messages.value)
    )
  }

  const syncIncrementalSessionHistory = async (
    sessionId = sessionManager.currentSessionId.value,
    isCurrentSync = () => true
  ) => {
    if (!sessionId || sessionId !== sessionManager.currentSessionId.value) {
      return { hasMore: false }
    }
    if (!initialHistoryLoaded.value) {
      return { hasMore: true }
    }

    ensureIncrementalHistoryCursor(sessionId)
    const requestId = historyMergeTracker.begin()
    const isCurrentMerge = () => (
      isSessionScopeActive()
      && sessionId === sessionManager.currentSessionId.value
      && historyMergeTracker.isLatest(requestId)
      && initialHistoryLoaded.value
      && isCurrentSync()
    )
    const result = await syncIncrementalHistory({
      initialAfterId: getIncrementalHistoryCursor(
        chatState.messages.value,
        incrementalHistoryCursors.get(sessionId)
      ),
      pageSize: HTTP_HISTORY_INCREMENTAL_PAGE_SIZE,
      maxPages: HTTP_HISTORY_INCREMENTAL_MAX_PAGES,
      isCurrent: isCurrentMerge,
      fetchPage: async ({ afterId, limit }) => {
        const res = await api.sessionsHistory(sessionId, 1, limit, { after_id: afterId })
        return res.data?.data || []
      },
      mergePage: historyData => {
        if (isCurrentMerge()) {
          mergeSessionHistoryPage(filterNewMessages(historyData))
        }
      }
    })
    if (!isCurrentMerge()) return { hasMore: true }
    if (!result.cancelled) {
      incrementalHistoryCursors.set(sessionId, result.lastMessageId)
    }
    return result
  }

  const canSyncCurrentSessionHistory = () => (
    isSessionScopeActive()
    && initialHistoryLoaded.value
    && !isCurrentSessionReadOnly.value
    && transport.transportMode.value === 'http'
  )

  const httpHistorySync = createHistorySyncController({
    getSessionId: () => sessionManager.currentSessionId.value,
    canSync: canSyncCurrentSessionHistory,
    isLoading: () => chatState.loading.value,
    onTrackingStarted: ensureIncrementalHistoryCursor,
    fetchPendingActivity: async sessionId => {
      const response = await api.backgroundTaskPendingActivity(sessionId)
      return response.data?.data?.has_pending_activity === true
    },
    mergeLatestHistory: syncIncrementalSessionHistory,
    intervalMs: HTTP_HISTORY_FAST_SYNC_INTERVAL_MS,
    onError: err => {
      console.error('HTTP session history synchronization failed:', err)
    }
  })

  const stopHttpHistorySync = httpHistorySync.stop
  const startHttpHistoryBackgroundTaskSync = httpHistorySync.start

  watch(
    [
      () => transport.transportMode.value,
      () => sessionManager.currentSessionId.value,
      () => isCurrentSessionReadOnly.value,
      () => initialHistoryLoaded.value
    ],
    () => httpHistorySync.handleSessionChanged(),
    { immediate: true, flush: 'sync' }
  )

  const handleScroll = async () => {
    const messageList = chatState.messageList.value
    if (!messageList || restoringHistoryScroll || chatState.messages.value.length === 0 || !sessionManager.hasMore.value || sessionManager.historyLoading.value) return
    if (messageList.scrollTop > 500) return

    const historyData = await sessionManager.loadSessionHistory(1)
    if (!historyData?.length) return

    const existingKeys = new Set(chatState.messages.value.flatMap(message => [...getMessageDedupeKeys(message)]))
    const uniqueMessages = filterNewMessages(historyData)
      .map(normalizeHistoryMessage)
      .filter((message) => {
        const messageKeys = getMessageDedupeKeys(message)
        if ([...messageKeys].some(key => existingKeys.has(key))) return false
        messageKeys.forEach(key => existingKeys.add(key))
        return true
      })
    if (!uniqueMessages.length) return

    const anchor = messageList.captureScrollAnchor()
    restoringHistoryScroll = true
    try {
      chatState.insertMessage(0, uniqueMessages)
      await nextTick()
      await messageList.restoreScrollAnchor(anchor)
    } finally {
      requestAnimationFrame(() => {
        restoringHistoryScroll = false
      })
    }
  }

  const bindScrollEvent = () => {
    if (chatState.messageList.value) {
      chatState.messageList.value.addEventListener('scroll', handleScroll)
    }
  }

  const unbindScrollEvent = () => {
    if (chatState.messageList.value) {
      chatState.messageList.value.removeEventListener('scroll', handleScroll)
    }
  }

  const getHistoryCursor = sessionId => getIncrementalHistoryCursor(
    chatState.messages.value,
    incrementalHistoryCursors.get(sessionId)
  )

  const invalidateHistory = () => historyMergeTracker.invalidate()

  const disposeHistory = () => {
    stopHttpHistorySync()
    incrementalHistoryCursors.clear()
  }

  return {
    initialHistoryLoaded,
    loadInitialSessionHistory,
    reloadCurrentSessionHistory,
    mergeLatestSessionHistory,
    syncIncrementalSessionHistory,
    ensureIncrementalHistoryCursor,
    getHistoryCursor,
    startHttpHistoryBackgroundTaskSync,
    stopHttpHistorySync,
    handleScroll,
    bindScrollEvent,
    unbindScrollEvent,
    invalidateHistory,
    disposeHistory
  }
}
