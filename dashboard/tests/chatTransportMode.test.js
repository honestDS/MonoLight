import assert from 'node:assert/strict'
import test from 'node:test'
import { sendHttpNonStream } from '../src/composables/chat/chatTransportRuntime.js'
import {
  activateSelectedSessionTransportMode,
  persistSessionTransportMode,
  resolveSessionTransportMode,
  resumeSelectedSessionByTransport
} from '../src/composables/chat/sessionTransportMode.js'
import { getHistoryMessageCursor } from '../src/composables/chat/streamResume.js'

test('http transport sends a real non-stream request', async () => {
  const requests = []
  const result = await sendHttpNonStream({
    api: {
      async completions(payload) {
        requests.push(payload)
        return { data: { data: { session_id: 'session-http', work_id: 'work-1' } } }
      }
    },
    message: 'hello',
    sessionId: 'session-http',
    attachments: [],
    requestId: 'request-1'
  })

  assert.deepEqual(result, { session_id: 'session-http', work_id: 'work-1' })
  assert.equal(requests.length, 1)
  assert.deepEqual(requests[0], {
    message: 'hello',
    session_id: 'session-http',
    attachments: [],
    request_id: 'request-1',
    stream: false
  })
})

test('http transport includes new-session display settings without changing persisted sessions', async () => {
  const requests = []
  await sendHttpNonStream({
    api: {
      async completions(payload) {
        requests.push(payload)
        return { data: { data: {} } }
      }
    },
    message: 'hello',
    sessionId: null,
    attachments: null,
    requestId: 'request-new',
    profileOverrideId: 7,
    showToolCalls: false,
    showReasoning: false
  })

  assert.deepEqual(requests[0], {
    message: 'hello',
    session_id: null,
    attachments: null,
    request_id: 'request-new',
    profile_override_id: 7,
    show_tool_calls: false,
    show_reasoning: false,
    stream: false
  })
})

test('persistSessionTransportMode persists the selected mode without owning runtime activation', async () => {
  const sessions = [{ session_id: 'session-1', source: 'ws' }]
  const calls = []

  await persistSessionTransportMode({
    sessionId: 'session-1',
    mode: 'http',
    sessions,
    updateSessionSetting: async (sessionId, payload) => {
      calls.push(['persist', sessionId, payload])
    }
  })

  assert.deepEqual(calls, [['persist', 'session-1', { transport_mode: 'http' }]])
  assert.equal(sessions[0].source, 'http')
})

test('persistSessionTransportMode rolls back the local mode when persistence fails', async () => {
  const sessions = [{ session_id: 'session-1', source: 'ws' }]

  await assert.rejects(
    persistSessionTransportMode({
      sessionId: 'session-1',
      mode: 'http',
      sessions,
      updateSessionSetting: async () => {
        throw new Error('persist failed')
      }
    }),
    /persist failed/
  )

  assert.equal(sessions[0].source, 'ws')
})

test('activating websocket mode immediately subscribes the selected session with the persisted history cursor', async () => {
  const calls = []
  const session = { session_id: 'session-1', source: 'ws' }
  const historyData = [
    { id: 1710000000000, db_id: 40 },
    { id: 1710000000001, db_id: 41 },
    { id: 42 }
  ]

  const result = await activateSelectedSessionTransportMode({
    session,
    mode: 'ws',
    historyData,
    applyTransportMode: async mode => {
      calls.push(['apply', mode])
    },
    resumeStream: async (selectedSession, messages) => {
      calls.push(['resume', selectedSession.session_id, getHistoryMessageCursor(messages)])
    }
  })

  assert.equal(result, 'ws')
  assert.deepEqual(calls, [
    ['apply', 'ws'],
    ['resume', 'session-1', 41]
  ])
  assert.equal(getHistoryMessageCursor([{ id: 40 }, { id: 42 }]), 42)
})

test('activating HTTP mode changes transport without starting a websocket subscription', async () => {
  const calls = []

  const result = await activateSelectedSessionTransportMode({
    session: { session_id: 'session-1', source: 'http' },
    mode: 'http',
    historyData: [{ id: 10 }],
    applyTransportMode: async mode => {
      calls.push(['apply', mode])
    },
    resumeStream: async () => {
      calls.push(['resume'])
    }
  })

  assert.equal(result, 'http')
  assert.deepEqual(calls, [['apply', 'http']])
})

test('selected sessions resume through their persisted transport mode', async () => {
  assert.equal(resolveSessionTransportMode({ source: 'http' }), 'http')
  assert.equal(resolveSessionTransportMode({ source: 'ws' }), 'ws')

  const calls = []
  const session = { session_id: 'session-http', source: 'http' }
  const sessions = [session]

  const httpResult = await resumeSelectedSessionByTransport({
    session,
    historyData: [{ id: 1 }],
    transportMode: 'http',
    getCurrentSessionId: () => 'session-http',
    sessions,
    processHttpSessionSnapshot: async value => {
      calls.push(['http', value])
    },
    resumeStream: async () => {
      calls.push(['ws'])
    }
  })

  assert.equal(httpResult, 'http')
  assert.deepEqual(calls, [['http', sessions]])

  calls.length = 0
  const wsResult = await resumeSelectedSessionByTransport({
    session: { session_id: 'session-ws', source: 'ws' },
    historyData: [{ id: 2 }],
    transportMode: 'ws',
    getCurrentSessionId: () => 'session-ws',
    sessions,
    processHttpSessionSnapshot: async () => {
      calls.push(['http'])
    },
    resumeStream: async (selectedSession, historyData) => {
      calls.push(['ws', selectedSession.session_id, historyData])
    }
  })

  assert.equal(wsResult, 'ws')
  assert.deepEqual(calls, [['ws', 'session-ws', [{ id: 2 }]]])
})
