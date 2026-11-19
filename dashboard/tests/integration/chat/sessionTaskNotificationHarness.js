import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import {
  computed,
  effectScope,
  h,
  nextTick,
  onScopeDispose,
  reactive,
  readonly,
  ref,
  shallowReactive,
  shallowRef,
  watch
} from 'vue'
import { resolveHistoryRequest } from '../../../src/composables/chat/historyPagination.js'
import {
  createSessionActivityController,
  formatSessionActivityTime
} from '../../../src/composables/chat/sessionActivity.js'
import { createSessionTaskController } from '../../../src/composables/chat/sessionTasks.js'

const useSessionTasksSource = readFileSync(
  new URL('../../../src/composables/chat/useSessionTasks.js', import.meta.url),
  'utf8'
)
const useSessionManagerSource = readFileSync(
  new URL('../../../src/composables/chat/useSessionManager.js', import.meta.url),
  'utf8'
)

const loadUseSessionTasks = dependencies => {
  const source = useSessionTasksSource
    .replace(/^import[^\n]*\n/gm, '')
    .replace('export const SESSION_TASKS_KEY', 'const SESSION_TASKS_KEY')
    .replace('export const SESSION_ACTIVITY_KEY', 'const SESSION_ACTIVITY_KEY')
    .replace('export const useSessionTasks', 'const useSessionTasks')
    + '\nglobalThis.__module = { SESSION_TASKS_KEY, SESSION_ACTIVITY_KEY, useSessionTasks }\n'
  const context = vm.createContext({ ...dependencies })
  vm.runInContext(source, context, { filename: 'useSessionTasks.js' })
  return context.__module
}

const loadUseSessionManager = dependencies => {
  const source = useSessionManagerSource
    .replace(/^import[^\n]*\n/gm, '')
    .replace('export function useSessionManager', 'function useSessionManager')
    + '\nglobalThis.__module = { useSessionManager }\n'
  const context = vm.createContext({ ...dependencies })
  vm.runInContext(source, context, { filename: 'useSessionManager.js' })
  return context.__module
}

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flushMicrotasks = async () => {
  for (let index = 0; index < 12; index += 1) await Promise.resolve()
}

const createManualTimerQueue = () => {
  const pending = []
  const scheduled = []
  const cancelled = []

  const schedule = (callback, delay) => {
    const handle = { callback, delay }
    pending.push(handle)
    scheduled.push(handle)
    return handle
  }

  const cancel = handle => {
    cancelled.push(handle)
    const index = pending.indexOf(handle)
    if (index !== -1) pending.splice(index, 1)
  }

  const tick = async () => {
    const handle = pending.shift()
    assert.ok(handle, 'expected a scheduled poll timer')
    return handle.callback()
  }

  return { pending, scheduled, cancelled, schedule, cancel, tick }
}

const createTask = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  title: sessionId,
  is_owned: true,
  is_running: false,
  completed_message_id: null,
  completed_status: null,
  last_read_message_id: null,
  ...overrides
})

const createActivity = (sessionId, overrides = {}) => ({
  ...createTask(sessionId),
  latest_message_id: 1,
  last_active: '2026-10-08 00:00:00',
  source: 'external',
  is_loading: false,
  is_reply_running: false,
  ...overrides
})

const createSession = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  title: `${sessionId} full title`,
  last_active: '2026-10-08 00:00:00',
  source: 'external',
  latest_message_id: null,
  is_loading: false,
  is_reply_running: false,
  metadata: `${sessionId} metadata`,
  ...overrides
})

const createHarness = (t, options = {}) => {
  const timers = createManualTimerQueue()
  const storageValues = new Map()
  const initialToken = options.token === undefined ? 'test-token' : options.token
  if (initialToken !== null) {
    storageValues.set('token', initialToken)
  }
  const storage = {
    getItem: key => storageValues.get(key) ?? null,
    setItem: (key, value) => storageValues.set(key, String(value)),
    removeItem: key => storageValues.delete(key)
  }

  const listeners = new Map()
  const window = {
    addEventListener: (type, listener) => {
      const entries = listeners.get(type) || new Set()
      entries.add(listener)
      listeners.set(type, entries)
    },
    removeEventListener: (type, listener) => {
      const entries = listeners.get(type)
      if (!entries) return
      entries.delete(listener)
      if (entries.size === 0) listeners.delete(type)
    },
    dispatch: (type, event = {}) => {
      for (const listener of [...(listeners.get(type) || [])]) {
        listener({ ...event, type })
      }
    },
    listenerCount: type => listeners.get(type)?.size || 0
  }
  const documentListeners = new Map()
  const document = {
    hidden: false,
    visibilityState: 'visible',
    addEventListener: (type, listener) => {
      const entries = documentListeners.get(type) || new Set()
      entries.add(listener)
      documentListeners.set(type, entries)
    },
    removeEventListener: (type, listener) => {
      const entries = documentListeners.get(type)
      if (!entries) return
      entries.delete(listener)
      if (entries.size === 0) documentListeners.delete(type)
    },
    dispatch: (type, event = {}) => {
      for (const listener of [...(documentListeners.get(type) || [])]) {
        listener({ ...event, type })
      }
    },
    listenerCount: type => documentListeners.get(type)?.size || 0
  }

  const route = reactive({
    path: options.routePath || '/profiles',
    fullPath: options.routePath || '/profiles',
    query: options.query || {}
  })
  const navigations = []
  const router = {
    navigations,
    push: location => {
      navigations.push(location)
      return Promise.resolve(location)
    }
  }
  const translations = {
    'chat.task_center_title': '会话任务',
    'chat.task_failure_notification_title': '会话任务处理失败',
    'chat.task_notification_title': '会话任务已完成',
    'chat.task_notification_message': '有新的处理结果',
    'chat.task_summary': '{running} 个进行中，{unread} 个未读'
  }
  const tTranslate = (key, values = {}) => {
    const template = translations[key] || key
    return template.replace(/\{(\w+)\}/g, (_match, name) => String(values[name] ?? ''))
  }

  const notifications = []
  const closedNotifications = []
  const ElNotification = optionsForNotification => {
    const renderedMessage = computed(() => (
      typeof optionsForNotification.message === 'function'
        ? optionsForNotification.message()
        : optionsForNotification.message
    ))
    const entry = { ...optionsForNotification, closed: false }
    Object.defineProperty(entry, 'message', {
      configurable: true,
      enumerable: true,
      get: () => renderedMessage.value
    })
    entry.handle = {
      close: () => {
        if (entry.closed) return
        entry.closed = true
        closedNotifications.push(entry)
      }
    }
    notifications.push(entry)
    return entry.handle
  }

  const fetchActivity = typeof options.fetchActivity === 'function'
    ? options.fetchActivity
    : (() => {
        const responses = Array.isArray(options.fetchActivity) ? options.fetchActivity : [[]]
        let index = 0
        return () => responses[Math.min(index++, responses.length - 1)]
      })()
  const fetchSessions = typeof options.sessionsList === 'function'
    ? options.sessionsList
    : (() => {
        const responses = Array.isArray(options.sessionsList) ? options.sessionsList : [[]]
        let index = 0
        return () => responses[Math.min(index++, responses.length - 1)]
      })()
  const markRead = options.markRead || ((_sessionId, messageId) => ({
    last_read_message_id: messageId
  }))
  const readCalls = []
  let fetchCallCount = 0
  let sessionsListCallCount = 0
  const chatApi = {
    sessionsActivity: async () => {
      const value = await fetchActivity(fetchCallCount++)
      if (value instanceof Error) throw value
      return { data: { data: value } }
    },
    sessionsList: async () => {
      const value = await fetchSessions(sessionsListCallCount++)
      if (value instanceof Error) throw value
      return { data: { data: value } }
    },
    markSessionRead: async (sessionId, messageId) => {
      readCalls.push([sessionId, messageId])
      const value = await markRead(sessionId, messageId)
      if (value instanceof Error) throw value
      return { data: { data: value } }
    }
  }

  const cleanupCallbacks = []
  const provided = new Map()
  const createController = controllerOptions => {
    const originalNotify = controllerOptions.onNotify
    const wrappedOptions = {
      ...controllerOptions,
      onNotify: task => {
        options.onNotifyObserved?.(task)
        return originalNotify(task)
      },
      schedule: timers.schedule,
      cancel: timers.cancel
    }
    return createSessionTaskController(wrappedOptions)
  }
  const loaded = loadUseSessionTasks({
    h,
    onBeforeUnmount: callback => cleanupCallbacks.push(callback),
    provide: (key, value) => provided.set(key, value),
    readonly,
    ref,
    shallowReactive,
    shallowRef,
    watch,
    useRoute: () => route,
    useRouter: () => router,
    useI18n: () => ({ t: tTranslate }),
    ElNotification,
    chatApi,
    createSessionTaskController: createController,
    document,
    localStorage: storage,
    window,
    console
  })
  const scope = effectScope()
  const instance = scope.run(() => loaded.useSessionTasks())
  const loadedManager = loadUseSessionManager({
    inject: key => provided.get(key) ?? null,
    onScopeDispose,
    ref,
    watch,
    ElMessage: { error: () => {} },
    chatApi,
    useDeleteConfirm: () => ({ handleDelete: () => {} }),
    PAGE_SIZE: 20,
    i18n: { global: { t: tTranslate } },
    resolveHistoryRequest,
    createSessionActivityController,
    formatSessionActivityTime,
    SESSION_ACTIVITY_KEY: loaded.SESSION_ACTIVITY_KEY,
    console
  })
  const managerScopes = new Set()
  const mountSessionManager = () => {
    const managerScope = effectScope()
    const managerInstance = managerScope.run(() => loadedManager.useSessionManager())
    const mounted = {
      instance: managerInstance,
      dispose: () => {
        if (mounted.disposed) return
        mounted.disposed = true
        managerScopes.delete(mounted)
        managerScope.stop()
      },
      disposed: false
    }
    managerScopes.add(mounted)
    return mounted
  }
  const harness = {
    instance,
    route,
    router,
    timers,
    document,
    storage,
    window,
    notifications,
    closedNotifications,
    readCalls,
    fetchCallCount: () => fetchCallCount,
    sessionsListCallCount: () => sessionsListCallCount,
    provided,
    activity: provided.get(loaded.SESSION_ACTIVITY_KEY),
    mountSessionManager,
    setVisibility: hidden => {
      document.hidden = hidden
      document.visibilityState = hidden ? 'hidden' : 'visible'
      document.dispatch('visibilitychange')
    },
    setToken: token => {
      if (token === null) storage.removeItem('token')
      else storage.setItem('token', token)
      window.dispatch('storage', { key: 'token' })
    },
    setRoute: async path => {
      route.path = path
      route.fullPath = path
      await nextTick()
      await flushMicrotasks()
    },
    refreshAndFlush: async () => {
      await instance.refresh()
      await flushMicrotasks()
    },
    readSession: (...args) => provided.get(loaded.SESSION_TASKS_KEY).readSession(...args),
    setViewingSession: sessionId => provided.get(loaded.SESSION_TASKS_KEY).setViewingSession(sessionId),
    unmount: () => {
      if (harness.unmounted) return
      harness.unmounted = true
      for (const mounted of [...managerScopes]) mounted.dispose()
      for (const callback of cleanupCallbacks.splice(0)) callback()
      scope.stop()
    },
    unmounted: false
  }
  t.after(() => harness.unmount())
  return harness
}

const notificationButtons = notification => (
  Array.isArray(notification.message?.children)
    ? notification.message.children.filter(child => child?.type === 'button')
    : []
)

const managerSession = (manager, sessionId) => (
  Array.from(manager.instance.sessions.value).find(session => session.session_id === sessionId)
)

export {
  createActivity,
  createDeferred,
  createHarness,
  createSession,
  createTask,
  flushMicrotasks,
  managerSession,
  notificationButtons
}
