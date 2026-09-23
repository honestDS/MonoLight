import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

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
  return { manager, sent }
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
