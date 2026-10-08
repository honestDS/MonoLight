import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import {
  computed,
  effectScope,
  nextTick,
  ref,
  watch
} from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createSessionAgentSettingUpdater } from '../src/composables/chat/sessionAgentSettings.js'
import { SESSION_MAX_TURNS_UPPER_BOUND } from '../src/constants/index.js'
import {
  filterProfilesByUid,
  formatProfileOptionLabel,
  resolveProfileOwnerUid,
  resolveSessionProfileDisplayId,
  resolveSessionProfilePlaceholder
} from '../src/utils/profileOptions.js'
import {
  shouldDeferChatContent,
  shouldExposeChatContent,
  shouldReleaseChatContent
} from '../src/utils/chatContentReveal.js'

const chatViewSource = readFileSync(
  new URL('../src/views/ChatView.vue', import.meta.url),
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

const chatViewScript = stripImports(scriptMatch[1])

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
  const tTranslate = (key, values = {}) => {
    const translations = {
      'chat.task_session_unavailable': 'task session unavailable'
    }
    const text = translations[key] || key
    return text.replace(/\{(\w+)\}/g, (_match, name) => String(values[name] ?? ''))
  }
  const ElMessage = {
    warning: message => warnings.push(message),
    error: () => {},
    success: () => {}
  }
  const profileApi = {
    list: async () => ({
      data: {
        data: {
          items: options.profiles || [],
          meta: { current_uid: 'user-a' }
        }
      }
    })
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
      `${chatViewScript}\nglobalThis.__module = { handleSelectSession, handleCreateNewSession, currentSessionId, inputMsg, messages, sessionEngaged, deferredContentSessionId }`,
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

test('deep links consume task_open, manual navigation survives refresh, and preserves unrelated URL parts', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'first-token', keep: 'yes' },
      hash: '#messages'
    }
  })

  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
  assert.equal(harness.url().hash, '#messages')
  assert.equal(harness.loadRequests.length, 1)
  assert.equal(harness.chat.messages.value[0].session_id, 'A')

  harness.chat.inputMsg.value = 'unsent draft'
  harness.module.handleSelectSession(sessions[1])
  assert.equal(harness.chat.inputMsg.value, '')
  harness.chat.inputMsg.value = 'B unsent draft'
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.chat.inputMsg.value, 'B unsent draft')
  assert.deepEqual(harness.url().query, { session_id: 'B', keep: 'yes' })
  assert.equal(harness.chat.messages.value[0].session_id, 'B')

  const refreshed = await createHarness(t, {
    sessions,
    initialRoute: harness.url()
  })
  await refreshed.flush()
  assert.equal(refreshed.chat.currentSessionId.value, 'B')
  assert.deepEqual(refreshed.url().query, { session_id: 'B', keep: 'yes' })
})

test('new session clears the URL and messages, refresh stays on welcome, and allocated IDs synchronize', async t => {
  const sessions = [createSession('A')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'new-session-token', keep: 'yes' },
      hash: '#welcome'
    }
  })
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')

  harness.chat.inputMsg.value = 'draft'
  harness.module.handleCreateNewSession()
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, null)
  assert.deepEqual(harness.chat.messages.value, [])
  assert.equal(harness.chat.inputMsg.value, '')
  assert.deepEqual(harness.url().query, { keep: 'yes' })
  assert.equal(harness.url().hash, '#welcome')

  const refreshed = await createHarness(t, {
    sessions,
    initialRoute: harness.url()
  })
  await refreshed.flush()
  assert.equal(refreshed.chat.currentSessionId.value, null)
  assert.deepEqual(refreshed.chat.messages.value, [])

  refreshed.chat.currentSessionId.value = 'allocated-id'
  await refreshed.flush()
  assert.deepEqual(refreshed.url().query, { session_id: 'allocated-id', keep: 'yes' })
})

test('the same task token and a new token reopen a session without a navigation loop', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A', keep: 'yes' }, hash: '#tasks' }
  })
  await harness.flush()

  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')

  await harness.replace({
    path: '/',
    query: { session_id: 'A', task_open: 'same-token', keep: 'yes' },
    hash: '#tasks'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })

  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')

  await harness.replace({
    path: '/',
    query: { session_id: 'A', task_open: 'same-token', keep: 'yes' },
    hash: '#tasks'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.chat.messages.value[0].session_id, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })

  const loadsBeforeNewToken = harness.loadRequests.length
  await harness.replace({
    path: '/',
    query: { session_id: 'A', task_open: 'new-token', keep: 'yes' },
    hash: '#tasks'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
  assert.equal(harness.loadRequests.length, loadsBeforeNewToken + 1)
})

test('a late B replace cannot win after A is selected again', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await harness.flush()

  const blockedB = createDeferred()
  const enteredB = createDeferred()
  const removeGuard = harness.router.beforeEach(to => {
    if (to.query.session_id !== 'B') return undefined
    enteredB.resolve()
    return blockedB.promise
  })
  t.after(removeGuard)

  harness.module.handleSelectSession(sessions[1])
  await enteredB.promise
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.url().query.session_id, 'A')
  harness.module.handleSelectSession(sessions[0])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.url().query.session_id, 'A')

  blockedB.resolve()
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.url().query.session_id, 'A')
})

test('stale session loads cannot overwrite a manual selection or a new-session welcome state', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const staleLoad = createDeferred()
  const harness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'pending-token', keep: 'yes' }
    },
    loadSessions: ({ index }) => index === 0 ? staleLoad.promise : sessions
  })
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, null)

  harness.chat.inputMsg.value = 'draft'
  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.chat.inputMsg.value, '')
  assert.equal(harness.chat.messages.value[0].session_id, 'B')
  assert.deepEqual(harness.url().query, { session_id: 'B', keep: 'yes' })

  staleLoad.resolve([sessions[0]])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.chat.messages.value[0].session_id, 'B')
  assert.equal(harness.url().query.session_id, 'B')

  const pendingNew = createDeferred()
  const newHarness = await createHarness(t, {
    sessions: [sessions[0]],
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'pending-new-token', keep: 'new' }
    },
    loadSessions: () => pendingNew.promise
  })
  await newHarness.flush()
  assert.equal(newHarness.chat.currentSessionId.value, null)
  assert.equal(newHarness.url().query.session_id, 'A')
  newHarness.module.handleCreateNewSession()
  await newHarness.flush()
  assert.deepEqual(newHarness.url().query, { keep: 'new' })
  pendingNew.resolve([sessions[0]])
  await newHarness.flush()
  assert.equal(newHarness.chat.currentSessionId.value, null)
  assert.deepEqual(newHarness.chat.messages.value, [])
  assert.deepEqual(newHarness.url().query, { keep: 'new' })
})

test('unavailable task sessions preserve the active session and remove only the task token', async t => {
  const active = createSession('A')
  const harness = await createHarness(t, {
    sessions: [active],
    initialSessions: [active],
    initialSessionId: 'A',
    initialRoute: {
      path: '/',
      query: { session_id: 'missing', task_open: 'missing-token', keep: 'yes' },
      hash: '#active'
    },
    loadResponses: [[active]]
  })
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.warnings.length, 1)
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
  assert.equal(harness.chat.messages.value[0].session_id, 'A')

  await harness.replace({
    path: '/',
    query: { session_id: 'missing', task_open: 'missing-token', keep: 'yes' },
    hash: '#active'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.warnings.length, 2)
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
})

test('invalid session IDs do not select sessions and late work after unmount is inert', async t => {
  for (const invalidSessionId of ['', ['A', 'B'], 'x'.repeat(101)]) {
    const harness = await createHarness(t, {
      sessions: [createSession('A')],
      initialRoute: { path: '/', query: { session_id: invalidSessionId } }
    })
    await harness.flush()
    assert.equal(harness.chat.currentSessionId.value, null)
    assert.deepEqual(harness.chat.messages.value, [])
  }

  const pending = createDeferred()
  const harness = await createHarness(t, {
    sessions: [createSession('A')],
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'late-token', keep: 'yes' }
    },
    loadSessions: () => pending.promise
  })
  await harness.flush()
  const urlBeforeUnmount = harness.url()
  harness.unmount()
  pending.resolve([createSession('A')])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, null)
  assert.deepEqual(harness.url(), urlBeforeUnmount)
})

test('session URLs restore welcome on root navigation but never rewrite another route', async t => {
  const sessions = [createSession('A')]
  const rootHarness = await createHarness(t, {
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A', keep: 'yes' }, hash: '#root' }
  })
  await rootHarness.flush()
  await rootHarness.replace({ path: '/', query: { keep: 'yes' }, hash: '#root' })
  assert.equal(rootHarness.chat.currentSessionId.value, null)
  assert.deepEqual(rootHarness.chat.messages.value, [])
  assert.deepEqual(rootHarness.url().query, { keep: 'yes' })

  const otherHarness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/profiles',
      query: { session_id: 'A', task_open: 'other-token', keep: 'yes' },
      hash: '#profiles'
    }
  })
  await otherHarness.flush()
  assert.equal(otherHarness.chat.currentSessionId.value, null)
  assert.equal(otherHarness.url().path, '/profiles')
  assert.deepEqual(otherHarness.url().query, {
    session_id: 'A',
    task_open: 'other-token',
    keep: 'yes'
  })
  assert.equal(otherHarness.url().hash, '#profiles')
})
