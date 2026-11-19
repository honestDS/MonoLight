import { markRaw, ref } from 'vue'
import { createWebSocketConnection } from '../../../src/composables/webSocketConnection.js'
import { createChatTransport } from '../../../src/composables/chat/chatTransportRuntime.js'

const createConnection = ({
  sockets = [],
  timers = [],
  heartbeatTimers = [],
  warnings = [],
  translations = []
} = {}) => {
  const manager = createWebSocketConnection({
    createSocket: token => {
      const socket = markRaw(new FakeWebSocket(token))
      sockets.push(socket)
      return socket
    },
    translate: key => {
      translations.push(key)
      return key
    },
    warn: message => warnings.push(message),
    scheduleReconnect: callback => {
      timers.push(callback)
      return callback
    },
    cancelReconnect: callback => {
      const index = timers.indexOf(callback)
      if (index !== -1) timers.splice(index, 1)
    },
    scheduleHeartbeat: callback => {
      heartbeatTimers.push(callback)
      return callback
    },
    cancelHeartbeat: callback => {
      const index = heartbeatTimers.indexOf(callback)
      if (index !== -1) heartbeatTimers.splice(index, 1)
    }
  })

  return { manager, sockets, timers, heartbeatTimers, warnings, translations }
}

const loadUseWebSocket = options => () => createConnection(options).manager

const createTransport = ({
  manager,
  api = { completions: async () => ({ data: {} }) },
  getToken = () => 'token',
  errors = [],
  translations = []
}) => {
  const transport = createChatTransport({
    wsManager: manager,
    api,
    getToken,
    reportError: message => errors.push(message),
    translate: key => {
      translations.push(key)
      return key
    }
  })
  return { transport, api, errors, translations }
}

const loadUseChatTransport = options => () => createTransport(options).transport

const createTransportManager = ({ sendResult = true } = {}) => {
  const handlers = []
  const sent = []
  const connectCalls = []
  let disconnectCalls = 0
  const manager = {
    isConnected: ref(false),
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
      return sendResult
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
    this.onmessage = null
    this.sent = []
  }

  send(data) {
    this.sent.push(data)
  }

  close(code, reason) {
    this.readyState = 3
    this.closeEvent = { code, reason }
    this.onclose?.(this.closeEvent)
  }
}

export {
  createConnection,
  loadUseWebSocket,
  createTransport,
  loadUseChatTransport,
  createTransportManager
}
