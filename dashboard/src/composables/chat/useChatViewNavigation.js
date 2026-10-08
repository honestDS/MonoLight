import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

import {
  shouldDeferChatContent,
  shouldExposeChatContent,
  shouldReleaseChatContent
} from '../../utils/chatContentReveal.js'

export function useChatViewNavigation({
  chat,
  layout,
  route,
  router,
  sessionTaskService,
  loadProfiles,
  notify,
  translate
}) {
  const t = translate
  const {
    currentSessionId,
    sessions,
    messages,
    initialHistoryLoaded,
    messageList,
    loadSessions,
    selectSession,
    createNewSession,
    disconnectWebSocket,
    handleScroll
  } = chat
  const { sessionEngaged, closeSessionsPanel, moreOptionsVisible } = layout

  let navigationVersion = 0
  let disposed = false
  const isValidTaskSessionId = sid => typeof sid === 'string' && sid.length > 0 && sid.length <= 100

  watch(
    [currentSessionId, () => route.path],
    ([sessionId, path]) => {
      sessionTaskService?.setViewingSession(path === '/' ? sessionId : null)
    },
    { immediate: true, flush: 'sync' }
  )

  const deferredContentSessionId = ref(null)
  const chatContentVisible = computed(() => shouldExposeChatContent({
    deferredSessionId: deferredContentSessionId.value,
    currentSessionId: currentSessionId.value
  }))
  const renderedMessages = computed(() => chatContentVisible.value ? messages.value : [])
  const renderedInitialHistoryLoaded = computed(() => (
    chatContentVisible.value && initialHistoryLoaded.value
  ))

  const syncSessionQuery = (sessionId) => {
    if (disposed || route.path !== '/') return

    const query = { ...route.query }
    delete query.task_open
    if (isValidTaskSessionId(sessionId)) query.session_id = sessionId
    else delete query.session_id

    router.replace({ path: route.path, query, hash: route.hash }).catch(console.error)
  }

  const handleSelectSession = (session) => {
    const previousSessionId = currentSessionId.value
    navigationVersion++
    closeSessionsPanel()
    moreOptionsVisible.value = false
    const sessionId = session?.session_id
    const shouldDefer = shouldDeferChatContent({
      wasWelcome: !sessionEngaged.value,
      deferActive: Boolean(deferredContentSessionId.value),
      sessionId
    })

    deferredContentSessionId.value = shouldDefer ? sessionId : null
    selectSession(session)
    if (currentSessionId.value === previousSessionId) syncSessionQuery(currentSessionId.value)
  }

  const handleCreateNewSession = () => {
    const previousSessionId = currentSessionId.value
    navigationVersion++
    closeSessionsPanel()
    moreOptionsVisible.value = false
    deferredContentSessionId.value = null
    createNewSession()
    if (currentSessionId.value === previousSessionId) syncSessionQuery(currentSessionId.value)
  }

  watch(currentSessionId, sessionId => {
    navigationVersion++
    syncSessionQuery(sessionId)
  }, { flush: 'sync' })

  watch(() => [route.query.session_id, route.query.task_open], async ([sid, openToken]) => {
    const version = ++navigationVersion
    if (route.path !== '/') return
    if (sid === undefined && currentSessionId.value) {
      handleCreateNewSession()
      return
    }
    if (!isValidTaskSessionId(sid)) return
    if (sid === currentSessionId.value && openToken === undefined) return
    await loadSessions()
    if (
      disposed ||
      version !== navigationVersion ||
      route.path !== '/' ||
      route.query.session_id !== sid ||
      route.query.task_open !== openToken
    ) return
    const session = sessions.value.find(item => item.session_id === sid)
    if (session) handleSelectSession(session)
    else {
      notify.warning(t('chat.task_session_unavailable'))
      syncSessionQuery(currentSessionId.value)
    }
  }, { immediate: true })

  const handleWelcomeExitTransitionEnd = async (event) => {
    if (!shouldReleaseChatContent({
      deferredSessionId: deferredContentSessionId.value,
      currentSessionId: currentSessionId.value,
      propertyName: event.propertyName
    })) return

    deferredContentSessionId.value = null
    await nextTick()
    await messageList.value?.scrollToBottom('auto')
  }

  onMounted(() => {
    if (!isValidTaskSessionId(route.query.session_id)) loadSessions()
    loadProfiles()
    if (messageList.value) {
      messageList.value.addEventListener('scroll', handleScroll)
    }
  })

  onUnmounted(() => {
    disposed = true
    navigationVersion++
    if (messageList.value) {
      messageList.value.removeEventListener('scroll', handleScroll)
    }
    sessionTaskService?.setViewingSession(null)
    disconnectWebSocket()
  })

  return {
    deferredContentSessionId,
    renderedMessages,
    renderedInitialHistoryLoaded,
    handleSelectSession,
    handleCreateNewSession,
    handleWelcomeExitTransitionEnd
  }
}
