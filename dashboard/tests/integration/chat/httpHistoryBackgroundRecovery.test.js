import assert from 'node:assert/strict'
import test from 'node:test'

test('HTTP background task recovery is rediscovered after switching away and back to the session', async () => {
  const { createHttpHistorySyncController } = await import('../../../src/composables/chat/httpHistorySync.js')
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
  const { createHttpHistorySyncController } = await import('../../../src/composables/chat/httpHistorySync.js')
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
  const { createHttpHistorySyncController } = await import('../../../src/composables/chat/httpHistorySync.js')
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
  const { createHttpHistorySyncController } = await import('../../../src/composables/chat/httpHistorySync.js')
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
  const { createHttpHistorySyncController } = await import('../../../src/composables/chat/httpHistorySync.js')
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
