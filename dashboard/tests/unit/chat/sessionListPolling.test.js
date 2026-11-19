import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createDeferred,
  createManualTimerQueue,
  loadSessionLoadingModule
} from '../../support/chat/sessionListHarness.js'

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
