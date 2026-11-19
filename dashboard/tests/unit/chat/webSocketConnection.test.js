import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createConnection,
  loadUseWebSocket
} from '../../support/chat/webSocketHarness.js'

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

test('the same websocket subscriber is registered once and can be cancelled safely', async () => {
  const sockets = []
  const timers = []
  const { manager } = createConnection({ sockets, timers })
  const messages = []
  const handler = message => messages.push(message)
  const cancelFirst = manager.onMessage(handler)
  const cancelSecond = manager.onMessage(handler)

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected

  sockets[0].onmessage({ data: JSON.stringify({ type: 'first' }) })
  assert.deepEqual(messages, [{ type: 'first' }])

  cancelFirst()
  sockets[0].onmessage({ data: JSON.stringify({ type: 'after-first-cancel' }) })
  assert.deepEqual(messages, [{ type: 'first' }])

  cancelSecond()
  cancelSecond()
  sockets[0].onmessage({ data: JSON.stringify({ type: 'after-second-cancel' }) })
  assert.deepEqual(messages, [{ type: 'first' }])
})

test('invalid websocket JSON is delivered as a raw message', async () => {
  const sockets = []
  const timers = []
  const { manager } = createConnection({ sockets, timers })
  const messages = []
  manager.onMessage(message => messages.push(message))

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected

  sockets[0].onmessage({ data: 'not-json' })

  assert.deepEqual(messages, [{ type: 'raw', data: 'not-json' }])
})

test('events from an old websocket are ignored after reconnecting', async () => {
  const sockets = []
  const timers = []
  const { manager } = createConnection({ sockets, timers })
  const events = []
  manager.onMessage(event => events.push(event))

  const connected = manager.connect('token')
  sockets[0].readyState = 1
  sockets[0].onopen()
  await connected

  sockets[0].onclose({ code: 1006, reason: 'abnormal' })
  timers.shift()()
  sockets[1].readyState = 1
  sockets[1].onopen()

  sockets[0].onmessage({ data: JSON.stringify({ type: 'stale' }) })
  sockets[0].onclose({ code: 1006, reason: 'stale close' })

  assert.deepEqual(events, [
    { type: 'connection_closed' },
    { type: 'connection_reopened' }
  ])
  assert.equal(timers.length, 0)
})

test('dispose cancels heartbeat and rejects a pending connection', async () => {
  const sockets = []
  const timers = []
  const heartbeatTimers = []
  const { manager } = createConnection({ sockets, timers, heartbeatTimers })

  const pendingConnection = manager.connect('token')
  manager.startHeartbeat()
  assert.equal(heartbeatTimers.length, 1)

  sockets[0].readyState = 1
  heartbeatTimers[0]()
  assert.deepEqual(sockets[0].sent, [JSON.stringify({ type: 'ping' })])

  manager.dispose()

  assert.equal(heartbeatTimers.length, 0)
  assert.equal(timers.length, 0)
  assert.deepEqual(sockets[0].closeEvent, {
    code: 1000,
    reason: 'User initiated close'
  })
  await assert.rejects(pendingConnection)
})
