import assert from 'node:assert/strict'
import test from 'node:test'
import { createDeferred, createHarness, createSession } from './sessionNavigationHarness.js'

test('session panel toggles and closes when navigating sessions or a new session', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, { sessions })

  assert.equal(harness.module.sessionsPanelOpen.value, false)
  harness.module.closeSessionsPanel()
  assert.equal(harness.module.sessionsPanelOpen.value, false)

  await harness.module.toggleSessionsPanel()
  assert.equal(harness.module.sessionsPanelOpen.value, true)
  await harness.module.toggleSessionsPanel()
  assert.equal(harness.module.sessionsPanelOpen.value, false)

  await harness.module.toggleSessionsPanel()
  assert.equal(harness.module.sessionsPanelOpen.value, true)
  harness.module.handleSelectSession(harness.session('B'))
  await harness.flush()
  assert.equal(harness.module.sessionsPanelOpen.value, false)
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.chat.messages.value[0].session_id, 'B')
  assert.deepEqual(harness.url().query, { session_id: 'B' })

  await harness.module.toggleSessionsPanel()
  assert.equal(harness.module.sessionsPanelOpen.value, true)
  harness.module.handleCreateNewSession()
  await harness.flush()
  assert.equal(harness.module.sessionsPanelOpen.value, false)
  assert.equal(harness.chat.currentSessionId.value, null)
  assert.deepEqual(harness.chat.messages.value, [])
  assert.equal(harness.chat.inputMsg.value, '')
  assert.deepEqual(harness.url().query, {})
})

test('deep links consume task_open, manual navigation survives refresh, and preserves unrelated URL parts', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'first-token', keep: 'yes' },
      hash: '#messages'
    }
  })

  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
  assert.equal(harness.url().hash, '#messages')
  assert.equal(harness.loadRequests.length, 1)
  assert.equal(harness.chat.messages.value[0].session_id, 'A')

  harness.chat.inputMsg.value = 'unsent draft'
  harness.module.handleSelectSession(sessions[1])
  assert.equal(harness.chat.inputMsg.value, '')
  harness.chat.inputMsg.value = 'B unsent draft'
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.chat.inputMsg.value, 'B unsent draft')
  assert.deepEqual(harness.url().query, { session_id: 'B', keep: 'yes' })
  assert.equal(harness.chat.messages.value[0].session_id, 'B')

  const refreshed = await createHarness(t, {
    sessions,
    initialRoute: harness.url()
  })
  await refreshed.flush()
  assert.equal(refreshed.chat.currentSessionId.value, 'B')
  assert.deepEqual(refreshed.url().query, { session_id: 'B', keep: 'yes' })
})

test('new session clears the URL and messages, refresh stays on welcome, and allocated IDs synchronize', async t => {
  const sessions = [createSession('A')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'new-session-token', keep: 'yes' },
      hash: '#welcome'
    }
  })
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')

  harness.chat.inputMsg.value = 'draft'
  harness.module.handleCreateNewSession()
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, null)
  assert.deepEqual(harness.chat.messages.value, [])
  assert.equal(harness.chat.inputMsg.value, '')
  assert.deepEqual(harness.url().query, { keep: 'yes' })
  assert.equal(harness.url().hash, '#welcome')

  const refreshed = await createHarness(t, {
    sessions,
    initialRoute: harness.url()
  })
  await refreshed.flush()
  assert.equal(refreshed.chat.currentSessionId.value, null)
  assert.deepEqual(refreshed.chat.messages.value, [])

  refreshed.chat.currentSessionId.value = 'allocated-id'
  await refreshed.flush()
  assert.deepEqual(refreshed.url().query, { session_id: 'allocated-id', keep: 'yes' })
})

test('the same task token and a new token reopen a session without a navigation loop', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A', keep: 'yes' }, hash: '#tasks' }
  })
  await harness.flush()

  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')

  await harness.replace({
    path: '/',
    query: { session_id: 'A', task_open: 'same-token', keep: 'yes' },
    hash: '#tasks'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })

  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')

  await harness.replace({
    path: '/',
    query: { session_id: 'A', task_open: 'same-token', keep: 'yes' },
    hash: '#tasks'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.chat.messages.value[0].session_id, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })

  const loadsBeforeNewToken = harness.loadRequests.length
  await harness.replace({
    path: '/',
    query: { session_id: 'A', task_open: 'new-token', keep: 'yes' },
    hash: '#tasks'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
  assert.equal(harness.loadRequests.length, loadsBeforeNewToken + 1)
})

test('a late B replace cannot win after A is selected again', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await harness.flush()

  const blockedB = createDeferred()
  const enteredB = createDeferred()
  const removeGuard = harness.router.beforeEach(to => {
    if (to.query.session_id !== 'B') return undefined
    enteredB.resolve()
    return blockedB.promise
  })
  t.after(removeGuard)

  harness.module.handleSelectSession(sessions[1])
  await enteredB.promise
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.url().query.session_id, 'A')
  harness.module.handleSelectSession(sessions[0])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.url().query.session_id, 'A')

  blockedB.resolve()
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.url().query.session_id, 'A')
})

test('stale session loads cannot overwrite a manual selection or a new-session welcome state', async t => {
  const sessions = [createSession('A'), createSession('B')]
  const staleLoad = createDeferred()
  const harness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'pending-token', keep: 'yes' }
    },
    loadSessions: ({ index }) => index === 0 ? staleLoad.promise : sessions
  })
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, null)

  harness.chat.inputMsg.value = 'draft'
  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.chat.inputMsg.value, '')
  assert.equal(harness.chat.messages.value[0].session_id, 'B')
  assert.deepEqual(harness.url().query, { session_id: 'B', keep: 'yes' })

  staleLoad.resolve([sessions[0]])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'B')
  assert.equal(harness.chat.messages.value[0].session_id, 'B')
  assert.equal(harness.url().query.session_id, 'B')

  const pendingNew = createDeferred()
  const newHarness = await createHarness(t, {
    sessions: [sessions[0]],
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'pending-new-token', keep: 'new' }
    },
    loadSessions: () => pendingNew.promise
  })
  await newHarness.flush()
  assert.equal(newHarness.chat.currentSessionId.value, null)
  assert.equal(newHarness.url().query.session_id, 'A')
  newHarness.module.handleCreateNewSession()
  await newHarness.flush()
  assert.deepEqual(newHarness.url().query, { keep: 'new' })
  pendingNew.resolve([sessions[0]])
  await newHarness.flush()
  assert.equal(newHarness.chat.currentSessionId.value, null)
  assert.deepEqual(newHarness.chat.messages.value, [])
  assert.deepEqual(newHarness.url().query, { keep: 'new' })
})

test('unavailable task sessions preserve the active session and remove only the task token', async t => {
  const active = createSession('A')
  const harness = await createHarness(t, {
    sessions: [active],
    initialSessions: [active],
    initialSessionId: 'A',
    initialRoute: {
      path: '/',
      query: { session_id: 'missing', task_open: 'missing-token', keep: 'yes' },
      hash: '#active'
    },
    loadResponses: [[active]]
  })
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.warnings.length, 1)
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
  assert.equal(harness.chat.messages.value[0].session_id, 'A')

  await harness.replace({
    path: '/',
    query: { session_id: 'missing', task_open: 'missing-token', keep: 'yes' },
    hash: '#active'
  })
  assert.equal(harness.chat.currentSessionId.value, 'A')
  assert.equal(harness.warnings.length, 2)
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })
})

test('invalid session IDs do not select sessions and late work after unmount is inert', async t => {
  for (const invalidSessionId of ['', ['A', 'B'], 'x'.repeat(101)]) {
    const harness = await createHarness(t, {
      sessions: [createSession('A')],
      initialRoute: { path: '/', query: { session_id: invalidSessionId } }
    })
    await harness.flush()
    assert.equal(harness.chat.currentSessionId.value, null)
    assert.deepEqual(harness.chat.messages.value, [])
  }

  const pending = createDeferred()
  const harness = await createHarness(t, {
    sessions: [createSession('A')],
    initialRoute: {
      path: '/',
      query: { session_id: 'A', task_open: 'late-token', keep: 'yes' }
    },
    loadSessions: () => pending.promise
  })
  await harness.flush()
  const urlBeforeUnmount = harness.url()
  harness.unmount()
  pending.resolve([createSession('A')])
  await harness.flush()
  assert.equal(harness.chat.currentSessionId.value, null)
  assert.deepEqual(harness.url(), urlBeforeUnmount)
})

test('session URLs restore welcome on root navigation but never rewrite another route', async t => {
  const sessions = [createSession('A')]
  const rootHarness = await createHarness(t, {
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A', keep: 'yes' }, hash: '#root' }
  })
  await rootHarness.flush()
  await rootHarness.replace({ path: '/', query: { keep: 'yes' }, hash: '#root' })
  assert.equal(rootHarness.chat.currentSessionId.value, null)
  assert.deepEqual(rootHarness.chat.messages.value, [])
  assert.deepEqual(rootHarness.url().query, { keep: 'yes' })

  const otherHarness = await createHarness(t, {
    sessions,
    initialRoute: {
      path: '/profiles',
      query: { session_id: 'A', task_open: 'other-token', keep: 'yes' },
      hash: '#profiles'
    }
  })
  await otherHarness.flush()
  assert.equal(otherHarness.chat.currentSessionId.value, null)
  assert.equal(otherHarness.url().path, '/profiles')
  assert.deepEqual(otherHarness.url().query, {
    session_id: 'A',
    task_open: 'other-token',
    keep: 'yes'
  })
  assert.equal(otherHarness.url().hash, '#profiles')
})
