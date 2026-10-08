import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
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
import { createSessionTaskController } from '../src/composables/chat/sessionTasks.js'
import {
  createSessionActivityController,
  formatSessionActivityTime
} from '../src/composables/chat/sessionActivity.js'
import { resolveHistoryRequest } from '../src/composables/chat/historyPagination.js'

const useSessionTasksSource = readFileSync(
  new URL('../src/composables/chat/useSessionTasks.js', import.meta.url),
  'utf8'
)
const useSessionManagerSource = readFileSync(
  new URL('../src/composables/chat/useSessionManager.js', import.meta.url),
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
  const document = {
    hidden: false,
    visibilityState: 'visible'
  }

  const route = reactive({
    path: options.routePath || '/profiles',
    fullPath: options.routePath || '/profiles'
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
      window.dispatch('visibilitychange')
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

test('protected routes restore tasks and keep one poller, while logged-out and standalone routes stay idle', async t => {
  const protectedHarness = createHarness(t, {
    routePath: '/profiles',
    token: 'token-a',
    fetchActivity: () => [createTask('profile-task', {
      title: 'Profile task',
      completed_message_id: 10,
      completed_status: 'succeeded'
    })]
  })

  await protectedHarness.refreshAndFlush()
  assert.deepEqual(protectedHarness.instance.tasks.value.map(task => task.session_id), ['profile-task'])
  assert.equal(protectedHarness.notifications.length, 1)
  const timerBeforeRouteChange = protectedHarness.timers.pending[0]
  const fetchesBeforeRouteChange = protectedHarness.fetchCallCount()

  await protectedHarness.setRoute('/memories')
  assert.equal(protectedHarness.fetchCallCount(), fetchesBeforeRouteChange)
  assert.equal(protectedHarness.timers.pending[0], timerBeforeRouteChange)
  await protectedHarness.timers.tick()
  await flushMicrotasks()
  assert.equal(protectedHarness.fetchCallCount(), fetchesBeforeRouteChange + 1)
  assert.equal(protectedHarness.notifications.length, 1)
  assert.deepEqual(protectedHarness.instance.tasks.value.map(task => task.session_id), ['profile-task'])

  const loggedOutHarness = createHarness(t, {
    routePath: '/profiles',
    token: null,
    fetchActivity: () => [createTask('should-not-load', { is_running: true })]
  })
  await flushMicrotasks()
  assert.equal(loggedOutHarness.timers.pending.length, 0)
  assert.equal(loggedOutHarness.fetchCallCount(), 0)
  assert.equal(loggedOutHarness.instance.tasks.value.length, 0)
  assert.equal(loggedOutHarness.notifications.length, 0)

  const standaloneHarness = createHarness(t, {
    routePath: '/login',
    token: 'token-standalone',
    fetchActivity: () => [createTask('should-not-load', { is_running: true })]
  })
  await flushMicrotasks()
  assert.equal(standaloneHarness.timers.pending.length, 0)
  assert.equal(standaloneHarness.fetchCallCount(), 0)
  assert.equal(standaloneHarness.instance.tasks.value.length, 0)
  assert.equal(standaloneHarness.notifications.length, 0)
})

test('identity changes discard stale responses and logout clears tasks, polling, and its notification', async t => {
  const oldResponse = createDeferred()
  const currentResponse = createDeferred()
  const harness = createHarness(t, {
    token: 'user-a',
    fetchActivity: call => {
      if (call === 0) return oldResponse.promise
      if (call === 1) return currentResponse.promise
      return [createTask('current-session', {
        completed_message_id: 21,
        completed_status: 'succeeded'
      })]
    }
  })
  assert.equal(harness.fetchCallCount(), 1)

  harness.setToken('user-b')
  assert.equal(harness.fetchCallCount(), 2)
  oldResponse.resolve([createTask('stale-session', {
    completed_message_id: 20,
    completed_status: 'succeeded'
  })])
  await flushMicrotasks()
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(harness.notifications.length, 0)

  const currentRefresh = harness.instance.refresh()
  currentResponse.resolve([createTask('current-session', {
    completed_message_id: 21,
    completed_status: 'succeeded'
  })])
  await currentRefresh
  await flushMicrotasks()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['current-session'])
  assert.equal(harness.notifications.length, 1)

  const timerBeforeLogout = harness.timers.pending[0]
  harness.setToken(null)
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(harness.timers.pending.length, 0)
  assert.equal(harness.timers.cancelled.includes(timerBeforeLogout), true)
  assert.deepEqual(harness.closedNotifications.map(notification => notification.title), ['会话任务已完成'])
})

test('completed tasks batch into one permanent toast, retain the full list, and open sessions without marking read', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [
      createTask('one', { title: 'One', completed_message_id: 1, completed_status: 'succeeded' }),
      createTask('two', { title: 'Two', completed_message_id: 2, completed_status: 'succeeded' }),
      createTask('three', { title: 'Three', completed_message_id: 3, completed_status: 'succeeded' }),
      createTask('four', { title: 'Four', completed_message_id: 4, completed_status: 'succeeded' })
    ]
  })

  await harness.refreshAndFlush()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['one', 'two', 'three', 'four'])
  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.notifications[0].title, '会话任务')
  assert.equal(harness.notifications[0].duration, 0)
  const buttons = notificationButtons(harness.notifications[0])
  assert.equal(buttons.length, 3)

  buttons[0].props.onClick()
  assert.equal(harness.notifications[0].closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].path, '/')
  assert.equal(harness.router.navigations[0].query.session_id, 'one')
  assert.equal(harness.router.navigations[0].query.task_open, '1')
  assert.equal(harness.instance.tasks.value.every(task => task.has_unread_result), true)
  assert.deepEqual(harness.readCalls, [])
})

test('reading merged notification entries removes confirmed sessions from the live card', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [
      createTask('first', { completed_message_id: 1, completed_status: 'succeeded' }),
      createTask('second', { completed_message_id: 2, completed_status: 'succeeded' }),
      createTask('third', { completed_message_id: 3, completed_status: 'succeeded' })
    ]
  })

  await harness.refreshAndFlush()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['first', 'second', 'third'])
  assert.equal(harness.notifications.length, 1)
  const notification = harness.notifications[0]
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), [
    'first',
    'second',
    'third'
  ])
  assert.equal(notification.message.children[0].children, '0 个进行中，3 个未读')

  await harness.readSession('first', 1)
  await flushMicrotasks()
  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.notifications[0], notification)
  assert.equal(notification.closed, false)
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), ['second', 'third'])
  assert.equal(notification.message.children[0].children, '0 个进行中，2 个未读')

  await harness.readSession('second', 2)
  await flushMicrotasks()
  assert.equal(harness.notifications.length, 1)
  assert.equal(notification.closed, false)
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), ['third'])
  assert.equal(notification.message.children[0].children, '0 个进行中，1 个未读')

  await harness.readSession('third', 3)
  await flushMicrotasks()
  assert.equal(notification.closed, true)
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(harness.notifications.length, 1)
})

test('a refreshed merged notification navigates to its only remaining unread session', async t => {
  let snapshot = [
    createTask('first', { completed_message_id: 11, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 12, completed_status: 'succeeded' }),
    createTask('third', { completed_message_id: 13, completed_status: 'succeeded' })
  ]
   const harness = createHarness(t, { fetchActivity: () => snapshot })

  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 1)
  const notification = harness.notifications[0]
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), [
    'first',
    'second',
    'third'
  ])

  snapshot = [
    createTask('first', {
      completed_message_id: 11,
      completed_status: 'succeeded',
      last_read_message_id: 11
    }),
    createTask('second', {
      completed_message_id: 12,
      completed_status: 'succeeded',
      last_read_message_id: 12
    }),
    createTask('third', { completed_message_id: 13, completed_status: 'succeeded' })
  ]
  await harness.refreshAndFlush()

  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.notifications[0], notification)
  assert.equal(notification.closed, false)
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), ['third'])
  assert.equal(notification.message.children[0].children, '0 个进行中，1 个未读')

  notification.onClick()

  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].query.session_id, 'third')
  assert.deepEqual(harness.readCalls, [])
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['third'])
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(harness.instance.tasks.value[0].last_read_message_id, null)
})

test('a single successful task keeps a permanent toast until its card opens the session', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('single-success', {
      completed_message_id: 10,
      completed_status: 'succeeded'
    })]
  })

  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 1)
  const notification = harness.notifications[0]
  assert.equal(notification.duration, 0)
  assert.equal(notification.closed, false)

  notification.onClick()
  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].path, '/')
  assert.equal(harness.router.navigations[0].query.session_id, 'single-success')
  assert.equal(harness.router.navigations[0].query.task_open, '1')
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.deepEqual(harness.readCalls, [])

  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 1)
})

test('a single task notification button stops bubbling before card navigation', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('button-success', {
      completed_message_id: 20,
      completed_status: 'succeeded'
    })]
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  const [button] = notificationButtons(notification)
  let propagationStopped = false
  const event = {
    stopPropagation: () => {
      propagationStopped = true
    }
  }

  button.props.onClick(event)
  if (!propagationStopped) notification.onClick(event)

  assert.equal(propagationStopped, true)
  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].query.session_id, 'button-success')
})

test('a multi-task notification card click closes without opening or marking tasks read', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [
      createTask('multi-one', { completed_message_id: 30, completed_status: 'succeeded' }),
      createTask('multi-two', { completed_message_id: 31, completed_status: 'succeeded' })
    ]
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  assert.equal(notification.duration, 0)
  assert.equal(notification.closed, false)

  notification.onClick()

  assert.equal(notification.closed, true)
  assert.deepEqual(harness.router.navigations, [])
  assert.deepEqual(harness.readCalls, [])
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['multi-one', 'multi-two'])
  assert.equal(harness.instance.tasks.value.every(task => task.has_unread_result), true)
})

test('a single failed task has an explicit failure title and notification entries close independently', async t => {
  const failureHarness = createHarness(t, {
    fetchActivity: () => [createTask('failed-session', {
      completed_message_id: 50,
      completed_status: 'failed'
    })]
  })
  await failureHarness.refreshAndFlush()
  assert.equal(failureHarness.notifications.length, 1)
  assert.equal(failureHarness.notifications[0].title, '会话任务处理失败')
  assert.equal(failureHarness.notifications[0].duration, 0)

  let snapshot = [
    createTask('first', { completed_message_id: 1, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 2, completed_status: 'succeeded' })
  ]
   const independentHarness = createHarness(t, { fetchActivity: () => snapshot })
  await independentHarness.refreshAndFlush()
  assert.equal(independentHarness.notifications.length, 1)

  snapshot = [
    createTask('first', { completed_message_id: 3, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 2, completed_status: 'succeeded' })
  ]
  await independentHarness.refreshAndFlush()
  assert.equal(independentHarness.notifications.length, 2)
  assert.equal(independentHarness.notifications[0].closed, false)
  assert.equal(independentHarness.notifications[1].closed, false)

  snapshot = [
    createTask('first', { completed_message_id: 3, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 4, completed_status: 'succeeded' })
  ]
  await independentHarness.refreshAndFlush()
  assert.equal(independentHarness.notifications.length, 3)
  assert.equal(independentHarness.notifications[0].closed, true)
  assert.equal(independentHarness.notifications[1].closed, false)
  assert.equal(independentHarness.notifications[2].closed, false)
})

test('readSession keeps the badge until the service acknowledges the read cursor', async t => {
  const readResponse = createDeferred()
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('read-session', {
      completed_message_id: 60,
      completed_status: 'succeeded'
    })],
    markRead: () => readResponse.promise
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  assert.equal(notification.closed, false)
  const pendingRead = harness.readSession('read-session', 60)
  await flushMicrotasks()
  assert.deepEqual(harness.readCalls, [['read-session', 60]])
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(notification.closed, false)
  assert.deepEqual(harness.router.navigations, [])

  readResponse.resolve({ last_read_message_id: 60 })
  await pendingRead
  await flushMicrotasks()
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(notification.closed, true)
  assert.equal(harness.closedNotifications.length, 1)
  assert.deepEqual(harness.router.navigations, [])
})

test('a failed fetch preserves the visible list and a later poll clears the error', async t => {
  let mode = 'success'
  const failure = new Error('temporary task failure')
  const stableTask = createTask('stable-session', { is_running: true })
  const harness = createHarness(t, {
    fetchActivity: () => mode === 'failure' ? failure : [stableTask]
  })

  await harness.refreshAndFlush()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['stable-session'])
  assert.equal(harness.instance.error.value, false)

  mode = 'failure'
  await harness.timers.tick()
  assert.equal(harness.instance.error.value, true)
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['stable-session'])
  assert.equal(harness.timers.pending.length, 1)

  mode = 'success'
  await harness.timers.tick()
  assert.equal(harness.instance.error.value, false)
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['stable-session'])
})

test('unmount removes listeners, stops polling, and prevents a pending notification batch', async t => {
  const batchSeen = createDeferred()
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('pending-notification', {
      completed_message_id: 70,
      completed_status: 'succeeded'
    })],
    onNotifyObserved: () => batchSeen.resolve()
  })
  const refresh = harness.instance.refresh()
  await batchSeen.promise
  harness.unmount()
  await refresh
  await flushMicrotasks()

  assert.equal(harness.notifications.length, 0)
  assert.equal(harness.window.listenerCount('storage'), 0)
  assert.equal(harness.window.listenerCount('focus'), 0)
  assert.equal(harness.timers.pending.length, 0)

  const fetchesAfterUnmount = harness.fetchCallCount()
  harness.window.dispatch('focus')
  harness.window.dispatch('storage', { key: 'token' })
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), fetchesAfterUnmount)

  const pollingHarness = createHarness(t, {
    fetchActivity: () => [createTask('polling-session', { is_running: true })]
  })
  await pollingHarness.refreshAndFlush()
  const timer = pollingHarness.timers.pending[0]
  pollingHarness.unmount()
  assert.equal(pollingHarness.timers.pending.length, 0)
  assert.equal(pollingHarness.timers.cancelled.includes(timer), true)
})

test('the root activity poll feeds a mounted session manager without a second poller', async t => {
  let activity = [
    createActivity('owned-session', {
      is_running: true,
      latest_message_id: 1,
      last_active: '2026-10-08 00:00:01'
    }),
    createActivity('admin-session', {
      is_owned: false,
      is_running: true,
      latest_message_id: 10,
      last_active: '2026-10-08 00:00:02'
    }),
    createActivity('idle-session', {
      latest_message_id: null,
      last_active: '2026-10-08 00:00:03'
    })
  ]
  const sessions = [
    createSession('owned-session', { title: 'Owned full', metadata: 'owned metadata' }),
    createSession('admin-session', { title: 'Admin full', metadata: 'admin metadata' }),
    createSession('idle-session', { title: 'Idle full', metadata: 'idle metadata' })
  ]
  const harness = createHarness(t, {
    routePath: '/memories',
    fetchActivity: () => activity,
    sessionsList: () => sessions
  })

  await harness.refreshAndFlush()
  const activityCallsBeforeManager = harness.fetchCallCount()
  const timerBeforeManager = harness.timers.pending[0]
  const manager = harness.mountSessionManager()
  await manager.instance.loadSessions()
  await flushMicrotasks()

  assert.equal(harness.fetchCallCount(), activityCallsBeforeManager)
  assert.equal(harness.sessionsListCallCount(), 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(timerBeforeManager.delay, 1500)
  assert.deepEqual(
    Array.from(manager.instance.sessions.value, session => session.session_id),
    ['owned-session', 'admin-session', 'idle-session']
  )

  activity = [
    createActivity('owned-session', {
      completed_message_id: 5,
      completed_status: 'succeeded',
      latest_message_id: 2,
      last_active: '2026-10-08 00:00:04'
    }),
    createActivity('admin-session', {
      is_owned: false,
      is_running: false,
      latest_message_id: 11,
      last_active: '2026-10-08 00:00:05',
      is_loading: true
    }),
    createActivity('idle-session', {
      latest_message_id: null,
      last_active: '2026-10-08 00:00:03'
    })
  ]
  await harness.timers.tick()
  await flushMicrotasks()

  assert.equal(harness.fetchCallCount(), activityCallsBeforeManager + 1)
  assert.equal(harness.sessionsListCallCount(), 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.timers.pending[0].delay, 1500)
  assert.deepEqual(Array.from(harness.instance.tasks.value, task => task.session_id), ['owned-session'])
  assert.equal(harness.notifications.length, 1)
  assert.equal(managerSession(manager, 'owned-session').latest_message_id, 2)
  assert.equal(managerSession(manager, 'admin-session').latest_message_id, 11)
  assert.equal(managerSession(manager, 'admin-session').is_loading, true)
  assert.equal(managerSession(manager, 'idle-session').title, 'Idle full')
  assert.deepEqual(
    Array.from(manager.instance.sessions.value, session => session.session_id).sort(),
    ['admin-session', 'idle-session', 'owned-session']
  )
})

test('disposing and rebuilding the manager leaves the root activity poller authoritative', async t => {
  let activity = [createActivity('web-session', {
    is_running: true,
    latest_message_id: 1,
    source: 'external'
  })]
  const sessions = [createSession('web-session', { title: 'Stable title', metadata: 'stable metadata' })]
  const harness = createHarness(t, {
    fetchActivity: () => activity,
    sessionsList: () => sessions
  })

  await harness.refreshAndFlush()
  const manager = harness.mountSessionManager()
  const oldSessionUpdates = []
  const oldActivityUpdates = []
  manager.instance.setSessionsUpdatedCallback(value => oldSessionUpdates.push(Array.from(value)))
  manager.instance.setSessionActivityUpdatedCallback(value => oldActivityUpdates.push(Array.from(value)))
  await manager.instance.loadSessions()
  await flushMicrotasks()
  const oldSessions = Array.from(manager.instance.sessions.value, session => ({ ...session }))
  const activityCallsBeforeDispose = harness.fetchCallCount()
  const sessionListCallsBeforeDispose = harness.sessionsListCallCount()
  manager.dispose()
  const callbacksAfterDispose = [oldSessionUpdates.length, oldActivityUpdates.length]

  await harness.setRoute('/memories')
  assert.equal(harness.timers.pending.length, 1)
  activity = [createActivity('web-session', {
    completed_message_id: 8,
    completed_status: 'succeeded',
    latest_message_id: 2,
    source: 'external'
  })]
  await harness.timers.tick()
  await flushMicrotasks()

  assert.equal(harness.fetchCallCount(), activityCallsBeforeDispose + 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.notifications.length, 1)
  assert.deepEqual([oldSessionUpdates.length, oldActivityUpdates.length], callbacksAfterDispose)
  assert.deepEqual(Array.from(manager.instance.sessions.value), oldSessions)

  const activityCallsBeforeRebuild = harness.fetchCallCount()
  const rebuilt = harness.mountSessionManager()
  await rebuilt.instance.loadSessions()
  await flushMicrotasks()
  assert.equal(harness.sessionsListCallCount(), sessionListCallsBeforeDispose + 1)
  assert.equal(harness.fetchCallCount(), activityCallsBeforeRebuild)
  assert.equal(harness.timers.pending.length, 1)
  rebuilt.dispose()
})

test('hidden and visible pages keep the same 1500ms root activity interval', async t => {
  let activityVersion = 0
  const harness = createHarness(t, {
    fetchActivity: () => [createActivity('visibility-session', {
      latest_message_id: ++activityVersion
    })]
  })

  await harness.refreshAndFlush()
  const initialFetchCount = harness.fetchCallCount()
  const initialTimer = harness.timers.pending[0]
  harness.setVisibility(true)
  assert.equal(harness.document.hidden, true)
  assert.equal(harness.document.visibilityState, 'hidden')
  assert.equal(harness.timers.pending[0], initialTimer)
  assert.equal(harness.timers.pending[0].delay, 1500)
  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), initialFetchCount + 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.timers.pending[0].delay, 1500)

  harness.setVisibility(false)
  assert.equal(harness.document.hidden, false)
  assert.equal(harness.document.visibilityState, 'visible')
  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), initialFetchCount + 2)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.timers.pending[0].delay, 1500)
  assert.equal(harness.timers.scheduled.every(timer => timer.delay === 1500), true)
})

test('shared activity sequence rejects an old root snapshot and accepts the next one', async t => {
  const oldActivity = createDeferred()
  const fullList = createDeferred()
  let activity = [createActivity('shared-session', {
    latest_message_id: 300,
    last_active: '2026-10-08 00:00:03',
    source: 'external'
  })]
  const harness = createHarness(t, {
    fetchActivity: call => call === 0 ? oldActivity.promise : activity,
    sessionsList: () => fullList.promise
  })
  const manager = harness.mountSessionManager()
  const loading = manager.instance.loadSessions()
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), 1)
  assert.equal(harness.sessionsListCallCount(), 1)

  fullList.resolve([createSession('shared-session', {
    title: 'Newer full title',
    metadata: 'newer full metadata',
    latest_message_id: 200,
    last_active: '2026-10-08 00:00:02'
  })])
  await loading
  await flushMicrotasks()
  oldActivity.resolve([createActivity('shared-session', {
    latest_message_id: 100,
    last_active: '2026-10-08 00:00:01',
    source: 'external'
  })])
  await flushMicrotasks()

  assert.equal(managerSession(manager, 'shared-session').latest_message_id, 200)
  assert.equal(managerSession(manager, 'shared-session').title, 'Newer full title')

  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(managerSession(manager, 'shared-session').latest_message_id, 300)
  assert.equal(managerSession(manager, 'shared-session').last_active, '2026-10-08 00:00:03')
})

test('a newer activity survives a pending full refresh while full metadata is retained', async t => {
  const pendingFull = createDeferred()
  let activity = [createActivity('known-session', {
    latest_message_id: 10,
    last_active: '2026-10-08 00:00:01',
    source: 'external'
  })]
  const harness = createHarness(t, {
    fetchActivity: () => activity,
    sessionsList: call => call === 1 ? pendingFull.promise : [createSession('known-session', {
      title: call === 0 ? 'Initial title' : 'Returned full title',
      metadata: call === 0 ? 'initial metadata' : 'returned full metadata',
      latest_message_id: call === 0 ? 10 : 15,
      last_active: call === 0 ? '2026-10-08 00:00:01' : '2026-10-08 00:00:02'
    })]
  })

  await harness.refreshAndFlush()
  const manager = harness.mountSessionManager()
  await manager.instance.loadSessions()
  await flushMicrotasks()
  const loading = manager.instance.loadSessions()
  await flushMicrotasks()
  assert.equal(harness.sessionsListCallCount(), 2)

  activity = [createActivity('known-session', {
    latest_message_id: 20,
    last_active: '2026-10-08 00:00:04',
    source: 'external',
    is_loading: true
  })]
  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(managerSession(manager, 'known-session').latest_message_id, 20)
  assert.equal(managerSession(manager, 'known-session').title, 'Initial title')
  assert.equal(managerSession(manager, 'known-session').metadata, 'initial metadata')

  pendingFull.resolve([createSession('known-session', {
    title: 'Returned full title',
    metadata: 'returned full metadata',
    latest_message_id: 15,
    last_active: '2026-10-08 00:00:02',
    source: 'external'
  })])
  await loading
  await flushMicrotasks()
  assert.equal(managerSession(manager, 'known-session').latest_message_id, 20)
  assert.equal(managerSession(manager, 'known-session').last_active, '2026-10-08 00:00:04')
  assert.equal(managerSession(manager, 'known-session').is_loading, true)
  assert.equal(managerSession(manager, 'known-session').title, 'Returned full title')
  assert.equal(managerSession(manager, 'known-session').metadata, 'returned full metadata')
})

test('selected web activity running-to-idle forwards activity and refreshed full snapshots', async t => {
  let activity = [createActivity('web-selected', {
    is_running: true,
    is_reply_running: true,
    latest_message_id: 1,
    source: 'http'
  })]
  const harness = createHarness(t, {
    fetchActivity: () => activity,
    sessionsList: call => [createSession('web-selected', {
      title: `full ${call}`,
      metadata: `metadata ${call}`,
      source: 'http',
      is_reply_running: call < 2
    })]
  })

  await harness.refreshAndFlush()
  const manager = harness.mountSessionManager()
  const activityUpdates = []
  const sessionUpdates = []
  manager.instance.setSessionActivityUpdatedCallback(value => activityUpdates.push(Array.from(value)))
  manager.instance.setSessionsUpdatedCallback(value => sessionUpdates.push(Array.from(value)))
  await manager.instance.loadSessions()
  manager.instance.selectSession({ session_id: 'web-selected' }, null, false, false)
  activity = [createActivity('web-selected', {
    is_running: true,
    is_reply_running: true,
    latest_message_id: 2,
    source: 'http'
  })]
  await harness.timers.tick()
  await flushMicrotasks()
  const activityUpdatesAfterRunning = activityUpdates.length
  assert.equal(harness.sessionsListCallCount(), 2)
  assert.equal(activityUpdatesAfterRunning > 0, true)

  activity = [createActivity('web-selected', {
    is_running: false,
    is_reply_running: false,
    latest_message_id: 3,
    source: 'http'
  })]
  await harness.timers.tick()
  await flushMicrotasks()

  assert.equal(harness.sessionsListCallCount(), 3)
  assert.equal(activityUpdates.length > activityUpdatesAfterRunning, true)
  assert.equal(sessionUpdates.length > 0, true)
  const latestSessionUpdate = sessionUpdates[sessionUpdates.length - 1]
  assert.equal(latestSessionUpdate.find(session => session.session_id === 'web-selected').is_reply_running, false)
})
