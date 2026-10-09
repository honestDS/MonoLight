import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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
import * as reasoningTracker from '../src/composables/chat/reasoningTracker.js'
import * as thinkingTracker from '../src/composables/chat/thinkingTracker.js'
import * as terminalHistory from '../src/composables/chat/terminalHistory.js'
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

export const createDeferred = () => {
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

export const createStorage = initialValues => {
  const values = new Map(Object.entries(initialValues || {}))
  return {
    values,
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key)
  }
}

export const draftKey = (uid, sessionId) => (
  `${CHAT_DRAFT_STORAGE_PREFIX}${JSON.stringify([uid, sessionId])}`
)

const cloneMessage = message => ({ ...message })

export const createSession = (sessionId, overrides = {}) => ({
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

export const createTask = (sessionId, overrides = {}) => ({
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
      document: {
        hidden: false,
        visibilityState: 'visible',
        addEventListener: () => {},
        removeEventListener: () => {}
      },
      console: quietConsole
    },
    ['SESSION_TASKS_KEY', 'SESSION_ACTIVITY_KEY', 'useSessionTasks'],
    'useSessionTasks.js'
  )
}

export const createHarness = async (t, options = {}) => {
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

  const messageProcessor = loadPath(
    '../src/composables/chat/useMessageProcessor.js',
    {
      ElMessage,
      chatApi,
      i18n,
      ...utils,
      ...errorMessage,
      appendStreamReasoning: reasoningTracker.appendStreamReasoning,
      finalizeReasoning: reasoningTracker.finalizeStreamReasoning,
      insertMessageBeforeThinking: thinkingTracker.insertMessageBeforeThinking,
      removeThinkingMessageByIdentity: thinkingTracker.removeThinkingMessageByIdentity,
      insertTerminalHistory: terminalHistory.insertTerminalHistory,
      processStreamError: terminalHistory.processStreamError,
      processStreamToolStart: terminalHistory.processStreamToolStart,
      applyResumedTurnEnd: streamResume.applyResumedTurnEnd
    },
    ['resolveAssistantDisplayContent', 'useMessageProcessor']
  )

  const sessionDependencies = {
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
    resolveAssistantDisplayContent: messageProcessor.resolveAssistantDisplayContent,
    useMessageProcessor: messageProcessor.useMessageProcessor,
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
  }

  const deps = loadPath(
    '../src/composables/chat/chatMessageHelpers.js',
    sessionDependencies,
    [
      'normalizeHistoryMessage',
      'getAuditConfirmationRecordId',
      'parseAuditConfirmationResponse',
      'getLocalMessageType',
      'findTransientHistoryMessageIndex'
    ]
  )
  Object.assign(sessionDependencies, deps)

  for (const [relativePath, exportName] of [
    ['../src/composables/chat/useChatHistory.js', 'useChatHistory'],
    ['../src/composables/chat/useChatSessionEvents.js', 'useChatSessionEvents'],
    ['../src/composables/chat/useChatHttpSend.js', 'useChatHttpSend'],
    ['../src/composables/chat/useChatWebSocketSend.js', 'useChatWebSocketSend'],
    ['../src/composables/chat/useChatStreamResume.js', 'useChatStreamResume']
  ]) {
    const module = loadPath(relativePath, sessionDependencies, [exportName])
    Object.assign(deps, module)
    Object.assign(sessionDependencies, module)
  }

  const useChatSession = loadVmModule(
    useChatSessionSource,
    sessionDependencies,
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
  const viewDependencies = {
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
  }

  for (const name of [
    'useChatViewLayout',
    'useChatViewSettings',
    'useChatComposer',
    'useChatViewNavigation'
  ]) {
    Object.assign(viewDependencies, loadPath(
      `../src/composables/chat/${name}.js`,
      viewDependencies,
      [name]
    ))
  }

  const viewContext = vm.createContext(viewDependencies)

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
