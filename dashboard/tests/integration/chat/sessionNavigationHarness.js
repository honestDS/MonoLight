import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import {
  computed,
  effectScope,
  nextTick,
  ref,
  watch
} from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createSessionAgentSettingUpdater } from '../../../src/composables/chat/sessionAgentSettings.js'
import { SESSION_MAX_TURNS_UPPER_BOUND } from '../../../src/constants/index.js'
import enChat from '../../../src/i18n/locales/en/chat.js'
import {
  filterProfilesByUid,
  formatProfileOptionLabel,
  resolveProfileOwnerUid,
  resolveSessionProfileDisplayId,
  resolveSessionProfilePlaceholder
} from '../../../src/utils/profileOptions.js'
import {
  shouldDeferChatContent,
  shouldExposeChatContent,
  shouldReleaseChatContent
} from '../../../src/utils/chatContentReveal.js'
import { groupChatSessions } from '../../../src/utils/chatPresentation.js'

const chatViewSource = readFileSync(
  new URL('../../../src/views/ChatView.vue', import.meta.url),
  'utf8'
)
const scriptMatch = chatViewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!scriptMatch) throw new Error('ChatView.vue script setup was not found')

const stripImports = source => {
  const keptLines = []
  let inImport = false

  for (const line of source.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const useChatViewLayoutScript = stripImports(readFileSync(
  new URL('../../../src/composables/chat/useChatViewLayout.js', import.meta.url),
  'utf8'
)).replace(/\bexport\s+function\b/g, 'function')
const useChatViewSettingsScript = stripImports(readFileSync(
  new URL('../../../src/composables/chat/useChatViewSettings.js', import.meta.url),
  'utf8'
)).replace(/\bexport\s+function\b/g, 'function')
const useChatComposerScript = stripImports(readFileSync(
  new URL('../../../src/composables/chat/useChatComposer.js', import.meta.url),
  'utf8'
)).replace(/\bexport\s+function\b/g, 'function')
const useChatViewNavigationScript = stripImports(readFileSync(
  new URL('../../../src/composables/chat/useChatViewNavigation.js', import.meta.url),
  'utf8'
)).replace(/\bexport\s+function\b/g, 'function')
const chatViewScript = [
  useChatViewLayoutScript,
  useChatViewSettingsScript,
  useChatComposerScript,
  useChatViewNavigationScript,
  stripImports(scriptMatch[1])
].join('\n')

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
  for (let index = 0; index < 4; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
  await new Promise(resolve => setImmediate(resolve))
  for (let index = 0; index < 8; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const createSession = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  title: sessionId,
  last_active: '2026-10-08 00:00:00',
  source: 'http',
  uid: 'user-a',
  messages: [{ id: `${sessionId}-message`, session_id: sessionId, content: sessionId }],
  ...overrides
})

const createHarness = async (t, options = {}) => {
  const defaultSessions = options.sessions || [createSession('A'), createSession('B'), createSession('C')]
  const initialSessions = options.initialSessions || defaultSessions
  const initialSessionId = options.initialSessionId ?? null
  const initialMessages = options.initialMessages || (
    initialSessionId
      ? (initialSessions.find(session => session.session_id === initialSessionId)?.messages || [])
      : []
  )
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: {} },
      { path: '/:pathMatch(.*)*', component: {} }
    ]
  })
  await router.push(options.initialRoute || '/')
  await router.isReady()

  const route = {}
  for (const key of ['path', 'query', 'hash', 'fullPath']) {
    Object.defineProperty(route, key, {
      enumerable: true,
      get: () => router.currentRoute.value[key]
    })
  }

  const currentSessionId = ref(initialSessionId)
  const inputMsg = ref('')
  const messages = ref([...initialMessages])
  const sessions = ref([...initialSessions])
  const currentSession = computed(() => (
    sessions.value.find(session => session.session_id === currentSessionId.value) || null
  ))
  const loadRequests = []
  const selectCalls = []
  const warnings = []
  let unmounted = false

  const loadSessions = () => {
    const index = loadRequests.length
    const pending = createDeferred()
    const planned = typeof options.loadSessions === 'function'
      ? options.loadSessions({ index, pending, sessions: defaultSessions })
      : Array.isArray(options.loadResponses)
        ? options.loadResponses[Math.min(index, options.loadResponses.length - 1)]
        : defaultSessions
    const response = planned === undefined ? pending.promise : planned
    const promise = Promise.resolve(response).then(value => {
      const nextSessions = value === undefined ? defaultSessions : value
      if (!unmounted && Array.isArray(nextSessions)) sessions.value = nextSessions
      return nextSessions
    })
    loadRequests.push({ index, pending, promise })
    return promise
  }

  const selectSession = session => {
    selectCalls.push(session?.session_id ?? null)
    currentSessionId.value = session?.session_id ?? null
    inputMsg.value = ''
    messages.value = Array.isArray(session?.messages)
      ? session.messages.map(message => ({ ...message }))
      : []
  }

  const createNewSession = () => {
    currentSessionId.value = null
    inputMsg.value = ''
    messages.value = []
  }

  const chat = {
    messages,
    inputMsg,
    loading: ref(false),
    isReplyRunning: ref(false),
    isStopping: ref(false),
    messageList: ref(null),
    sessions,
    sessionsLoading: ref(false),
    currentSessionId,
    typingSessionId: ref(null),
    activeCollapse: ref(null),
    currentSession,
    transportMode: ref('http'),
    modeSettingSubmitting: ref(false),
    transportModeChangeBlocked: ref(false),
    attachments: ref([]),
    isCurrentSessionReadOnly: ref(false),
    isContextSummarizing: ref(false),
    llmRequestMetadata: ref(null),
    historyLoading: ref(false),
    initialHistoryLoaded: ref(true),
    newSessionProfileOverrideId: ref(null),
    currentSessionShowToolCalls: ref(false),
    currentSessionShowReasoning: ref(true),
    currentSessionGoalMode: ref(true),
    currentSessionMaxTurns: ref(5),
    goalModeDefault: ref(true),
    maxTurnsDefault: ref(5),
    currentTodoPlan: ref(null),
    enableMarkdownDefault: ref(true),
    loadSessions,
    handleDeleteSession: async () => {},
    selectSession,
    createNewSession,
    reloadCurrentSessionHistory: async () => {},
    send: async () => {},
    stopReply: () => {},
    setTransportMode: async mode => { chat.transportMode.value = mode },
    disconnectWebSocket: () => {},
    handleScroll: () => {},
    enqueueMessage: () => {}
  }

  const mountedCallbacks = []
  const unmountedCallbacks = []
  const tTranslate = typeof options.translate === 'function'
    ? options.translate
    : (key, values = {}) => {
        const translations = {
          'chat.task_session_unavailable': 'task session unavailable',
          'chat.session_group_user': enChat.session_group_user
        }
        const text = translations[key] || key
        return text.replace(/\{(\w+)\}/g, (_match, name) => String(values[name] ?? ''))
      }
  const ElMessage = {
    warning: message => warnings.push(message),
    error: () => {},
    success: () => {}
  }
  const profileResponse = options.profileResponse ?? {
    data: {
      data: {
        items: options.profiles || [],
        meta: { current_uid: 'user-a' }
      }
    }
  }
  const profileApi = {
    list: async () => profileResponse
  }
  const chatApi = {
    updateSessionSetting: async () => ({ data: { data: {} } })
  }
  const context = vm.createContext({
    ref,
    onMounted: callback => mountedCallbacks.push(callback),
    onUnmounted: callback => unmountedCallbacks.push(callback),
    computed,
    nextTick,
    watch,
    inject: () => null,
    ElMessage,
    ClickOutside: {},
    ChatLineSquare: {},
    Delete: {},
    InfoFilled: {},
    Plus: {},
    Refresh: {},
    UploadFilled: {},
    ArrowDown: {},
    useI18n: () => ({ t: tTranslate }),
    useRoute: () => route,
    useRouter: () => router,
    ChatMessageList: {},
    SessionTodoPanel: {},
    HelpTooltip: {},
    useChatSession: () => chat,
    SESSION_TASKS_KEY: Symbol('session-tasks'),
    createSessionAgentSettingUpdater,
    fileApi: { upload: async () => ({ data: {} }) },
    chatApi,
    profileApi,
    SESSION_MAX_TURNS_UPPER_BOUND,
    filterProfilesByUid,
    formatProfileOptionLabel,
    resolveSessionProfileDisplayId,
    resolveSessionProfilePlaceholder,
    resolveProfileOwnerUid,
    shouldDeferChatContent,
    shouldExposeChatContent,
    shouldReleaseChatContent,
    groupChatSessions,
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    console: { error: () => {} }
  })
  const scope = effectScope()
  let module
  scope.run(() => {
    vm.runInContext(
      `${chatViewScript}\nglobalThis.__module = { handleSelectSession, handleCreateNewSession, currentSessionId, inputMsg, messages, sessionEngaged, deferredContentSessionId, currentUid, groupedSessions, collapsedGroups, toggleGroup, sessionsPanelOpen, toggleSessionsPanel, closeSessionsPanel }`,
      context,
      { filename: 'ChatView.vue?script-setup' }
    )
    module = context.__module
  })

  for (const callback of mountedCallbacks) callback()

  const harness = {
    chat,
    module,
    router,
    loadRequests,
    selectCalls,
    warnings,
    session: sessionId => defaultSessions.find(session => session.session_id === sessionId),
    url: () => {
      const current = router.currentRoute.value
      return {
        path: current.path,
        query: { ...current.query },
        hash: current.hash,
        fullPath: current.fullPath
      }
    },
    flush,
    replace: async location => {
      await router.replace(location)
      await flush()
    },
    unmount: () => {
      if (unmounted) return
      unmounted = true
      for (const callback of unmountedCallbacks.splice(0)) callback()
      scope.stop()
    }
  }
  t.after(() => harness.unmount())
  return harness
}

export { createDeferred, createSession, createHarness }
