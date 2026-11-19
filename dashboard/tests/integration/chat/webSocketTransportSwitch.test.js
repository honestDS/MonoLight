import assert from 'node:assert/strict'
import test from 'node:test'
import { resumeSessionStream } from '../../../src/composables/chat/streamResume.js'
import {
  createTransportManager,
  loadUseChatTransport
} from '../../support/chat/webSocketHarness.js'

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
