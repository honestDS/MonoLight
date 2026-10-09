import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyResumedTurnEnd,
  createSessionReconnectHandler,
  getInitialResumeLoading,
  resumeSessionStream,
  shouldResumeSessionStream
} from '../src/composables/chat/streamResume.js'
import { appendStreamReasoning } from '../src/composables/chat/reasoningTracker.js'
import { mergeAssistantResponseIntoList } from '../src/utils/assistantResponseIdentity.js'
import { getReasoningCollapseName } from '../src/utils/chatPresentation.js'

test('writable sessions resume only in websocket mode', () => {
  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'ws', is_loading: true, is_reply_running: true },
    transportMode: 'ws'
  }), true)

  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'ws', is_loading: false, is_reply_running: false },
    transportMode: 'ws'
  }), true)

  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'http', is_loading: true, is_reply_running: true },
    transportMode: 'http'
  }), false)
})

test('external read-only sessions do not start websocket resume', () => {
  assert.equal(shouldResumeSessionStream({
    session: { session_id: 'session-1', source: 'weixin', is_loading: true, is_reply_running: true },
    transportMode: 'ws'
  }), false)
})

test('initial resume loading only reflects active writable websocket sessions', () => {
  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'ws', is_loading: true, is_reply_running: true },
    transportMode: 'ws'
  }), true)

  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'ws', is_loading: false, is_reply_running: false },
    transportMode: 'ws'
  }), false)

  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'http', is_loading: true, is_reply_running: true },
    transportMode: 'http'
  }), false)

  assert.equal(getInitialResumeLoading({
    session: { session_id: 'session-1', source: 'weixin', is_loading: true, is_reply_running: true },
    transportMode: 'ws'
  }), false)
})

test('aggregate loading does not enter resume loading when the foreground reply is idle', async () => {
  const session = {
    session_id: 'session-1',
    source: 'ws',
    is_loading: true,
    is_reply_running: false
  }
  assert.equal(getInitialResumeLoading({ session, transportMode: 'ws' }), false)

  const loading = []
  let resumed = 0
  await resumeSessionStream({
    session,
    latestSession: { ...session },
    transportMode: 'ws',
    isCurrentSession: () => true,
    setLoading: value => { loading.push(value) },
    resume: async () => { resumed++; return false }
  })
  assert.equal(resumed, 1)
  assert.deepEqual(loading, [false])
})

test('foreground reply loading wins over an idle aggregate across repeated resumes', async () => {
  const session = {
    session_id: 'session-1',
    source: 'ws',
    is_loading: false,
    is_reply_running: true
  }
  assert.equal(getInitialResumeLoading({ session, transportMode: 'ws' }), true)

  const loading = []
  let resumed = 0
  const resume = () => {
    resumed++
    return Promise.resolve(false)
  }
  const options = {
    session,
    latestSession: { ...session },
    transportMode: 'ws',
    isCurrentSession: () => true,
    setLoading: value => { loading.push(value) },
    resume
  }
  await resumeSessionStream(options)
  await resumeSessionStream(options)
  assert.equal(resumed, 2)
  assert.deepEqual(loading, [true, false, true, false])
})

test('a fresh aggregate-only snapshot does not enter resume loading', async () => {
  const loading = []
  let resumed = 0
  const session = {
    session_id: 'session-1',
    source: 'ws',
    is_loading: false,
    is_reply_running: false
  }
  await resumeSessionStream({
    session,
    latestSession: { ...session, is_loading: true, is_reply_running: false },
    transportMode: 'ws',
    isCurrentSession: () => true,
    setLoading: value => { loading.push(value) },
    resume: async () => { resumed++; return false }
  })
  assert.equal(resumed, 1)
  assert.deepEqual(loading, [false])
})

test('missing foreground state is not inferred from aggregate loading', async () => {
  const session = { session_id: 'session-1', source: 'ws', is_loading: true }
  assert.equal(getInitialResumeLoading({ session, transportMode: 'ws' }), false)

  const loading = []
  let resumed = 0
  await resumeSessionStream({
    session,
    latestSession: { ...session },
    transportMode: 'ws',
    isCurrentSession: () => true,
    setLoading: value => { loading.push(value) },
    resume: async () => { resumed++; return false }
  })
  assert.equal(resumed, 1)
  assert.deepEqual(loading, [false])
})

test('history cursor resume survives a refresh that reports the writable session idle', async () => {
  const session = { session_id: 'session-1', source: 'ws', is_loading: true, is_reply_running: true }
  const loading = []
  let resumed = 0
  await resumeSessionStream({
    session, latestSession: { ...session, is_loading: false, is_reply_running: false }, transportMode: 'ws',
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
    session: { session_id: 'session-1', source: 'ws', is_loading: false, is_reply_running: false },
    latestSession: { session_id: 'session-1', source: 'ws', is_loading: false, is_reply_running: false },
    transportMode: 'ws', isCurrentSession: () => true,
    setLoading: value => { loading.push(value) },
    resume: async () => { resumed++; return false }
  })
  assert.equal(resumed, 1)
  assert.deepEqual(loading, [false])
})

test('HTTP and external read-only sessions do not subscribe', async () => {
  for (const { session, transportMode } of [
    { session: { session_id: 'http-session', source: 'http', is_loading: true, is_reply_running: true }, transportMode: 'http' },
    { session: { session_id: 'external-session', source: 'weixin', is_loading: true, is_reply_running: true }, transportMode: 'ws' }
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
    session: { session_id: 'session-1', source: 'ws', is_loading: true, is_reply_running: true },
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
      session: { session_id: 'session-1', is_loading: true, is_reply_running: true },
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
    session: { session_id: 'session-1', is_loading: false, is_reply_running: false },
    latestSession: { session_id: 'session-1', is_loading: true, is_reply_running: true },
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
  assert.deepEqual(resumeArgs, [session, [{ id: 2 }, { id: 0 }, { id: 7 }]])
})

test('session reconnect handler normalizes compatible persisted history cursors', async () => {
  const session = { session_id: 'session-compatible-cursors', source: 'ws' }
  const localId = Date.now()
  let resumeArgs = null

  const handler = createSessionReconnectHandler({
    getCurrentSessionId: () => session.session_id,
    getSession: sessionId => {
      assert.equal(sessionId, session.session_id)
      return session
    },
    getHistoryMessages: () => [
      { db_id: '91' },
      { db_id: 92 },
      { db_id: '0' },
      { db_id: null },
      { db_id: undefined },
      { db_id: '' },
      { db_id: ' ' },
      { db_id: Number.NaN },
      { db_id: 'not-a-number' },
      { db_id: 1.5 },
      { db_id: true },
      { db_id: false },
      { id: localId }
    ],
    resumeSession: async (...args) => {
      resumeArgs = args
      return true
    }
  })

  assert.equal(await handler(), true)
  assert.deepEqual(resumeArgs, [session, [{ id: 91 }, { id: 92 }, { id: 0 }]])
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

for (const turnEndReasoningContent of ['完整思考', undefined]) {
  test(`resume turn_end archives temporary reasoning (${turnEndReasoningContent === undefined ? 'omitted' : 'provided'})`, () => {
    const identity = {
      responseId: 'response-1',
      requestId: 'request-1',
      workId: 7,
      turn: 2
    }
    let messages = appendStreamReasoning([], '完整思考', identity)
    messages = mergeAssistantResponseIntoList(messages, {
      id: 'assistant-response-1',
      role: 'assistant',
      content: 'final reply',
      response_id: identity.responseId,
      request_id: identity.requestId,
      work_id: identity.workId,
      turn: identity.turn
    })

    const collapseNameBefore = getReasoningCollapseName(messages.find(message => message.role === 'reasoning'))
    const turnEnd = {
      type: 'turn_end',
      content: 'final reply',
      message_id: 91,
      response_id: identity.responseId,
      work_id: identity.workId,
      turn: identity.turn,
      ...(turnEndReasoningContent === undefined ? {} : { reasoning_content: turnEndReasoningContent }),
      finish_reason: 'stop'
    }

    messages = applyResumedTurnEnd(messages, turnEnd, identity.requestId)

    assert.equal(messages.length, 1)
    assert.equal(messages[0].role, 'assistant')
    assert.equal(messages[0].content, 'final reply')
    assert.equal(messages[0].reasoning_content, '完整思考')
    assert.equal(messages[0].db_id, '91')
    assert.equal(getReasoningCollapseName(messages[0]), collapseNameBefore)
    assert.equal(messages.some(message => message.role === 'reasoning'), false)

    const repeated = applyResumedTurnEnd(messages, turnEnd, identity.requestId)
    assert.deepEqual(repeated, messages)

    const mergedWithHistory = mergeAssistantResponseIntoList(repeated, {
      id: 91,
      role: 'assistant',
      content: 'final reply'
    })
    assert.equal(mergedWithHistory.length, 1)
    assert.equal(mergedWithHistory[0].role, 'assistant')
    assert.equal(mergedWithHistory[0].content, 'final reply')
    assert.equal(mergedWithHistory[0].reasoning_content, '完整思考')
    assert.equal(mergedWithHistory[0].db_id, '91')
    assert.deepEqual(mergeAssistantResponseIntoList(mergedWithHistory, {
      id: 91,
      role: 'assistant',
      content: 'final reply'
    }), mergedWithHistory)
  })
}

test('resume turn_end archives reasoning-only responses with their persisted message id', () => {
  const identity = {
    responseId: 'response-2',
    requestId: 'request-2',
    workId: 8,
    turn: 3
  }
  const messages = appendStreamReasoning([], 'only reasoning', identity)
  const archived = applyResumedTurnEnd(messages, {
    type: 'turn_end',
    content: '',
    message_id: 92,
    response_id: identity.responseId,
    work_id: identity.workId,
    turn: identity.turn,
    reasoning_content: 'only reasoning',
    finish_reason: 'stop'
  }, identity.requestId)

  assert.equal(archived.length, 1)
  assert.equal(archived[0].role, 'assistant')
  assert.equal(archived[0].content, '')
  assert.equal(archived[0].reasoning_content, 'only reasoning')
  assert.equal(archived[0].db_id, '92')
  assert.equal(archived.some(message => message.role === 'reasoning'), false)

  const mergedWithHistory = mergeAssistantResponseIntoList(archived, {
    id: 92,
    role: 'assistant',
    content: ''
  })
  assert.equal(mergedWithHistory.length, 1)
  assert.equal(mergedWithHistory[0].db_id, '92')
  assert.equal(mergedWithHistory[0].reasoning_content, 'only reasoning')
  assert.deepEqual(mergeAssistantResponseIntoList(mergedWithHistory, {
    id: 92,
    role: 'assistant',
    content: ''
  }), mergedWithHistory)
})

test('resume turn_end isolates responses sharing work and turn', () => {
  const otherResponse = {
    responseId: 'response-other',
    requestId: 'request-other',
    workId: 9,
    turn: 4
  }
  const currentResponse = {
    responseId: 'response-current',
    requestId: 'request-current',
    workId: 9,
    turn: 4
  }
  let messages = appendStreamReasoning([], 'other reasoning', otherResponse)
  messages = mergeAssistantResponseIntoList(messages, {
    id: 'assistant-other',
    role: 'assistant',
    content: 'other response',
    response_id: otherResponse.responseId,
    request_id: otherResponse.requestId,
    work_id: otherResponse.workId,
    turn: otherResponse.turn
  })
  const otherMessagesBefore = messages
    .filter(message => message.response_id === otherResponse.responseId)
    .map(message => ({ ...message }))
  messages = appendStreamReasoning(messages, 'current reasoning', currentResponse)
  assert.deepEqual(
    messages.filter(message => message.response_id === otherResponse.responseId),
    otherMessagesBefore
  )
  const currentReasoning = messages.find(message => message.response_id === currentResponse.responseId)
  assert.equal(currentReasoning?.role, 'reasoning')
  assert.equal(currentReasoning?.reasoning_content, 'current reasoning')
  assert.equal(currentReasoning?.request_id, currentResponse.requestId)
  messages = mergeAssistantResponseIntoList(messages, {
    id: 'assistant-current',
    role: 'assistant',
    content: 'current draft',
    response_id: currentResponse.responseId,
    request_id: currentResponse.requestId,
    work_id: currentResponse.workId,
    turn: currentResponse.turn
  })

  const resumed = applyResumedTurnEnd(messages, {
    type: 'turn_end',
    content: 'current response',
    message_id: 93,
    response_id: currentResponse.responseId,
    work_id: currentResponse.workId,
    turn: currentResponse.turn,
    reasoning_content: 'current reasoning',
    finish_reason: 'stop'
  }, currentResponse.requestId)

  assert.deepEqual(
    resumed.filter(message => message.response_id === otherResponse.responseId),
    otherMessagesBefore
  )
  assert.deepEqual(
    resumed.filter(message => message.response_id === currentResponse.responseId),
    [{
      id: 'assistant-current',
      role: 'assistant',
      content: 'current response',
      response_id: currentResponse.responseId,
      request_id: currentResponse.requestId,
      work_id: currentResponse.workId,
      turn: currentResponse.turn,
      db_id: '93',
      reasoning_content: 'current reasoning',
      finish_reason: 'stop',
      _stream_finalized: true
    }]
  )
})

test('resume turn_end creates a complete assistant when no messages were streamed', () => {
  const turnEnd = {
    type: 'turn_end',
    content: 'fresh reply',
    message_id: 101,
    response_id: 'response-empty',
    work_id: 10,
    turn: 5,
    finish_reason: 'stop'
  }

  const messages = applyResumedTurnEnd([], turnEnd, 'request-empty')

  assert.equal(messages.length, 1)
  assert.equal(messages[0].role, 'assistant')
  assert.equal(messages[0].content, 'fresh reply')
  assert.equal(messages[0].response_id, 'response-empty')
  assert.equal(messages[0].request_id, 'request-empty')
  assert.equal(messages[0].work_id, 10)
  assert.equal(messages[0].turn, 5)
  assert.equal(messages[0].db_id, '101')
  assert.deepEqual(applyResumedTurnEnd(messages, turnEnd, 'request-empty'), messages)
})

test('resume turn_end coalesces db history, streamed body, and reasoning in either order', () => {
  const identity = {
    responseId: 'response-merge',
    requestId: 'request-merge',
    workId: 11,
    turn: 6
  }
  const history = {
    id: 102,
    role: 'assistant',
    content: 'history draft'
  }
  const streamed = {
    id: 'assistant-stream',
    role: 'assistant',
    content: 'stream draft',
    response_id: identity.responseId,
    request_id: identity.requestId,
    work_id: identity.workId,
    turn: identity.turn
  }
  const temporaryReasoning = appendStreamReasoning([], 'merged reasoning', identity)

  for (const orderedMessages of [
    [history, streamed],
    [streamed, history]
  ]) {
    const messages = [...temporaryReasoning, ...orderedMessages]
    const resumed = applyResumedTurnEnd(messages, {
      type: 'turn_end',
      content: 'final response',
      message_id: 102,
      response_id: identity.responseId,
      work_id: identity.workId,
      turn: identity.turn,
      finish_reason: 'stop'
    }, identity.requestId)

    assert.equal(resumed.length, 1)
    assert.equal(resumed[0].role, 'assistant')
    assert.equal(resumed[0].content, 'final response')
    assert.equal(resumed[0].response_id, identity.responseId)
    assert.equal(resumed[0].db_id, '102')
    assert.equal(resumed[0].reasoning_content, 'merged reasoning')
    assert.equal(resumed.some(message => message.role === 'reasoning'), false)
  }
})

test('resume turn_end preserves a tool assistant body on a length finish', () => {
  const identity = {
    responseId: 'response-tool',
    requestId: 'request-tool',
    workId: 12,
    turn: 7
  }
  const toolCalls = [{
    id: 'call-1',
    type: 'function',
    function: { name: 'lookup', arguments: '{}' }
  }]
  let messages = appendStreamReasoning([], 'tool reasoning', identity)
  messages = mergeAssistantResponseIntoList(messages, {
    id: 'assistant-tool',
    role: 'assistant',
    content: JSON.stringify({ content: 'tool body', tool_calls: toolCalls }),
    tool_calls: toolCalls,
    response_id: identity.responseId,
    request_id: identity.requestId,
    work_id: identity.workId,
    turn: identity.turn
  })

  const archived = applyResumedTurnEnd(messages, {
    type: 'turn_end',
    message_id: 103,
    response_id: identity.responseId,
    work_id: identity.workId,
    turn: identity.turn,
    finish_reason: 'length'
  }, identity.requestId)

  assert.equal(archived.length, 1)
  assert.equal(archived[0].role, 'assistant')
  const parsedContent = JSON.parse(archived[0].content)
  assert.equal(parsedContent.content, 'tool body')
  assert.deepEqual(parsedContent.tool_calls, toolCalls)
  assert.deepEqual(archived[0].tool_calls, toolCalls)
  assert.equal(archived[0].reasoning_content, 'tool reasoning')
  assert.equal(archived[0].response_id, identity.responseId)
  assert.equal(archived[0].db_id, '103')
  assert.equal(archived.some(message => message.role === 'reasoning'), false)
})

test('resume turn_end displays a refusal when an empty assistant has no content', () => {
  const refusal = 'request refused'
  const archived = applyResumedTurnEnd([], {
    type: 'turn_end',
    content: '',
    refusal,
    message_id: 104,
    response_id: 'response-refusal',
    work_id: 13,
    turn: 8,
    finish_reason: 'stop'
  }, 'request-refusal')

  assert.equal(archived.length, 1)
  assert.equal(archived[0].role, 'assistant')
  assert.equal(archived[0].content, refusal)
  assert.equal(archived[0].response_id, 'response-refusal')
  assert.equal(archived[0].db_id, '104')
})

test('resume turn_end creates a formal assistant for reasoning-only completion', () => {
  const archived = applyResumedTurnEnd([], {
    type: 'turn_end',
    message_id: 105,
    response_id: 'response-reasoning-only',
    work_id: 14,
    turn: 9,
    reasoning_content: 'reasoning without deltas',
    finish_reason: 'stop'
  }, 'request-reasoning-only')

  assert.equal(archived.length, 1)
  assert.equal(archived[0].role, 'assistant')
  assert.equal(archived[0].content, '')
  assert.equal(archived[0].reasoning_content, 'reasoning without deltas')
  assert.equal(archived[0].response_id, 'response-reasoning-only')
  assert.equal(archived[0].request_id, 'request-reasoning-only')
  assert.equal(archived[0].db_id, '105')
  assert.equal(archived.some(message => message.role === 'reasoning'), false)
})
