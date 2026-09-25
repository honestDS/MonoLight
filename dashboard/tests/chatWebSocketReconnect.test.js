import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { resumeSessionStream } from '../src/composables/chat/streamResume.js'

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
      return timers.length
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
  return { manager, sent, connectCalls }
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
