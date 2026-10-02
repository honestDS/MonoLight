import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import * as Vue from 'vue'
import { createContextSummaryTracker } from '../src/composables/chat/contextSummaryTracker.js'
import { shouldFetchHttpWorkStatus } from '../src/composables/chat/sessionListLoading.js'
import { createWorkLifecycleTracker } from '../src/composables/chat/workLifecycleTracker.js'

const useChatSessionPath = new URL('../src/composables/chat/useChatSession.js', import.meta.url)

const extractBetween = (source, startMarker, endMarker, label) => {
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker, start + startMarker.length)
  assert.ok(start >= 0, `${label} start should be found`)
  assert.ok(end > start, `${label} end should be found`)
  return source.slice(start, end)
}

let implementationPromise
const loadImplementation = () => {
  implementationPromise ||= readFile(useChatSessionPath, 'utf8').then(source => ({
    currentSessionSource: extractBetween(
      source,
      'const currentSession = computed(() =>',
      'const currentSessionShowToolCalls = computed({',
      'currentSession computed'
    ),
    readOnlySource: extractBetween(
      source,
      'const isCurrentSessionReadOnly = computed(() =>',
      'const isStopping = computed(() =>',
      'read-only computed'
    ),
    stoppingSource: extractBetween(
      source,
      'const isStopping = computed(() =>',
      'const isReplyRunning = computed(() =>',
      'stopping computed'
    ),
    replyRunningSource: extractBetween(
      source,
      'const isReplyRunning = computed(() =>',
      '// 3. 通信层',
      'reply-running computed'
    ),
    trackSubmissionSource: extractBetween(
      source,
      'const trackSubmission = async (getSessionId, submit) => {',
      'const normalizeHttpIdentity =',
      'trackSubmission'
    ),
    normalizeHttpIdentitySource: extractBetween(
      source,
      'const normalizeHttpIdentity =',
      'const trackHttpSubmission =',
      'HTTP identity normalizer'
    ),
    resetHttpPollingStateSource: extractBetween(
      source,
      'const resetHttpPollingState = () => {',
      '// 默认 Markdown 开关状态',
      'HTTP polling reset'
    ),
    stopReplySource: extractBetween(
      source,
      'const stopReply = async () => {',
      '// ==================== 核心发送方法 ====================',
      'stopReply'
    ),
    finishHttpWorkLifecycleSource: extractBetween(
      source,
      'const finishHttpWorkLifecycle = ({ sessionId, workId, resolvedWorkId, requestIds }) => {',
      'const maybeMergeHttpSessionHistory = (sessionId, latestMessageId) => {',
      'HTTP work lifecycle finish'
    ),
    applyHttpWorkStatusSource: extractBetween(
      source,
      'const applyHttpWorkStatus = (work, statusData, sessionId) => {',
      'const fetchHttpWorkStatus = async (work, sessionId) => {',
      'HTTP work status application'
    ),
    processHttpSessionSnapshotSource: extractBetween(
      source,
      'const processHttpSessionSnapshot = async (sessions) => {',
      'const handleSessionsUpdated = sessions => {',
      'HTTP session snapshot processing'
    )
  }))
  return implementationPromise
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

const createHarness = (implementation, options = {}) => {
  const sessions = Vue.ref(options.sessions || [])
  const currentSessionId = Vue.ref(options.currentSessionId ?? null)
  const transport = {
    transportMode: Vue.ref(options.transportMode ?? 'http')
  }
  const sessionManager = {
    sessions,
    currentSessionId,
    refreshSessionLoadingState: async () => {
      refreshCalls.push(currentSessionId.value)
    }
  }
  const currentSession = new Function(
    'computed',
    'sessionManager',
    `${implementation.currentSessionSource}\nreturn currentSession`
  )(Vue.computed, sessionManager)

  const chatState = {
    messages: Vue.ref(options.messages || []),
    loading: Vue.ref(options.loading ?? false),
    inputMsg: Vue.ref(options.inputMsg || '')
  }
  const attachments = Vue.ref(options.attachments || [])
  const contextSummaryWorkKeys = Vue.ref(new Set())
  const contextSummaryRequestKeys = new Map()
  const contextSummaryTracker = createContextSummaryTracker()
  const workLifecycleTracker = createWorkLifecycleTracker()
  const initialHistoryLoaded = Vue.ref(options.initialHistoryLoaded ?? true)
  const pendingHttpRequests = new Map(options.pendingHttpRequests || [])
  const inFlightSubmissions = new Set()
  const stoppingSessionIds = Vue.ref(new Set())
  const observedHttpWorkStatuses = new Map()
  const observedHttpLatestMessageIds = new Map()
  const fetchingHttpWorks = new Set()
  const resolvedHttpWorks = new Set()
  const refreshCalls = []
  const historyMergeCalls = []
  const maybeMergeHttpSessionHistory = () => undefined
  const hasPendingHttpRequestForWork = () => false
  const errors = []
  const streamErrors = []
  const stopCalls = []
  let stopBehavior = async () => undefined

  const normalizeHttpIdentity = new Function(
    `${implementation.normalizeHttpIdentitySource}\nreturn normalizeHttpIdentity`
  )()
  const isCurrentWritableHttpSession = sessionId => (
    normalizeHttpIdentity(currentSessionId.value) === normalizeHttpIdentity(sessionId)
  )
  const getPendingHttpRequestIdsForWork = (_sessionId, _workId, requestIds) => (
    new Set(
      (Array.isArray(requestIds) ? requestIds : [])
        .map(normalizeHttpIdentity)
        .filter(Boolean)
    )
  )
  const resetHttpPollingState = new Function(
    'httpPollingStateVersion',
    'pendingHttpRequests',
    'observedHttpWorkStatuses',
    'observedHttpLatestMessageIds',
    'fetchingHttpWorks',
    'resolvedHttpWorks',
    `${implementation.resetHttpPollingStateSource}\nreturn resetHttpPollingState`
  )(
    0,
    pendingHttpRequests,
    observedHttpWorkStatuses,
    observedHttpLatestMessageIds,
    fetchingHttpWorks,
    resolvedHttpWorks
  )
  const trackSubmission = new Function(
    'inFlightSubmissions',
    `${implementation.trackSubmissionSource}\nreturn trackSubmission`
  )(inFlightSubmissions)

  const isCurrentSessionReadOnly = new Function(
    'computed',
    'currentSession',
    `${implementation.readOnlySource}\nreturn isCurrentSessionReadOnly`
  )(Vue.computed, currentSession)
  const isStopping = new Function(
    'computed',
    'sessionManager',
    'stoppingSessionIds',
    `${implementation.stoppingSource}\nreturn isStopping`
  )(Vue.computed, sessionManager, stoppingSessionIds)
  const isReplyRunning = new Function(
    'computed',
    'isCurrentSessionReadOnly',
    'chatState',
    'currentSession',
    'isStopping',
    `${implementation.replyRunningSource}\nreturn isReplyRunning`
  )(Vue.computed, isCurrentSessionReadOnly, chatState, currentSession, isStopping)

  const chatApi = {
    stopSession: sessionId => {
      stopCalls.push(sessionId)
      return stopBehavior(sessionId)
    }
  }
  const mergeLatestSessionHistory = async sessionId => {
    historyMergeCalls.push(sessionId)
  }
  const finishHttpWorkLifecycle = new Function(
    'normalizeHttpIdentity',
    'getPendingHttpRequestIdsForWork',
    'isCurrentWritableHttpSession',
    'chatState',
    'workLifecycleTracker',
    'pendingHttpRequests',
    `${implementation.finishHttpWorkLifecycleSource}\nreturn finishHttpWorkLifecycle`
  )(
    normalizeHttpIdentity,
    getPendingHttpRequestIdsForWork,
    isCurrentWritableHttpSession,
    chatState,
    workLifecycleTracker,
    pendingHttpRequests
  )
  const messageProcessor = {
    processStreamError: (...args) => {
      streamErrors.push(args)
      return true
    }
  }
  const applyHttpWorkStatus = new Function(
    'normalizeHttpIdentity',
    'isCurrentWritableHttpSession',
    'observedHttpWorkStatuses',
    'getPendingHttpRequestIdsForWork',
    'applyTodoTransportPayload',
    'updateLlmRequestMetadata',
    'startHttpHistoryBackgroundTaskSync',
    'applyNonStreamSessionEvents',
    'shouldProcessCompletedWork',
    'processAiResponse',
    'hasHttpResultMessage',
    'messageProcessor',
    'chatState',
    't',
    'ElMessage',
    'finishHttpWorkLifecycle',
    'resolvedHttpWorks',
    `${implementation.applyHttpWorkStatusSource}\nreturn applyHttpWorkStatus`
  )(
    normalizeHttpIdentity,
    isCurrentWritableHttpSession,
    observedHttpWorkStatuses,
    getPendingHttpRequestIdsForWork,
    () => undefined,
    () => undefined,
    () => undefined,
    () => undefined,
    () => false,
    () => undefined,
    () => false,
    messageProcessor,
    chatState,
    key => key,
    { error: message => errors.push(message) },
    finishHttpWorkLifecycle,
    resolvedHttpWorks
  )
  const fetchHttpWorkStatus = async () => null
  const processHttpSessionSnapshot = new Function(
    'transport',
    'sessionManager',
    'isCurrentSessionReadOnly',
    'normalizeHttpIdentity',
    'maybeMergeHttpSessionHistory',
    'observedHttpWorkStatuses',
    'hasPendingHttpRequestForWork',
    'initialHistoryLoaded',
    'shouldFetchHttpWorkStatus',
    'resolvedHttpWorks',
    'fetchingHttpWorks',
    'fetchHttpWorkStatus',
    'isCurrentWritableHttpSession',
    'mergeLatestSessionHistory',
    'chatState',
    `${implementation.processHttpSessionSnapshotSource}\nreturn processHttpSessionSnapshot`
  )(
    transport,
    sessionManager,
    isCurrentSessionReadOnly,
    normalizeHttpIdentity,
    maybeMergeHttpSessionHistory,
    observedHttpWorkStatuses,
    hasPendingHttpRequestForWork,
    initialHistoryLoaded,
    shouldFetchHttpWorkStatus,
    resolvedHttpWorks,
    fetchingHttpWorks,
    fetchHttpWorkStatus,
    isCurrentWritableHttpSession,
    mergeLatestSessionHistory,
    chatState
  )
  const stopReply = new Function(
    'sessionManager',
    'isCurrentSessionReadOnly',
    'isReplyRunning',
    'stoppingSessionIds',
    'chatApi',
    'inFlightSubmissions',
    'pendingHttpRequests',
    'sessionScopeActive',
    'normalizeHttpIdentity',
    'chatState',
    'contextSummaryWorkKeys',
    'contextSummaryRequestKeys',
    'workLifecycleTracker',
    'contextSummaryTracker',
    'resetHttpPollingState',
    'mergeLatestSessionHistory',
    'ElMessage',
    't',
    `${implementation.stopReplySource}\nreturn stopReply`
  )(
    sessionManager,
    isCurrentSessionReadOnly,
    isReplyRunning,
    stoppingSessionIds,
    chatApi,
    inFlightSubmissions,
    pendingHttpRequests,
    true,
    normalizeHttpIdentity,
    chatState,
    contextSummaryWorkKeys,
    contextSummaryRequestKeys,
    workLifecycleTracker,
    contextSummaryTracker,
    resetHttpPollingState,
    mergeLatestSessionHistory,
    { error: message => errors.push(message) },
    key => key
  )

  return {
    attachments,
    chatState,
    contextSummaryRequestKeys,
    contextSummaryTracker,
    contextSummaryWorkKeys,
    currentSessionId,
    errors,
    finishHttpWorkLifecycle,
    historyMergeCalls,
    inFlightSubmissions,
    isCurrentSessionReadOnly,
    isReplyRunning,
    applyHttpWorkStatus,
    processHttpSessionSnapshot,
    messageProcessorErrors: streamErrors,
    pendingHttpRequests,
    resolvedHttpWorks,
    isStopping,
    refreshCalls,
    sessions,
    set stopBehavior(value) {
      stopBehavior = value
    },
    stopCalls,
    stopReply,
    trackSubmission,
    workLifecycleTracker
  }
}

test('stopReply clears active lifecycle state, preserves history and prevents late loop resurrection', async () => {
  const implementation = await loadImplementation()
  const historicalMessage = {
    id: 'history',
    db_id: 42,
    role: 'assistant',
    content: 'historical answer'
  }
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: true,
    is_reply_running: true,
    reply_works: [
      { work_id: 'work-A', status: 'running', request_ids: ['request-A'] }
    ]
  }
  const harness = createHarness(implementation, {
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'draft prompt',
    attachments: [{ path: 'draft.txt' }],
    messages: [
      historicalMessage,
      {
        id: 'user-A',
        role: 'user',
        content: 'queued prompt',
        request_id: 'request-A',
        status: 'queued'
      },
      {
        id: 'thinking-A',
        role: 'thinking',
        content: 'partial reply',
        work_id: 'work-A',
        request_ids: ['request-A']
      }
    ]
  })

  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(session.is_loading, true)
  assert.equal(session.is_reply_running, false)
  assert.equal(harness.chatState.inputMsg.value, 'draft prompt')
  assert.deepEqual(harness.attachments.value, [{ path: 'draft.txt' }])
  assert.equal(
    harness.chatState.messages.value.some(message => message.role === 'thinking'),
    false
  )
  assert.equal(
    harness.chatState.messages.value.find(message => message.id === 'user-A').status,
    undefined
  )
  assert.equal(
    harness.chatState.messages.value.find(message => message.id === 'history').content,
    'historical answer'
  )
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)

  const afterLateLoop = harness.workLifecycleTracker.startAgentLoop(
    harness.chatState.messages.value,
    { work_id: 'work-A', response_id: 'late-response', request_ids: ['request-A'] }
  )
  assert.deepEqual(afterLateLoop, harness.chatState.messages.value)
  assert.equal(afterLateLoop.some(message => message.role === 'thinking'), false)
})

test('stopReply sends only one stop without in-flight submissions and blocks repeats while stopping', async () => {
  const implementation = await loadImplementation()
  const stopDeferred = createDeferred()
  const harness = createHarness(implementation, {
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true
  })
  harness.stopBehavior = () => stopDeferred.promise

  const stopping = harness.stopReply()
  assert.equal(harness.isStopping.value, true)
  assert.equal(await harness.stopReply(), false)
  assert.deepEqual(harness.stopCalls, ['A'])

  stopDeferred.resolve()
  assert.equal(await stopping, true)
  assert.equal(harness.isStopping.value, false)
})

test('stopReply waits for same-session submissions before a second stop, including rejected submissions', async () => {
  const implementation = await loadImplementation()

  for (const shouldReject of [false, true]) {
    const stopDeferreds = []
    const secondStopCalled = createDeferred()
    const harness = createHarness(implementation, {
      sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
      currentSessionId: 'A',
      loading: true
    })
    harness.stopBehavior = () => {
      const deferred = createDeferred()
      stopDeferreds.push(deferred)
      if (stopDeferreds.length === 2) secondStopCalled.resolve()
      return deferred.promise
    }

    const submissionDeferred = createDeferred()
    const trackedSubmission = harness.trackSubmission(
      () => 'A',
      () => submissionDeferred.promise
    )
    trackedSubmission.catch(() => undefined)

    const stopping = harness.stopReply()
    assert.equal(harness.isStopping.value, true)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, ['A'])

    stopDeferreds[0].resolve()
    if (shouldReject) {
      submissionDeferred.reject(new Error('submission failed'))
    } else {
      submissionDeferred.resolve('submitted')
    }
    await secondStopCalled.promise
    assert.deepEqual(harness.stopCalls, ['A', 'A'])

    stopDeferreds[1].resolve()
    assert.equal(await stopping, true)
    await trackedSubmission.catch(() => undefined)
    assert.equal(harness.isStopping.value, false)
    assert.equal(harness.chatState.loading.value, false)
  }
})

test('stopReply ignores in-flight submissions belonging to another session', async () => {
  const implementation = await loadImplementation()
  const submissionDeferred = createDeferred()
  const harness = createHarness(implementation, {
    sessions: [
      { session_id: 'A', source: 'http', is_loading: true, is_reply_running: true },
      { session_id: 'B', source: 'http', is_loading: true, is_reply_running: true }
    ],
    currentSessionId: 'A',
    loading: true
  })
  const trackedSubmission = harness.trackSubmission(
    () => 'B',
    () => submissionDeferred.promise
  )
  trackedSubmission.catch(() => undefined)

  harness.stopBehavior = async () => undefined
  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.inFlightSubmissions.size, 1)

  submissionDeferred.resolve('submitted')
  await trackedSubmission
  assert.equal(harness.inFlightSubmissions.size, 0)
})

test('stopReply preserves loading, messages and draft when the stop request fails', async () => {
  const implementation = await loadImplementation()
  const messages = [
    { id: 'queued', role: 'user', request_id: 'request-A', status: 'queued' },
    { id: 'thinking', role: 'thinking', request_ids: ['request-A'] }
  ]
  const harness = createHarness(implementation, {
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'keep this draft',
    attachments: [{ path: 'keep-me.txt' }],
    messages
  })
  harness.stopBehavior = async () => {
    throw new Error('stop failed')
  }

  assert.equal(await harness.stopReply(), false)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.errors.at(-1), 'stop failed')
  assert.equal(harness.chatState.loading.value, true)
  assert.equal(harness.sessions.value[0].is_loading, true)
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.deepEqual(harness.chatState.messages.value, messages)
  assert.deepEqual(harness.attachments.value, [{ path: 'keep-me.txt' }])
  assert.equal(harness.isStopping.value, false)
})

test('stopReply does not send a stop for read-only, missing or idle sessions', async () => {
  const implementation = await loadImplementation()
  const cases = [
    {
      sessions: [{ session_id: 'read-only', source: 'external', is_loading: true, is_reply_running: false }],
      currentSessionId: 'read-only',
      loading: true
    },
    {
      sessions: [],
      currentSessionId: null,
      loading: true
    },
    {
      sessions: [{ session_id: 'idle', source: 'http', is_loading: false, is_reply_running: false }],
      currentSessionId: 'idle',
      loading: false
    }
  ]

  for (const options of cases) {
    const harness = createHarness(implementation, options)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, [])
  }
})

test('stopReply ignores aggregate-only busy sessions for HTTP and WS', async () => {
  const implementation = await loadImplementation()

  for (const source of ['http', 'ws']) {
    const session = {
      session_id: 'A',
      source,
      is_loading: true,
      is_reply_running: false,
      reply_works: []
    }
    const harness = createHarness(implementation, {
      sessions: [session],
      currentSessionId: 'A',
      transportMode: source,
      inputMsg: 'keep this draft',
      attachments: [{ path: 'keep-me.txt' }]
    })

    assert.equal(harness.isReplyRunning.value, false)
    assert.equal(await harness.stopReply(), false)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, [])
    assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
    assert.deepEqual(harness.attachments.value, [{ path: 'keep-me.txt' }])
    assert.equal(session.is_loading, true)
    assert.equal(session.is_reply_running, false)
  }
})

test('processHttpSessionSnapshot clears stale local loading for an aggregate-only session', async () => {
  const implementation = await loadImplementation()
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: true,
    is_reply_running: false,
    reply_works: []
  }
  const harness = createHarness(implementation, {
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'keep this draft'
  })

  assert.equal(harness.isReplyRunning.value, true)
  await harness.processHttpSessionSnapshot([session])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(harness.isReplyRunning.value, false)
  assert.equal(session.is_loading, true)
  assert.equal(session.is_reply_running, false)

  await harness.processHttpSessionSnapshot([session])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(harness.isReplyRunning.value, false)
  assert.equal(await harness.stopReply(), false)
  assert.equal(await harness.stopReply(), false)
  assert.deepEqual(harness.stopCalls, [])
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.equal(session.is_loading, true)
})

test('stopReply clears only remote reply state when aggregate work remains for HTTP and WS', async () => {
  const implementation = await loadImplementation()

  for (const source of ['http', 'ws']) {
    const session = {
      session_id: 'A',
      source,
      is_loading: true,
      is_reply_running: true,
      reply_works: []
    }
    const harness = createHarness(implementation, {
      sessions: [session],
      currentSessionId: 'A',
      transportMode: source,
      inputMsg: 'keep this draft'
    })

    assert.equal(harness.isReplyRunning.value, true)
    assert.equal(await harness.stopReply(), true)
    assert.deepEqual(harness.stopCalls, ['A'])
    assert.equal(harness.chatState.loading.value, false)
    assert.equal(session.is_reply_running, false)
    assert.equal(session.is_loading, true)
    assert.equal(harness.isReplyRunning.value, false)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, ['A'])
    assert.equal(session.is_loading, true)
  }
})

test('stopReply uses local loading before remote reply confirmation', async () => {
  const implementation = await loadImplementation()
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: false,
    is_reply_running: false,
    reply_works: []
  }
  const harness = createHarness(implementation, {
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'keep this draft'
  })

  assert.equal(harness.isReplyRunning.value, true)
  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(session.is_loading, false)
  assert.equal(session.is_reply_running, false)
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
})

test('stopReply isolates a completed stop for A after switching to B and allows B to stop independently', async () => {
  const implementation = await loadImplementation()
  const stopA = createDeferred()
  const sessionA = { session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }
  const sessionB = { session_id: 'B', source: 'http', is_loading: true, is_reply_running: true }
  const harness = createHarness(implementation, {
    sessions: [sessionA, sessionB],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'A draft',
    attachments: [{ path: 'A-draft.txt' }],
    messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }]
  })
  harness.stopBehavior = sessionId => sessionId === 'A' ? stopA.promise : Promise.resolve()

  const stoppingA = harness.stopReply()
  assert.equal(harness.isStopping.value, true)

  harness.currentSessionId.value = 'B'
  harness.chatState.messages.value = [
    { role: 'thinking', work_id: 'work-B', request_ids: ['request-B'] }
  ]
  harness.attachments.value = [{ path: 'B-draft.txt' }]
  harness.chatState.inputMsg.value = 'B draft'
  harness.chatState.loading.value = true

  stopA.resolve()
  assert.equal(await stoppingA, true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.chatState.loading.value, true)
  assert.deepEqual(harness.chatState.messages.value, [
    { role: 'thinking', work_id: 'work-B', request_ids: ['request-B'] }
  ])
  assert.equal(harness.chatState.inputMsg.value, 'B draft')
  assert.deepEqual(harness.attachments.value, [{ path: 'B-draft.txt' }])

  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A', 'B'])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(sessionB.is_loading, true)
  assert.equal(sessionB.is_reply_running, false)
})

test('stopReply terminates compression tracked only by a work key', async () => {
  const implementation = await loadImplementation()
  const harness = createHarness(implementation, {
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true,
    messages: [
      { id: 'history', role: 'assistant', db_id: 7, content: 'keep this' },
      { id: 'summary', role: 'thinking', request_id: 'summary-request', content: 'compressing' }
    ]
  })
  harness.contextSummaryTracker.startContextSummaryWork(
    harness.contextSummaryWorkKeys.value,
    harness.contextSummaryRequestKeys,
    { session_id: 'A', work_id: 'summary-work' },
    'summary-request'
  )
  assert.equal(harness.contextSummaryWorkKeys.value.has('work:summary-work'), true)

  assert.equal(await harness.stopReply(), true)
  assert.equal(harness.contextSummaryWorkKeys.value.has('work:summary-work'), false)
  assert.equal(harness.contextSummaryRequestKeys.has('summary-request'), false)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('summary-work'), true)
  assert.equal(
    harness.chatState.messages.value.some(message => message.role === 'thinking'),
    false
  )
  assert.equal(harness.chatState.messages.value[0].content, 'keep this')
})

test('stopReply clears residual no-work thinking after a cancelled lifecycle was already terminal', async () => {
  const implementation = await loadImplementation()
  const harness = createHarness(implementation, {
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true,
    messages: [
      { id: 'residual', role: 'thinking', request_id: 'cancel-request', content: 'stale' }
    ]
  })
  harness.workLifecycleTracker.finishWorkLifecycle([], {
    type: 'cancelled',
    session_id: 'A',
    work_id: 'cancelled-work',
    request_ids: ['cancel-request']
  })

  assert.equal(await harness.stopReply(), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('cancelled-work'), true)
  assert.equal(
    harness.chatState.messages.value.some(message => message.role === 'thinking'),
    false
  )
  assert.equal(harness.chatState.loading.value, false)
})

test('cancelled HTTP work status finishes quietly and clears its lifecycle state', async () => {
  const implementation = await loadImplementation()
  const harness = createHarness(implementation, {
    sessions: [{ session_id: 'A', source: 'http' }],
    currentSessionId: 'A',
    inputMsg: 'keep this draft',
    messages: [
      { id: 'history', db_id: 42, role: 'assistant', content: 'historical answer' },
      { id: 'queued', role: 'user', content: 'queued prompt', request_id: 'request-A', status: 'queued' },
      { id: 'thinking', role: 'thinking', content: 'partial reply', work_id: 'work-A', request_ids: ['request-A', 'request-B'] }
    ],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }],
      ['request-B', { sessionId: 'A', workId: 'work-A' }],
      ['request-other', { sessionId: 'B', workId: 'work-B' }]
    ]
  })

  const result = harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A', 'request-B'] },
    {
      session_id: 'A',
      status: 'cancelled',
      error: 'backend cancellation details',
      request_ids: ['request-A', 'request-B']
    },
    'A'
  )

  assert.equal(result?.terminal, true)
  assert.equal(result?.succeeded, false)
  assert.deepEqual(harness.messageProcessorErrors, [])
  assert.deepEqual(harness.errors, [])
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'queued').status, undefined)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'history').content, 'historical answer')
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.equal(harness.pendingHttpRequests.has('request-A'), false)
  assert.equal(harness.pendingHttpRequests.has('request-B'), false)
  assert.equal(harness.pendingHttpRequests.has('request-other'), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)
  assert.equal(harness.resolvedHttpWorks.has('work-A'), true)
})

test('failed HTTP work status reports its actual error once before finishing cleanup', async () => {
  const implementation = await loadImplementation()
  const harness = createHarness(implementation, {
    sessions: [{ session_id: 'A', source: 'http' }],
    currentSessionId: 'A',
    inputMsg: 'keep this draft',
    messages: [
      { id: 'history', db_id: 42, role: 'assistant', content: 'historical answer' },
      { id: 'queued', role: 'user', content: 'queued prompt', request_id: 'request-A', status: 'queued' },
      { id: 'thinking', role: 'thinking', content: 'partial reply', work_id: 'work-A', request_ids: ['request-A', 'request-B'] }
    ],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }],
      ['request-B', { sessionId: 'A', workId: 'work-A' }],
      ['request-other', { sessionId: 'B', workId: 'work-B' }]
    ]
  })

  const result = harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A', 'request-B'] },
    {
      session_id: 'A',
      status: 'failed',
      error: 'backend failure details',
      request_ids: ['request-A', 'request-B']
    },
    'A'
  )

  assert.equal(result?.terminal, true)
  assert.equal(result?.succeeded, false)
  assert.equal(harness.messageProcessorErrors.length, 1)
  assert.equal(harness.messageProcessorErrors[0][1], 'backend failure details')
  assert.deepEqual(harness.errors, ['backend failure details'])
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'queued').status, undefined)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'history').content, 'historical answer')
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.equal(harness.pendingHttpRequests.has('request-A'), false)
  assert.equal(harness.pendingHttpRequests.has('request-B'), false)
  assert.equal(harness.pendingHttpRequests.has('request-other'), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)
  assert.equal(harness.resolvedHttpWorks.has('work-A'), true)
})
