import assert from 'node:assert/strict'
import test from 'node:test'
import { createDeferred, createHarness } from './replyControlHarness.js'

test('stopReply clears active lifecycle state, preserves history and prevents late loop resurrection', async () => {
  const historicalMessage = {
    id: 'history',
    db_id: 42,
    role: 'assistant',
    content: 'historical answer'
  }
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: true,
    is_reply_running: true,
    reply_works: [
      { work_id: 'work-A', status: 'running', request_ids: ['request-A'] }
    ]
  }
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'draft prompt',
    attachments: [{ path: 'draft.txt' }],
    messages: [
      historicalMessage,
      {
        id: 'user-A',
        role: 'user',
        content: 'queued prompt',
        request_id: 'request-A',
        status: 'queued'
      },
      {
        id: 'thinking-A',
        role: 'thinking',
        content: 'partial reply',
        work_id: 'work-A',
        request_ids: ['request-A']
      }
    ]
  })

  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(session.is_loading, true)
  assert.equal(session.is_reply_running, false)
  assert.equal(harness.chatState.inputMsg.value, 'draft prompt')
  assert.deepEqual(harness.attachments.value, [{ path: 'draft.txt' }])
  assert.equal(
    harness.chatState.messages.value.some(message => message.role === 'thinking'),
    false
  )
  assert.equal(
    harness.chatState.messages.value.find(message => message.id === 'user-A').status,
    undefined
  )
  assert.equal(
    harness.chatState.messages.value.find(message => message.id === 'history').content,
    'historical answer'
  )
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)

  const afterLateLoop = harness.workLifecycleTracker.startAgentLoop(
    harness.chatState.messages.value,
    { work_id: 'work-A', response_id: 'late-response', request_ids: ['request-A'] }
  )
  assert.deepEqual(afterLateLoop, harness.chatState.messages.value)
  assert.equal(afterLateLoop.some(message => message.role === 'thinking'), false)
})

test('stopReply sends only one stop and blocks repeats while stopping', async () => {
  const stopDeferred = createDeferred()
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true
  })
  harness.stopBehavior = () => stopDeferred.promise

  const stopping = harness.stopReply()
  assert.equal(harness.isStopping.value, true)
  assert.equal(await harness.stopReply(), false)
  assert.deepEqual(harness.stopCalls, ['A'])

  stopDeferred.resolve()
  assert.equal(await stopping, true)
  assert.equal(harness.isStopping.value, false)
})

test('stopReply waits for same-session submissions before a second stop, including rejected submissions', async () => {
  for (const shouldReject of [false, true]) {
    const stopDeferreds = []
    const secondStopCalled = createDeferred()
    const harness = createHarness({
      sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
      currentSessionId: 'A',
      loading: true
    })
    harness.stopBehavior = () => {
      const deferred = createDeferred()
      stopDeferreds.push(deferred)
      if (stopDeferreds.length === 2) secondStopCalled.resolve()
      return deferred.promise
    }

    const submissionDeferred = createDeferred()
    const trackedSubmission = harness.trackSubmission(
      () => 'A',
      () => submissionDeferred.promise
    )
    trackedSubmission.catch(() => undefined)

    const stopping = harness.stopReply()
    assert.equal(harness.isStopping.value, true)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, ['A'])

    stopDeferreds[0].resolve()
    if (shouldReject) {
      submissionDeferred.reject(new Error('submission failed'))
    } else {
      submissionDeferred.resolve('submitted')
    }
    await secondStopCalled.promise
    assert.deepEqual(harness.stopCalls, ['A', 'A'])

    stopDeferreds[1].resolve()
    assert.equal(await stopping, true)
    await trackedSubmission.catch(() => undefined)
    assert.equal(harness.isStopping.value, false)
    assert.equal(harness.chatState.loading.value, false)
  }
})

test('stopReply ignores in-flight submissions belonging to another session', async () => {
  const submissionDeferred = createDeferred()
  let submissionSettled = false
  const harness = createHarness({
    sessions: [
      { session_id: 'A', source: 'http', is_loading: true, is_reply_running: true },
      { session_id: 'B', source: 'http', is_loading: true, is_reply_running: true }
    ],
    currentSessionId: 'A',
    loading: true
  })
  const trackedSubmission = harness.trackSubmission(
    () => 'B',
    () => submissionDeferred.promise
  )
  trackedSubmission.then(() => {
    submissionSettled = true
  })

  harness.stopBehavior = async () => undefined
  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(submissionSettled, false)

  submissionDeferred.resolve('submitted')
  await trackedSubmission
  assert.equal(submissionSettled, true)
})

test('stopReply preserves loading, messages and draft when the stop request fails', async () => {
  const messages = [
    { id: 'queued', role: 'user', request_id: 'request-A', status: 'queued' },
    { id: 'thinking', role: 'thinking', request_ids: ['request-A'] }
  ]
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'keep this draft',
    attachments: [{ path: 'keep-me.txt' }],
    messages
  })
  harness.stopBehavior = async () => {
    throw new Error('stop failed')
  }

  assert.equal(await harness.stopReply(), false)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.errors.at(-1), 'stop failed')
  assert.equal(harness.chatState.loading.value, true)
  assert.equal(harness.sessions.value[0].is_loading, true)
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.deepEqual(harness.chatState.messages.value, messages)
  assert.deepEqual(harness.attachments.value, [{ path: 'keep-me.txt' }])
  assert.equal(harness.isStopping.value, false)
})

test('stopReply does not send a stop for read-only, missing or idle sessions', async () => {
  const cases = [
    {
      sessions: [{ session_id: 'read-only', source: 'external', is_loading: true, is_reply_running: false }],
      currentSessionId: 'read-only',
      loading: true
    },
    {
      sessions: [],
      currentSessionId: null,
      loading: true
    },
    {
      sessions: [{ session_id: 'idle', source: 'http', is_loading: false, is_reply_running: false }],
      currentSessionId: 'idle',
      loading: false
    }
  ]

  for (const options of cases) {
    const harness = createHarness(options)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, [])
  }
})

test('stopReply ignores aggregate-only busy sessions for HTTP and WS', async () => {
  for (const source of ['http', 'ws']) {
    const session = {
      session_id: 'A',
      source,
      is_loading: true,
      is_reply_running: false,
      reply_works: []
    }
    const harness = createHarness({
      sessions: [session],
      currentSessionId: 'A',
      transportMode: source,
      inputMsg: 'keep this draft',
      attachments: [{ path: 'keep-me.txt' }]
    })

    assert.equal(harness.isReplyRunning.value, false)
    assert.equal(await harness.stopReply(), false)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, [])
    assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
    assert.deepEqual(harness.attachments.value, [{ path: 'keep-me.txt' }])
    assert.equal(session.is_loading, true)
    assert.equal(session.is_reply_running, false)
  }
})

test('processHttpSessionSnapshot clears stale local loading for an aggregate-only session', async () => {
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: true,
    is_reply_running: false,
    reply_works: []
  }
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'keep this draft'
  })

  assert.equal(harness.isReplyRunning.value, true)
  await harness.processHttpSessionSnapshot([session])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(harness.isReplyRunning.value, false)
  assert.equal(session.is_loading, true)
  assert.equal(session.is_reply_running, false)

  await harness.processHttpSessionSnapshot([session])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(harness.isReplyRunning.value, false)
  assert.equal(await harness.stopReply(), false)
  assert.equal(await harness.stopReply(), false)
  assert.deepEqual(harness.stopCalls, [])
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.equal(session.is_loading, true)
})

test('stopReply clears only remote reply state when aggregate work remains for HTTP and WS', async () => {
  for (const source of ['http', 'ws']) {
    const session = {
      session_id: 'A',
      source,
      is_loading: true,
      is_reply_running: true,
      reply_works: []
    }
    const harness = createHarness({
      sessions: [session],
      currentSessionId: 'A',
      transportMode: source,
      inputMsg: 'keep this draft'
    })

    assert.equal(harness.isReplyRunning.value, true)
    assert.equal(await harness.stopReply(), true)
    assert.deepEqual(harness.stopCalls, ['A'])
    assert.equal(harness.chatState.loading.value, false)
    assert.equal(session.is_reply_running, false)
    assert.equal(session.is_loading, true)
    assert.equal(harness.isReplyRunning.value, false)
    assert.equal(await harness.stopReply(), false)
    assert.deepEqual(harness.stopCalls, ['A'])
    assert.equal(session.is_loading, true)
  }
})

test('stopReply uses local loading before remote reply confirmation', async () => {
  const session = {
    session_id: 'A',
    source: 'http',
    is_loading: false,
    is_reply_running: false,
    reply_works: []
  }
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'keep this draft'
  })

  assert.equal(harness.isReplyRunning.value, true)
  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(session.is_loading, false)
  assert.equal(session.is_reply_running, false)
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
})

test('stopReply isolates a completed stop for A after switching to B and allows B to stop independently', async () => {
  const stopA = createDeferred()
  const sessionA = { session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }
  const sessionB = { session_id: 'B', source: 'http', is_loading: true, is_reply_running: true }
  const harness = createHarness({
    sessions: [sessionA, sessionB],
    currentSessionId: 'A',
    loading: true,
    inputMsg: 'A draft',
    attachments: [{ path: 'A-draft.txt' }],
    messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }]
  })
  harness.stopBehavior = sessionId => sessionId === 'A' ? stopA.promise : Promise.resolve()

  const stoppingA = harness.stopReply()
  assert.equal(harness.isStopping.value, true)

  harness.currentSessionId.value = 'B'
  harness.chatState.messages.value = [
    { role: 'thinking', work_id: 'work-B', request_ids: ['request-B'] }
  ]
  harness.attachments.value = [{ path: 'B-draft.txt' }]
  harness.chatState.inputMsg.value = 'B draft'
  harness.chatState.loading.value = true

  stopA.resolve()
  assert.equal(await stoppingA, true)
  assert.deepEqual(harness.stopCalls, ['A'])
  assert.equal(harness.chatState.loading.value, true)
  assert.deepEqual(harness.chatState.messages.value, [
    { role: 'thinking', work_id: 'work-B', request_ids: ['request-B'] }
  ])
  assert.equal(harness.chatState.inputMsg.value, 'B draft')
  assert.deepEqual(harness.attachments.value, [{ path: 'B-draft.txt' }])

  assert.equal(await harness.stopReply(), true)
  assert.deepEqual(harness.stopCalls, ['A', 'B'])
  assert.equal(harness.chatState.loading.value, false)
  assert.equal(sessionB.is_loading, true)
  assert.equal(sessionB.is_reply_running, false)
})

test('stopReply does not clear current state after the session scope is disposed', async () => {
  const stopDeferred = createDeferred()
  const session = { session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    loading: true,
    messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }]
  })
  harness.stopBehavior = () => stopDeferred.promise

  const stopping = harness.stopReply()
  harness.sessionScopeActive = false
  stopDeferred.resolve()

  assert.equal(await stopping, true)
  assert.equal(harness.chatState.loading.value, true)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)
  assert.equal(session.is_reply_running, true)
  assert.deepEqual(harness.historyMergeCalls, [])
})

test('stopReply terminates compression tracked only by a work key', async () => {
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true,
    messages: [
      { id: 'history', role: 'assistant', db_id: 7, content: 'keep this' },
      { id: 'summary', role: 'thinking', request_id: 'summary-request', content: 'compressing' }
    ]
  })
  harness.contextSummaryTracker.startContextSummaryWork(
    harness.contextSummaryWorkKeys.value,
    harness.contextSummaryRequestKeys,
    { session_id: 'A', work_id: 'summary-work' },
    'summary-request'
  )
  assert.equal(harness.contextSummaryWorkKeys.value.has('work:summary-work'), true)

  assert.equal(await harness.stopReply(), true)
  assert.equal(harness.contextSummaryWorkKeys.value.has('work:summary-work'), false)
  assert.equal(harness.contextSummaryRequestKeys.has('summary-request'), false)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('summary-work'), true)
  assert.equal(
    harness.chatState.messages.value.some(message => message.role === 'thinking'),
    false
  )
  assert.equal(harness.chatState.messages.value[0].content, 'keep this')
})

test('stopReply clears residual no-work thinking after a cancelled lifecycle was already terminal', async () => {
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http', is_loading: true, is_reply_running: true }],
    currentSessionId: 'A',
    loading: true,
    messages: [
      { id: 'residual', role: 'thinking', request_id: 'cancel-request', content: 'stale' }
    ]
  })
  harness.workLifecycleTracker.finishWorkLifecycle([], {
    type: 'cancelled',
    session_id: 'A',
    work_id: 'cancelled-work',
    request_ids: ['cancel-request']
  })

  assert.equal(await harness.stopReply(), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('cancelled-work'), true)
  assert.equal(
    harness.chatState.messages.value.some(message => message.role === 'thinking'),
    false
  )
  assert.equal(harness.chatState.loading.value, false)
})
