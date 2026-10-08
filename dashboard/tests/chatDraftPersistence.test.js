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
  ref,
  shallowReactive,
  shallowRef,
  readonly,
  watch
} from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import * as identity from '../src/utils/assistantResponseIdentity.js'
import * as profileOptions from '../src/utils/profileOptions.js'
import * as todoPresentation from '../src/utils/todoPresentation.js'
import * as toolOutputVisibility from '../src/utils/toolOutputVisibility.js'
import * as errorMessage from '../src/utils/errorMessage.js'
import * as contextSummaryTracker from '../src/composables/chat/contextSummaryTracker.js'
import * as historyMergeTracker from '../src/composables/chat/historyMergeTracker.js'
import * as historyIncrementalSync from '../src/composables/chat/historyIncrementalSync.js'
import * as streamResume from '../src/composables/chat/streamResume.js'
import * as sessionActivity from '../src/composables/chat/sessionActivity.js'
import * as workLifecycleTracker from '../src/composables/chat/workLifecycleTracker.js'
import * as sessionAgentSettings from '../src/composables/chat/sessionAgentSettings.js'
import * as replyControl from '../src/composables/chat/replyControl.js'
import * as auditConfirmationState from '../src/composables/chat/auditConfirmationState.js'
import * as httpHistorySync from '../src/composables/chat/httpHistorySync.js'
import * as httpReplyPolling from '../src/composables/chat/httpReplyPolling.js'
import * as sessionTransportMode from '../src/composables/chat/sessionTransportMode.js'
import * as transportNotifications from '../src/composables/chat/transportNotifications.js'
import * as llmRequestMetadata from '../src/composables/chat/llmRequestMetadata.js'
import * as chatContentReveal from '../src/utils/chatContentReveal.js'
import * as historyPagination from '../src/composables/chat/historyPagination.js'
import * as chatTransportRuntime from '../src/composables/chat/chatTransportRuntime.js'
import { useChatState } from '../src/composables/chat/useChatState.js'
import { CHAT_DRAFT_STORAGE_PREFIX, useChatDrafts } from '../src/composables/chat/useChatDrafts.js'
import { createSessionTaskController } from '../src/composables/chat/sessionTasks.js'
import { PAGE_SIZE, SESSION_MAX_TURNS_UPPER_BOUND } from '../src/constants/index.js'

const source = relativePath => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const stripImports = sourceText => {
  const keptLines = []
  let inImport = false

  for (const line of sourceText.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const stripExports = sourceText => sourceText
  .replace(/export\s*\{[\s\S]*?\}\s*;?/g, '')
  .replace(/\bexport\s+default\s+/g, '')
  .replace(/\bexport\s+(?=(?:async\s+)?function\b|const\b|let\b|var\b|class\b)/g, '')

const quietConsole = {
  log: () => {},
  warn: () => {},
  error: () => {},
  info: () => {}
}

const defaultVmGlobals = {
  console: quietConsole,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: callback => {
    callback()
    return 1
  },
  cancelAnimationFrame: () => {}
}

const loadVmModule = (sourceText, dependencies, exportNames, filename) => {
  const body = stripExports(stripImports(sourceText))
  const exports = exportNames.map(name => `${name}: ${name}`).join(',\n')
  const context = vm.createContext({
    ...defaultVmGlobals,
    ...dependencies
  })
  vm.runInContext(
    `${body}\nglobalThis.__module = { ${exports} }\n`,
    context,
    { filename }
  )
  return context.__module
}

const loadPath = (relativePath, dependencies, exportNames) => loadVmModule(
  source(relativePath),
  dependencies,
  exportNames,
  relativePath
)

const translations = {
  'chat.default_title': 'New chat',
  'chat.send_failed': 'send failed',
  'chat.external_session_read_only': 'external session is read-only',
  'chat.guidance_created': 'guidance created',
  'chat.guidance_create_failed': 'guidance failed',
  'chat.task_session_unavailable': 'task session unavailable',
  'chat.task_center_title': 'Session tasks',
  'chat.task_failure_notification_title': 'Session task failed',
  'chat.task_notification_title': 'Session task completed',
  'chat.task_notification_message': 'New result: {title}',
  'chat.task_summary': '{running} running, {unread} unread',
  'chat.session_source_http': 'HTTP',
  'chat.session_source_ws': 'WebSocket',
  'chat.input_placeholder': 'Type a message',
  'chat.guidance_placeholder': 'Type guidance',
  'chat.default_profile_suffix': ' (default)',
  'chat.inherited_profile': 'Inherited',
  'chat.profile_setting_saved': 'Saved',
  'chat.setting_failed': 'Setting failed',
  'chat.load_profiles_failed': 'Profiles failed',
  'chat.load_sessions_failed': 'Sessions failed',
  'chat.load_history_failed': 'History failed',
  'chat.delete_success': 'Deleted',
  'common.delete_named_confirm': 'Delete {name}?',
  'common.warning': 'Warning',
  'common.confirm': 'Confirm',
  'common.cancel': 'Cancel',
  'common.delete_success': 'Deleted',
  'common.delete_failed': 'Delete failed',
  'common.action_failed': 'Action failed'
}

const translate = (key, values = {}) => String(translations[key] || key)
  .replace(/\{(\w+)\}/g, (_match, name) => String(values[name] ?? ''))

const i18n = {
  global: {
    t: translate,
    locale: ref('en')
  }
}

const utils = loadPath(
  '../src/utils/index.js',
  { i18n, ...identity },
  [
    'findAssistantResponseReplacementIndex',
    'getMessageDbId',
    'isAssistantResponse',
    'isPlainAssistantResponse',
    'mergeAssistantResponse',
    'mergeAssistantResponseIntoList',
    'normalizeMessageContent',
    'getToolResultCallId',
    'findMessageReplacementIndex',
    'mergeRemoteMessage',
    'mergeRemoteMessageIntoList',
    'getMessageDedupeKeys',
    'formatTimestamp',
    'getShortContent',
    'isToolCall',
    'getToolCallContent',
    'getToolCalls',
    'getToolCallName',
    'getToolCallArguments',
    'getToolName',
    'getToolArguments',
    'isToolResult',
    'getToolResultName',
    'getToolResultContent',
    'getMessageTimestamp',
    'debounce',
    'formatTime'
  ]
)

const useSessionManagerSource = source('../src/composables/chat/useSessionManager.js')
const useChatSessionSource = source('../src/composables/chat/useChatSession.js')
const useChatTransportSource = source('../src/composables/chat/useChatTransport.js')
const useDeleteConfirmSource = source('../src/composables/useDeleteConfirm.js')
const useSessionTasksSource = source('../src/composables/chat/useSessionTasks.js')
const chatViewSource = source('../src/views/ChatView.vue')
const chatViewScriptMatch = chatViewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!chatViewScriptMatch) throw new Error('ChatView.vue script setup was not found')
const chatViewScript = chatViewScriptMatch[1]

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flush = async () => {
  for (let index = 0; index < 10; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
  await new Promise(resolve => setImmediate(resolve))
  for (let index = 0; index < 10; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
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
    assert.ok(handle, 'expected a manually scheduled timer')
    return handle.callback()
  }

  return { pending, scheduled, cancelled, schedule, cancel, tick }
}

const createStorage = initialValues => {
  const values = new Map(Object.entries(initialValues || {}))
  return {
    values,
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key)
  }
}

const draftKey = (uid, sessionId) => (
  `${CHAT_DRAFT_STORAGE_PREFIX}${JSON.stringify([uid, sessionId])}`
)

const cloneMessage = message => ({ ...message })

const createSession = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  title: sessionId,
  last_active: '2026-10-08 00:00:00',
  source: 'http',
  uid: 'user-a',
  is_loading: false,
  is_reply_running: false,
  messages: [],
  ...overrides
})

const cloneSession = session => ({
  ...session,
  messages: Array.isArray(session.messages) ? session.messages.map(cloneMessage) : []
})

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

const resolvePlan = (plan, context) => (
  typeof plan === 'function' ? plan(context) : plan
)

const createElMessage = messages => {
  const message = value => messages.push({ type: 'message', value })
  message.warning = value => messages.push({ type: 'warning', value })
  message.error = value => messages.push({ type: 'error', value })
  message.success = value => messages.push({ type: 'success', value })
  return message
}

const createFakeNotification = notifications => options => {
  const entry = { ...options, closed: false }
  let renderedMessage = options.message
  Object.defineProperty(entry, 'message', {
    configurable: true,
    enumerable: true,
    get: () => {
      renderedMessage = typeof options.message === 'function' ? options.message() : options.message
      return renderedMessage
    }
  })
  entry.handle = {
    close: () => {
      entry.closed = true
    }
  }
  notifications.push(entry)
  return entry.handle
}

const createMessageProcessor = () => ({
  processAiResponse(messagesRef, response, _thinkingId, requestId) {
    const choice = response?.choices?.[0]
    const content = choice?.message?.content ?? response?.content
    if (content === undefined || content === null) return
    messagesRef.value.push({
      role: 'assistant',
      content,
      request_id: requestId,
      response_id: response?.response_id,
      work_id: response?.work_id
    })
  },
  processStreamReasoning: () => {},
  finalizeStreamReasoning: () => {},
  processStreamContent: () => {},
  processStreamToolStart: () => {},
  processStreamToolEnd: () => {},
  processStreamError: () => true
})

const createUseSessionTasks = ({ chatApi, storage, route, router, provided, notifications, timers, onBeforeUnmount }) => {
  const controllerFactory = options => {
    return createSessionTaskController({
      ...options,
      schedule: timers.schedule,
      cancel: timers.cancel
    })
  }

  return loadVmModule(
    useSessionTasksSource,
    {
      h,
      onBeforeUnmount,
      provide: (key, value) => provided.set(key, value),
      readonly,
      ref,
      shallowReactive,
      shallowRef,
      watch,
      useRoute: () => route,
      useRouter: () => router,
      useI18n: () => ({ t: translate }),
      ElNotification: createFakeNotification(notifications),
      chatApi,
      createSessionTaskController: controllerFactory,
      localStorage: storage,
      window: {
        addEventListener: () => {},
        removeEventListener: () => {}
      },
      document: { hidden: false, visibilityState: 'visible' },
      console: quietConsole
    },
    ['SESSION_TASKS_KEY', 'SESSION_ACTIVITY_KEY', 'useSessionTasks'],
    'useSessionTasks.js'
  )
}

const createHarness = async (t, options = {}) => {
  const timers = createManualTimerQueue()
  const storage = options.storage || createStorage({ token: options.token ?? 'user-a' })
  if (options.token === null) storage.removeItem('token')
  const messages = []
  const notifications = []
  const confirmations = []
  const provided = new Map()
  const sessions = (options.sessions || [createSession('A'), createSession('B')]).map(cloneSession)
  const apiState = {
    sessions,
    listRequests: [],
    historyRequests: [],
    completionRequests: [],
    profileRequests: [],
    guidanceRequests: [],
    activityRequests: [],
    deleteCalls: [],
    guidanceCalls: [],
    deletePlan: null
  }

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: {} },
      { path: '/:pathMatch(.*)*', component: {} }
    ]
  })
  await router.push(options.initialRoute || { path: '/', query: {} })
  await router.isReady()
  const route = {}
  for (const key of ['path', 'query', 'hash', 'fullPath']) {
    Object.defineProperty(route, key, {
      enumerable: true,
      get: () => router.currentRoute.value[key]
    })
  }

  const makeListResponse = (planned, request) => Promise.resolve(planned).then(value => {
    if (value instanceof Error) throw value
    const nextSessions = Array.isArray(value) ? value : sessions
    return nextSessions.map(cloneSession)
  })

  const chatApi = {
    sessionsList: () => {
      const index = apiState.listRequests.length
      const pending = createDeferred()
      const request = { index, pending, promise: null }
      const planned = resolvePlan(options.sessionsList, { index, pending, sessions })
      request.promise = makeListResponse(planned === undefined ? sessions : planned, request)
        .then(nextSessions => ({ data: { data: nextSessions } }))
      apiState.listRequests.push(request)
      return request.promise
    },
    sessionsHistory: (sessionId, page, size, params) => {
      const index = apiState.historyRequests.length
      const pending = createDeferred()
      const request = { index, sessionId, page, size, params, pending, promise: null }
      const defaultHistory = sessions.find(session => session.session_id === sessionId)?.messages || []
      const planned = resolvePlan(options.sessionsHistory, {
        index,
        sessionId,
        page,
        size,
        params,
        pending,
        sessions
      })
      request.promise = Promise.resolve(planned === undefined ? defaultHistory : planned).then(value => {
        if (value instanceof Error) throw value
        return { data: { data: Array.isArray(value) ? value.map(cloneMessage) : [] } }
      })
      apiState.historyRequests.push(request)
      return request.promise
    },
    sessionTodo: async () => ({ data: { data: null } }),
    backgroundTaskPendingActivity: async () => ({ data: { data: { has_pending_activity: false } } }),
    updateSessionSetting: async (sessionId, settings) => {
      const session = sessions.find(item => item.session_id === sessionId)
      if (session) Object.assign(session, settings)
      return { data: { data: {} } }
    },
    generateTitle: async () => {
      throw new Error('title generation is not part of this harness')
    },
    deleteSession: async sessionId => {
      apiState.deleteCalls.push(sessionId)
      const planned = resolvePlan(
        apiState.deletePlan === null ? options.deleteSession : apiState.deletePlan,
        { sessionId, sessions }
      )
      if (planned instanceof Error) throw planned
      if (planned && typeof planned.then === 'function') {
        const value = await planned
        if (value instanceof Error) throw value
      }
      const index = sessions.findIndex(session => session.session_id === sessionId)
      if (index !== -1) sessions.splice(index, 1)
      return { data: { data: {} } }
    },
    stopSession: async () => ({ data: { data: {} } }),
    createGuidance: (payload) => {
      apiState.guidanceCalls.push(payload)
      const index = apiState.guidanceRequests.length
      const pending = createDeferred()
      const request = { index, payload, pending, promise: null }
      const planned = resolvePlan(options.createGuidance, { index, payload, pending })
      request.promise = Promise.resolve(planned === undefined
        ? { id: `guidance-${index}`, role: 'user', content: payload.content }
        : planned).then(value => {
        if (value instanceof Error) throw value
        return { data: { data: value } }
      })
      apiState.guidanceRequests.push(request)
      return request.promise
    },
    completions: payload => {
      const index = apiState.completionRequests.length
      const pending = createDeferred()
      const request = { index, payload, pending, promise: null }
      const planned = Array.isArray(options.httpResponses)
        ? options.httpResponses[Math.min(index, options.httpResponses.length - 1)]
        : resolvePlan(options.httpResponses, { index, payload, pending })
      request.promise = Promise.resolve(planned === undefined
        ? {
            session_id: payload.session_id,
            work_id: `work-${index + 1}`,
            submission_status: 'accepted'
          }
        : planned).then(value => {
        if (value instanceof Error) throw value
        return { data: { data: value } }
      })
      apiState.completionRequests.push(request)
      return request.promise
    },
    replyWorkStatus: async workId => ({
      data: {
        data: {
          work_id: workId,
          status: 'succeeded',
          session_id: route.query.session_id,
          response: null
        }
      }
    }),
    sessionsActivity: () => {
      const index = apiState.activityRequests.length
      const planned = Array.isArray(options.activities)
        ? options.activities[Math.min(index, options.activities.length - 1)]
        : resolvePlan(options.activities, { index })
      const promise = Promise.resolve(planned === undefined ? [] : planned).then(value => {
        if (value instanceof Error) throw value
        return { data: { data: Array.isArray(value) ? value : [] } }
      })
      apiState.activityRequests.push({ index, promise })
      return promise
    },
    markSessionRead: async (_sessionId, messageId) => ({
      data: { data: { last_read_message_id: messageId } }
    })
  }

  const profileApi = {
    list: () => {
      const index = apiState.profileRequests.length
      const pending = createDeferred()
      const request = { index, pending, promise: null }
      const planned = resolvePlan(options.profiles, { index, pending })
      request.promise = Promise.resolve(planned === undefined
        ? { items: [], currentUid: options.profileUid || 'user-a' }
        : planned).then(value => {
        if (value instanceof Error) throw value
        return {
          data: {
            data: {
              items: value.items || [],
              meta: { current_uid: value.currentUid ?? options.profileUid ?? 'user-a' }
            }
          }
        }
      })
      apiState.profileRequests.push(request)
      return request.promise
    }
  }

  const wsManager = {
    isConnected: ref(false),
    sent: [],
    listener: null,
    onMessage(listener) {
      this.listener = listener
    },
    async connect() {
      this.isConnected.value = true
    },
    sendMessage(data) {
      this.sent.push(data)
      return true
    },
    disconnect() {
      this.isConnected.value = false
    },
    dispose() {
      this.disconnect()
      this.listener = null
    }
  }

  const ElMessage = createElMessage(messages)
  const confirmationApi = {
    confirm: () => {
      const planned = confirmations.length > 0 ? confirmations.shift() : true
      if (planned === 'cancel') return Promise.reject('cancel')
      if (planned instanceof Error) return Promise.reject(planned)
      return Promise.resolve(planned)
    }
  }
  const useDeleteConfirm = loadVmModule(
    useDeleteConfirmSource,
    { ElMessageBox: confirmationApi, ElMessage, i18n },
    ['useDeleteConfirm'],
    'useDeleteConfirm.js'
  ).useDeleteConfirm

  const useSessionManager = loadVmModule(
    useSessionManagerSource,
    {
      inject: key => provided.get(key) ?? null,
      onScopeDispose,
      ref,
      watch,
      ElMessage,
      chatApi,
      useDeleteConfirm: (...args) => useDeleteConfirm(...args),
      PAGE_SIZE,
      i18n,
      ...historyPagination,
      ...sessionActivity,
      SESSION_ACTIVITY_KEY: 'sessionActivity'
    },
    ['useSessionManager'],
    'useSessionManager.js'
  ).useSessionManager

  const useChatTransport = loadVmModule(
    useChatTransportSource,
    {
      ElMessage,
      chatApi,
      useWebSocket: () => wsManager,
      i18n,
      createChatTransport: optionsForTransport => chatTransportRuntime.createChatTransport(optionsForTransport),
      localStorage: storage
    },
    ['useChatTransport'],
    'useChatTransport.js'
  ).useChatTransport

  const createHttpHistorySync = optionsForSync => httpHistorySync.createHttpHistorySyncController({
    ...optionsForSync,
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  const useChatSession = loadVmModule(
    useChatSessionSource,
    {
      computed,
      nextTick,
      onScopeDispose,
      ref,
      watch,
      ElMessage,
      useChatState,
      useChatDrafts: optionsForDrafts => useChatDrafts({
        ...optionsForDrafts,
        storage
      }),
      useSessionManager,
      useChatTransport,
      resolveAssistantDisplayContent: (content, refusal) => content ?? refusal ?? '',
      useMessageProcessor: () => createMessageProcessor(),
      ...contextSummaryTracker,
      ...historyMergeTracker,
      ...historyIncrementalSync,
      ...streamResume,
      withSessionActivity: sessionActivity.withSessionActivity,
      ...workLifecycleTracker,
      ...sessionAgentSettings,
      ...replyControl,
      ...auditConfirmationState,
      createHttpHistorySyncController: createHttpHistorySync,
      ...httpReplyPolling,
      ...sessionTransportMode,
      ...transportNotifications,
      ...llmRequestMetadata,
      ...utils,
      ...profileOptions,
      ...todoPresentation,
      ...toolOutputVisibility,
      ...chatContentReveal,
      chatApi,
      i18n,
      ...errorMessage,
      localStorage: storage
    },
    ['useChatSession'],
    'useChatSession.js'
  ).useChatSession

  const onBeforeUnmountCallbacks = []
  if (options.withTasks) {
    const taskModule = createUseSessionTasks({
      chatApi,
      storage,
      route,
      router,
      provided,
      notifications,
      timers,
      onBeforeUnmount: callback => onBeforeUnmountCallbacks.push(callback)
    })
    provided.set('sessionTasksModule', taskModule)
    const taskScope = effectScope()
    taskScope.run(() => {
      const taskInstance = taskModule.useSessionTasks()
      provided.set('sessionTasksInstance', taskInstance)
    })
    provided.set('sessionTasksScope', taskScope)
  }

  const mountedCallbacks = []
  const unmountedCallbacks = []
  const viewContext = vm.createContext({
    ref,
    computed,
    nextTick,
    watch,
    inject: key => provided.get(key) ?? null,
    onMounted: callback => mountedCallbacks.push(callback),
    onUnmounted: callback => unmountedCallbacks.push(callback),
    ElMessage,
    ClickOutside: {},
    ChatLineSquare: {},
    Delete: {},
    InfoFilled: {},
    Plus: {},
    Refresh: {},
    UploadFilled: {},
    ArrowDown: {},
    useI18n: () => ({ t: translate }),
    useRoute: () => route,
    useRouter: () => router,
    ChatMessageList: {},
    SessionTodoPanel: {},
    HelpTooltip: {},
    useChatSession,
    SESSION_TASKS_KEY: 'sessionTasks',
    createSessionAgentSettingUpdater: sessionAgentSettings.createSessionAgentSettingUpdater,
    fileApi: { upload: async () => ({ data: {} }) },
    chatApi,
    profileApi,
    SESSION_MAX_TURNS_UPPER_BOUND,
    ...profileOptions,
    ...chatContentReveal,
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    localStorage: storage,
    window: {
      clipboardData: null,
      addEventListener: () => {},
      removeEventListener: () => {}
    },
    console: quietConsole
  })

  const scope = effectScope()
  let module
  scope.run(() => {
    vm.runInContext(
      `${stripImports(chatViewScript)}\nglobalThis.__module = {\n        chat,\n        handleSelectSession,\n        handleCreateNewSession,\n        handleDeleteSession,\n        send,\n        loadSessions,\n        currentUid,\n        currentSessionId,\n        inputMsg,\n        messages,\n        sessions,\n        isCurrentSessionReadOnly,\n        sessionEngaged,\n        deferredContentSessionId\n      }`,
      viewContext,
      { filename: 'ChatView.vue?script-setup' }
    )
    module = viewContext.__module
  })

  for (const callback of mountedCallbacks) callback()

  const harness = {
    apiState,
    chat: module.chat,
    module,
    router,
    route,
    storage,
    timers,
    wsManager,
    messages,
    notifications,
    confirmations,
    sessions,
    task: provided.get('sessionTasksInstance'),
    url: () => {
      const current = router.currentRoute.value
      return {
        path: current.path,
        query: { ...current.query },
        hash: current.hash,
        fullPath: current.fullPath
      }
    },
    draft: sessionId => storage.getItem(draftKey('user-a', sessionId)),
    async flush() {
      await flush()
    },
    async replace(location) {
      await router.replace(location)
      await flush()
    },
    receiveWs(data) {
      wsManager.listener?.(data)
    },
    resolveProfile(value = { items: [], currentUid: 'user-a' }) {
      const request = apiState.profileRequests[apiState.profileRequests.length - 1]
      request?.pending.resolve(value)
    },
    enqueueConfirmation(value) {
      confirmations.push(value)
    },
    notificationButtons(notification) {
      const message = notification.message
      return Array.isArray(message?.children)
        ? message.children.filter(child => child?.type === 'button')
        : []
    },
    unmount() {
      if (harness.unmounted) return
      harness.unmounted = true
      for (const callback of onBeforeUnmountCallbacks.splice(0)) callback()
      for (const callback of unmountedCallbacks.splice(0)) callback()
      provided.get('sessionTasksScope')?.stop()
      scope.stop()
    },
    unmounted: false
  }

  t.after(() => harness.unmount())
  return harness
}

test('real session navigation persists per-session drafts across route consumption and rebuild', async t => {
  const storage = createStorage({
    token: 'user-a',
    [draftKey('user-a', 'A')]: 'route task draft'
  })
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    storage,
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A', task_open: 'completed-1', keep: 'yes' }, hash: '#chat' }
  })
  await harness.flush()

  assert.equal(harness.module.currentSessionId.value, 'A')
  assert.equal(harness.module.inputMsg.value, 'route task draft')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })

  harness.module.inputMsg.value = 'draft A'
  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  harness.module.inputMsg.value = 'draft B'
  await harness.flush()

  assert.equal(harness.draft('A'), 'draft A')
  assert.equal(harness.draft('B'), 'draft B')

  harness.module.handleSelectSession(sessions[0])
  await harness.flush()
  assert.equal(harness.module.inputMsg.value, 'draft A')

  const refreshed = await createHarness(t, {
    storage,
    sessions,
    initialRoute: harness.url()
  })
  await refreshed.flush()

  assert.equal(refreshed.module.currentSessionId.value, 'A')
  assert.equal(refreshed.module.inputMsg.value, 'draft A')
  assert.deepEqual(refreshed.url().query, { session_id: 'A', keep: 'yes' })

  refreshed.module.handleCreateNewSession()
  await refreshed.flush()
  refreshed.module.inputMsg.value = 'welcome draft'
  await refreshed.flush()
  const welcomeRefresh = await createHarness(t, {
    storage,
    sessions,
    initialRoute: refreshed.url()
  })
  await welcomeRefresh.flush()
  assert.equal(welcomeRefresh.module.currentSessionId.value, null)
  assert.equal(welcomeRefresh.module.inputMsg.value, 'welcome draft')
})

test('task notifications and openSession use real router selection without clearing repeated-session drafts', async t => {
  const storage = createStorage({
    token: 'user-a',
    [draftKey('user-a', 'A')]: 'task draft A',
    [draftKey('user-a', 'B')]: 'task draft B'
  })
  const harness = await createHarness(t, {
    storage,
    withTasks: true,
    sessions: [createSession('A'), createSession('B')],
    initialRoute: { path: '/profiles', query: { keep: 'yes' } },
    activities: [
      [createTask('A', { completed_message_id: 7, completed_status: 'succeeded' })]
    ]
  })
  await harness.flush()

  assert.equal(harness.notifications.length, 1)
  const [button] = harness.notificationButtons(harness.notifications[0])
  button.props.onClick({ stopPropagation: () => {} })
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'A')
  assert.equal(harness.module.inputMsg.value, 'task draft A')
  assert.equal(harness.url().path, '/')
  assert.equal(harness.url().query.task_open, undefined)

  harness.module.handleCreateNewSession()
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, null)

  harness.task.openSession('B')
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'task draft B')

  harness.task.openSession('B')
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'task draft B')
})

test('HTTP and WebSocket sends clear only the submitted draft, migrate waiting text, and never revive sent text', async t => {
  const httpFirst = createDeferred()
  const httpHarness = await createHarness(t, {
    sessions: [],
    httpResponses: [
      httpFirst.promise,
      { session_id: 'HTTP-1', work_id: 'http-work-1', submission_status: 'accepted' }
    ]
  })
  await httpHarness.flush()
  await httpHarness.chat.setTransportMode('http')

  httpHarness.module.inputMsg.value = 'sent over HTTP'
  const httpSend = httpHarness.module.send()
  await httpHarness.flush()
  assert.equal(httpHarness.module.inputMsg.value, '')
  httpHarness.module.inputMsg.value = 'typed while ID waits'
  httpFirst.resolve({
    choices: [{ finish_reason: 'new_session', message: { content: 'HTTP-1' } }]
  })
  await httpSend
  await httpHarness.flush()

  assert.equal(httpHarness.module.currentSessionId.value, 'HTTP-1')
  assert.equal(httpHarness.module.inputMsg.value, 'typed while ID waits')
  assert.equal(httpHarness.draft('HTTP-1'), 'typed while ID waits')
  assert.equal(httpHarness.storage.getItem(draftKey('user-a', null)), null)
  assert.equal(httpHarness.module.messages.value.some(message => message.content === 'sent over HTTP'), true)

  const wsHarness = await createHarness(t, { sessions: [] })
  await wsHarness.flush()
  wsHarness.module.inputMsg.value = 'sent over WebSocket'
  const wsSend = wsHarness.module.send()
  await wsHarness.flush()
  const wsChat = wsHarness.wsManager.sent.findLast(item => item.type === 'chat')
  assert.ok(wsChat)
  assert.equal(wsHarness.module.inputMsg.value, '')
  wsHarness.module.inputMsg.value = 'typed while WS ID waits'
  wsHarness.receiveWs({
    type: 'session_id',
    session_id: 'WS-1',
    request_id: wsChat.request_id
  })
  await wsHarness.flush()
  wsHarness.receiveWs({
    type: 'input_accepted',
    session_id: 'WS-1',
    request_id: wsChat.request_id
  })
  await wsSend
  await wsHarness.flush()

  assert.equal(wsHarness.module.currentSessionId.value, 'WS-1')
  assert.equal(wsHarness.module.inputMsg.value, 'typed while WS ID waits')
  assert.equal(wsHarness.draft('WS-1'), 'typed while WS ID waits')
  assert.equal(wsHarness.storage.getItem(draftKey('user-a', null)), null)
  assert.equal(wsHarness.module.messages.value.some(message => message.content === 'sent over WebSocket'), true)
})

test('HTTP and WebSocket in-flight queued sends remove their own draft and preserve another session', async t => {
  const httpHarness = await createHarness(t, {
    sessions: [createSession('A', { source: 'http' }), createSession('B', { source: 'http' })],
    httpResponses: [
      { session_id: 'A', work_id: 'http-1', submission_status: 'accepted' },
      { session_id: 'A', work_id: 'http-2', submission_status: 'accepted' }
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await httpHarness.flush()
  httpHarness.module.handleSelectSession(httpHarness.sessions[1])
  await httpHarness.flush()
  httpHarness.module.inputMsg.value = 'keep B'
  await httpHarness.flush()
  httpHarness.module.handleSelectSession(httpHarness.sessions[0])
  await httpHarness.flush()
  httpHarness.module.inputMsg.value = 'first A'
  await httpHarness.module.send()
  httpHarness.chat.loading.value = true
  httpHarness.module.inputMsg.value = 'queued A'
  await httpHarness.module.send()
  await httpHarness.flush()

  assert.equal(httpHarness.draft('A'), null)
  assert.equal(httpHarness.draft('B'), 'keep B')

  const wsHarness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws' }), createSession('B', { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await wsHarness.flush()
  wsHarness.module.handleSelectSession(wsHarness.sessions[1])
  await wsHarness.flush()
  wsHarness.module.inputMsg.value = 'keep WS B'
  await wsHarness.flush()
  wsHarness.module.handleSelectSession(wsHarness.sessions[0])
  await wsHarness.flush()
  wsHarness.module.inputMsg.value = 'first WS A'
  const firstSend = wsHarness.module.send()
  await wsHarness.flush()
  const firstChat = wsHarness.wsManager.sent.findLast(item => item.type === 'chat')
  wsHarness.receiveWs({ type: 'input_accepted', request_id: firstChat.request_id, session_id: 'A' })
  await firstSend
  wsHarness.chat.loading.value = true
  wsHarness.module.inputMsg.value = 'queued WS A'
  await wsHarness.module.send()
  await wsHarness.flush()
  const secondChat = wsHarness.wsManager.sent.findLast(item => item.type === 'chat')
  wsHarness.receiveWs({ type: 'input_accepted', request_id: secondChat.request_id, session_id: 'A' })
  await wsHarness.flush()

  assert.equal(wsHarness.draft('A'), null)
  assert.equal(wsHarness.draft('B'), 'keep WS B')
})

test('read-only guidance never persists or restores and a late response cannot replace a B draft', async t => {
  const guidance = createDeferred()
  const storage = createStorage({ token: 'user-a' })
  const harness = await createHarness(t, {
    storage,
    sessions: [
      createSession('external', { source: 'external', uid: 'owner-b' }),
      createSession('B', { source: 'http' })
    ],
    createGuidance: () => guidance.promise,
    initialRoute: { path: '/', query: { session_id: 'external' } }
  })
  await harness.flush()
  assert.equal(harness.module.isCurrentSessionReadOnly.value, true)
  assert.equal(harness.module.inputMsg.value, '')

  harness.module.inputMsg.value = 'external guidance'
  await harness.flush()
  assert.equal(harness.draft('external'), null)
  const guidanceSend = harness.module.send()
  await harness.flush()

  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  harness.module.inputMsg.value = 'local B draft'
  await harness.flush()
  guidance.resolve({ id: 11, role: 'user', content: 'external guidance' })
  await guidanceSend
  await harness.flush()

  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'local B draft')
  assert.equal(harness.draft('external'), null)
  assert.equal(harness.draft('B'), 'local B draft')
})

test('late profile identity preserves fresh typing and a clear instead of restoring stale null-session text', async t => {
  const staleStorage = createStorage({
    token: 'user-a',
    [draftKey('user-a', null)]: 'stale draft'
  })
  const staleProfile = createDeferred()
  const staleHarness = await createHarness(t, {
    storage: staleStorage,
    profiles: () => staleProfile.promise
  })
  staleHarness.module.inputMsg.value = 'fresh draft'
  await staleHarness.flush()
  staleProfile.resolve({ items: [], currentUid: 'user-a' })
  await staleHarness.flush()
  assert.equal(staleHarness.module.inputMsg.value, 'fresh draft')
  assert.equal(staleHarness.storage.getItem(draftKey('user-a', null)), 'fresh draft')

  const clearedStorage = createStorage({
    token: 'user-a',
    [draftKey('user-a', null)]: 'stale draft'
  })
  const clearedProfile = createDeferred()
  const clearedHarness = await createHarness(t, {
    storage: clearedStorage,
    profiles: () => clearedProfile.promise
  })
  clearedHarness.module.inputMsg.value = 'draft then clear'
  clearedHarness.module.inputMsg.value = ''
  clearedProfile.resolve({ items: [], currentUid: 'user-a' })
  await clearedHarness.flush()
  assert.equal(clearedHarness.module.inputMsg.value, '')
  assert.equal(clearedHarness.storage.getItem(draftKey('user-a', null)), null)
})

test('real delete confirmation only removes a draft after successful deletion', async t => {
  const storage = createStorage({ token: 'user-a' })
  const harness = await createHarness(t, {
    storage,
    sessions: [createSession('A')],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await harness.flush()
  harness.module.inputMsg.value = 'draft to delete'
  await harness.flush()
  assert.equal(harness.draft('A'), 'draft to delete')

  harness.enqueueConfirmation('cancel')
  assert.equal(await harness.module.handleDeleteSession('A', 'A'), false)
  assert.equal(harness.draft('A'), 'draft to delete')

  harness.enqueueConfirmation(true)
  const failure = new Error('delete failed')
  harness.apiState.deletePlan = failure
  assert.equal(await harness.module.handleDeleteSession('A', 'A'), false)
  assert.equal(harness.draft('A'), 'draft to delete')

  harness.apiState.deletePlan = null
  harness.enqueueConfirmation(true)
  assert.equal(await harness.module.handleDeleteSession('A', 'A'), true)
  await harness.flush()
  assert.equal(harness.draft('A'), null)
  assert.equal(harness.module.currentSessionId.value, null)
})

test('invalid, missing, and stale list/history work cannot overwrite the active draft', async t => {
  const staleList = createDeferred()
  const staleHistory = createDeferred()
  let delayNextList = false
  const harness = await createHarness(t, {
    sessions: [createSession('A'), createSession('B')],
    sessionsList: () => {
      if (!delayNextList) return undefined
      delayNextList = false
      return staleList.promise
    },
    sessionsHistory: ({ sessionId }) => sessionId === 'A' ? staleHistory.promise : undefined
  })
  await harness.flush()

  harness.module.handleSelectSession(harness.sessions[0])
  await harness.flush()
  harness.module.inputMsg.value = 'A draft'
  await harness.flush()
  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  harness.module.inputMsg.value = 'B draft'
  await harness.flush()

  delayNextList = true
  await harness.replace({ path: '/', query: { session_id: 'missing', task_open: 'late' } })
  assert.equal(harness.module.currentSessionId.value, 'B')
  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  staleList.resolve([harness.sessions[0], harness.sessions[1]])
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'B draft')
  assert.equal(harness.draft('B'), 'B draft')

  await harness.replace({ path: '/', query: { session_id: 'missing', task_open: 'missing' } })
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'B draft')
  assert.equal(harness.messages.some(entry => entry.type === 'warning'), true)

  await harness.replace({ path: '/', query: { session_id: '', task_open: 'invalid' } })
  assert.equal(harness.module.currentSessionId.value, 'B')
  await harness.replace({ path: '/', query: { session_id: ['B'], task_open: 'invalid-array' } })
  assert.equal(harness.module.currentSessionId.value, 'B')

  harness.module.handleSelectSession(harness.sessions[0])
  await harness.flush()
  harness.module.inputMsg.value = 'A again'
  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  staleHistory.resolve([{ id: 99, role: 'assistant', content: 'late A history' }])
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'B draft')
  assert.equal(harness.module.messages.value.some(message => message.content === 'late A history'), false)
})

test('a late HTTP new-session response after leaving the page cannot remove the new page null draft', async t => {
  const storage = createStorage({ token: 'user-a' })
  const oldResponse = createDeferred()
  const oldHarness = await createHarness(t, {
    storage,
    sessions: [],
    httpResponses: [
      oldResponse.promise,
      { session_id: 'late-HTTP', work_id: 'late-work', submission_status: 'accepted' }
    ]
  })
  await oldHarness.flush()
  await oldHarness.chat.setTransportMode('http')

  oldHarness.module.inputMsg.value = 'first message'
  const oldSendPromise = oldHarness.module.send()
  await oldHarness.flush()
  assert.equal(oldHarness.module.inputMsg.value, '')
  oldHarness.unmount()

  const newHarness = await createHarness(t, { storage, sessions: [] })
  await newHarness.flush()
  newHarness.module.inputMsg.value = 'new page draft'
  await newHarness.flush()

  oldResponse.resolve({
    choices: [{ finish_reason: 'new_session', message: { content: 'late-HTTP' } }]
  })
  await oldSendPromise
  await oldHarness.flush()

  assert.equal(newHarness.module.inputMsg.value, 'new page draft')
  assert.equal(storage.getItem(draftKey('user-a', null)), 'new page draft')
  assert.equal(storage.getItem(draftKey('user-a', 'late-HTTP')), null)
  assert.equal(newHarness.module.currentSessionId.value, null)
})
