import assert from 'node:assert/strict'
import test from 'node:test'
import * as Vue from 'vue'
import { createContextSummaryTracker } from '../src/composables/chat/contextSummaryTracker.js'
import { createHttpReplyPolling } from '../src/composables/chat/httpReplyPolling.js'
import { createReplyController } from '../src/composables/chat/replyControl.js'
import { createWorkLifecycleTracker } from '../src/composables/chat/workLifecycleTracker.js'
import { processStreamError } from '../src/composables/chat/terminalHistory.js'

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createHarness = (options = {}) => {
  const sessions = Vue.ref(options.sessions ?? [])
  const currentSessionId = Vue.ref(options.currentSessionId ?? null)
  const transport = {
    transportMode: Vue.ref(options.transportMode ?? 'http')
  }
  const chatState = {
    messages: Vue.ref(options.messages ?? []),
    loading: Vue.ref(options.loading ?? false),
    inputMsg: Vue.ref(options.inputMsg ?? '')
  }
  const attachments = Vue.ref(options.attachments ?? [])
  const contextSummaryWorkKeys = Vue.ref(new Set())
  const contextSummaryRequestKeys = new Map()
  const contextSummaryTracker = createContextSummaryTracker()
  const workLifecycleTracker = createWorkLifecycleTracker()
  const initialHistoryLoaded = Vue.ref(options.initialHistoryLoaded ?? true)
  const pendingHttpRequests = new Map(options.pendingHttpRequests ?? [])
  const refreshCalls = []
  const historyMergeCalls = []
  const stopCalls = []
  const errors = []
  const streamErrors = []
  const replyWorkStatusCalls = []
  let sessionScopeActive = options.sessionScopeActive ?? true
  let stopBehavior = options.stopBehavior ?? (async () => undefined)
  let replyWorkStatusBehavior = options.replyWorkStatusBehavior ?? (() => null)

  const sessionManager = {
    sessions,
    currentSessionId,
    refreshSessionLoadingState: async () => {
      refreshCalls.push(currentSessionId.value)
    }
  }
  const currentSession = Vue.computed(() => (
    sessions.value.find(session => session.session_id === currentSessionId.value) || null
  ))
  const isCurrentSessionReadOnly = Vue.computed(() => {
    const source = currentSession.value?.source
    return Boolean(source && !['http', 'ws'].includes(source))
  })

  const reportError = message => {
    errors.push(message)
  }
  const translate = key => key
  const mergeLatestSessionHistory = async sessionId => {
    historyMergeCalls.push(sessionId)
  }
  const stopSession = sessionId => {
    stopCalls.push(sessionId)
    return typeof stopBehavior === 'function' ? stopBehavior(sessionId) : stopBehavior
  }

  const messageProcessor = {
    processStreamError: (...args) => {
      streamErrors.push(args)
      return processStreamError(...args)
    }
  }

  const api = {
    replyWorkStatus: workId => {
      replyWorkStatusCalls.push(workId)
      return typeof replyWorkStatusBehavior === 'function'
        ? replyWorkStatusBehavior(workId)
        : replyWorkStatusBehavior
    }
  }
  const httpPolling = createHttpReplyPolling({
    transport,
    sessionManager,
    isCurrentSessionReadOnly,
    chatState,
    initialHistoryLoaded,
    pendingHttpRequests,
    workLifecycleTracker,
    applyTodoTransportPayload: () => undefined,
    updateLlmRequestMetadata: () => undefined,
    startHttpHistoryBackgroundTaskSync: () => undefined,
    applyNonStreamSessionEvents: () => undefined,
    shouldProcessCompletedWork: () => false,
    processAiResponse: () => undefined,
    messageProcessor,
    api,
    mergeLatestSessionHistory,
    reportError,
    translate
  })
  const replyController = createReplyController({
    sessionManager,
    chatState,
    currentSession,
    isCurrentSessionReadOnly,
    pendingHttpRequests,
    contextSummaryWorkKeys,
    contextSummaryRequestKeys,
    workLifecycleTracker,
    contextSummaryTracker,
    stopSession,
    isSessionScopeActive: () => sessionScopeActive,
    resetHttpPollingState: httpPolling.resetHttpPollingState,
    mergeLatestSessionHistory,
    reportError,
    translate
  })

  return {
    ...replyController,
    ...httpPolling,
    attachments,
    api,
    chatState,
    contextSummaryRequestKeys,
    contextSummaryTracker,
    contextSummaryWorkKeys,
    currentSessionId,
    errors,
    historyMergeCalls,
    initialHistoryLoaded,
    isCurrentSessionReadOnly,
    messageProcessorErrors: streamErrors,
    pendingHttpRequests,
    refreshCalls,
    replyWorkStatusCalls,
    sessions,
    set replyWorkStatusBehavior(value) {
      replyWorkStatusBehavior = value
    },
    set sessionScopeActive(value) {
      sessionScopeActive = value
    },
    set stopBehavior(value) {
      stopBehavior = value
    },
    stopCalls,
    transport,
    workLifecycleTracker
  }
}

test('stopReply clears active lifecycle state, preserves history and prevents late loop resurrection', async () => {
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
  const harness = createHarness({
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

test('stopReply sends only one stop and blocks repeats while stopping', async () => {
  const stopDeferred = createDeferred()
  const harness = createHarness({
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
  for (const shouldReject of [false, true]) {
    const stopDeferreds = []
    const secondStopCalled = createDeferred()
    const harness = createHarness({
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
  const submissionDeferred = createDeferred()
  let submissionSettled = false
  const harness = createHarness({
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
  trackedSubmission.then(() => {
    submissionSettled = true
  })

  harness.stopBehavior = async () => undefined
  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(submissionSettled, false)

  submissionDeferred.resolve('submitted')
  await trackedSubmission
  assert.equal(submissionSettled, true)
})

test('stopReply preserves loading, messages and draft when the stop request fails', async () => {
  const messages = [
    { id: 'queued', role: 'user', request_id: 'request-A', status: 'queued' },
    { id: 'thinking', role: 'thinking', request_ids: ['request-A'] }
  ]
  const harness = createHarness({
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
    const harness = createHarness(options)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, [])
  }
})

test('stopReply ignores aggregate-only busy sessions for HTTP and WS', async () => {
  for (const source of ['http', 'ws']) {
    const session = {
      session_id: 'A',
      source,
      is_loading: true,
      is_reply_running: false,
      reply_works: []
    }
    const harness = createHarness({
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
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: true,
    is_reply_running: false,
    reply_works: []
  }
  const harness = createHarness({
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
  for (const source of ['http', 'ws']) {
    const session = {
      session_id: 'A',
      source,
      is_loading: true,
      is_reply_running: true,
      reply_works: []
    }
    const harness = createHarness({
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
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: false,
    is_reply_running: false,
    reply_works: []
  }
  const harness = createHarness({
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
  const stopA = createDeferred()
  const sessionA = { session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }
  const sessionB = { session_id: 'B', source: 'http', is_loading: true, is_reply_running: true }
  const harness = createHarness({
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

test('stopReply does not clear current state after the session scope is disposed', async () => {
  const stopDeferred = createDeferred()
  const session = { session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }]
  })
  harness.stopBehavior = () => stopDeferred.promise

  const stopping = harness.stopReply()
  harness.sessionScopeActive = false
  stopDeferred.resolve()

  assert.equal(await stopping, true)
  assert.equal(harness.chatState.loading.value, true)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)
  assert.equal(session.is_reply_running, true)
  assert.deepEqual(harness.historyMergeCalls, [])
})

test('stopReply terminates compression tracked only by a work key', async () => {
  const harness = createHarness({
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
  const harness = createHarness({
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
  const harness = createHarness({
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
})

test('failed HTTP work status reports its actual error once and cleans up lifecycle state', async () => {
  const harness = createHarness({
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

  const statusData = {
    session_id: 'A',
    status: 'failed',
    error: 'backend failure details',
    result_message_id: 100,
    request_ids: ['request-A', 'request-B']
  }
  const result = harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A', 'request-B'] },
    statusData,
    'A'
  )

  assert.equal(result?.terminal, true)
  assert.equal(result?.succeeded, false)
  assert.equal(harness.messageProcessorErrors.length, 1)
  assert.equal(harness.messageProcessorErrors[0][1], 'backend failure details')
  assert.deepEqual(harness.errors, ['backend failure details'])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'queued').status, undefined)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'history').content, 'historical answer')
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.equal(harness.pendingHttpRequests.has('request-A'), false)
  assert.equal(harness.pendingHttpRequests.has('request-B'), false)
  assert.equal(harness.pendingHttpRequests.has('request-other'), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)

  harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A', 'request-B'] },
    statusData,
    'A'
  )
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
  assert.deepEqual(harness.errors, ['backend failure details'])
})

test('failed HTTP status does not duplicate an existing error result message', async () => {
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http' }],
    currentSessionId: 'A',
    messages: [
      { id: 'thinking', role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] },
      { id: 'existing-error', role: 'err', db_id: 77, content: 'already recorded' }
    ],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }]
    ]
  })

  const result = harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A'] },
    {
      session_id: 'A',
      status: 'failed',
      error: 'backend failure details',
      result_message_id: 77,
      request_ids: ['request-A']
    },
    'A'
  )

  assert.equal(result?.terminal, true)
  assert.deepEqual(harness.messageProcessorErrors, [])
  assert.deepEqual(harness.errors, [])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
})

test('HTTP terminal status is fetched once and duplicate snapshots do not report a second error', async () => {
  const session = {
    session_id: 'A',
    source: 'http',
    is_reply_running: false,
    reply_works: [
      { work_id: 'work-A', status: 'failed', request_ids: ['request-A'] }
    ]
  }
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }]
    ]
  })
  harness.replyWorkStatusBehavior = async workId => ({
    data: {
      data: {
        session_id: 'A',
        status: 'failed',
        error: `failure for ${workId}`,
        request_ids: ['request-A']
      }
    }
  })

  await harness.processHttpSessionSnapshot([session])
  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
  assert.deepEqual(harness.errors, ['failure for work-A'])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)

  await harness.processHttpSessionSnapshot([session])
  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
  assert.deepEqual(harness.errors, ['failure for work-A'])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
})

test('HTTP status from another session or transport mode is not applied', async () => {
  const session = {
    session_id: 'A',
    source: 'http',
    reply_works: [{ work_id: 'work-A', request_ids: ['request-A'] }]
  }
  const messages = [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }]
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    messages,
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }]
    ]
  })
  const status = {
    status: 'failed',
    error: 'should not be applied',
    request_ids: ['request-A']
  }

  assert.equal(
    harness.applyHttpWorkStatus({ work_id: 'work-A', request_ids: ['request-A'] }, {
      ...status,
      session_id: 'B'
    }, 'A'),
    null
  )
  assert.deepEqual(harness.chatState.messages.value, messages)
  assert.equal(harness.pendingHttpRequests.has('request-A'), true)

  harness.transport.transportMode.value = 'ws'
  assert.equal(
    harness.applyHttpWorkStatus({ work_id: 'work-A', request_ids: ['request-A'] }, {
      ...status,
      session_id: 'A'
    }, 'A'),
    null
  )
  assert.deepEqual(harness.chatState.messages.value, messages)
  assert.equal(harness.pendingHttpRequests.has('request-A'), true)
  assert.deepEqual(harness.errors, [])
})

test('HTTP status queries returning after reset or session/mode changes are ignored', async () => {
  for (const transition of ['reset', 'session', 'mode']) {
    const statusDeferred = createDeferred()
    const sessionA = {
      session_id: 'A',
      source: 'http',
      is_reply_running: false,
      reply_works: [{ work_id: 'work-A', status: 'failed', request_ids: ['request-A'] }]
    }
    const sessionB = {
      session_id: 'B',
      source: 'http',
      is_reply_running: false,
      reply_works: []
    }
    const harness = createHarness({
      sessions: [sessionA, sessionB],
      currentSessionId: 'A',
      messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }],
      pendingHttpRequests: [
        ['request-A', { sessionId: 'A', workId: 'work-A' }]
      ]
    })
    harness.replyWorkStatusBehavior = () => statusDeferred.promise

    const processing = harness.processHttpSessionSnapshot([sessionA])
    assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
    if (transition === 'reset') {
      harness.resetHttpPollingState()
    } else if (transition === 'session') {
      harness.currentSessionId.value = 'B'
    } else {
      harness.transport.transportMode.value = 'ws'
    }

    statusDeferred.resolve({
      data: {
        data: {
          session_id: 'A',
          status: 'failed',
          error: 'late failure',
          request_ids: ['request-A']
        }
      }
    })
    await processing

    assert.deepEqual(harness.errors, [])
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'err'), false)
  }
})

test('processStreamError deduplicates event IDs and omits invalid message IDs', () => {
  const messages = Vue.ref([])

  assert.equal(
    processStreamError(messages, 'first error', null, 'request-A', 'work-A', 'event-A', 'invalid'),
    true
  )
  assert.equal(messages.value[0].role, 'err')
  assert.equal(Object.hasOwn(messages.value[0], 'db_id'), false)

  assert.equal(
    processStreamError(messages, 'duplicate error', null, 'request-B', 'work-B', 'event-A', 17),
    false
  )
  assert.equal(messages.value.length, 1)
})
