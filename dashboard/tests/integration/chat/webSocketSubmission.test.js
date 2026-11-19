import assert from 'node:assert/strict'
import test from 'node:test'
import { createWorkLifecycleTracker } from '../../../src/composables/chat/workLifecycleTracker.js'
import {
  createTransport,
  createTransportManager,
  loadUseChatTransport
} from '../../support/chat/webSocketHarness.js'

test('wsSend forwards session settings for new sessions and keeps repeated payloads stable', async () => {
  const { manager, sent } = createTransportManager()
  const transport = loadUseChatTransport({ manager })()

  for (const goalMode of [true, false]) {
    for (const maxTurns of [1, 21, 1000000]) {
      assert.equal(await transport.wsSend({
        message: 'new session',
        goalMode,
        maxTurns
      }), true)

      const data = sent.at(-1)
      assert.equal(Object.hasOwn(data, 'goal_mode'), true)
      assert.equal(data.goal_mode, goalMode)
      assert.equal(Object.hasOwn(data, 'max_turns'), true)
      assert.equal(data.max_turns, maxTurns)
    }
  }

  const repeatedOptions = {
    message: 'repeated session settings',
    goalMode: true,
    maxTurns: 21
  }
  await transport.wsSend(repeatedOptions)
  const firstRepeatedPayload = sent.at(-1)
  await transport.wsSend(repeatedOptions)
  assert.deepEqual(sent.at(-1), firstRepeatedPayload)

  await transport.wsSend({ message: 'default session settings' })
  const defaultPayload = sent.at(-1)
  assert.equal(Object.hasOwn(defaultPayload, 'goal_mode'), false)
  assert.equal(Object.hasOwn(defaultPayload, 'max_turns'), false)
})

test('wsSend ignores removed session reasoning effort for new and existing sessions', async () => {
  const { manager, sent } = createTransportManager()
  const transport = loadUseChatTransport({ manager })()

  for (const reasoningEffort of ['custom-tier', 'none']) {
    assert.equal(await transport.wsSend({
      message: 'new session reasoning effort',
      reasoningEffort
    }), true)

    const data = sent.at(-1)
    assert.equal(Object.hasOwn(data, 'reasoning_effort'), false)
  }

  for (const reasoningEffort of [null, undefined]) {
    assert.equal(await transport.wsSend({
      message: 'new session without reasoning effort',
      reasoningEffort
    }), true)

    assert.equal(Object.hasOwn(sent.at(-1), 'reasoning_effort'), false)
  }

  const repeatedPayload = {
    message: 'repeated reasoning effort',
    reasoningEffort: 'custom-tier'
  }
  await transport.wsSend(repeatedPayload)
  const firstRepeatedPayload = sent.at(-1)
  await transport.wsSend(repeatedPayload)
  assert.deepEqual(sent.at(-1), firstRepeatedPayload)

  for (const reasoningEffort of ['high', null]) {
    assert.equal(await transport.wsSend({
      message: 'existing session reasoning effort',
      sessionId: 'existing-reasoning-session',
      reasoningEffort
    }), true)

    const data = sent.at(-1)
    assert.equal(data.session_id, 'existing-reasoning-session')
    assert.equal(Object.hasOwn(data, 'reasoning_effort'), false)
  }
})

test('wsSend does not override settings for an existing session', async () => {
  const { manager, sent } = createTransportManager()
  const transport = loadUseChatTransport({ manager })()

  assert.equal(await transport.wsSend({
    message: 'existing session',
    sessionId: 'existing-session',
    goalMode: false,
    maxTurns: 1000000
  }), true)

  const data = sent.at(-1)
  assert.equal(data.session_id, 'existing-session')
  assert.equal(Object.hasOwn(data, 'goal_mode'), false)
  assert.equal(Object.hasOwn(data, 'max_turns'), false)
})

test('WebSocket submission acknowledgement is routed to the matching request callback', async () => {
  const { manager } = createTransportManager()
  const transport = loadUseChatTransport({ manager })()
  let accepted = null

  const sent = await transport.wsSend({
    message: 'hello',
    sessionId: 'session-1',
    requestId: 'request-1',
    callbacks: {
      onInputAccepted: event => {
        accepted = event
      }
    }
  })
  assert.equal(sent, true)

  const acknowledgementPromise = transport.waitForSubmissionAcknowledgement('request-1')
  manager.emit({
    type: 'input_accepted',
    session_id: 'session-1',
    request_id: 'request-1',
    work_id: 42,
    submission_status: 'accepted'
  })

  const acknowledgement = await acknowledgementPromise
  assert.equal(acknowledgement.status, 'accepted')
  assert.deepEqual(acknowledgement.data, accepted)
  assert.deepEqual(accepted, {
    type: 'input_accepted',
    session_id: 'session-1',
    request_id: 'request-1',
    work_id: 42,
    submission_status: 'accepted'
  })
})

test('WebSocket disconnect before submission acknowledgement is treated as unknown rather than safe to retry', async () => {
  const { manager } = createTransportManager()
  const transport = loadUseChatTransport({ manager })()

  assert.equal(await transport.wsSend({
    message: 'hello',
    sessionId: 'session-1',
    requestId: 'request-unknown'
  }), true)

  const acknowledgementPromise = transport.waitForSubmissionAcknowledgement('request-unknown')
  manager.emit({ type: 'connection_closed' })

  assert.deepEqual(await acknowledgementPromise, {
    status: 'unknown',
    data: null
  })
  assert.equal(transport.transportMode.value, 'ws')
})

test('manual websocket disconnect resolves pending submission acknowledgement immediately', async () => {
  const transportManager = createTransportManager()
  const transport = loadUseChatTransport({ manager: transportManager.manager })()

  assert.equal(await transport.wsSend({
    message: 'hello',
    sessionId: 'session-1',
    requestId: 'request-manual-disconnect'
  }), true)

  const acknowledgementPromise = transport.waitForSubmissionAcknowledgement('request-manual-disconnect')
  transport.disconnectWebSocket()

  assert.deepEqual(await acknowledgementPromise, {
    status: 'unknown',
    data: null
  })
  assert.equal(transportManager.disconnectCalls, 1)
})

test('cancelled ws work finishes lifecycle without completion, error, or disconnect', async () => {
  const transportManager = createTransportManager()
  const transport = loadUseChatTransport({ manager: transportManager.manager })()
  const lifecycle = createWorkLifecycleTracker()
  let messages = lifecycle.startRequestLifecycle([], {
    request_id: 'request-cancel',
    work_id: 'work-cancel'
  })
  let acceptedRequestId = null
  const finished = []
  let loading = true
  let completeCalls = 0
  let errorCalls = 0
  const inputAccepted = {
    type: 'input_accepted',
    session_id: 'session-cancel',
    request_id: 'request-cancel',
    work_id: 'work-cancel',
    submission_status: 'accepted'
  }
  const cancelled = {
    type: 'cancelled',
    session_id: 'session-cancel',
    request_ids: ['request-cancel'],
    work_id: 'work-cancel'
  }

  assert.equal(await transport.wsSend({
    message: 'hello',
    sessionId: 'session-cancel',
    requestId: 'request-cancel',
    callbacks: {
      onInputAccepted: event => {
        acceptedRequestId = event.request_id
      },
      onWorkFinished: event => {
        finished.push(event)
        messages = lifecycle.finishWorkLifecycle(messages, event)
      },
      onComplete: () => {
        completeCalls++
      },
      onError: () => {
        errorCalls++
      },
      setLoading: value => {
        loading = value
      }
    }
  }), true)

  transportManager.manager.emit(inputAccepted)
  assert.equal(acceptedRequestId, 'request-cancel')

  transportManager.manager.emit(cancelled)

  assert.deepEqual(finished, [cancelled])
  assert.equal(loading, false)
  assert.equal(completeCalls, 0)
  assert.equal(errorCalls, 0)
  assert.equal(lifecycle.isWorkTerminal('work-cancel'), true)
  assert.deepEqual(
    lifecycle.startRequestLifecycle(messages, {
      request_id: 'request-cancel',
      work_id: 'work-cancel'
    }),
    messages
  )
  assert.equal(transportManager.disconnectCalls, 0)
  assert.equal(transportManager.manager.isConnected.value, true)
  assert.equal(transport.wsConnected.value, true)
})

test('cancelled resumed work keeps loading until resume completes', async () => {
  const transportManager = createTransportManager()
  const transport = loadUseChatTransport({ manager: transportManager.manager })()
  const lifecycle = createWorkLifecycleTracker()
  let messages = lifecycle.startRequestLifecycle([], {
    request_id: 'resume-request',
    work_id: 'resume-work'
  })
  let acceptedRequestId = null
  const finished = []
  let loading = true
  let completeCalls = 0
  let errorCalls = 0
  let resumeCompleteCalls = 0
  const inputAccepted = {
    type: 'input_accepted',
    session_id: 'session-resume',
    request_id: 'resume-request',
    work_id: 'resume-work',
    submission_status: 'accepted'
  }
  const cancelled = {
    type: 'cancelled',
    session_id: 'session-resume',
    request_ids: ['resume-request'],
    work_id: 'resume-work'
  }

  assert.equal(await transport.resumeSession({
    sessionId: 'session-resume',
    historyMessageId: 7,
    callbacks: {
      onInputAccepted: event => {
        acceptedRequestId = event.request_id
      },
      onWorkFinished: event => {
        finished.push(event)
        messages = lifecycle.finishWorkLifecycle(messages, event)
      },
      onComplete: () => {
        completeCalls++
      },
      onError: () => {
        errorCalls++
      },
      onResumeComplete: () => {
        resumeCompleteCalls++
      },
      deferLoadingUntilResumeComplete: true,
      setLoading: value => {
        loading = value
      }
    }
  }), true)

  transportManager.manager.emit(inputAccepted)
  assert.equal(acceptedRequestId, 'resume-request')

  transportManager.manager.emit(cancelled)

  assert.deepEqual(finished, [cancelled])
  assert.equal(loading, true)
  assert.equal(completeCalls, 0)
  assert.equal(errorCalls, 0)
  assert.equal(lifecycle.isWorkTerminal('resume-work'), true)

  transportManager.manager.emit({
    type: 'resume_complete',
    session_id: 'session-resume'
  })

  assert.equal(resumeCompleteCalls, 1)
  assert.equal(loading, false)
})

test('cancelled merged work clears only its request callbacks', async () => {
  const transportManager = createTransportManager()
  const transport = loadUseChatTransport({ manager: transportManager.manager })()
  const lifecycle = createWorkLifecycleTracker()
  let messages = lifecycle.startRequestLifecycle([], {
    request_id: 'request-a1',
    work_id: 'work-a'
  })
  messages = lifecycle.startRequestLifecycle(messages, {
    request_id: 'request-a2',
    work_id: 'work-a'
  })
  const acceptedRequestIds = []
  const workFinished = []
  const workContents = []
  const otherContents = []
  const workCallbacks = {
    onInputAccepted: event => {
      acceptedRequestIds.push(event.request_id)
    },
    onWorkFinished: event => {
      workFinished.push(event)
      messages = lifecycle.finishWorkLifecycle(messages, event)
    },
    onContent: content => {
      workContents.push(content)
    }
  }
  const otherCallbacks = {
    onInputAccepted: event => {
      acceptedRequestIds.push(event.request_id)
    },
    onContent: content => {
      otherContents.push(content)
    }
  }

  assert.equal(await transport.wsSend({
    message: 'first merged request',
    sessionId: 'session-a',
    requestId: 'request-a1',
    callbacks: workCallbacks
  }), true)
  assert.equal(await transport.wsSend({
    message: 'second merged request',
    sessionId: 'session-a',
    requestId: 'request-a2',
    callbacks: workCallbacks
  }), true)
  assert.equal(await transport.wsSend({
    message: 'other work',
    sessionId: 'session-b',
    requestId: 'request-b',
    callbacks: otherCallbacks
  }), true)

  transportManager.manager.emit({
    type: 'input_accepted',
    session_id: 'session-a',
    request_id: 'request-a1',
    work_id: 'work-a',
    submission_status: 'accepted'
  })
  transportManager.manager.emit({
    type: 'input_accepted',
    session_id: 'session-a',
    request_id: 'request-a2',
    work_id: 'work-a',
    submission_status: 'accepted'
  })
  transportManager.manager.emit({
    type: 'input_accepted',
    session_id: 'session-b',
    request_id: 'request-b',
    work_id: 'work-b',
    submission_status: 'accepted'
  })
  assert.deepEqual(acceptedRequestIds, ['request-a1', 'request-a2', 'request-b'])

  const cancelled = {
    type: 'cancelled',
    session_id: 'session-a',
    request_ids: ['request-a1', 'request-a2'],
    work_id: 'work-a'
  }
  transportManager.manager.emit(cancelled)

  transportManager.manager.emit({
    type: 'content',
    session_id: 'session-a',
    request_id: 'request-a1',
    work_id: 'work-a',
    content: 'late first request'
  })
  transportManager.manager.emit({
    type: 'content',
    session_id: 'session-a',
    request_id: 'request-a2',
    work_id: 'work-a',
    content: 'late second request'
  })
  transportManager.manager.emit({
    type: 'content',
    session_id: 'session-b',
    request_id: 'request-b',
    work_id: 'work-b',
    content: 'other work remains active'
  })

  assert.deepEqual(workFinished, [cancelled])
  assert.deepEqual(workContents, [])
  assert.deepEqual(otherContents, ['other work remains active'])
  assert.equal(lifecycle.isWorkTerminal('work-a'), true)
  assert.deepEqual(
    lifecycle.startRequestLifecycle(messages, {
      request_id: 'request-a1',
      work_id: 'work-a'
    }),
    messages
  )
})

test('transport exposes missing-token and websocket-send failure results', async () => {
  const missingTokenManager = createTransportManager()
  const missingTokenTransport = createTransport({
    manager: missingTokenManager.manager,
    getToken: () => null
  }).transport

  await assert.rejects(
    () => missingTokenTransport.wsSend({ message: 'not sent' }),
    error => error.message === 'chat.not_logged_in'
  )
  assert.deepEqual(missingTokenManager.sent, [])

  const failedManager = createTransportManager({ sendResult: false })
  const errors = []
  const failedTransport = createTransport({
    manager: failedManager.manager,
    errors
  }).transport

  assert.equal(await failedTransport.wsSend({
    message: 'send failure',
    requestId: 'failed-request'
  }), false)
  assert.deepEqual(
    await failedTransport.waitForSubmissionAcknowledgement('failed-request'),
    { status: 'unknown', data: null }
  )
  assert.equal(await failedTransport.resumeSession({ sessionId: 'failed-session' }), false)
  assert.deepEqual(errors, ['chat.ws_message_send_failed'])
})
