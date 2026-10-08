import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createHarness,
  createDeferred,
  createStorage,
  draftKey,
  createSession,
  createTask
} from './chatDraftHarness.js'

test('real session navigation persists per-session drafts across route consumption and rebuild', async t => {
  const storage = createStorage({
    token: 'user-a',
    [draftKey('user-a', 'A')]: 'route task draft'
  })
  const sessions = [createSession('A'), createSession('B')]
  const harness = await createHarness(t, {
    storage,
    sessions,
    initialRoute: { path: '/', query: { session_id: 'A', task_open: 'completed-1', keep: 'yes' }, hash: '#chat' }
  })
  await harness.flush()

  assert.equal(harness.module.currentSessionId.value, 'A')
  assert.equal(harness.module.inputMsg.value, 'route task draft')
  assert.deepEqual(harness.url().query, { session_id: 'A', keep: 'yes' })

  harness.module.inputMsg.value = 'draft A'
  harness.module.handleSelectSession(sessions[1])
  await harness.flush()
  harness.module.inputMsg.value = 'draft B'
  await harness.flush()

  assert.equal(harness.draft('A'), 'draft A')
  assert.equal(harness.draft('B'), 'draft B')

  harness.module.handleSelectSession(sessions[0])
  await harness.flush()
  assert.equal(harness.module.inputMsg.value, 'draft A')

  const refreshed = await createHarness(t, {
    storage,
    sessions,
    initialRoute: harness.url()
  })
  await refreshed.flush()

  assert.equal(refreshed.module.currentSessionId.value, 'A')
  assert.equal(refreshed.module.inputMsg.value, 'draft A')
  assert.deepEqual(refreshed.url().query, { session_id: 'A', keep: 'yes' })

  refreshed.module.handleCreateNewSession()
  await refreshed.flush()
  refreshed.module.inputMsg.value = 'welcome draft'
  await refreshed.flush()
  const welcomeRefresh = await createHarness(t, {
    storage,
    sessions,
    initialRoute: refreshed.url()
  })
  await welcomeRefresh.flush()
  assert.equal(welcomeRefresh.module.currentSessionId.value, null)
  assert.equal(welcomeRefresh.module.inputMsg.value, 'welcome draft')
})

test('task notifications and openSession use real router selection without clearing repeated-session drafts', async t => {
  const storage = createStorage({
    token: 'user-a',
    [draftKey('user-a', 'A')]: 'task draft A',
    [draftKey('user-a', 'B')]: 'task draft B'
  })
  const harness = await createHarness(t, {
    storage,
    withTasks: true,
    sessions: [createSession('A'), createSession('B')],
    initialRoute: { path: '/profiles', query: { keep: 'yes' } },
    activities: [
      [createTask('A', { completed_message_id: 7, completed_status: 'succeeded' })]
    ]
  })
  await harness.flush()

  assert.equal(harness.notifications.length, 1)
  const [button] = harness.notificationButtons(harness.notifications[0])
  button.props.onClick({ stopPropagation: () => {} })
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'A')
  assert.equal(harness.module.inputMsg.value, 'task draft A')
  assert.equal(harness.url().path, '/')
  assert.equal(harness.url().query.task_open, undefined)

  harness.module.handleCreateNewSession()
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, null)

  harness.task.openSession('B')
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'task draft B')

  harness.task.openSession('B')
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'task draft B')
})

test('HTTP and WebSocket sends clear only the submitted draft, migrate waiting text, and never revive sent text', async t => {
  const httpFirst = createDeferred()
  const httpHarness = await createHarness(t, {
    sessions: [],
    httpResponses: [
      httpFirst.promise,
      { session_id: 'HTTP-1', work_id: 'http-work-1', submission_status: 'accepted' }
    ]
  })
  await httpHarness.flush()
  await httpHarness.chat.setTransportMode('http')

  httpHarness.module.inputMsg.value = 'sent over HTTP'
  const httpSend = httpHarness.module.send()
  await httpHarness.flush()
  assert.equal(httpHarness.module.inputMsg.value, '')
  httpHarness.module.inputMsg.value = 'typed while ID waits'
  httpFirst.resolve({
    choices: [{ finish_reason: 'new_session', message: { content: 'HTTP-1' } }]
  })
  await httpSend
  await httpHarness.flush()

  assert.equal(httpHarness.module.currentSessionId.value, 'HTTP-1')
  assert.equal(httpHarness.module.inputMsg.value, 'typed while ID waits')
  assert.equal(httpHarness.draft('HTTP-1'), 'typed while ID waits')
  assert.equal(httpHarness.storage.getItem(draftKey('user-a', null)), null)
  assert.equal(httpHarness.module.messages.value.some(message => message.content === 'sent over HTTP'), true)

  const wsHarness = await createHarness(t, { sessions: [] })
  await wsHarness.flush()
  wsHarness.module.inputMsg.value = 'sent over WebSocket'
  const wsSend = wsHarness.module.send()
  await wsHarness.flush()
  const wsChat = wsHarness.wsManager.sent.findLast(item => item.type === 'chat')
  assert.ok(wsChat)
  assert.equal(wsHarness.module.inputMsg.value, '')
  wsHarness.module.inputMsg.value = 'typed while WS ID waits'
  wsHarness.receiveWs({
    type: 'session_id',
    session_id: 'WS-1',
    request_id: wsChat.request_id
  })
  await wsHarness.flush()
  wsHarness.receiveWs({
    type: 'input_accepted',
    session_id: 'WS-1',
    request_id: wsChat.request_id
  })
  await wsSend
  await wsHarness.flush()

  assert.equal(wsHarness.module.currentSessionId.value, 'WS-1')
  assert.equal(wsHarness.module.inputMsg.value, 'typed while WS ID waits')
  assert.equal(wsHarness.draft('WS-1'), 'typed while WS ID waits')
  assert.equal(wsHarness.storage.getItem(draftKey('user-a', null)), null)
  assert.equal(wsHarness.module.messages.value.some(message => message.content === 'sent over WebSocket'), true)
})

test('HTTP and WebSocket in-flight queued sends remove their own draft and preserve another session', async t => {
  const httpHarness = await createHarness(t, {
    sessions: [createSession('A', { source: 'http' }), createSession('B', { source: 'http' })],
    httpResponses: [
      { session_id: 'A', work_id: 'http-1', submission_status: 'accepted' },
      { session_id: 'A', work_id: 'http-2', submission_status: 'accepted' }
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await httpHarness.flush()
  httpHarness.module.handleSelectSession(httpHarness.sessions[1])
  await httpHarness.flush()
  httpHarness.module.inputMsg.value = 'keep B'
  await httpHarness.flush()
  httpHarness.module.handleSelectSession(httpHarness.sessions[0])
  await httpHarness.flush()
  httpHarness.module.inputMsg.value = 'first A'
  await httpHarness.module.send()
  httpHarness.chat.loading.value = true
  httpHarness.module.inputMsg.value = 'queued A'
  await httpHarness.module.send()
  await httpHarness.flush()

  assert.equal(httpHarness.draft('A'), null)
  assert.equal(httpHarness.draft('B'), 'keep B')

  const wsHarness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws' }), createSession('B', { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await wsHarness.flush()
  wsHarness.module.handleSelectSession(wsHarness.sessions[1])
  await wsHarness.flush()
  wsHarness.module.inputMsg.value = 'keep WS B'
  await wsHarness.flush()
  wsHarness.module.handleSelectSession(wsHarness.sessions[0])
  await wsHarness.flush()
  wsHarness.module.inputMsg.value = 'first WS A'
  const firstSend = wsHarness.module.send()
  await wsHarness.flush()
  const firstChat = wsHarness.wsManager.sent.findLast(item => item.type === 'chat')
  wsHarness.receiveWs({ type: 'input_accepted', request_id: firstChat.request_id, session_id: 'A' })
  await firstSend
  wsHarness.chat.loading.value = true
  wsHarness.module.inputMsg.value = 'queued WS A'
  await wsHarness.module.send()
  await wsHarness.flush()
  const secondChat = wsHarness.wsManager.sent.findLast(item => item.type === 'chat')
  wsHarness.receiveWs({ type: 'input_accepted', request_id: secondChat.request_id, session_id: 'A' })
  await wsHarness.flush()

  assert.equal(wsHarness.draft('A'), null)
  assert.equal(wsHarness.draft('B'), 'keep WS B')
})

test('read-only guidance never persists or restores and a late response cannot replace a B draft', async t => {
  const guidance = createDeferred()
  const storage = createStorage({ token: 'user-a' })
  const harness = await createHarness(t, {
    storage,
    sessions: [
      createSession('external', { source: 'external', uid: 'owner-b' }),
      createSession('B', { source: 'http' })
    ],
    createGuidance: () => guidance.promise,
    initialRoute: { path: '/', query: { session_id: 'external' } }
  })
  await harness.flush()
  assert.equal(harness.module.isCurrentSessionReadOnly.value, true)
  assert.equal(harness.module.inputMsg.value, '')

  harness.module.inputMsg.value = 'external guidance'
  await harness.flush()
  assert.equal(harness.draft('external'), null)
  const guidanceSend = harness.module.send()
  await harness.flush()

  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  harness.module.inputMsg.value = 'local B draft'
  await harness.flush()
  guidance.resolve({ id: 11, role: 'user', content: 'external guidance' })
  await guidanceSend
  await harness.flush()

  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'local B draft')
  assert.equal(harness.draft('external'), null)
  assert.equal(harness.draft('B'), 'local B draft')
})

test('late profile identity preserves fresh typing and a clear instead of restoring stale null-session text', async t => {
  const staleStorage = createStorage({
    token: 'user-a',
    [draftKey('user-a', null)]: 'stale draft'
  })
  const staleProfile = createDeferred()
  const staleHarness = await createHarness(t, {
    storage: staleStorage,
    profiles: () => staleProfile.promise
  })
  staleHarness.module.inputMsg.value = 'fresh draft'
  await staleHarness.flush()
  staleProfile.resolve({ items: [], currentUid: 'user-a' })
  await staleHarness.flush()
  assert.equal(staleHarness.module.inputMsg.value, 'fresh draft')
  assert.equal(staleHarness.storage.getItem(draftKey('user-a', null)), 'fresh draft')

  const clearedStorage = createStorage({
    token: 'user-a',
    [draftKey('user-a', null)]: 'stale draft'
  })
  const clearedProfile = createDeferred()
  const clearedHarness = await createHarness(t, {
    storage: clearedStorage,
    profiles: () => clearedProfile.promise
  })
  clearedHarness.module.inputMsg.value = 'draft then clear'
  clearedHarness.module.inputMsg.value = ''
  clearedProfile.resolve({ items: [], currentUid: 'user-a' })
  await clearedHarness.flush()
  assert.equal(clearedHarness.module.inputMsg.value, '')
  assert.equal(clearedHarness.storage.getItem(draftKey('user-a', null)), null)
})

test('real delete confirmation only removes a draft after successful deletion', async t => {
  const storage = createStorage({ token: 'user-a' })
  const harness = await createHarness(t, {
    storage,
    sessions: [createSession('A')],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  await harness.flush()
  harness.module.inputMsg.value = 'draft to delete'
  await harness.flush()
  assert.equal(harness.draft('A'), 'draft to delete')

  harness.enqueueConfirmation('cancel')
  assert.equal(await harness.module.handleDeleteSession('A', 'A'), false)
  assert.equal(harness.draft('A'), 'draft to delete')

  harness.enqueueConfirmation(true)
  const failure = new Error('delete failed')
  harness.apiState.deletePlan = failure
  assert.equal(await harness.module.handleDeleteSession('A', 'A'), false)
  assert.equal(harness.draft('A'), 'draft to delete')

  harness.apiState.deletePlan = null
  harness.enqueueConfirmation(true)
  assert.equal(await harness.module.handleDeleteSession('A', 'A'), true)
  await harness.flush()
  assert.equal(harness.draft('A'), null)
  assert.equal(harness.module.currentSessionId.value, null)
})

test('invalid, missing, and stale list/history work cannot overwrite the active draft', async t => {
  const staleList = createDeferred()
  const staleHistory = createDeferred()
  let delayNextList = false
  const harness = await createHarness(t, {
    sessions: [createSession('A'), createSession('B')],
    sessionsList: () => {
      if (!delayNextList) return undefined
      delayNextList = false
      return staleList.promise
    },
    sessionsHistory: ({ sessionId }) => sessionId === 'A' ? staleHistory.promise : undefined
  })
  await harness.flush()

  harness.module.handleSelectSession(harness.sessions[0])
  await harness.flush()
  harness.module.inputMsg.value = 'A draft'
  await harness.flush()
  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  harness.module.inputMsg.value = 'B draft'
  await harness.flush()

  delayNextList = true
  await harness.replace({ path: '/', query: { session_id: 'missing', task_open: 'late' } })
  assert.equal(harness.module.currentSessionId.value, 'B')
  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  staleList.resolve([harness.sessions[0], harness.sessions[1]])
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'B draft')
  assert.equal(harness.draft('B'), 'B draft')

  await harness.replace({ path: '/', query: { session_id: 'missing', task_open: 'missing' } })
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'B draft')
  assert.equal(harness.messages.some(entry => entry.type === 'warning'), true)

  await harness.replace({ path: '/', query: { session_id: '', task_open: 'invalid' } })
  assert.equal(harness.module.currentSessionId.value, 'B')
  await harness.replace({ path: '/', query: { session_id: ['B'], task_open: 'invalid-array' } })
  assert.equal(harness.module.currentSessionId.value, 'B')

  harness.module.handleSelectSession(harness.sessions[0])
  await harness.flush()
  harness.module.inputMsg.value = 'A again'
  harness.module.handleSelectSession(harness.sessions[1])
  await harness.flush()
  staleHistory.resolve([{ id: 99, role: 'assistant', content: 'late A history' }])
  await harness.flush()
  assert.equal(harness.module.currentSessionId.value, 'B')
  assert.equal(harness.module.inputMsg.value, 'B draft')
  assert.equal(harness.module.messages.value.some(message => message.content === 'late A history'), false)
})

test('a late HTTP new-session response after leaving the page cannot remove the new page null draft', async t => {
  const storage = createStorage({ token: 'user-a' })
  const oldResponse = createDeferred()
  const oldHarness = await createHarness(t, {
    storage,
    sessions: [],
    httpResponses: [
      oldResponse.promise,
      { session_id: 'late-HTTP', work_id: 'late-work', submission_status: 'accepted' }
    ]
  })
  await oldHarness.flush()
  await oldHarness.chat.setTransportMode('http')

  oldHarness.module.inputMsg.value = 'first message'
  const oldSendPromise = oldHarness.module.send()
  await oldHarness.flush()
  assert.equal(oldHarness.module.inputMsg.value, '')
  oldHarness.unmount()

  const newHarness = await createHarness(t, { storage, sessions: [] })
  await newHarness.flush()
  newHarness.module.inputMsg.value = 'new page draft'
  await newHarness.flush()

  oldResponse.resolve({
    choices: [{ finish_reason: 'new_session', message: { content: 'late-HTTP' } }]
  })
  await oldSendPromise
  await oldHarness.flush()

  assert.equal(newHarness.module.inputMsg.value, 'new page draft')
  assert.equal(storage.getItem(draftKey('user-a', null)), 'new page draft')
  assert.equal(storage.getItem(draftKey('user-a', 'late-HTTP')), null)
  assert.equal(newHarness.module.currentSessionId.value, null)
})
