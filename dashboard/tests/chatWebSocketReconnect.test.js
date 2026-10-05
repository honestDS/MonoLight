import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { resumeSessionStream } from '../src/composables/chat/streamResume.js'
import { createWorkLifecycleTracker } from '../src/composables/chat/workLifecycleTracker.js'

const source = readFileSync(
  new URL('../src/composables/useWebSocket.js', import.meta.url),
  'utf8'
)
const transportSource = readFileSync(
  new URL('../src/composables/chat/useChatTransport.js', import.meta.url),
  'utf8'
)

const loadUseWebSocket = ({ sockets, timers }) => {
  const moduleSource = source
    .replace(/^import .* from 'vue'$/m, '')
    .replace(/^import .* from '\.\.\/api'$/m, '')
    .replace(/^import .* from 'element-plus'$/m, '')
    .replace(/^import .* from '\.\.\/i18n'$/m, '')
    .replace('export function useWebSocket()', 'function useWebSocket()')

  return new Function(
    'ref',
    'onUnmounted',
    'chatApi',
    'ElMessage',
    'i18n',
    'setTimeout',
    'clearTimeout',
    'WebSocket',
    `${moduleSource}\nreturn useWebSocket`
  )(
    value => ({ value }),
    () => {},
    { createWebSocket: token => {
      const socket = new FakeWebSocket(token)
      sockets.push(socket)
      return socket
    } },
    { warning: () => {} },
    { global: { t: key => key } },
    callback => {
      timers.push(callback)
      return callback
    },
    callback => {
      const index = timers.indexOf(callback)
      if (index !== -1) timers.splice(index, 1)
    },
    { OPEN: 1 }
  )
}

const loadUseChatTransport = ({ manager }) => {
  const moduleSource = transportSource
    .replace(/^import .*$/gm, '')
    .replace('export function useChatTransport()', 'function useChatTransport()')

  return new Function(
    'ref',
    'ElMessage',
    'chatApi',
    'useWebSocket',
    'i18n',
    'truncateErrorMessage',
    'getStreamEventIdentity',
    'localStorage',
    `${moduleSource}\nreturn useChatTransport`
  )(
    value => ({ value }),
    { error: () => {} },
    { completions: async () => ({ data: {} }) },
    () => manager,
    { global: { t: key => key } },
    value => value,
    () => null,
    { getItem: () => 'token' }
  )
}

const createTransportManager = () => {
  const handlers = []
  const sent = []
  const connectCalls = []
  let disconnectCalls = 0
  const manager = {
    isConnected: { value: false },
    onMessage(handler) {
      handlers.push(handler)
      return () => {
        const index = handlers.indexOf(handler)
        if (index >= 0) handlers.splice(index, 1)
      }
    },
    async connect() {
      connectCalls.push(true)
      manager.isConnected.value = true
    },
    disconnect() {
      disconnectCalls += 1
      manager.isConnected.value = false
    },
    sendMessage(data) {
      sent.push(data)
      return true
    },
    emit(data) {
      if (data.type === 'connection_closed') manager.isConnected.value = false
      if (data.type === 'connection_reopened') manager.isConnected.value = true
      handlers.forEach(handler => handler(data))
    }
  }
  return {
    manager,
    sent,
    connectCalls,
    get disconnectCalls() {
      return disconnectCalls
    }
  }
}

class FakeWebSocket {
  constructor(token) {
    this.token = token
    this.readyState = 0
    this.onopen = null
    this.onclose = null
    this.onerror = null
  }

  close(code, reason) {
    this.readyState = 3
    this.closeEvent = { code, reason }
    this.onclose?.(this.closeEvent)
  }
}

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

test('initial connection does not notify connection_reopened', async () => {
  const sockets = []
  const timers = []
  const useWebSocket = loadUseWebSocket({ sockets, timers })
  const manager = useWebSocket()
  const messages = []
  manager.onMessage(message => messages.push(message))

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected

  assert.deepEqual(messages, [])
})

test('concurrent connect calls reuse the in-flight connection', async () => {
  const sockets = []
  const timers = []
  const useWebSocket = loadUseWebSocket({ sockets, timers })
  const manager = useWebSocket()

  const first = manager.connect('token')
  const second = manager.connect('token')

  assert.equal(sockets.length, 1)
  assert.strictEqual(second, first)

  sockets[0].readyState = 1
  sockets[0].onopen()
  await Promise.all([first, second])

  assert.equal(manager.isConnected.value, true)
})

test('abnormal close reconnects and notifies subscribers after reopening', async () => {
  const sockets = []
  const timers = []
  const useWebSocket = loadUseWebSocket({ sockets, timers })
  const manager = useWebSocket()
  const messages = []
  manager.onMessage(message => messages.push(message))

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected

  sockets[0].onclose({ code: 1006, reason: 'abnormal' })
  assert.deepEqual(messages, [{ type: 'connection_closed' }])
  assert.equal(timers.length, 1)

  timers.shift()()
  assert.equal(sockets.length, 2)
  sockets[1].readyState = 1
  sockets[1].onopen()

  assert.deepEqual(messages, [
    { type: 'connection_closed' },
    { type: 'connection_reopened' }
  ])
})

test('manual disconnect does not notify connection_reopened or schedule reconnect', async () => {
  const sockets = []
  const timers = []
  const useWebSocket = loadUseWebSocket({ sockets, timers })
  const manager = useWebSocket()
  const messages = []
  manager.onMessage(message => messages.push(message))

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected
  manager.disconnect()

  assert.deepEqual(messages, [])
  assert.equal(timers.length, 0)
})

test('remote normal close notifies transport state without scheduling reconnect', async () => {
  const sockets = []
  const timers = []
  const useWebSocket = loadUseWebSocket({ sockets, timers })
  const manager = useWebSocket()
  const events = []
  manager.onMessage(event => events.push(event.type))

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected

  sockets[0].onclose({ code: 1000, reason: 'server shutdown' })

  assert.equal(manager.isConnected.value, false)
  assert.deepEqual(events, ['connection_closed'])
  assert.equal(timers.length, 0)
})

test('manual disconnect cancels an already scheduled reconnect', async () => {
  const sockets = []
  const timers = []
  const useWebSocket = loadUseWebSocket({ sockets, timers })
  const manager = useWebSocket()

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected

  sockets[0].onclose({ code: 1006, reason: 'abnormal' })
  assert.equal(timers.length, 1)

  manager.disconnect()
  assert.equal(timers.length, 0)
  assert.equal(sockets.length, 1)
})

test('a fresh websocket connection after manual disconnect gets a new reconnect budget', async () => {
  const sockets = []
  const timers = []
  const useWebSocket = loadUseWebSocket({ sockets, timers })
  const manager = useWebSocket()

  const firstConnection = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await firstConnection
  manager.disconnect()

  const secondConnection = manager.connect('token')
  sockets[1].onclose({ code: 1006, reason: 'failed before open' })
  await assert.rejects(secondConnection)

  assert.equal(timers.length, 1)
})

test('transport clears request callbacks and resumes the session after reconnect', async () => {
  const { manager, sent } = createTransportManager()
  const transport = loadUseChatTransport({ manager })()
  let reconnectCalls = 0
  let oldRequestCompletions = 0

  transport.setReconnectHandler(() => {
    reconnectCalls++
    return transport.resumeSession({ sessionId: 's', historyMessageId: 7 })
  })

  await transport.wsSend({
    message: 'old request',
    requestId: 'old-request',
    callbacks: {
      onComplete: () => {
        oldRequestCompletions++
      }
    }
  })
  assert.equal(sent[0].type, 'chat')

  manager.emit({ type: 'connection_closed' })
  assert.equal(transport.wsConnected.value, false)

  manager.emit({ type: 'connection_reopened' })
  await Promise.resolve()
  assert.equal(transport.wsConnected.value, true)
  assert.equal(reconnectCalls, 1)
  assert.deepEqual(sent.at(-1), {
    type: 'resume',
    session_id: 's',
    history_message_id: 7
  })

  manager.emit({ type: 'done', request_id: 'old-request' })
  assert.equal(oldRequestCompletions, 0)
})

test('a stale websocket reopened event cannot reactivate transport after switching to HTTP', async () => {
  const transportManager = createTransportManager()
  const transport = loadUseChatTransport({ manager: transportManager.manager })()
  let reconnectCalls = 0
  transport.setReconnectHandler(() => {
    reconnectCalls += 1
  })

  await transport.setTransportMode('http')
  transportManager.manager.emit({ type: 'connection_reopened' })
  await Promise.resolve()

  assert.equal(transport.transportMode.value, 'http')
  assert.equal(transport.wsConnected.value, false)
  assert.equal(reconnectCalls, 0)
})

test('selected HTTP sessions restore mode after remount without resuming a stream', async () => {
  const session = {
    session_id: 'http-session',
    source: 'http',
    is_loading: true
  }
  const httpManager = createTransportManager()
  const httpTransport = loadUseChatTransport({ manager: httpManager.manager })()
  const sessionTransportMode = session.source === 'http' ? 'http' : 'ws'

  await httpTransport.setTransportMode(sessionTransportMode)
  assert.equal(httpTransport.transportMode.value, 'http')

  let resumeCalls = 0
  await resumeSessionStream({
    session,
    transportMode: httpTransport.transportMode.value,
    isCurrentSession: () => true,
    setLoading: () => {},
    resume: () => {
      resumeCalls++
      return httpTransport.resumeSession({ sessionId: session.session_id })
    }
  })

  assert.equal(resumeCalls, 0)
  assert.equal(httpManager.connectCalls.length, 0)
  assert.deepEqual(httpManager.sent, [])
})

test('switching to HTTP cancels an in-flight websocket resume before it can subscribe', async () => {
  const transportManager = createTransportManager()
  let resolveConnect
  transportManager.manager.connect = () => {
    transportManager.connectCalls.push(true)
    return new Promise(resolve => {
      resolveConnect = () => {
        transportManager.manager.isConnected.value = true
        resolve()
      }
    })
  }
  const transport = loadUseChatTransport({ manager: transportManager.manager })()

  const resumePromise = transport.resumeSession({
    sessionId: 'session-1',
    historyMessageId: 7
  })
  await Promise.resolve()
  assert.equal(transportManager.connectCalls.length, 1)

  await transport.setTransportMode('http')
  resolveConnect()

  assert.equal(await resumePromise, false)
  assert.equal(transport.transportMode.value, 'http')
  assert.equal(transport.wsConnected.value, false)
  assert.equal(transportManager.disconnectCalls, 1)
  assert.deepEqual(transportManager.sent, [])
})

test('manual transport disposal invalidates an in-flight websocket send before it can submit', async () => {
  const transportManager = createTransportManager()
  let resolveConnect
  transportManager.manager.connect = () => new Promise(resolve => {
    resolveConnect = () => {
      transportManager.manager.isConnected.value = true
      resolve()
    }
  })
  const transport = loadUseChatTransport({ manager: transportManager.manager })()

  const sendPromise = transport.wsSend({
    message: 'hello',
    sessionId: 'session-1',
    requestId: 'request-disposed'
  })
  await Promise.resolve()

  transport.disconnectWebSocket()
  resolveConnect()

  assert.equal(await sendPromise, false)
  assert.equal(transport.wsConnected.value, false)
  assert.deepEqual(transportManager.sent, [])
})

test('switching to HTTP invalidates an in-flight websocket initialization', async () => {
  const transportManager = createTransportManager()
  let resolveConnect
  transportManager.manager.connect = () => new Promise(resolve => {
    resolveConnect = () => {
      transportManager.manager.isConnected.value = true
      resolve()
    }
  })
  const transport = loadUseChatTransport({ manager: transportManager.manager })()

  const initPromise = transport.initWebSocket()
  await Promise.resolve()
  await transport.setTransportMode('http')
  resolveConnect()

  assert.equal(await initPromise, false)
  assert.equal(transport.wsConnected.value, false)
  assert.equal(transport.transportMode.value, 'http')
})

test('selected WS sessions resume after remount when server source is persisted as WS', async () => {
  const session = {
    session_id: 'ws-session',
    source: 'ws',
    is_loading: true
  }
  const wsManager = createTransportManager()
  const wsTransport = loadUseChatTransport({ manager: wsManager.manager })()

  assert.equal(wsTransport.transportMode.value, 'ws')
  await wsTransport.setTransportMode(session.source)

  let resumeCalls = 0
  await resumeSessionStream({
    session,
    transportMode: wsTransport.transportMode.value,
    isCurrentSession: () => true,
    setLoading: () => {},
    resume: () => {
      resumeCalls++
      return wsTransport.resumeSession({ sessionId: session.session_id, historyMessageId: 7 })
    }
  })

  assert.equal(resumeCalls, 1)
  assert.equal(wsManager.connectCalls.length, 1)
  assert.deepEqual(wsManager.sent, [{
    type: 'resume',
    session_id: 'ws-session',
    history_message_id: 7
  }])
})

test('WebSocket connection failure leaves transport mode unchanged for the session layer to decide fallback', async () => {
  const failedManager = createTransportManager()
  failedManager.manager.connect = async () => {
    throw new Error('connection failed')
  }
  const transport = loadUseChatTransport({ manager: failedManager.manager })()

  assert.equal(transport.transportMode.value, 'ws')
  assert.equal(await transport.wsSend({ message: 'hello' }), false)
  assert.equal(transport.transportMode.value, 'ws')

  const remountedTransport = loadUseChatTransport({
    manager: createTransportManager().manager
  })()
  assert.equal(remountedTransport.transportMode.value, 'ws')
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
