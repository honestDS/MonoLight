import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const chatViewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')
const chatStyles = readFileSync(new URL('../src/assets/css/chat.scss', import.meta.url), 'utf8')

const loadSessionLoadingModule = () => import('../src/composables/chat/sessionListLoading.js')

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

test('session list renders a loading indicator from the backend is_loading field', () => {
  assert.match(chatViewSource, /v-if="session\.is_loading"/)
})

test('session refresh uses the Element Plus icon and only rotates while loading', () => {
  assert.match(chatViewSource, /class="sidebar-icon refresh-icon"[\s\S]*?:class="\{ loading: sessionsLoading \}"/)
  assert.match(chatViewSource, /<Refresh class="refresh-icon-glyph" aria-hidden="true" \/>/)
  assert.match(chatViewSource, /\bRefresh\b[^\n]*from '@element-plus\/icons-vue'/)
  assert.match(chatStyles, /&\.loading \.refresh-icon-glyph\s*\{\s*animation: refresh-icon-rotate 1s linear infinite;/)
  assert.doesNotMatch(chatStyles, /assets\/svg\/refresh\.svg/)
})

test('session loading indicator is rendered beside the session title instead of the delete action', () => {
  const titleStart = chatViewSource.indexOf('class="session-title"')
  const titleEnd = chatViewSource.indexOf('</div>', titleStart)
  const titleBlock = chatViewSource.slice(titleStart, titleEnd)
  const actionsStart = chatViewSource.indexOf('class="session-actions"')
  const actionsEnd = chatViewSource.indexOf('</div>', actionsStart)
  const actionsBlock = chatViewSource.slice(actionsStart, actionsEnd)

  assert.notEqual(titleStart, -1)
  assert.match(titleBlock, /session-loading-indicator/)
  assert.doesNotMatch(actionsBlock, /session-loading-indicator/)
})

test('session loading indicator stays immediately beside the visible title text', () => {
  assert.match(
    chatStyles,
    /\.session-content\s*\{[\s\S]*?\.session-title\s*\{[\s\S]*?display:\s*inline-flex;[\s\S]*?max-width:\s*100%;[\s\S]*?gap:\s*6px;/
  )
  assert.match(
    chatStyles,
    /\.session-content\s*\{[\s\S]*?\.session-title-text\s*\{[\s\S]*?flex:\s*0 1 auto;[\s\S]*?text-overflow:\s*ellipsis;/
  )
})

test('session loading poller keeps refreshing while any session is loading and stops when all finish', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const refreshResults = [
    [{ session_id: 'a', is_loading: true }],
    [{ session_id: 'a', is_loading: true }],
    [{ session_id: 'a', is_loading: false }]
  ]
  const scheduled = []
  let refreshCount = 0

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => {
      refreshCount += 1
      return refreshResults.shift() || []
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

  await poller.refreshNow()
  assert.equal(refreshCount, 1)
  assert.equal(scheduled.length, 1)

  await scheduled.shift()()
  assert.equal(refreshCount, 2)
  assert.equal(scheduled.length, 1)

  await scheduled.shift()()
  assert.equal(refreshCount, 3)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})

test('session loading poller does not schedule when refreshed sessions are already idle', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => [{ session_id: 'a', is_loading: false }],
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: () => {}
  })

  await poller.refreshNow()
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})


test('session loading poller follows loading state from an already loaded session list', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => [],
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  poller.sync([{ session_id: 'a', is_loading: true }])
  assert.equal(scheduled.length, 1)
  assert.equal(poller.isPolling(), true)

  poller.sync([{ session_id: 'a', is_loading: false }])
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
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

test('disposed session loading poller does not restart after an in-flight refresh resolves', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []
  let resolveRefresh
  const refreshResult = new Promise(resolve => {
    resolveRefresh = resolve
  })

  const poller = createSessionListLoadingPoller({
    refreshSessions: () => refreshResult,
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: () => {}
  })

  const pendingRefresh = poller.refreshNow()
  poller.dispose()
  resolveRefresh([{ session_id: 'a', is_loading: true }])
  await pendingRefresh

  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})


test('session loading poller performs a trailing refresh when refreshNow is requested during an in-flight refresh', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []
  let firstResolve
  let refreshCount = 0
  const firstResult = new Promise(resolve => {
    firstResolve = resolve
  })

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => {
      refreshCount += 1
      if (refreshCount === 1) return firstResult
      return [{ session_id: 'a', is_loading: true }]
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

  const firstRefresh = poller.refreshNow()
  const trailingRefresh = poller.refreshNow()
  firstResolve([{ session_id: 'a', is_loading: false }])

  await Promise.all([firstRefresh, trailingRefresh])

  assert.equal(refreshCount, 2)
  assert.equal(scheduled.length, 1)
  assert.equal(poller.isPolling(), true)
})

test('session loading poller keeps a bounded fallback refresh while submissions remain pending', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []
  let refreshCount = 0

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => {
      refreshCount += 1
      return [{ session_id: 'a', is_loading: false }]
    },
    hasPendingSubmissions: () => true,
    maxPendingIdleRefreshes: 2,
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  await poller.refreshNow()
  assert.equal(refreshCount, 1)
  assert.equal(scheduled.length, 1)

  await scheduled.shift()()
  assert.equal(refreshCount, 2)
  assert.equal(scheduled.length, 1)

  await scheduled.shift()()
  assert.equal(refreshCount, 3)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})

test('session loading poller resets the pending fallback budget when submissions disappear', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []
  let pending = true
  let refreshCount = 0

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => {
      refreshCount += 1
      return [{ session_id: 'a', is_loading: false }]
    },
    hasPendingSubmissions: () => pending,
    maxPendingIdleRefreshes: 1,
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  await poller.refreshNow()
  assert.equal(scheduled.length, 1)

  pending = false
  await scheduled.shift()()
  assert.equal(refreshCount, 2)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)

  pending = true
  poller.sync([{ session_id: 'a', is_loading: false }])
  assert.equal(scheduled.length, 1)

  await scheduled.shift()()
  assert.equal(refreshCount, 3)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})

test('session loading poller resumes for real loading after the pending fallback limit', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []
  let pending = true
  let refreshCount = 0

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => {
      refreshCount += 1
      return [{ session_id: 'a', is_loading: false }]
    },
    hasPendingSubmissions: () => pending,
    maxPendingIdleRefreshes: 1,
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  await poller.refreshNow()
  await scheduled.shift()()
  assert.equal(refreshCount, 2)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)

  poller.sync([{ session_id: 'a', is_loading: true }])
  assert.equal(scheduled.length, 1)

  pending = false
  await scheduled.shift()()
  assert.equal(refreshCount, 3)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})

test('session loading poller does not schedule a pending fallback when its limit is zero', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []
  let refreshCount = 0

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => {
      refreshCount += 1
      return [{ session_id: 'a', is_loading: false }]
    },
    hasPendingSubmissions: () => true,
    maxPendingIdleRefreshes: 0,
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  await poller.refreshNow()
  assert.equal(refreshCount, 1)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)

  await poller.refreshNow()
  assert.equal(refreshCount, 2)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})

test('disposed session loading poller ignores a queued pending fallback callback', async () => {
  const { createSessionListLoadingPoller } = await loadSessionLoadingModule()
  const scheduled = []
  let cancelCount = 0
  let refreshCount = 0

  const poller = createSessionListLoadingPoller({
    refreshSessions: async () => {
      refreshCount += 1
      return [{ session_id: 'a', is_loading: false }]
    },
    hasPendingSubmissions: () => true,
    schedule: callback => {
      scheduled.push(callback)
      return callback
    },
    cancel: callback => {
      cancelCount += 1
      const index = scheduled.indexOf(callback)
      if (index !== -1) scheduled.splice(index, 1)
    }
  })

  await poller.refreshNow()
  const queuedCallback = scheduled[0]
  poller.dispose()
  poller.dispose()

  assert.equal(cancelCount, 1)
  assert.equal(scheduled.length, 0)

  await queuedCallback()
  assert.equal(refreshCount, 1)
  assert.equal(scheduled.length, 0)
  assert.equal(poller.isPolling(), false)
})
