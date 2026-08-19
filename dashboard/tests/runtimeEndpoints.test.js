import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveApiBaseUrl, resolveWebSocketBaseUrl } from '../src/utils/runtimeEndpoints.js'

test('uses the HTTP page origin for default API and WebSocket endpoints', () => {
  const pageOrigin = 'http://192.168.1.20:9123'
  const apiBaseUrl = resolveApiBaseUrl('', pageOrigin)

  assert.equal(apiBaseUrl, 'http://192.168.1.20:9123/api/v1')
  assert.equal(resolveWebSocketBaseUrl('', apiBaseUrl, pageOrigin), 'ws://192.168.1.20:9123')
})

test('uses secure default protocols and preserves an HTTPS page port', () => {
  const pageOrigin = 'https://192.168.1.20:9443'
  const apiBaseUrl = resolveApiBaseUrl('', pageOrigin)

  assert.equal(apiBaseUrl, 'https://192.168.1.20:9443/api/v1')
  assert.equal(resolveWebSocketBaseUrl('', apiBaseUrl, pageOrigin), 'wss://192.168.1.20:9443')
})

test('normalizes explicit API and WebSocket overrides while retaining a custom WebSocket path', () => {
  const pageOrigin = 'http://192.168.1.20:9123'
  const apiBaseUrl = resolveApiBaseUrl('  https://api.example.test:9443/backend///  ', pageOrigin)
  const webSocketBaseUrl = resolveWebSocketBaseUrl(
    '  wss://socket.example.test:9444/realtime///  ',
    apiBaseUrl,
    pageOrigin,
  )

  assert.equal(apiBaseUrl, 'https://api.example.test:9443/backend')
  assert.equal(webSocketBaseUrl, 'wss://socket.example.test:9444/realtime')
})
