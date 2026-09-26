import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyResumedTurnEnd,
  createSessionReconnectHandler,
  getInitialResumeLoading,
  resumeSessionStream,
  shouldResumeSessionStream
} from '../src/composables/chat/streamResume.js'
import { mergeAssistantResponseIntoList } from '../src/utils/assistantResponseIdentity.js'

test('writable sessions resume only in websocket mode', () => {
  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'ws', is_loading: true },
    transportMode: 'ws'
  }), true)

  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'ws', is_loading: false },
    transportMode: 'ws'
  }), true)

  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'http', is_loading: true },
    transportMode: 'http'
  }), false)
})

test('external read-only sessions do not start websocket resume', () => {
  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'weixin', is_loading: true },
    transportMode: 'ws'
  }), false)
})

test('initial resume loading only reflects active writable websocket sessions', () => {
  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'ws', is_loading: true },
    transportMode: 'ws'
  }), true)

  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'ws', is_loading: false },
    transportMode: 'ws'
  }), false)

  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'http', is_loading: true },
    transportMode: 'http'
  }), false)

  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'weixin', is_loading: true },
    transportMode: 'ws'
  }), false)
})

test('history cursor resume survives a refresh that reports the writable session idle', async () => {
  const session = { session_id: 'session-1', source: 'ws', is_loading: true }
  const loading = []
  let resumed = 0
  await resumeSessionStream({
    session, latestSession: { ...session, is_loading: false }, transportMode: 'ws',
    isCurrentSession: () => true, setLoading: value => { loading.push(value) },
    resume: async () => { resumed++; return false }
  })
  assert.equal(resumed, 1)
  assert.deepEqual(loading, [true, false])
})

test('idle writable sessions resume without setting loading true', async () => {
  const loading = []
  let resumed = 0
  await resumeSessionStream({
    session: { session_id: 'session-1', source: 'ws', is_loading: false },
    latestSession: { session_id: 'session-1', source: 'ws', is_loading: false },
    transportMode: 'ws', isCurrentSession: () => true,
    setLoading: value => { loading.push(value) },
    resume: async () => { resumed++; return false }
  })
  assert.equal(resumed, 1)
  assert.deepEqual(loading, [false])
})

test('HTTP and external read-only sessions do not subscribe', async () => {
  for (const { session, transportMode } of [
    { session: { session_id: 'http-session', source: 'http', is_loading: true }, transportMode: 'http' },
    { session: { session_id: 'external-session', source: 'weixin', is_loading: true }, transportMode: 'ws' }
  ]) {
    const loading = []
    let resumed = 0
    await resumeSessionStream({
      session, transportMode, isCurrentSession: () => true,
      setLoading: value => { loading.push(value) },
      resume: async () => { resumed++ }
    })
    assert.equal(resumed, 0)
    assert.deepEqual(loading, [false])
  }
})

test('switched-away sessions do not change loading or subscribe', async () => {
  const loading = []
  let resumed = 0
  await resumeSessionStream({
    session: { session_id: 'session-1', source: 'ws', is_loading: true },
    transportMode: 'ws', isCurrentSession: () => false,
    setLoading: value => { loading.push(value) },
    resume: async () => { resumed++ }
  })
  assert.equal(resumed, 0)
  assert.deepEqual(loading, [])
})

test('resume errors clear only the current selection loading state', async () => {
  for (const changed of [false, true]) {
    let current = true
    let loading = false
    await assert.rejects(resumeSessionStream({
      session: { session_id: 'session-1', is_loading: true },
      transportMode: 'ws', isCurrentSession: () => current,
      setLoading: value => { loading = value },
      resume: async () => { current = !changed; throw new Error('connection failed') }
    }), /connection failed/)
    assert.equal(loading, changed)
  }
})

test('successful resume keeps loading until completion and supports a freshly started work', async () => {
  const loading = []
  let resumed = 0
  await resumeSessionStream({
    session: { session_id: 'session-1', is_loading: false },
    latestSession: { session_id: 'session-1', is_loading: true },
    transportMode: 'ws', isCurrentSession: () => true,
    setLoading: value => { loading.push(value) },
    resume: async () => { resumed++; return true }
  })
  assert.equal(resumed, 1)
  assert.deepEqual(loading, [true])
})

test('session reconnect handler skips resume without a current session', async () => {
  let resumed = false
  const handler = createSessionReconnectHandler({
    getCurrentSessionId: () => null,
    getSession: () => {
      throw new Error('session lookup should be skipped')
    },
    getHistoryMessages: () => [],
    resumeSession: async () => {
      resumed = true
      return true
    }
  })

  assert.equal(await handler(), false)
  assert.equal(resumed, false)
})

test('session reconnect handler resumes with persisted history cursors only', async () => {
  const session = { session_id: 'session-1', source: 'ws' }
  const historyMessages = [
    { id: 'local-only' },
    { db_id: null },
    { db_id: '' },
    { db_id: -1 },
    { db_id: 1.5 },
    { db_id: '2' },
    { db_id: 0 },
    { db_id: 7 }
  ]
  const resumeResult = { resumed: true }
  let lookedUpSessionId = null
  let resumeArgs = null

  const handler = createSessionReconnectHandler({
    getCurrentSessionId: () => 'session-1',
    getSession: sessionId => {
      lookedUpSessionId = sessionId
      return session
    },
    getHistoryMessages: () => historyMessages,
    resumeSession: async (...args) => {
      resumeArgs = args
      return resumeResult
    }
  })

  assert.equal(await handler(), resumeResult)
  assert.equal(lookedUpSessionId, 'session-1')
  assert.deepEqual(resumeArgs, [session, [{ id: 0 }, { id: 7 }]])
})

test('resume turn_end binds a streamed reply to its persisted message before history refresh', () => {
  const streamedReply = {
    id: 'assistant-response-1',
    role: 'assistant',
    content: 'final reply',
    response_id: 'response-1',
    request_id: 'request-1',
    work_id: 7,
    turn: 2
  }

  const bridged = applyResumedTurnEnd([streamedReply], {
    type: 'turn_end',
    content: 'final reply',
    message_id: 91,
    response_id: 'response-1',
    work_id: 7,
    turn: 2,
    finish_reason: 'stop'
  }, 'request-1')

  assert.equal(bridged.length, 1)
  assert.equal(bridged[0].db_id, '91')
  assert.equal(bridged[0].response_id, 'response-1')

  const mergedWithHistory = mergeAssistantResponseIntoList(bridged, {
    id: 91,
    role: 'assistant',
    content: 'final reply'
  })
  assert.equal(mergedWithHistory.length, 1)
  assert.equal(mergedWithHistory[0].content, 'final reply')
})
