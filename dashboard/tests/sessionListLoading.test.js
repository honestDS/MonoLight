import assert from 'node:assert/strict'
import test from 'node:test'
import { createHttpReplyPolling } from '../src/composables/chat/httpReplyPolling.js'
import { getIncrementalHistoryCursor, syncIncrementalHistory } from '../src/composables/chat/historyIncrementalSync.js'

const loadSessionLoadingModule = () => import('../src/composables/chat/sessionListLoading.js')

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createManualTimerQueue = () => {
  const pending = []
  const handles = []
  const cancelled = []

  const schedule = (callback, delay) => {
    const handle = { callback, delay }
    pending.push(handle)
    handles.push(handle)
    return handle
  }

  const cancel = handle => {
    cancelled.push(handle)
    const index = pending.indexOf(handle)
    if (index !== -1) pending.splice(index, 1)
  }

  const tick = async () => {
    const handle = pending.shift()
    assert.ok(handle, 'expected a scheduled timer')
    return handle.callback()
  }

  return { pending, handles, cancelled, schedule, cancel, tick }
}

const createIncrementalHistoryMerger = ({
  fetchPage,
  pageSize = 50,
  maxPages = 4,
  initialLastMessageIds = new Map(),
  getMessages = () => []
}) => {
  const lastMessageIds = new Map(initialLastMessageIds)
  const mergedBySession = new Map()

  const mergeLatestHistory = async (sessionId, isCurrentSync) => {
    const result = await syncIncrementalHistory({
      initialAfterId: getIncrementalHistoryCursor(
        getMessages(sessionId),
        lastMessageIds.get(sessionId)
      ),
      pageSize,
      maxPages,
      fetchPage: request => fetchPage(sessionId, request),
      mergePage: page => {
        const merged = mergedBySession.get(sessionId) || []
        merged.push(...page)
        mergedBySession.set(sessionId, merged)
      },
      isCurrent: isCurrentSync
    })

    if (!result.cancelled && isCurrentSync()) {
      lastMessageIds.set(sessionId, result.lastMessageId)
    }
    return result
  }

  return { lastMessageIds, mergedBySession, mergeLatestHistory }
}

const createExternalHttpPollingFixture = ({
  currentSessionId = 'external',
  readOnly = true,
  transportMode = 'http',
  initialHistoryLoaded = true,
  loading = false,
  messages = [],
  initialLastMessageIds = new Map(),
  fetchPage = () => [],
  pageSize = 50,
  maxPages = 4,
  pendingHttpRequests = new Map()
} = {}) => {
  const state = {
    transport: { transportMode: { value: transportMode } },
    sessionManager: {
      currentSessionId: { value: currentSessionId },
      sessions: { value: [] }
    },
    isCurrentSessionReadOnly: { value: readOnly },
    chatState: {
      messages: { value: messages },
      loading: { value: loading }
    },
    initialHistoryLoaded: { value: initialHistoryLoaded },
    pendingHttpRequests
  }
  const merger = createIncrementalHistoryMerger({
    fetchPage,
    pageSize,
    maxPages,
    initialLastMessageIds,
    getMessages: () => state.chatState.messages.value
  })
  const failWritable = action => () => assert.fail(`unexpected writable HTTP action: ${action}`)
  const polling = createHttpReplyPolling({
    transport: state.transport,
    sessionManager: state.sessionManager,
    isCurrentSessionReadOnly: state.isCurrentSessionReadOnly,
    chatState: state.chatState,
    initialHistoryLoaded: state.initialHistoryLoaded,
    pendingHttpRequests: state.pendingHttpRequests,
    workLifecycleTracker: {
      finishWorkLifecycle: failWritable('finishWorkLifecycle')
    },
    applyTodoTransportPayload: failWritable('applyTodoTransportPayload'),
    updateLlmRequestMetadata: failWritable('updateLlmRequestMetadata'),
    startHttpHistoryBackgroundTaskSync: failWritable('startHttpHistoryBackgroundTaskSync'),
    applyNonStreamSessionEvents: failWritable('applyNonStreamSessionEvents'),
    shouldProcessCompletedWork: failWritable('shouldProcessCompletedWork'),
    processAiResponse: failWritable('processAiResponse'),
    messageProcessor: {
      processStreamError: failWritable('processStreamError')
    },
    api: {
      replyWorkStatus: failWritable('replyWorkStatus')
    },
    mergeLatestSessionHistory: failWritable('mergeLatestSessionHistory'),
    getHistoryCursor: sessionId => getIncrementalHistoryCursor(
      state.chatState.messages.value,
      merger.lastMessageIds.get(sessionId)
    ),
    mergeIncrementalSessionHistory: merger.mergeLatestHistory,
    reportError: failWritable('reportError'),
    translate: key => key
  })

  return { ...state, merger, polling }
}

test('HTTP work recovery fetches a terminal result after observing the work in progress and reuses persisted failures', async () => {
  const { hasHttpResultMessage, shouldFetchHttpWorkStatus } = await loadSessionLoadingModule()

  assert.equal(shouldFetchHttpWorkStatus({
    status: 'running',
    previousStatus: undefined,
    hasPendingRequest: false,
    historyLoaded: true,
    sessionLoading: true,
    resolved: false,
    fetching: false
  }), false)

  assert.equal(shouldFetchHttpWorkStatus({
    status: 'failed',
    previousStatus: 'running',
    hasPendingRequest: false,
    historyLoaded: true,
    sessionLoading: false,
    resolved: false,
    fetching: false
  }), true)

  const history = [
    { role: 'user', db_id: 40, content: 'question' },
    { role: 'err', db_id: 41, content: 'worker failed' }
  ]
  assert.equal(hasHttpResultMessage(history, 41), true)
  assert.equal(hasHttpResultMessage(history, '41'), true)
  assert.equal(hasHttpResultMessage(history, 42), false)
})

test('shouldFetchHttpWorkStatus uses the observed work state to decide whether to fetch', async () => {
  const { shouldFetchHttpWorkStatus } = await loadSessionLoadingModule()
  const cases = [
    {
      name: 'does not fetch an initially completed old work',
      input: {
        status: 'succeeded',
        previousStatus: undefined,
        hasPendingRequest: false,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'does not fetch before history is loaded',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: false,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'fetches a pending request that succeeded',
      input: {
        status: 'succeeded',
        previousStatus: undefined,
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'fetches a pending request that failed',
      input: {
        status: 'failed',
        previousStatus: undefined,
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'fetches an observed active work that succeeded',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: false,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'does not fetch merged work while the session is loading',
      input: {
        status: 'merged',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: true,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'fetches merged work after the session becomes idle',
      input: {
        status: 'merged',
        previousStatus: 'running',
        hasPendingRequest: false,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'does not fetch an already resolved work',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: true,
        fetching: false
      },
      expected: false
    },
    {
      name: 'does not fetch work that is already being fetched',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: true
      },
      expected: false
    },
    {
      name: 'does not fetch a non-terminal work',
      input: {
        status: 'running',
        previousStatus: undefined,
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'does not fetch a malformed status',
      input: {
        status: null,
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    }
  ]

  for (const { name, input, expected } of cases) {
    assert.equal(shouldFetchHttpWorkStatus(input), expected, name)
  }
})

test('session list poller keeps refreshing after a busy session becomes idle', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  const results = [
    [{ session_id: 'a', is_loading: true }],
    [{ session_id: 'a', is_loading: false }],
    []
  ]
  const observed = []

  const poller = createSessionListPoller({
    refreshSessions: async () => {
      const result = results.shift() || []
      observed.push(result)
      return result
    },
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  assert.deepEqual(await poller.refreshNow(), [{ session_id: 'a', is_loading: true }])
  assert.equal(timers.pending.length, 1)
  assert.equal(timers.pending[0].delay, 1500)

  await timers.tick()
  assert.deepEqual(observed[1], [{ session_id: 'a', is_loading: false }])
  assert.equal(timers.pending.length, 1)

  await timers.tick()
  assert.deepEqual(observed[2], [])
  assert.equal(timers.pending.length, 1)
  assert.equal(poller.isPolling(), true)
  poller.dispose()
})

test('session list poller keeps refreshing empty lists so external sessions are discovered', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  const results = [
    [],
    [{ session_id: 'external', is_loading: false }],
    []
  ]
  const observed = []

  const poller = createSessionListPoller({
    refreshSessions: async () => {
      const result = results.shift() || []
      observed.push(result)
      return result
    },
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  assert.deepEqual(await poller.refreshNow(), [])
  assert.equal(timers.pending.length, 1)

  await timers.tick()
  assert.deepEqual(observed[1], [{ session_id: 'external', is_loading: false }])
  assert.equal(timers.pending.length, 1)

  await timers.tick()
  assert.equal(observed.length, 3)
  assert.equal(timers.pending.length, 1)
  poller.dispose()
})

test('session list poller keeps one timer after repeated sync and manual refresh requests', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  let refreshCount = 0

  const poller = createSessionListPoller({
    refreshSessions: async () => {
      refreshCount += 1
      return []
    },
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  poller.sync()
  poller.sync()
  poller.sync()
  assert.equal(timers.pending.length, 1)
  assert.equal(timers.pending[0].delay, 1500)

  await poller.refreshNow()
  assert.equal(refreshCount, 1)
  assert.equal(timers.pending.length, 1)

  await poller.refreshNow()
  assert.equal(refreshCount, 2)
  assert.equal(timers.pending.length, 1)
  poller.dispose()
})

test('HTTP background task recovery is rediscovered after switching away and back to the session', async () => {
  const { createHttpHistorySyncController } = await import('../src/composables/chat/httpHistorySync.js')
  const scheduled = []
  const cancelled = []
  const taskStates = new Map([
    ['session-a', true],
    ['session-b', false]
  ])
  const fetches = []
  const mergedSessions = []
  let currentSessionId = 'session-a'

  const controller = createHttpHistorySyncController({
    getSessionId: () => currentSessionId,
    canSync: () => true,
    isLoading: () => false,
    fetchPendingActivity: async sessionId => {
      fetches.push(sessionId)
      return taskStates.get(sessionId) === true
    },
    mergeLatestHistory: async sessionId => {
      mergedSessions.push(sessionId)
    },
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      cancelled.push(callback)
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  await controller.handleSessionChanged()
  assert.deepEqual(fetches, ['session-a'])
  assert.equal(controller.isTracking('session-a'), true)
  assert.equal(scheduled.length, 1)

  currentSessionId = 'session-b'
  await controller.handleSessionChanged()
  assert.deepEqual(fetches, ['session-a', 'session-b'])
  assert.equal(controller.isTracking('session-a'), false)
  assert.equal(scheduled.length, 0)
  assert.deepEqual(mergedSessions, ['session-b'])
  assert.equal(cancelled.length, 1)

  currentSessionId = 'session-a'
  await controller.handleSessionChanged()
  assert.deepEqual(fetches, ['session-a', 'session-b', 'session-a'])
  assert.equal(controller.isTracking('session-a'), true)
  assert.equal(scheduled.length, 1)

  taskStates.set('session-a', false)
  const nextPoll = scheduled.shift()
  await nextPoll()

  assert.deepEqual(fetches, ['session-a', 'session-b', 'session-a', 'session-a'])
  assert.deepEqual(mergedSessions, ['session-b', 'session-a'])
  assert.equal(controller.isTracking('session-a'), false)
  assert.equal(scheduled.length, 0)
})

test('HTTP background task recovery ignores a stale result after the selected session changes', async () => {
  const { createHttpHistorySyncController } = await import('../src/composables/chat/httpHistorySync.js')
  let currentSessionId = 'session-a'
  let resolveSessionA
  const sessionAResponse = new Promise(resolve => {
    resolveSessionA = resolve
  })
  const fetches = []
  const mergedSessions = []
  const scheduled = []

  const controller = createHttpHistorySyncController({
    getSessionId: () => currentSessionId,
    canSync: () => true,
    isLoading: () => false,
    fetchPendingActivity: async sessionId => {
      fetches.push(sessionId)
      if (sessionId === 'session-a') return sessionAResponse
      return false
    },
    mergeLatestHistory: async sessionId => {
      mergedSessions.push(sessionId)
    },
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  const staleSync = controller.handleSessionChanged()
  currentSessionId = 'session-b'
  await controller.handleSessionChanged()
  resolveSessionA(true)
  await staleSync

  assert.deepEqual(fetches, ['session-a', 'session-b'])
  assert.deepEqual(mergedSessions, ['session-b'])
  assert.equal(controller.isTracking('session-a'), false)
  assert.equal(controller.isTracking('session-b'), false)
  assert.equal(scheduled.length, 0)
})

test('HTTP background task recovery retries after a transient activity lookup failure', async () => {
  const { createHttpHistorySyncController } = await import('../src/composables/chat/httpHistorySync.js')
  const scheduled = []
  const errors = []
  let attempts = 0

  const controller = createHttpHistorySyncController({
    getSessionId: () => 'session-a',
    canSync: () => true,
    isLoading: () => false,
    fetchPendingActivity: async () => {
      attempts += 1
      if (attempts === 1) throw new Error('temporary failure')
      return true
    },
    mergeLatestHistory: async () => {},
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    },
    onError: error => {
      errors.push(error.message)
    }
  })

  await controller.handleSessionChanged()
  assert.equal(attempts, 1)
  assert.deepEqual(errors, ['temporary failure'])
  assert.equal(controller.isTracking('session-a'), true)
  assert.equal(scheduled.length, 1)

  await scheduled.shift()()
  assert.equal(attempts, 2)
  assert.equal(controller.isTracking('session-a'), true)
  assert.equal(scheduled.length, 1)
})

test('HTTP background task recovery keeps polling until bounded incremental history is fully merged', async () => {
  const { createHttpHistorySyncController } = await import('../src/composables/chat/httpHistorySync.js')
  const scheduled = []
  let mergeCalls = 0

  const controller = createHttpHistorySyncController({
    getSessionId: () => 'session-a',
    canSync: () => true,
    isLoading: () => false,
    fetchPendingActivity: async () => false,
    mergeLatestHistory: async () => {
      mergeCalls += 1
      return { hasMore: mergeCalls === 1 }
    },
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  await controller.handleSessionChanged()
  assert.equal(mergeCalls, 1)
  assert.equal(controller.isTracking('session-a'), true)
  assert.equal(scheduled.length, 1)

  await scheduled.shift()()
  assert.equal(mergeCalls, 2)
  assert.equal(controller.isTracking('session-a'), false)
  assert.equal(scheduled.length, 0)
})

test('HTTP background task recovery initializes tracking before a loading session can skip polling work', async () => {
  const { createHttpHistorySyncController } = await import('../src/composables/chat/httpHistorySync.js')
  const scheduled = []
  const tracked = []
  let activityFetches = 0

  const controller = createHttpHistorySyncController({
    getSessionId: () => 'session-a',
    canSync: () => true,
    isLoading: () => true,
    onTrackingStarted: sessionId => {
      tracked.push(sessionId)
    },
    fetchPendingActivity: async () => {
      activityFetches += 1
      return true
    },
    mergeLatestHistory: async () => ({ hasMore: false }),
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: () => {}
  })

  await controller.handleSessionChanged()

  assert.deepEqual(tracked, ['session-a'])
  assert.equal(activityFetches, 0)
  assert.equal(controller.isTracking('session-a'), true)
  assert.equal(scheduled.length, 1)
})

test('session list poller serializes in-flight refreshes and runs one trailing refresh', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  const firstResult = createDeferred()
  const secondResult = createDeferred()
  const secondStarted = createDeferred()
  let refreshCount = 0
  let activeRefreshes = 0
  let maxActiveRefreshes = 0

  const poller = createSessionListPoller({
    refreshSessions: async () => {
      refreshCount += 1
      activeRefreshes += 1
      maxActiveRefreshes = Math.max(maxActiveRefreshes, activeRefreshes)
      if (refreshCount === 2) secondStarted.resolve()
      const result = refreshCount === 1 ? firstResult.promise : secondResult.promise
      try {
        return await result
      } finally {
        activeRefreshes -= 1
      }
    },
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  const firstRefresh = poller.refreshNow()
  const secondRefresh = poller.refreshNow()
  const thirdRefresh = poller.refreshNow()
  assert.equal(refreshCount, 1)
  assert.equal(maxActiveRefreshes, 1)

  firstResult.resolve([{ session_id: 'a', is_loading: true }])
  await secondStarted.promise
  assert.equal(refreshCount, 2)
  assert.equal(maxActiveRefreshes, 1)

  secondResult.resolve([{ session_id: 'a', is_loading: false }])
  await Promise.all([firstRefresh, secondRefresh, thirdRefresh])
  assert.equal(refreshCount, 2)
  assert.equal(maxActiveRefreshes, 1)
  assert.equal(timers.pending.length, 1)
  poller.dispose()
})

test('session list poller rejects a failed manual refresh and schedules the next round', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  const firstResult = createDeferred()
  const failure = new Error('manual refresh failed')
  let refreshCount = 0

  const poller = createSessionListPoller({
    refreshSessions: async () => {
      refreshCount += 1
      if (refreshCount === 1) return firstResult.promise
      return []
    },
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  const pendingRefresh = poller.refreshNow()
  firstResult.reject(failure)
  await assert.rejects(pendingRefresh, error => error === failure)
  assert.equal(refreshCount, 1)
  assert.equal(timers.pending.length, 1)
  assert.equal(timers.pending[0].delay, 1500)

  await timers.tick()
  assert.equal(refreshCount, 2)
  assert.equal(timers.pending.length, 1)
  poller.dispose()
})

test('session list poller reports automatic failures and continues after recovery', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  const failure = new Error('automatic refresh failed')
  const errors = []
  let refreshCount = 0

  const poller = createSessionListPoller({
    refreshSessions: async () => {
      refreshCount += 1
      if (refreshCount === 2) throw failure
      return [{ session_id: 'external', is_loading: false }]
    },
    schedule: timers.schedule,
    cancel: timers.cancel,
    onError: error => errors.push(error)
  })

  await poller.refreshNow()
  await timers.tick()
  assert.equal(refreshCount, 2)
  assert.deepEqual(errors, [failure])
  assert.equal(timers.pending.length, 1)

  await timers.tick()
  assert.equal(refreshCount, 3)
  assert.deepEqual(errors, [failure])
  assert.equal(timers.pending.length, 1)
  poller.dispose()
})

test('session list poller dispose is idempotent and prevents queued or in-flight restarts', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const queuedTimers = createManualTimerQueue()
  let queuedRefreshes = 0
  const queuedPoller = createSessionListPoller({
    refreshSessions: async () => {
      queuedRefreshes += 1
      return []
    },
    schedule: queuedTimers.schedule,
    cancel: queuedTimers.cancel
  })

  await queuedPoller.refreshNow()
  const queuedCallback = queuedTimers.pending[0].callback
  queuedPoller.dispose()
  queuedPoller.dispose()
  assert.equal(queuedTimers.cancelled.length, 1)
  assert.equal(queuedTimers.pending.length, 0)

  await queuedCallback()
  assert.equal(queuedRefreshes, 1)
  assert.equal(queuedTimers.pending.length, 0)
  assert.equal(queuedPoller.isPolling(), false)

  const successTimers = createManualTimerQueue()
  const successResult = createDeferred()
  const successPoller = createSessionListPoller({
    refreshSessions: () => successResult.promise,
    schedule: successTimers.schedule,
    cancel: successTimers.cancel
  })
  const pendingSuccess = successPoller.refreshNow()
  successPoller.dispose()
  successPoller.dispose()
  successResult.resolve([{ session_id: 'a', is_loading: true }])
  await pendingSuccess
  assert.equal(successTimers.pending.length, 0)

  const failureTimers = createManualTimerQueue()
  const failureResult = createDeferred()
  const inFlightFailure = new Error('in-flight refresh failed')
  const failurePoller = createSessionListPoller({
    refreshSessions: () => failureResult.promise,
    schedule: failureTimers.schedule,
    cancel: failureTimers.cancel
  })
  const pendingFailure = failurePoller.refreshNow()
  failurePoller.dispose()
  failurePoller.dispose()
  failureResult.reject(inFlightFailure)
  await assert.rejects(pendingFailure, error => error === inFlightFailure)
  assert.equal(failureTimers.pending.length, 0)
})

test('session list snapshots drive external readonly history without blocking list polling', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  const historyResponse = createDeferred()
  const fetches = []
  let activeFetches = 0
  let maxActiveFetches = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: async (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      activeFetches += 1
      maxActiveFetches = Math.max(maxActiveFetches, activeFetches)
      try {
        return await historyResponse.promise
      } finally {
        activeFetches -= 1
      }
    }
  })
  const snapshots = [
    [{ session_id: 'external', latest_message_id: 1, is_reply_running: false }],
    [{ session_id: 'external', latest_message_id: 1, is_reply_running: false }],
    [
      { session_id: 'external', latest_message_id: 2, is_reply_running: false },
      { session_id: 'other', latest_message_id: 99, is_reply_running: false }
    ],
    [
      { session_id: 'external', latest_message_id: 2, is_reply_running: false },
      { session_id: 'other', latest_message_id: 99, is_reply_running: false }
    ],
    [
      { session_id: 'external', latest_message_id: 2, is_reply_running: false },
      { session_id: 'other', latest_message_id: 100, is_reply_running: false }
    ]
  ]
  const observed = []
  const processing = []
  const poller = createSessionListPoller({
    refreshSessions: async () => {
      const snapshot = snapshots.shift() || []
      observed.push(snapshot)
      fixture.sessionManager.sessions.value = snapshot
      processing.push(fixture.polling.processHttpSessionSnapshot(snapshot))
      return snapshot
    },
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  await poller.refreshNow()
  await timers.tick()
  await timers.tick()
  assert.equal(observed.length, 3)
  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 1, limit: 50 }])

  await timers.tick()
  assert.equal(observed.length, 4)
  assert.equal(fetches.length, 1)
  assert.equal(maxActiveFetches, 1)
  assert.equal(timers.pending.length, 1)

  historyResponse.resolve([{ db_id: 2, content: 'two' }])
  await Promise.all(processing)
  await timers.tick()
  await Promise.all(processing)

  assert.equal(observed.length, 5)
  assert.equal(fetches.length, 1)
  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 2)
  assert.equal(timers.pending.length, 1)
  poller.dispose()
})

test('external readonly history waits for initial history and validates list message ids', async () => {
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    initialHistoryLoaded: false,
    fetchPage: (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      return []
    }
  })
  const validSnapshot = [{ session_id: 'external', latest_message_id: '5' }]

  await fixture.polling.processHttpSessionSnapshot(validSnapshot)
  fixture.sessionManager.currentSessionId.value = null
  await fixture.polling.processHttpSessionSnapshot(validSnapshot)
  fixture.sessionManager.currentSessionId.value = 'external'
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'other', latest_message_id: 5 }
  ])

  fixture.initialHistoryLoaded.value = true
  for (const latestMessageId of [
    undefined,
    null,
    '',
    '   ',
    0,
    -1,
    1.5,
    'not-a-number',
    Infinity,
    Number.MAX_SAFE_INTEGER + 1,
    true,
    [],
    {}
  ]) {
    await fixture.polling.processHttpSessionSnapshot([
      { session_id: 'external', latest_message_id: latestMessageId }
    ])
  }

  assert.deepEqual(fetches, [])
  await fixture.polling.processHttpSessionSnapshot(validSnapshot)

  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 0, limit: 50 }])
})

test('external readonly history falls back to the loaded history maximum when its cursor is uncached', async () => {
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    messages: [{ db_id: 5, content: 'five' }],
    fetchPage: (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      return [{ db_id: 6, content: 'six' }]
    }
  })

  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 5 }
  ])
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 4 }
  ])
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 6 }
  ])
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 6 }
  ])

  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 5, limit: 50 }])
})

test('external readonly history drains bounded pages on repeated identical list snapshots', async () => {
  const serverMessages = Array.from({ length: 7 }, (_, index) => ({
    db_id: index + 2,
    content: `message-${index + 2}`
  }))
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    pageSize: 2,
    maxPages: 1,
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      return serverMessages.filter(message => Number(message.db_id) > afterId).slice(0, limit)
    }
  })
  const snapshot = [{ session_id: 'external', latest_message_id: 8 }]

  for (let attempt = 0; attempt < 5; attempt += 1) {
    await fixture.polling.processHttpSessionSnapshot(snapshot)
  }

  assert.deepEqual(fetches, [
    { sessionId: 'external', afterId: 1, limit: 2 },
    { sessionId: 'external', afterId: 3, limit: 2 },
    { sessionId: 'external', afterId: 5, limit: 2 },
    { sessionId: 'external', afterId: 7, limit: 2 }
  ])
  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2, 3, 4, 5, 6, 7, 8]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 8)
})

test('external readonly history does not confirm a filtered list id as its history cursor', async () => {
  const afterIds = []
  let attempt = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 5]]),
    fetchPage: (sessionId, { afterId }) => {
      afterIds.push({ sessionId, afterId })
      attempt += 1
      return attempt === 1 ? [] : [{ db_id: 10, content: 'ten' }]
    }
  })

  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 9 }
  ])
  assert.equal(fixture.merger.lastMessageIds.get('external'), 5)
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 9 }
  ])
  assert.deepEqual(afterIds, [{ sessionId: 'external', afterId: 5 }])

  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 10 }
  ])
  assert.deepEqual(afterIds, [
    { sessionId: 'external', afterId: 5 },
    { sessionId: 'external', afterId: 5 }
  ])
  assert.equal(fixture.merger.lastMessageIds.get('external'), 10)
})

test('external readonly history uses the actual cursor when raw history is not visible', async () => {
  const visibleMessages = [{ db_id: 5, content: 'visible' }]
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    messages: visibleMessages,
    initialLastMessageIds: new Map([['external', 5]]),
    fetchPage: (sessionId, { afterId }) => {
      fetches.push({ sessionId, afterId })
      return [{ db_id: 9, content: 'filtered raw message' }]
    }
  })
  const snapshot = [{ session_id: 'external', latest_message_id: 9 }]

  await fixture.polling.processHttpSessionSnapshot(snapshot)
  assert.deepEqual(fixture.chatState.messages.value, visibleMessages)
  assert.equal(fixture.merger.lastMessageIds.get('external'), 9)

  fixture.polling.resetHttpPollingState()
  await fixture.polling.processHttpSessionSnapshot(snapshot)
  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 5 }])
})

test('external readonly history retries the same list snapshot after a failed page without advancing its cursor', async () => {
  const failure = new Error('temporary history failure')
  const afterIds = []
  let attempt = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: (sessionId, { afterId }) => {
      afterIds.push({ sessionId, afterId })
      attempt += 1
      if (attempt === 1) return Promise.reject(failure)
      return [{ db_id: 2, content: 'two' }]
    }
  })
  const snapshot = [{ session_id: 'external', latest_message_id: 2 }]

  await assert.rejects(
    fixture.polling.processHttpSessionSnapshot(snapshot),
    error => error === failure
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 1)
  assert.deepEqual(fixture.merger.mergedBySession.get('external') || [], [])

  await fixture.polling.processHttpSessionSnapshot(snapshot)
  assert.deepEqual(afterIds, [
    { sessionId: 'external', afterId: 1 },
    { sessionId: 'external', afterId: 1 }
  ])
  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 2)
})

test('external readonly history serializes duplicate and higher list ids while a page is delayed', async () => {
  const firstResponse = createDeferred()
  const secondResponse = createDeferred()
  const fetches = []
  let activeFetches = 0
  let maxActiveFetches = 0
  let attempt = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: (sessionId, request) => {
      fetches.push({ sessionId, ...request })
      activeFetches += 1
      maxActiveFetches = Math.max(maxActiveFetches, activeFetches)
      const response = attempt === 0 ? firstResponse : secondResponse
      attempt += 1
      return response.promise.finally(() => {
        activeFetches -= 1
      })
    }
  })
  const duplicateSnapshot = [{ session_id: 'external', latest_message_id: 2 }]
  const higherSnapshot = [{ session_id: 'external', latest_message_id: 3 }]

  const firstSync = fixture.polling.processHttpSessionSnapshot(duplicateSnapshot)
  await fixture.polling.processHttpSessionSnapshot(duplicateSnapshot)
  await fixture.polling.processHttpSessionSnapshot(higherSnapshot)
  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 1, limit: 50 }])
  assert.equal(maxActiveFetches, 1)

  firstResponse.resolve([{ db_id: 2, content: 'two' }])
  await firstSync
  const catchUp = fixture.polling.processHttpSessionSnapshot(higherSnapshot)
  assert.deepEqual(fetches, [
    { sessionId: 'external', afterId: 1, limit: 50 },
    { sessionId: 'external', afterId: 2, limit: 50 }
  ])
  secondResponse.resolve([{ db_id: 3, content: 'three' }])
  await catchUp

  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2, 3]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 3)
  assert.equal(activeFetches, 0)
})

test('external readonly history invalidates stale A responses across an A to B to A reset cycle', async () => {
  const firstAResponse = createDeferred()
  const secondAResponse = createDeferred()
  const fetches = []
  let aRequestCount = 0
  const fixture = createExternalHttpPollingFixture({
    currentSessionId: 'session-a',
    fetchPage: (sessionId, request) => {
      fetches.push({ sessionId, ...request })
      if (sessionId === 'session-a') {
        aRequestCount += 1
        return (aRequestCount === 1 ? firstAResponse : secondAResponse).promise
      }
      return []
    }
  })
  const firstASnapshot = [{ session_id: 'session-a', latest_message_id: 1 }]
  const secondASnapshot = [{ session_id: 'session-a', latest_message_id: 2 }]

  const staleA = fixture.polling.processHttpSessionSnapshot(firstASnapshot)
  fixture.sessionManager.currentSessionId.value = 'session-b'
  fixture.polling.resetHttpPollingState()
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'session-b', latest_message_id: 1 }
  ])
  fixture.sessionManager.currentSessionId.value = 'session-a'
  fixture.polling.resetHttpPollingState()
  const currentA = fixture.polling.processHttpSessionSnapshot(secondASnapshot)
  assert.equal(aRequestCount, 2)

  firstAResponse.resolve([{ db_id: 1, content: 'stale A' }])
  await staleA
  assert.deepEqual(fixture.merger.mergedBySession.get('session-a') || [], [])
  assert.equal(fixture.merger.lastMessageIds.get('session-a'), undefined)

  await fixture.polling.processHttpSessionSnapshot(secondASnapshot)
  assert.equal(aRequestCount, 2)
  secondAResponse.resolve([{ db_id: 2, content: 'current A' }])
  await currentA

  assert.deepEqual(
    (fixture.merger.mergedBySession.get('session-a') || []).map(message => message.db_id),
    [2]
  )
  assert.equal(fixture.merger.lastMessageIds.get('session-a'), 2)
  assert.deepEqual(fetches.filter(({ sessionId }) => sessionId === 'session-a').map(({ afterId }) => afterId), [0, 0])
})

test('external readonly history ignores reset, leave, source, and initial-history stale returns and retries after recovery', async () => {
  const runStaleCase = async ({ mutate, restore, staleType }) => {
    const staleResponse = createDeferred()
    let attempts = 0
    const fixture = createExternalHttpPollingFixture({
      fetchPage: () => {
        attempts += 1
        if (attempts === 1) return staleResponse.promise
        return [{ db_id: 1, content: 'retry' }]
      }
    })
    const snapshot = [{ session_id: 'external', latest_message_id: 1 }]
    const staleSync = fixture.polling.processHttpSessionSnapshot(snapshot)
    mutate(fixture)
    if (staleType === 'error') {
      staleResponse.reject(new Error('stale history failure'))
    } else {
      staleResponse.resolve([{ db_id: 1, content: 'stale' }])
    }
    await staleSync

    assert.equal(attempts, 1)
    assert.deepEqual(fixture.merger.mergedBySession.get('external') || [], [])
    assert.equal(fixture.merger.lastMessageIds.get('external'), undefined)
    restore(fixture)
    const retry = fixture.polling.processHttpSessionSnapshot(snapshot)
    assert.equal(attempts, 2)
    await retry
    assert.deepEqual(
      (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
      [1]
    )
    assert.equal(fixture.merger.lastMessageIds.get('external'), 1)
  }

  await runStaleCase({
    mutate: fixture => fixture.polling.resetHttpPollingState(),
    restore: () => {},
    staleType: 'response'
  })
  await runStaleCase({
    mutate: fixture => {
      fixture.sessionManager.currentSessionId.value = null
    },
    restore: fixture => {
      fixture.sessionManager.currentSessionId.value = 'external'
    },
    staleType: 'error'
  })
  await runStaleCase({
    mutate: fixture => {
      fixture.isCurrentSessionReadOnly.value = false
    },
    restore: fixture => {
      fixture.isCurrentSessionReadOnly.value = true
    },
    staleType: 'error'
  })
  await runStaleCase({
    mutate: fixture => {
      fixture.initialHistoryLoaded.value = false
    },
    restore: fixture => {
      fixture.initialHistoryLoaded.value = true
    },
    staleType: 'error'
  })
})

test('external readonly history works for every transport and loading state without writable HTTP effects', async () => {
  for (const transportMode of ['http', 'ws']) {
    for (const loading of [false, true]) {
      const visibleMessages = [{ db_id: 7, content: 'visible' }]
      const pendingHttpRequests = new Map([
        ['request-1', { sessionId: 'external', workId: 'work-1' }]
      ])
      const pendingBefore = Array.from(pendingHttpRequests.entries())
      const fetches = []
      const fixture = createExternalHttpPollingFixture({
        transportMode,
        loading,
        messages: visibleMessages,
        initialLastMessageIds: new Map([['external', 7]]),
        pendingHttpRequests,
        fetchPage: (sessionId, { afterId, limit }) => {
          fetches.push({ sessionId, afterId, limit })
          return [{ db_id: 8, content: 'eight' }]
        }
      })
      const snapshot = [{
        session_id: 'external',
        latest_message_id: 8,
        is_reply_running: loading,
        reply_works: [{
          work_id: 'work-1',
          status: 'succeeded',
          request_ids: ['request-1']
        }]
      }]

      await fixture.polling.processHttpSessionSnapshot(snapshot)
      assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 7, limit: 50 }])
      assert.deepEqual(
        (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
        [8]
      )
      assert.equal(fixture.chatState.loading.value, loading)
      assert.deepEqual(fixture.chatState.messages.value, visibleMessages)
      assert.deepEqual(Array.from(pendingHttpRequests.entries()), pendingBefore)
    }
  }
})
