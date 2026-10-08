import { h, onBeforeUnmount, provide, readonly, ref, shallowReactive, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElNotification } from 'element-plus'
import { chatApi } from '../../api/index.js'
import { createSessionTaskController } from './sessionTasks.js'

export const SESSION_TASKS_KEY = 'sessionTasks'
export const SESSION_ACTIVITY_KEY = 'sessionActivity'

export const useSessionTasks = () => {
  const route = useRoute()
  const router = useRouter()
  const { t } = useI18n()
  const tasks = ref([])
  const error = ref(false)
  const loading = ref(false)
  const standalonePaths = ['/login', '/setup', '/backend-unavailable']
  let controller
  let identity = null
  let viewingSession = null
  let identityVersion = 0
  let requestSequence = 0
  let refreshSequence = 0
  let openSequence = 0
  let pendingBatch = null
  const nextSequence = () => ++requestSequence
  const activitySnapshot = shallowRef(null)
  const notifyButtonStyle = {
    border: '0',
    background: 'transparent',
    color: 'var(--el-color-primary)',
    cursor: 'pointer',
    minHeight: '44px',
    width: '100%',
    textAlign: 'left',
    whiteSpace: 'normal',
    overflowWrap: 'anywhere'
  }
  const openSession = sessionId => {
    if (typeof sessionId !== 'string' || sessionId.trim() === '') return
    try {
      let openToken = String(++openSequence)
      if (openToken === String(route.query?.task_open)) {
        openToken = String(++openSequence)
      }
      const navigation = router.push({
        path: '/',
        query: { session_id: sessionId, task_open: openToken }
      })
      if (navigation && typeof navigation.catch === 'function') {
        navigation.catch(navigationError => console.error(navigationError))
      }
    } catch (navigationError) {
      console.error(navigationError)
    }
  }
  const taskTitle = task => (typeof task.title === 'string' && task.title.trim() !== '')
    ? task.title
    : task.session_id.slice(0, 8)
  const closeBatchToast = batch => {
    const toast = batch.toast
    batch.toast = null
    if (typeof toast === 'function') toast()
    else if (toast && typeof toast.close === 'function') toast.close()
  }
  const closeBatchEntry = (batch, sessionId, entry) => {
    if (entry.closed) return
    entry.closed = true
    if (batch.tasks.get(sessionId) !== entry) return
    batch.tasks.delete(sessionId)
    if (batch.flushed && batch.tasks.size === 0) closeBatchToast(batch)
  }
  const flushNotificationBatch = batch => {
    if (pendingBatch === batch) pendingBatch = null
    batch.flushed = true
    const list = [...batch.tasks.values()].map(entry => entry.task)
    if (list.length === 0) return

    const handleNotificationClick = (sessionId, event) => {
      event?.stopPropagation()
      closeBatchToast(batch)
      if (sessionId) openSession(sessionId)
    }
    let title
    let message
    if (list.length === 1) {
      const task = list[0]
      title = task.completed_status === 'failed'
        ? t('chat.task_failure_notification_title')
        : t('chat.task_notification_title')
      message = h('div', { role: 'status', 'aria-live': 'polite' }, [
        h('button', {
          type: 'button',
          style: notifyButtonStyle,
          onClick: event => handleNotificationClick(task.session_id, event)
        }, t('chat.task_notification_message', { title: taskTitle(task) }))
      ])
    } else {
      title = t('chat.task_center_title')
      message = () => {
        const currentList = [...batch.tasks.values()].map(entry => entry.task)
        const running = currentList.filter(task => task.is_running).length
        const unread = currentList.filter(task => task.has_unread_result).length
        const buttons = currentList.slice(0, 3).map(task => h('button', {
          key: task.session_id,
          type: 'button',
          style: notifyButtonStyle,
          onClick: event => handleNotificationClick(task.session_id, event)
        }, taskTitle(task)))
        return h('div', { role: 'status', 'aria-live': 'polite' }, [
          h('div', {}, t('chat.task_summary', { running, unread })),
          ...buttons
        ])
      }
    }

    try {
      batch.toast = ElNotification({
        title,
        message,
        duration: 0,
        onClick: event => {
          const currentEntry = batch.tasks.size === 1
            ? batch.tasks.values().next().value
            : null
          handleNotificationClick(currentEntry?.task.session_id, event)
        }
      })
    } catch (notificationError) {
      console.error(notificationError)
    }
  }
  const onNotify = task => {
    if (!pendingBatch) {
      pendingBatch = { tasks: shallowReactive(new Map()), flushed: false, toast: null }
      const batch = pendingBatch
      Promise.resolve().then(() => flushNotificationBatch(batch))
    }
    const batch = pendingBatch
    const sessionId = task.session_id
    const previous = batch.tasks.get(sessionId)
    if (previous) previous.closed = true
    const entry = { task, closed: false }
    batch.tasks.set(sessionId, entry)
    return { close: () => closeBatchEntry(batch, sessionId, entry) }
  }
  const readIdentity = () => {
    if (standalonePaths.includes(route.path)) return null
    const token = localStorage.getItem('token')
    return typeof token === 'string' && token.trim() !== '' ? token : null
  }
  const syncIdentity = () => {
    const nextIdentity = readIdentity()
    if (nextIdentity !== identity) {
      identity = nextIdentity
      viewingSession = null
      identityVersion += 1
      loading.value = false
      error.value = false
    }
    return controller.setIdentity(nextIdentity)
  }
  const syncViewingSession = () => {
    const sessionId = route.path === '/' && document.visibilityState === 'visible'
      ? viewingSession
      : null
    controller.setViewingSession(sessionId)
  }
  const fetchActivity = async () => {
    try {
      const response = await chatApi.sessionsActivity()
      return response.data.data
    } finally {
      syncIdentity()
    }
  }
  const markRead = async (sessionId, messageId) => {
    try {
      const response = await chatApi.markSessionRead(sessionId, messageId)
      return response.data.data
    } finally {
      syncIdentity()
    }
  }
  controller = createSessionTaskController({
    fetchActivity,
    nextSequence,
    markRead,
    onActivityUpdated: snapshot => {
      activitySnapshot.value = snapshot
    },
    onTasksUpdated: nextTasks => {
      tasks.value = nextTasks
      error.value = false
    },
    onNotify,
    onError: controllerError => {
      error.value = true
      console.error(controllerError)
    }
  })
  const refresh = () => {
    syncIdentity()
    const requestIdentityVersion = identityVersion
    const requestSequence = ++refreshSequence
    loading.value = true
    return Promise.resolve(controller.refresh()).finally(() => {
      if (requestIdentityVersion === identityVersion && requestSequence === refreshSequence) {
        loading.value = false
      }
    })
  }
  provide(SESSION_TASKS_KEY, {
    readSession: (...args) => {
      syncIdentity()
      return controller.readSession(...args)
    },
    setViewingSession: sessionId => {
      syncIdentity()
      viewingSession = typeof sessionId === 'string' && sessionId.trim() !== ''
        ? sessionId
        : null
      syncViewingSession()
    }
  })
  provide(SESSION_ACTIVITY_KEY, {
    snapshot: readonly(activitySnapshot),
    nextSequence
  })
  const onStorage = event => {
    if (event.key === 'token' || event.key === null) {
      syncIdentity()
      syncViewingSession()
    }
  }
  const onFocus = () => {
    syncIdentity()
    syncViewingSession()
    refresh()
  }
  const onVisibilityChange = () => {
    syncViewingSession()
  }
  const stopIdentityWatch = watch(() => route.fullPath, () => {
    syncIdentity()
    syncViewingSession()
  }, { immediate: true, flush: 'sync' })
  window.addEventListener('storage', onStorage)
  window.addEventListener('focus', onFocus)
  document.addEventListener('visibilitychange', onVisibilityChange)
  onBeforeUnmount(() => {
    stopIdentityWatch()
    window.removeEventListener('storage', onStorage)
    window.removeEventListener('focus', onFocus)
    document.removeEventListener('visibilitychange', onVisibilityChange)
    controller.dispose()
  })
  return { tasks, error, loading, refresh, openSession, activitySnapshot }
}
