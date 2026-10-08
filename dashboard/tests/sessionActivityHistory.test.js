import assert from 'node:assert/strict'
import test from 'node:test'

import { createSessionActivityController } from '../src/composables/chat/sessionActivity.js'
import { createHttpReplyPolling } from '../src/composables/chat/httpReplyPolling.js'
import {
  getIncrementalHistoryCursor,
  syncIncrementalHistory
} from '../src/composables/chat/historyIncrementalSync.js'

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createSession = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  title: `Title ${sessionId}`,
  config: { model: `model-${sessionId}`, temperature: 0.1 },
  latest_message_id: 1,
  source: 'external',
  is_reply_running: false,
  ...overrides
})

const createActivity = (sessionId, latestMessageId) => ({
  session_id: sessionId,
  latest_message_id: latestMessageId,
  source: 'external',
  is_reply_running: false
})

const createReadonlyHistoryHarness = ({
  initialSessions,
  currentSessionId = 'external',
  initialMessages = [{ db_id: 1, content: 'message-1' }],
  initialCursor = 1,
  pageSize = 2,
  maxPages = 2,
  fetchSessions = async () => [],
  fetchHistoryPage = async () => []
}) => {
  const state = {
    transport: { transportMode: { value: 'http' } },
    sessionManager: {
      currentSessionId: { value: currentSessionId },
      sessions: { value: initialSessions }
    },
    isCurrentSessionReadOnly: { value: true },
    chatState: {
      messages: { value: [...initialMessages] },
      loading: { value: false }
    },
    initialHistoryLoaded: { value: true },
    pendingHttpRequests: new Map()
  }
  const cursors = new Map([[currentSessionId, initialCursor]])
  const historyRequests = []
  const errors = []
  const callbackWaiters = {
    activity: [],
    sessions: []
  }
  const callbackTasks = {
    activity: [],
    sessions: []
  }
  let polling

  const waitForCallback = kind => {
    const deferred = createDeferred()
    callbackWaiters[kind].push(deferred)
    return deferred.promise
  }

  const publishCallback = (kind, task) => {
    callbackTasks[kind].push(task)
    const deferred = callbackWaiters[kind].shift()
    if (deferred) deferred.resolve({ task })
    return task
  }

  const mergeHistoryPage = messages => {
    const existingIds = new Set(
      state.chatState.messages.value
        .map(message => Number(message?.db_id))
        .filter(Number.isSafeInteger)
    )
    for (const message of messages) {
      const messageId = Number(message?.db_id ?? message?.id)
      if (!Number.isSafeInteger(messageId) || existingIds.has(messageId)) continue
      state.chatState.messages.value.push(message)
      existingIds.add(messageId)
    }
  }

  const mergeIncrementalSessionHistory = async (sessionId, isCurrentSync) => {
    if (!sessionId || sessionId !== state.sessionManager.currentSessionId.value) {
      return { hasMore: false }
    }
    if (!state.initialHistoryLoaded.value) return { hasMore: true }

    if (!cursors.has(sessionId)) {
      cursors.set(
        sessionId,
        getIncrementalHistoryCursor(state.chatState.messages.value)
      )
    }

    const result = await syncIncrementalHistory({
      initialAfterId: getIncrementalHistoryCursor(
        state.chatState.messages.value,
        cursors.get(sessionId)
      ),
      pageSize,
      maxPages,
      isCurrent: isCurrentSync,
      fetchPage: request => {
        historyRequests.push({ sessionId, ...request })
        return fetchHistoryPage(sessionId, request)
      },
      mergePage: messages => {
        if (isCurrentSync()) mergeHistoryPage(messages)
      }
    })

    if (!isCurrentSync()) return { hasMore: true }
    if (!result.cancelled) cursors.set(sessionId, result.lastMessageId)
    return result
  }

  polling = createHttpReplyPolling({
    transport: state.transport,
    sessionManager: state.sessionManager,
    isCurrentSessionReadOnly: state.isCurrentSessionReadOnly,
    chatState: state.chatState,
    initialHistoryLoaded: state.initialHistoryLoaded,
    pendingHttpRequests: state.pendingHttpRequests,
    workLifecycleTracker: {
      finishWorkLifecycle: () => assert.fail('unexpected writable HTTP lifecycle')
    },
    applyTodoTransportPayload: () => assert.fail('unexpected writable HTTP payload'),
    updateLlmRequestMetadata: () => assert.fail('unexpected writable HTTP metadata'),
    startHttpHistoryBackgroundTaskSync: () => assert.fail('unexpected writable HTTP task'),
    applyNonStreamSessionEvents: () => assert.fail('unexpected writable HTTP event'),
    shouldProcessCompletedWork: () => assert.fail('unexpected writable HTTP result'),
    processAiResponse: () => assert.fail('unexpected writable HTTP response'),
    messageProcessor: {
      processStreamError: () => assert.fail('unexpected writable HTTP error')
    },
    api: {
      replyWorkStatus: () => assert.fail('replyWorkStatus must not run for readonly sessions')
    },
    mergeLatestSessionHistory: () => assert.fail('unexpected full history merge'),
    getHistoryCursor: sessionId => getIncrementalHistoryCursor(
      state.chatState.messages.value,
      cursors.get(sessionId)
    ),
    mergeIncrementalSessionHistory,
    reportError: error => errors.push(error),
    translate: key => key
  })

  const fullCalls = []
  const activityResponses = []
  let controller
  controller = createSessionActivityController({
    fetchSessions: async (...args) => {
      fullCalls.push(args)
      return fetchSessions(...args)
    },
    fetchActivity: async () => activityResponses.shift(),
    getSessions: () => state.sessionManager.sessions.value,
    setSessions: value => {
      state.sessionManager.sessions.value = value
    },
    getCurrentSessionId: () => state.sessionManager.currentSessionId.value,
    onSessionsUpdated: sessions => publishCallback(
      'sessions',
      polling.processHttpSessionSnapshot(sessions)
    ),
    onActivityUpdated: activities => {
      if (!state.isCurrentSessionReadOnly.value) return
      return publishCallback(
        'activity',
        polling.processHttpSessionSnapshot(activities)
      )
    },
    onError: error => errors.push(error)
  })

  const startActivity = async activities => {
    activityResponses.push(activities)
    const callback = waitForCallback('activity')
    const result = await controller.refreshActivity()
    return { result, task: (await callback).task }
  }

  return {
    state,
    cursors,
    historyRequests,
    fullCalls,
    callbackTasks,
    errors,
    controller,
    polling,
    waitForCallback,
    startActivity
  }
}

test('known readonly activity drains bounded history pages without a full list refresh', async () => {
  const serverMessages = Array.from({ length: 7 }, (_, index) => ({
    db_id: index + 2,
    content: `message-${index + 2}`
  }))
  const fixture = createReadonlyHistoryHarness({
    initialSessions: [createSession('external')],
    fetchSessions: async () => [createSession('external')],
    fetchHistoryPage: async (sessionId, { afterId, limit }) => {
      assert.equal(sessionId, 'external')
      return serverMessages
        .filter(message => message.db_id > afterId)
        .slice(0, limit)
    }
  })
  const activity = [createActivity('external', 8)]

  const first = await fixture.startActivity(activity)
  await first.task
  assert.equal(fixture.historyRequests.length, 2)
  assert.equal(fixture.cursors.get('external'), 5)

  const second = await fixture.startActivity(activity)
  await second.task

  assert.equal(fixture.fullCalls.length, 0)
  assert.equal(fixture.historyRequests.length, 4)
  assert.equal(fixture.cursors.get('external'), 8)
  assert.deepEqual(fixture.historyRequests, [
    { sessionId: 'external', afterId: 1, limit: 2 },
    { sessionId: 'external', afterId: 3, limit: 2 },
    { sessionId: 'external', afterId: 5, limit: 2 },
    { sessionId: 'external', afterId: 7, limit: 2 }
  ])
  assert.deepEqual(
    fixture.state.chatState.messages.value.map(message => message.db_id),
    [1, 2, 3, 4, 5, 6, 7, 8]
  )

  fixture.controller.dispose()
})

test('duplicate readonly activities do not overlap history requests and a newer id catches up afterward', async () => {
  const firstResponse = createDeferred()
  const secondResponse = createDeferred()
  let attempt = 0
  let activeRequests = 0
  let maxActiveRequests = 0
  const fixture = createReadonlyHistoryHarness({
    initialSessions: [createSession('external')],
    fetchSessions: async () => [createSession('external')],
    fetchHistoryPage: (sessionId, request) => {
      assert.equal(sessionId, 'external')
      assert.equal(request.limit, 2)
      activeRequests += 1
      maxActiveRequests = Math.max(maxActiveRequests, activeRequests)
      const response = attempt === 0 ? firstResponse : secondResponse
      attempt += 1
      return response.promise.finally(() => {
        activeRequests -= 1
      })
    }
  })
  const activityAtTwo = [createActivity('external', 2)]
  const activityAtThree = [createActivity('external', 3)]

  const first = await fixture.startActivity(activityAtTwo)
  assert.equal(fixture.historyRequests.length, 1)

  const duplicate = await fixture.startActivity(activityAtTwo)
  const higher = await fixture.startActivity(activityAtThree)
  assert.equal(fixture.historyRequests.length, 1)
  assert.equal(maxActiveRequests, 1)
  assert.equal(activeRequests, 1)
  await duplicate.task
  await higher.task

  firstResponse.resolve([{ db_id: 2, content: 'message-2' }])
  await first.task
  assert.equal(fixture.cursors.get('external'), 2)

  const retryHigher = await fixture.startActivity(activityAtThree)
  assert.deepEqual(fixture.historyRequests, [
    { sessionId: 'external', afterId: 1, limit: 2 },
    { sessionId: 'external', afterId: 2, limit: 2 }
  ])

  secondResponse.resolve([{ db_id: 3, content: 'message-3' }])
  await retryHigher.task
  assert.equal(fixture.cursors.get('external'), 3)
  assert.deepEqual(
    fixture.state.chatState.messages.value.map(message => message.db_id),
    [1, 2, 3]
  )
  assert.equal(activeRequests, 0)

  fixture.controller.dispose()
})

test('unknown activity causes one full refresh while complete existing session fields survive', async () => {
  const fullResponse = createDeferred()
  let fullRequestCount = 0
  const oldSession = createSession('old', {
    title: 'old title',
    config: { model: 'old-model', temperature: 0.2 }
  })
  const fixture = createReadonlyHistoryHarness({
    initialSessions: [oldSession],
    currentSessionId: 'old',
    initialCursor: 1,
    fetchSessions: () => {
      fullRequestCount += 1
      return fullResponse.promise
    }
  })
  const activity = [
    createActivity('old', 1),
    createActivity('new', 9)
  ]

  const first = await fixture.startActivity(activity)
  await first.task
  assert.equal(fullRequestCount, 1)
  assert.equal(fixture.fullCalls.length, 1)
  assert.equal(fixture.state.sessionManager.sessions.value[0].title, 'old title')
  assert.deepEqual(
    fixture.state.sessionManager.sessions.value[0].config,
    oldSession.config
  )

  const duplicate = await fixture.startActivity(activity)
  await duplicate.task
  assert.equal(fullRequestCount, 1)

  const fullUpdate = fixture.waitForCallback('sessions')
  fullResponse.resolve([
    createSession('old', {
      title: 'old title',
      config: { model: 'old-model', temperature: 0.2 }
    }),
    createSession('new', {
      title: 'new title',
      config: { model: 'new-model', temperature: 0.3 },
      latest_message_id: 9
    })
  ])
  const fullTask = (await fullUpdate).task
  await fullTask

  const sessions = fixture.state.sessionManager.sessions.value
  assert.equal(fullRequestCount, 1)
  assert.equal(sessions.find(session => session.session_id === 'old').title, 'old title')
  assert.deepEqual(
    sessions.find(session => session.session_id === 'old').config,
    { model: 'old-model', temperature: 0.2 }
  )
  assert.equal(sessions.find(session => session.session_id === 'new').title, 'new title')
  assert.deepEqual(
    sessions.find(session => session.session_id === 'new').config,
    { model: 'new-model', temperature: 0.3 }
  )

  fixture.controller.dispose()
})

test('a failed readonly history page leaves its cursor unchanged and retries the same activity', async () => {
  const failure = new Error('temporary history failure')
  let attempt = 0
  const fixture = createReadonlyHistoryHarness({
    initialSessions: [createSession('external')],
    fetchHistoryPage: async () => {
      attempt += 1
      if (attempt === 1) throw failure
      return [{ db_id: 2, content: 'message-2' }]
    }
  })
  const activity = [createActivity('external', 2)]

  const first = await fixture.startActivity(activity)
  await assert.rejects(first.task, error => error === failure)
  assert.equal(fixture.cursors.get('external'), 1)
  assert.deepEqual(
    fixture.state.chatState.messages.value.map(message => message.db_id),
    [1]
  )

  const second = await fixture.startActivity(activity)
  await second.task
  assert.deepEqual(fixture.historyRequests, [
    { sessionId: 'external', afterId: 1, limit: 2 },
    { sessionId: 'external', afterId: 1, limit: 2 }
  ])
  assert.equal(fixture.cursors.get('external'), 2)
  assert.deepEqual(
    fixture.state.chatState.messages.value.map(message => message.db_id),
    [1, 2]
  )

  fixture.controller.dispose()
})

test('late readonly history is ignored after a session switch or polling reset', async () => {
  const runStaleCase = async ({ invalidate }) => {
    const response = createDeferred()
    const fixture = createReadonlyHistoryHarness({
      initialSessions: [createSession('external'), createSession('other')],
      fetchHistoryPage: async () => response.promise
    })
    const activity = [createActivity('external', 2)]

    const started = await fixture.startActivity(activity)
    assert.equal(fixture.historyRequests.length, 1)
    invalidate(fixture)
    response.resolve([{ db_id: 2, content: 'late message' }])
    await started.task

    assert.equal(fixture.cursors.get('external'), 1)
    assert.deepEqual(
      fixture.state.chatState.messages.value.map(message => message.db_id),
      [1]
    )
    fixture.controller.dispose()
  }

  await runStaleCase({
    invalidate: fixture => {
      fixture.state.sessionManager.currentSessionId.value = 'other'
    }
  })
  await runStaleCase({
    invalidate: fixture => fixture.polling.resetHttpPollingState()
  })
})
