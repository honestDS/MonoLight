import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const chatViewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')
const sessionManagerSource = readFileSync(new URL('../src/composables/chat/useSessionManager.js', import.meta.url), 'utf8')
const chatSessionSource = readFileSync(new URL('../src/composables/chat/useChatSession.js', import.meta.url), 'utf8')
const chatStyleSource = readFileSync(new URL('../src/assets/css/chat.scss', import.meta.url), 'utf8')

const loadSessionLoadingModule = () => import('../src/composables/chat/sessionListLoading.js')

test('session list renders a loading indicator from the backend is_loading field', () => {
  assert.match(chatViewSource, /session\.is_loading/)
  assert.match(chatViewSource, /session-loading-indicator/)
})

test('session loading indicator uses the bootstrap spinner style at the session bottom right', () => {
  assert.doesNotMatch(chatViewSource, /<Loading \/>/)
  assert.match(chatStyleSource, /\.session-item \{[\s\S]*?position: relative/)
  assert.match(chatStyleSource, /\.session-loading-indicator \{[\s\S]*?position: absolute;[\s\S]*?right: 12px;[\s\S]*?bottom: 10px/)
  assert.match(chatStyleSource, /\.session-loading-indicator \{[\s\S]*?width: 14px;[\s\S]*?height: 14px;[\s\S]*?border: 2px solid #d2d6da;[\s\S]*?border-top-color: #59636d;[\s\S]*?animation: session-loading-spin 0\.8s linear infinite/)
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

test('session loading state is wired through the session manager and work lifecycle', () => {
  assert.match(sessionManagerSource, /createSessionListLoadingPoller/)
  assert.match(sessionManagerSource, /sessionLoadingPoller\.sync\(nextSessions\)/)
  assert.match(sessionManagerSource, /refreshSessionLoadingState/)
  assert.match(chatSessionSource, /onInputQueued:[\s\S]*?refreshSessionLoadingState\(\)/)
  assert.match(chatSessionSource, /onWorkFinished:[\s\S]*?refreshSessionLoadingState\(\)/)
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
