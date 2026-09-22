import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const chatViewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')
const chatStyles = readFileSync(new URL('../src/assets/css/chat.scss', import.meta.url), 'utf8')
const sessionManagerSource = readFileSync(new URL('../src/composables/chat/useSessionManager.js', import.meta.url), 'utf8')
const chatSessionSource = readFileSync(new URL('../src/composables/chat/useChatSession.js', import.meta.url), 'utf8')

const loadSessionLoadingModule = () => import('../src/composables/chat/sessionListLoading.js')

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
