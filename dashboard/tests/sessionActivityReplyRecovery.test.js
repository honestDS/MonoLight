import assert from 'node:assert/strict'
import test from 'node:test'

import { createHttpReplyPolling } from '../src/composables/chat/httpReplyPolling.js'
import { createSessionActivityController } from '../src/composables/chat/sessionActivity.js'
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

const createWaitQueue = () => {
  const waiters = []
  return {
    next() {
      const deferred = createDeferred()
      waiters.push(deferred)
      return deferred.promise
    },
    resolve(value) {
      waiters.shift()?.resolve(value)
    },
    reject(error) {
      waiters.shift()?.reject(error)
    }
  }
}

const makeSession = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  source: 'http',
  latest_message_id: 1,
  last_active: '2026-10-08 00:00:00',
  is_reply_running: false,
  reply_works: [],
  ...overrides
})

const makeActivity = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  latest_message_id: 1,
  last_active: '2026-10-08 00:00:00',
  source: 'http',
  is_reply_running: false,
  ...overrides
})

const makeMessages = () => [
  { id: 'history', db_id: 1, role: 'assistant', content: 'history' },
  {
    id: 'user-A',
    role: 'user',
    content: 'prompt',
    request_id: 'request-A',
    status: 'queued'
  },
  {
    id: 'thinking-A',
    role: 'thinking',
    content: '',
    work_id: 'work-A',
    request_ids: ['request-A']
  }
]

const createHarness = ({
  initialSessions = [makeSession('A')],
  initialMessages = makeMessages(),
  currentSessionId = 'A',
  activityRounds = [],
  fullSnapshots = [],
  replyWorkStatusBehavior = () => ({
    session_id: 'A',
    status: 'succeeded',
    request_ids: ['request-A'],
    response: { request_ids: ['request-A'], content: 'answer' }
  })
} = {}) => {
  const sessions = { value: initialSessions }
  const currentSession = { value: currentSessionId }
  const transport = { transportMode: { value: 'http' } }
  const chatState = {
    messages: { value: initialMessages },
    loading: { value: true }
  }
  const initialHistoryLoaded = { value: true }
  const pendingHttpRequests = new Map([
    ['request-A', { sessionId: 'A', workId: 'work-A' }],
    ['request-other', { sessionId: 'B', workId: 'work-B' }]
  ])
  const workLifecycleTracker = createWorkLifecycleTracker()
  const isCurrentSessionReadOnly = {
    get value() {
      const session = sessions.value.find(item => item?.session_id === currentSession.value)
      return Boolean(session?.source && !['http', 'ws'].includes(session.source))
    }
  }
  const activityWaiters = createWaitQueue()
  const snapshotWaiters = createWaitQueue()
  const activityUpdates = []
  const snapshotUpdates = []
  const errors = []
  const streamErrorCalls = []
  const replyWorkStatusCalls = []
  const renderedContents = []
  const historyMergeCalls = []
  let activityCallCount = 0
  let fullCallCount = 0
  let httpPolling

  const fetchActivity = () => {
    activityCallCount += 1
    const index = Math.min(activityCallCount - 1, activityRounds.length - 1)
    return activityRounds[index] || []
  }
  const fetchSessions = () => {
    fullCallCount += 1
    const index = Math.min(fullCallCount - 1, fullSnapshots.length - 1)
    return fullSnapshots[index] || sessions.value
  }

  const sessionManager = {
    sessions,
    currentSessionId: currentSession
  }
  const messageProcessor = {
    processStreamError: (...args) => {
      streamErrorCalls.push(args)
      return processStreamError(...args)
    }
  }
  const api = {
    replyWorkStatus: workId => {
      replyWorkStatusCalls.push(workId)
      return Promise.resolve(replyWorkStatusBehavior(workId, replyWorkStatusCalls.length))
        .then(data => ({ data: { data } }))
    }
  }

  const handleSessionsUpdated = snapshot => {
    snapshotUpdates.push(snapshot)
    const processing = httpPolling.processHttpSessionSnapshot(snapshot)
    processing.then(snapshotWaiters.resolve, snapshotWaiters.reject)
    return processing
  }
  const handleActivityUpdated = activities => {
    activityUpdates.push(activities)
    activityWaiters.resolve(activities)
    if (isCurrentSessionReadOnly.value) {
      return httpPolling.processHttpSessionSnapshot(activities)
    }
    return undefined
  }

  httpPolling = createHttpReplyPolling({
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
    shouldProcessCompletedWork: () => true,
    processAiResponse: response => {
      renderedContents.push(response.content)
    },
    messageProcessor,
    api,
    mergeLatestSessionHistory: async sessionId => {
      historyMergeCalls.push(sessionId)
    },
    getHistoryCursor: () => 1,
    mergeIncrementalSessionHistory: async () => ({ hasMore: false }),
    reportError: error => errors.push(error),
    translate: key => key
  })

  const controller = createSessionActivityController({
    fetchSessions,
    fetchActivity,
    getSessions: () => sessions.value,
    setSessions: value => {
      sessions.value = value
    },
    getCurrentSessionId: () => currentSession.value,
    onSessionsUpdated: handleSessionsUpdated,
    onActivityUpdated: handleActivityUpdated,
    onError: error => errors.push(error)
  })

  return {
    activityUpdates,
    chatState,
    controller,
    currentSession,
    errors,
    get activityCallCount() {
      return activityCallCount
    },
    get fullCallCount() {
      return fullCallCount
    },
    historyMergeCalls,
    initialHistoryLoaded,
    isCurrentSessionReadOnly,
    nextActivityUpdate: activityWaiters.next,
    nextSnapshotUpdate: snapshotWaiters.next,
    pendingHttpRequests,
    polling: httpPolling,
    renderedContents,
    replyWorkStatusCalls,
    sessionManager,
    sessions,
    snapshotUpdates,
    streamErrorCalls,
    transport,
    workLifecycleTracker
  }
}

const runningWork = {
  work_id: 'work-A',
  status: 'running',
  request_ids: ['request-A']
}

const terminalWork = status => ({
  work_id: 'work-A',
  status,
  request_ids: ['request-A']
})

const terminalStatus = status => ({
  session_id: 'A',
  status,
  request_ids: ['request-A'],
  ...(status === 'succeeded'
    ? { response: { request_ids: ['request-A'], content: 'answer-A' } }
    : status === 'failed'
      ? { error: 'backend failure' }
      : {})
})

test('selected HTTP Web activity edges recover one complete snapshot and finish every terminal work status', async () => {
  for (const status of ['succeeded', 'failed', 'cancelled']) {
    const harness = createHarness({
      activityRounds: [
        [makeActivity('A', { is_reply_running: true })],
        [makeActivity('A', { is_reply_running: true })],
        [makeActivity('A', { is_reply_running: true })],
        [makeActivity('A', { is_reply_running: false })],
        [makeActivity('A', { is_reply_running: false })]
      ],
      fullSnapshots: [
        [makeSession('A', { is_reply_running: true, reply_works: [runningWork] })],
        [makeSession('A', { is_reply_running: false, reply_works: [terminalWork(status)] })]
      ],
      replyWorkStatusBehavior: () => terminalStatus(status)
    })

    const firstActivity = harness.nextActivityUpdate()
    const firstSnapshot = harness.nextSnapshotUpdate()
    const returnedActivity = await harness.controller.refreshActivity()
    await firstActivity
    await firstSnapshot

    assert.equal(Object.hasOwn(returnedActivity[0], 'reply_works'), false)
    assert.equal(Object.hasOwn(harness.activityUpdates[0][0], 'reply_works'), false)
    assert.equal(harness.snapshotUpdates[0][0].reply_works[0].status, 'running')
    assert.equal(harness.fullCallCount, 1)
    assert.deepEqual(harness.replyWorkStatusCalls, [])
    assert.equal(harness.chatState.loading.value, true)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)

    for (let index = 0; index < 2; index += 1) {
      const stableActivity = harness.nextActivityUpdate()
      await harness.controller.refreshActivity()
      await stableActivity
    }
    assert.equal(harness.fullCallCount, 1)
    assert.deepEqual(harness.replyWorkStatusCalls, [])

    const terminalActivity = harness.nextActivityUpdate()
    const terminalSnapshot = harness.nextSnapshotUpdate()
    await harness.controller.refreshActivity()
    await terminalActivity
    await terminalSnapshot

    assert.equal(harness.fullCallCount, 2)
    assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
    assert.equal(harness.chatState.loading.value, false)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
    assert.equal(harness.chatState.messages.value.find(message => message.id === 'user-A').status, undefined)
    assert.equal(harness.pendingHttpRequests.has('request-A'), false)
    assert.equal(harness.pendingHttpRequests.has('request-other'), true)
    assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)
    assert.equal(harness.renderedContents.length, status === 'succeeded' ? 1 : 0)
    assert.equal(harness.streamErrorCalls.length, status === 'failed' ? 1 : 0)
    assert.equal(harness.errors.length, status === 'failed' ? 1 : 0)
    assert.equal(
      harness.chatState.messages.value.filter(message => message.role === 'err').length,
      status === 'failed' ? 1 : 0
    )

    const stableTerminalActivity = harness.nextActivityUpdate()
    await harness.controller.refreshActivity()
    await stableTerminalActivity
    assert.equal(harness.fullCallCount, 2)
    assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
    assert.equal(harness.renderedContents.length, status === 'succeeded' ? 1 : 0)

    harness.controller.dispose()
  }
})

test('concurrent stable activity and duplicate snapshot processing fetch delayed work status once', async () => {
  const statusDeferred = createDeferred()
  const statusStarted = createDeferred()
  const harness = createHarness({
    activityRounds: [
      [makeActivity('A', { is_reply_running: true })],
      [makeActivity('A', { is_reply_running: false })],
      [makeActivity('A', { is_reply_running: false })]
    ],
    fullSnapshots: [
      [makeSession('A', { is_reply_running: true, reply_works: [runningWork] })],
      [makeSession('A', { is_reply_running: false, reply_works: [terminalWork('succeeded')] })]
    ],
    replyWorkStatusBehavior: workId => {
      statusStarted.resolve(workId)
      return statusDeferred.promise
    }
  })

  const firstActivity = harness.nextActivityUpdate()
  const firstSnapshot = harness.nextSnapshotUpdate()
  await harness.controller.refreshActivity()
  await firstActivity
  await firstSnapshot

  const terminalActivity = harness.nextActivityUpdate()
  const terminalSnapshot = harness.nextSnapshotUpdate()
  await harness.controller.refreshActivity()
  await terminalActivity
  await statusStarted.promise
  assert.equal(harness.fullCallCount, 2)
  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])

  const duplicateOne = harness.polling.processHttpSessionSnapshot(harness.sessions.value)
  const duplicateTwo = harness.polling.processHttpSessionSnapshot(harness.sessions.value)
  const stableActivity = harness.nextActivityUpdate()
  const firstConcurrentActivity = harness.controller.refreshActivity()
  const secondConcurrentActivity = harness.controller.refreshActivity()
  assert.strictEqual(secondConcurrentActivity, firstConcurrentActivity)
  await Promise.all([firstConcurrentActivity, secondConcurrentActivity])
  await stableActivity
  assert.equal(harness.fullCallCount, 2)
  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])

  statusDeferred.resolve(terminalStatus('succeeded'))
  await terminalSnapshot
  await Promise.all([duplicateOne, duplicateTwo])
  assert.equal(harness.replyWorkStatusCalls.length, 1)
  assert.deepEqual(harness.renderedContents, ['answer-A'])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(harness.pendingHttpRequests.has('request-A'), false)

  harness.controller.dispose()
})

test('unknown activity and selected Web transition share one complete refresh', async () => {
  const harness = createHarness({
    initialSessions: [makeSession('web')],
    currentSessionId: 'web',
    activityRounds: [
      [
        makeActivity('web', { is_reply_running: true }),
        makeActivity('unknown', { source: 'external' })
      ],
      [
        makeActivity('web', { is_reply_running: true }),
        makeActivity('unknown', { source: 'external' })
      ]
    ],
    fullSnapshots: [[
      makeSession('web', { is_reply_running: true, reply_works: [runningWork] }),
      makeSession('unknown', { source: 'external' })
    ]]
  })

  const activity = harness.nextActivityUpdate()
  const snapshot = harness.nextSnapshotUpdate()
  await harness.controller.refreshActivity()
  await activity
  await snapshot
  assert.equal(harness.fullCallCount, 1)
  assert.equal(harness.snapshotUpdates.length, 1)
  assert.equal(harness.activityUpdates[0].some(item => Object.hasOwn(item, 'reply_works')), false)

  const stableActivity = harness.nextActivityUpdate()
  await harness.controller.refreshActivity()
  await stableActivity
  assert.equal(harness.fullCallCount, 1)
  assert.deepEqual(harness.replyWorkStatusCalls, [])

  harness.controller.dispose()
})

test('late HTTP work replies after a session switch or polling reset do not mutate the current lifecycle', async () => {
  for (const transition of ['session', 'reset']) {
    const statusDeferred = createDeferred()
    const statusStarted = createDeferred()
    const harness = createHarness({
      activityRounds: [
        [makeActivity('A', { is_reply_running: true })],
        [makeActivity('A', { is_reply_running: false })]
      ],
      fullSnapshots: [
        [makeSession('A', { is_reply_running: true, reply_works: [runningWork] })],
        [makeSession('A', { is_reply_running: false, reply_works: [terminalWork('succeeded')] })]
      ],
      replyWorkStatusBehavior: workId => {
        statusStarted.resolve(workId)
        return statusDeferred.promise
      }
    })

    const firstActivity = harness.nextActivityUpdate()
    const firstSnapshot = harness.nextSnapshotUpdate()
    await harness.controller.refreshActivity()
    await firstActivity
    await firstSnapshot

    const terminalActivity = harness.nextActivityUpdate()
    const terminalSnapshot = harness.nextSnapshotUpdate()
    await harness.controller.refreshActivity()
    await terminalActivity
    await statusStarted.promise

    let expectedMessages
    if (transition === 'session') {
      harness.currentSession.value = 'B'
      harness.sessions.value = [makeSession('B', { is_reply_running: true })]
      harness.chatState.messages.value = [{
        id: 'thinking-B',
        role: 'thinking',
        content: '',
        work_id: 'work-B',
        request_ids: ['request-B']
      }]
      expectedMessages = harness.chatState.messages.value
    } else {
      expectedMessages = harness.chatState.messages.value
      harness.polling.resetHttpPollingState()
      assert.equal(harness.pendingHttpRequests.size, 0)
    }

    statusDeferred.resolve(terminalStatus('succeeded'))
    await terminalSnapshot

    assert.deepEqual(harness.renderedContents, [])
    assert.deepEqual(harness.errors, [])
    assert.deepEqual(harness.chatState.messages.value, expectedMessages)
    assert.equal(harness.chatState.loading.value, true)

    harness.controller.dispose()
  }
})
