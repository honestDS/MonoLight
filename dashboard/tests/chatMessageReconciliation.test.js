import assert from 'node:assert/strict'
import test from 'node:test'

import { getReasoningCollapseName } from '../src/utils/chatPresentation.js'
import { createHarness, createSession } from './chatDraftHarness.js'

const SESSION_A = 'A'
const SESSION_B = 'B'
const WORK_ID = 7
const FINAL_CONTENT = '最终正文'
const FINAL_REASONING = '完整思考'

const persistedMessage = ({
  id,
  role,
  type = 'text',
  content,
  reasoningContent = null,
  attachments = [],
  ...overrides
}) => ({
  id,
  role,
  type,
  content,
  created_at: 1700000000 + id,
  reasoning_content: reasoningContent,
  attachments,
  ...overrides
})

const createPersistedHistory = () => [
  persistedMessage({
    id: 41,
    role: 'user',
    content: '你好',
    attachments: []
  }),
  persistedMessage({
    id: 50,
    role: 'assistant',
    content: FINAL_CONTENT,
    reasoningContent: FINAL_REASONING,
    attachments: []
  })
]

const createWsHarness = (t, history = []) => createHarness(t, {
  sessions: [
    createSession(SESSION_A, { source: 'ws' }),
    createSession(SESSION_B, { source: 'ws' })
  ],
  initialRoute: { path: '/', query: { session_id: SESSION_A } },
  sessionsHistory: ({ index }) => index === 0 ? [] : history
})

const replayEvent = event => ({
  ...event,
  ...(Array.isArray(event.request_ids) ? { request_ids: [...event.request_ids] } : {})
})

const parseMessageContent = message => {
  if (typeof message?.content !== 'string') return message?.content
  try {
    return JSON.parse(message.content)
  } catch {
    return null
  }
}

const responseHistory = history => (Array.isArray(history) ? history : [])
  .filter(message => message?.role !== 'user')
  .map(message => {
    const parsedContent = parseMessageContent(message)
    const reasoning = message.reasoning_content
    if (
      message.role === 'assistant'
      && (Array.isArray(message.tool_calls) || Array.isArray(parsedContent?.tool_calls))
    ) {
      return {
        id: message.id,
        role: message.role,
        tool_calls: message.tool_calls || parsedContent.tool_calls,
        reasoning_content: reasoning
      }
    }

    if (message.role === 'tool' || message.type === 'tool_result' || parsedContent?.role === 'tool') {
      return {
        id: message.id,
        role: message.role,
        tool_call_id: parsedContent?.tool_call_id ?? message.tool_call_id,
        content: parsedContent?.content ?? message.content
      }
    }

    return {
      id: message.id,
      role: message.role,
      content: message.content,
      reasoning_content: reasoning,
      created_at: message.created_at
    }
  })

const receiveEvent = async (harness, event, replay = false) => {
  harness.receiveWs(replay ? replayEvent(event) : event)
  await harness.flush()
}

const sendInput = async (harness, text, workId = WORK_ID) => {
  const sendPromise = harness.chat.wsSend(text)
  await harness.flush()

  const request = harness.wsManager.sent
    .filter(message => message.type === 'chat')
    .at(-1)
  assert.ok(request)

  const accepted = {
    type: 'input_accepted',
    session_id: SESSION_A,
    request_id: request.request_id,
    work_id: workId,
    submission_status: 'accepted'
  }
  await receiveEvent(harness, accepted)
  await receiveEvent(harness, accepted, true)
  await sendPromise
  return request.request_id
}

const streamEvents = requestId => [
  {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: 'response-7-1',
    turn: 1,
    event_sequence_no: 1
  },
  {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: 'response-7-1',
    turn: 1,
    content: '完整',
    event_sequence_no: 2
  },
  {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: 'response-7-1',
    turn: 1,
    content: '思考',
    event_sequence_no: 3
  },
  {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: 'response-7-1',
    turn: 1,
    content: '最终',
    event_sequence_no: 4
  },
  {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: 'response-7-1',
    turn: 1,
    content: '正文',
    event_sequence_no: 5
  }
]

const createTurnEnd = requestId => ({
  type: 'turn_end',
  session_id: SESSION_A,
  request_id: requestId,
  work_id: WORK_ID,
  response_id: 'response-7-1',
  turn: 1,
  content: FINAL_CONTENT,
  reasoning_content: FINAL_REASONING,
  message_id: 50,
  finish_reason: 'stop',
  event_sequence_no: 6
})

const createDone = (
  requestId,
  history,
  eventSequenceNo = 7,
  content = FINAL_CONTENT,
  reasoningContent = FINAL_REASONING
) => ({
  type: 'done',
  session_id: SESSION_A,
  request_ids: [requestId],
  work_id: WORK_ID,
  response_id: `session-reply-work:${WORK_ID}`,
  event_sequence_no: eventSequenceNo,
  response: {
    choices: [{
      message: {
        role: 'assistant',
        content,
        reasoning_content: reasoningContent
      },
      finish_reason: 'stop'
    }],
    history: responseHistory(history),
    work_id: WORK_ID,
    message_id: 50
  }
})

const createProactiveReply = (requestId, history, eventSequenceNo = 7) => ({
  type: 'proactive_reply',
  session_id: SESSION_A,
  request_ids: [requestId],
  work_id: WORK_ID,
  event_sequence_no: eventSequenceNo,
  content: FINAL_CONTENT,
  message_id: 50,
  history: responseHistory(history)
})

const auditHistoryUpdate = () => ({
  type: 'audit_tool_results_update',
  session_id: SESSION_A,
  messages: []
})

const assertSingleReconciledReply = messages => {
  const users = messages.filter(message => message.role === 'user')
  const assistants = messages.filter(message => message.role === 'assistant')
  const persistedAssistant = assistants.filter(message => String(message.db_id) === '50')
  const reasoningMessages = messages.filter(message => message.role === 'reasoning')
  const thinkingMessages = messages.filter(message => message.role === 'thinking')
  const repliesWithReasoning = assistants.filter(message => (
    typeof message.reasoning_content === 'string' && message.reasoning_content.trim()
  ))
  const collapseNames = repliesWithReasoning.map(getReasoningCollapseName)

  assert.equal(users.length, 1)
  assert.equal(String(users[0].db_id ?? users[0].id), '41')
  assert.equal(assistants.length, 1)
  assert.equal(persistedAssistant.length, 1)
  assert.equal(assistants[0].content, FINAL_CONTENT)
  assert.equal(assistants[0].reasoning_content, FINAL_REASONING)
  assert.equal(reasoningMessages.length, 0)
  assert.equal(thinkingMessages.length, 0)
  assert.equal(new Set(collapseNames).size, collapseNames.length)
}

const eventOrders = [
  ['history', 'turn_end', 'terminal'],
  ['history', 'terminal', 'turn_end'],
  ['turn_end', 'history', 'terminal'],
  ['turn_end', 'terminal', 'history'],
  ['terminal', 'history', 'turn_end'],
  ['terminal', 'turn_end', 'history']
]

for (const terminalType of ['done', 'proactive_reply']) {
  for (const order of eventOrders) {
    test(`reconciles stream, history, turn_end and ${terminalType} in ${order.join('-')} order`, async t => {
      const history = createPersistedHistory()
      const harness = await createWsHarness(t, history)
      await harness.flush()

      const requestId = await sendInput(harness, '你好')
      const baseEvents = streamEvents(requestId)
      for (const event of baseEvents) {
        await receiveEvent(harness, event)
        await receiveEvent(harness, event, true)
      }

      const events = {
        history: auditHistoryUpdate(),
        turn_end: createTurnEnd(requestId),
        terminal: terminalType === 'done'
          ? createDone(requestId, history)
          : createProactiveReply(requestId, history)
      }

      for (const name of order) await receiveEvent(harness, events[name])
      for (const name of order) await receiveEvent(harness, events[name], true)

      assertSingleReconciledReply(harness.chat.messages.value)
    })
  }
}

const createToolHistory = () => {
  const toolCall = {
    id: 'call-search',
    name: 'search',
    arguments: '{"query":"资料"}'
  }
  const serializedToolCall = JSON.stringify({
    role: 'assistant',
    content: '',
    tool_calls: [toolCall]
  })
  const serializedToolResult = JSON.stringify({
    role: 'tool',
    tool_call_id: toolCall.id,
    content: '已找到资料'
  })

  return [
    persistedMessage({ id: 41, role: 'user', content: '查资料' }),
    persistedMessage({
      id: 43,
      role: 'assistant',
      type: 'tool_call',
      content: serializedToolCall,
      tool_calls: [toolCall]
    }),
    persistedMessage({
      id: 44,
      role: 'tool',
      type: 'tool_result',
      content: serializedToolResult,
      tool_call_id: toolCall.id
    }),
    persistedMessage({ id: 49, role: 'user', content: '停止' }),
    persistedMessage({ id: 50, role: 'assistant', content: FINAL_CONTENT })
  ]
}

test('reconciles repeated tool rounds and a second request absorbed by one work', async t => {
  const history = createToolHistory()
  const harness = await createWsHarness(t, history)
  await harness.flush()

  const firstRequestId = await sendInput(harness, '查资料')
  const firstResponseId = 'response-tool-1'
  const toolStart = {
    type: 'tool_start',
    session_id: SESSION_A,
    request_id: firstRequestId,
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    tool_call_id: 'call-search',
    name: 'search',
    arguments: '{"query":"资料"}',
    event_sequence_no: 2
  }
  const toolEnd = {
    type: 'tool_end',
    session_id: SESSION_A,
    request_id: firstRequestId,
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    tool_call_id: 'call-search',
    name: 'search',
    result: '已找到资料',
    event_sequence_no: 3
  }

  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: firstRequestId,
    request_ids: [firstRequestId],
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    event_sequence_no: 1
  })
  await receiveEvent(harness, toolStart)
  await receiveEvent(harness, toolStart, true)
  await receiveEvent(harness, toolEnd)
  await receiveEvent(harness, toolEnd, true)

  const secondRequestId = await sendInput(harness, '停止')
  await receiveEvent(harness, {
    type: 'input_dequeued',
    session_id: SESSION_A,
    request_ids: [firstRequestId, secondRequestId],
    work_id: WORK_ID,
    event_sequence_no: 4
  })
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: secondRequestId,
    request_ids: [firstRequestId, secondRequestId],
    work_id: WORK_ID,
    response_id: 'response-tool-2',
    turn: 1,
    event_sequence_no: 5
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: secondRequestId,
    work_id: WORK_ID,
    response_id: 'response-tool-2',
    turn: 1,
    content: FINAL_CONTENT,
    event_sequence_no: 6
  })

  const turnEnd = {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: secondRequestId,
    work_id: WORK_ID,
    response_id: 'response-tool-2',
    turn: 1,
    content: FINAL_CONTENT,
    message_id: 50,
    finish_reason: 'stop',
    event_sequence_no: 7
  }
  await receiveEvent(harness, turnEnd)
  await receiveEvent(harness, turnEnd, true)

  const done = createDone(secondRequestId, history, 8)
  done.request_ids = [firstRequestId, secondRequestId]
  await receiveEvent(harness, done)
  await receiveEvent(harness, done, true)
  await receiveEvent(harness, auditHistoryUpdate())
  await receiveEvent(harness, auditHistoryUpdate(), true)

  const messages = harness.chat.messages.value
  const users = messages.filter(message => message.role === 'user')
  const toolCalls = messages.filter(message => (
    parseMessageContent(message)?.tool_calls?.some(call => call.id === 'call-search')
  ))
  const toolResults = messages.filter(message => (
    parseMessageContent(message)?.tool_call_id === 'call-search'
  ))
  const finalReplies = messages.filter(message => (
    message.role === 'assistant' && message.content === FINAL_CONTENT
  ))

  assert.equal(users.length, 2)
  assert.deepEqual(Array.from(users, message => message.content), ['查资料', '停止'])
  assert.deepEqual(
    new Set(users.map(message => String(message.db_id ?? message.id))),
    new Set(['41', '49'])
  )
  assert.equal(toolCalls.length, 1)
  assert.equal(toolResults.length, 1)
  assert.equal(finalReplies.length, 1)
  assert.equal(finalReplies[0].response_id, 'response-tool-2')
  assert.equal(String(finalReplies[0].db_id), '50')
  assert.equal(messages.filter(message => message.role === 'thinking').length, 0)
  assert.equal(messages.filter(message => message.role === 'reasoning').length, 0)
})

test('keeps legitimate repeated chunks, ignores one replay, and freezes a turn after turn_end', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const requestId = await sendInput(harness, '重复片段')
  const responseId = 'response-repeated'
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 1
  })

  const repeatedChunk = {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '片',
    event_sequence_no: 2
  }
  const sameTextNewEvent = { ...repeatedChunk, event_sequence_no: 3 }
  await receiveEvent(harness, repeatedChunk)
  await receiveEvent(harness, sameTextNewEvent)
  await receiveEvent(harness, repeatedChunk, true)

  const reasoning = {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '思考',
    event_sequence_no: 4
  }
  await receiveEvent(harness, reasoning)
  await receiveEvent(harness, reasoning, true)

  await receiveEvent(harness, {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '片片',
    reasoning_content: '思考',
    message_id: 50,
    finish_reason: 'stop',
    event_sequence_no: 5
  })

  const replyAfterTurnEnd = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === responseId
  ))
  assert.ok(replyAfterTurnEnd)
  assert.equal(replyAfterTurnEnd.content, '片片')
  assert.equal(replyAfterTurnEnd.reasoning_content, '思考')

  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '迟到正文',
    event_sequence_no: 6
  })
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '迟到思考',
    event_sequence_no: 7
  })

  const unchangedReply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === responseId
  ))
  assert.equal(unchangedReply.content, '片片')
  assert.equal(unchangedReply.reasoning_content, '思考')

  await receiveEvent(harness, createDone(requestId, [], 8, '片片', '思考'))
  const completedReply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && String(message.db_id) === '50'
  ))
  assert.ok(completedReply)
  assert.equal(completedReply.content, '片片')
  assert.equal(completedReply.reasoning_content, '思考')
})

const selectRealSession = async (harness, sessionId) => {
  const session = harness.chat.sessions.value.find(item => item.session_id === sessionId)
  assert.ok(session)
  harness.chat.selectSession(session)
  await harness.flush()
}

test('replays an A stream after a real B switch without letting old event identities drop text', async t => {
  const harness = await createHarness(t, {
    sessions: [
      createSession(SESSION_A, { source: 'ws' }),
      createSession(SESSION_B, { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: SESSION_A } },
    sessionsHistory: () => []
  })
  await harness.flush()

  const requestId = await sendInput(harness, '恢复流')
  const responseId = 'response-resumed'
  const firstEvents = [
    {
      type: 'agent_loop_start',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      event_sequence_no: 1
    },
    {
      type: 'agent_loop_output',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      event_sequence_no: 2
    },
    {
      type: 'reasoning',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '原思考',
      event_sequence_no: 3
    },
    {
      type: 'content',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '原文',
      event_sequence_no: 4
    }
  ]
  for (const event of firstEvents) await receiveEvent(harness, event)

  await selectRealSession(harness, SESSION_B)
  await selectRealSession(harness, SESSION_A)

  for (const event of firstEvents) await receiveEvent(harness, event, true)
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '继续思考',
    event_sequence_no: 5
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '继续',
    event_sequence_no: 6
  })

  const resumedStreamReply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === responseId
  ))
  assert.ok(resumedStreamReply)
  assert.equal(resumedStreamReply.content, '原文继续')
  const visibleReasoning = harness.chat.messages.value.filter(message => (
    message.response_id === responseId
    && typeof message.reasoning_content === 'string'
  ))
  assert.equal(visibleReasoning.length, 1)
  assert.equal(
    Array.from(visibleReasoning, message => message.reasoning_content).join(''),
    '原思考继续思考'
  )

  await receiveEvent(harness, {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '原文继续',
    reasoning_content: '原思考继续思考',
    message_id: 70,
    finish_reason: 'stop',
    event_sequence_no: 7
  })

  const finalReplies = harness.chat.messages.value.filter(message => message.role === 'assistant')
  assert.equal(finalReplies.length, 1)
  assert.equal(finalReplies[0].content, '原文继续')
  assert.equal(finalReplies[0].reasoning_content, '原思考继续思考')
  assert.equal(String(finalReplies[0].db_id), '70')
  assert.equal(harness.chat.messages.value.some(message => message.role === 'reasoning'), false)
  assert.equal(harness.chat.messages.value.some(message => message.role === 'thinking'), false)
})

test('keeps same-content replies separate when database and response identities differ', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const sendReply = async ({ requestText, workId, responseId, messageId }) => {
    const requestId = await sendInput(harness, requestText, workId)
    await receiveEvent(harness, {
      type: 'agent_loop_start',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: workId,
      response_id: responseId,
      turn: 1,
      event_sequence_no: 1
    })
    await receiveEvent(harness, {
      type: 'content',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: workId,
      response_id: responseId,
      turn: 1,
      content: '相同正文',
      event_sequence_no: 2
    })
    await receiveEvent(harness, {
      type: 'turn_end',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: workId,
      response_id: responseId,
      turn: 1,
      content: '相同正文',
      message_id: messageId,
      finish_reason: 'stop',
      event_sequence_no: 3
    })
    await receiveEvent(harness, {
      type: 'done',
      session_id: SESSION_A,
      request_ids: [requestId],
      work_id: workId,
      response_id: `session-reply-work:${workId}`,
      event_sequence_no: 4,
      response: {
        choices: [{
          message: { role: 'assistant', content: '相同正文' },
          finish_reason: 'stop'
        }],
        history: [],
        work_id: workId,
        message_id: messageId
      }
    })
  }

  await sendReply({
    requestText: '第一次',
    workId: 7,
    responseId: 'response-same-1',
    messageId: 61
  })
  await sendReply({
    requestText: '第二次',
    workId: 8,
    responseId: 'response-same-2',
    messageId: 62
  })

  const replies = harness.chat.messages.value.filter(message => (
    message.role === 'assistant' && message.content === '相同正文'
  ))
  assert.equal(replies.length, 2)
  assert.deepEqual(
    Array.from(replies, message => message.response_id),
    ['response-same-1', 'response-same-2']
  )
  assert.deepEqual(
    new Set(replies.map(message => String(message.db_id))),
    new Set(['61', '62'])
  )
})

test('keeps repeated content and reasoning chunks without explicit event identities', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const requestId = await sendInput(harness, '无事件身份')
  const responseId = 'response-missing-event-identity'
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1
  })

  for (const event of [
    {
      type: 'content',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '片'
    },
    {
      type: 'content',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '片'
    },
    {
      type: 'reasoning',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '思'
    },
    {
      type: 'reasoning',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '思'
    }
  ]) await receiveEvent(harness, event)

  const reply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === responseId
  ))
  assert.ok(reply)
  assert.equal(reply.content, '片片')
  assert.equal(reply.reasoning_content, '思思')
})

test('keeps long streams deduplicated after the replay cache rolls over', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const requestId = await sendInput(harness, '长回复')
  const responseId = 'response-long-stream'
  harness.receiveWs({
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 1
  })
  await harness.flush()

  const reasoningEvents = Array.from({ length: 2105 }, (_, index) => ({
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '思',
    event_sequence_no: index + 2
  }))
  const contentEvents = Array.from({ length: 2105 }, (_, index) => ({
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '片',
    event_sequence_no: index + 2107
  }))

  for (const event of reasoningEvents) harness.receiveWs(event)
  for (const event of contentEvents) harness.receiveWs(event)
  harness.receiveWs(replayEvent(reasoningEvents[0]))
  harness.receiveWs(replayEvent(contentEvents[0]))
  await harness.flush()

  const reply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === responseId
  ))
  assert.ok(reply)
  assert.equal(reply.content.length, 2105)
  const reasoningSources = harness.chat.messages.value.filter(message => (
    message.response_id === responseId
    && typeof message.reasoning_content === 'string'
    && message.reasoning_content.length > 0
  ))
  assert.equal(reasoningSources.length, 1)
  assert.equal(
    Array.from(reasoningSources, message => message.reasoning_content).join('').length,
    2105
  )
})

test('freezes one response after an identity-light turn_end without blocking the next response', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const requestId = await sendInput(harness, '结束生命周期')
  const firstResponseId = 'response-frozen'
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    event_sequence_no: 1
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    content: '首',
    event_sequence_no: 2
  })
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    content: '思',
    event_sequence_no: 3
  })
  await receiveEvent(harness, {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    content: '首',
    reasoning_content: '思'
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    content: '迟到正文',
    event_sequence_no: 4
  })
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: firstResponseId,
    turn: 1,
    content: '迟到思考',
    event_sequence_no: 5
  })

  const firstReply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === firstResponseId
  ))
  assert.ok(firstReply)
  assert.equal(firstReply.content, '首')
  assert.equal(firstReply.reasoning_content, '思')

  const secondResponseId = 'response-after-freeze'
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: secondResponseId,
    turn: 1,
    event_sequence_no: 6
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: secondResponseId,
    turn: 1,
    content: '继续正文',
    event_sequence_no: 7
  })
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: secondResponseId,
    turn: 1,
    content: '继续思考',
    event_sequence_no: 8
  })

  const secondReply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === secondResponseId
  ))
  assert.ok(secondReply)
  assert.equal(secondReply.content, '继续正文')
  assert.equal(secondReply.reasoning_content, '继续思考')
})

const doneSnapshotCases = [
  {
    name: 'streamed reasoning',
    stream: [{ type: 'reasoning', content: '流式思考' }],
    expectedContent: '',
    expectedReasoning: '流式思考',
    files: []
  },
  {
    name: 'files only',
    stream: [],
    expectedContent: '',
    expectedReasoning: '',
    files: [{ name: 'result.txt', path: 'result.txt' }]
  },
  {
    name: 'refusal',
    stream: [],
    refusal: '拒绝内容',
    expectedContent: '拒绝内容',
    expectedReasoning: '',
    files: []
  },
  {
    name: 'existing streamed content',
    stream: [{ type: 'content', content: '流式正文' }],
    expectedContent: '流式正文',
    expectedReasoning: '',
    files: []
  }
]

for (const snapshotCase of doneSnapshotCases) {
  test(`reconciles done without turn_end for ${snapshotCase.name}`, async t => {
    const harness = await createWsHarness(t)
    await harness.flush()

    const requestId = await sendInput(harness, snapshotCase.name)
    const responseId = `response-done-${snapshotCase.name.replaceAll(' ', '-')}`
    await receiveEvent(harness, {
      type: 'agent_loop_start',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      event_sequence_no: 1
    })

    for (const [index, streamEvent] of snapshotCase.stream.entries()) {
      await receiveEvent(harness, {
        type: streamEvent.type,
        session_id: SESSION_A,
        request_id: requestId,
        work_id: WORK_ID,
        response_id: responseId,
        turn: 1,
        content: streamEvent.content,
        event_sequence_no: index + 2
      })
    }

    const done = createDone(requestId, [], 4, '', null)
    const snapshotMessage = done.response.choices[0].message
    delete snapshotMessage.reasoning_content
    if (snapshotCase.refusal) snapshotMessage.refusal = snapshotCase.refusal
    if (snapshotCase.files.length) done.response.files = snapshotCase.files
    await receiveEvent(harness, done)

    const messages = harness.chat.messages.value
    const assistants = messages.filter(message => message.role === 'assistant')
    assert.equal(assistants.length, 1)
    assert.equal(String(assistants[0].db_id), '50')
    assert.equal(assistants[0].content, snapshotCase.expectedContent)
    assert.equal(assistants[0].reasoning_content || '', snapshotCase.expectedReasoning)
    assert.deepEqual(assistants[0].files || [], snapshotCase.files)
    assert.equal(messages.filter(message => message.role === 'reasoning').length, 0)
  })
}

test('keeps complete historical reasoning when a later done snapshot omits it', async t => {
  const history = createPersistedHistory()
  const harness = await createWsHarness(t, history)
  await harness.flush()

  const requestId = await sendInput(harness, '历史完整思考')
  const responseId = 'response-history-reasoning'
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 1
  })
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '完整',
    event_sequence_no: 2
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '最终',
    event_sequence_no: 3
  })

  await receiveEvent(harness, {
    ...auditHistoryUpdate(),
    messages: history
  })

  const done = createDone(requestId, [], 4, FINAL_CONTENT, null)
  delete done.response.choices[0].message.reasoning_content
  await receiveEvent(harness, done)

  const messages = harness.chat.messages.value
  const assistants = messages.filter(message => message.role === 'assistant')
  const reasoningSources = messages.filter(message => (
    typeof message.reasoning_content === 'string' && message.reasoning_content.trim()
  ))
  assert.equal(assistants.length, 1)
  assert.equal(String(assistants[0].db_id), '50')
  assert.equal(assistants[0].content, FINAL_CONTENT)
  assert.equal(assistants[0].reasoning_content, FINAL_REASONING)
  assert.equal(reasoningSources.length, 1)
  assert.equal(messages.filter(message => message.role === 'reasoning').length, 0)
})

for (const terminalResponse of [
  {
    name: 'explicit response identity',
    responseId: 'response-final',
    expectedResponseId: 'response-final'
  },
  {
    name: 'outer synthetic response identity',
    responseId: null,
    expectedResponseId: `session-reply-work:${WORK_ID}`
  }
]) {
test(`completes live tool messages from terminal history without leaking tool reasoning with ${terminalResponse.name}`, async t => {
  const history = createToolHistory().map(message => {
    if (message.id === 43) {
      return { ...message, reasoning_content: '工具完整思考' }
    }
    if (message.id === 44) {
      return {
        ...message,
        content: JSON.stringify({
          role: 'tool',
          tool_call_id: 'call-search',
          content: '完整结果'
        })
      }
    }
    if (message.id === 50) return { ...message, content: '最终正文' }
    return message
  })
  const harness = await createWsHarness(t)
  await harness.flush()

  const requestId = await sendInput(harness, '工具完整终态')
  const liveResponseId = 'response-tool-live'
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: liveResponseId,
    turn: 1,
    event_sequence_no: 1
  })
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: liveResponseId,
    turn: 1,
    content: '工具局部思考',
    event_sequence_no: 2
  })
  await receiveEvent(harness, {
    type: 'tool_start',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: liveResponseId,
    turn: 1,
    tool_call_id: 'call-search',
    name: 'search',
    arguments: '{"query":"资料"}',
    event_sequence_no: 3
  })
  await receiveEvent(harness, {
    type: 'tool_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: liveResponseId,
    turn: 1,
    tool_call_id: 'call-search',
    name: 'search',
    result: '局部结果',
    event_sequence_no: 4
  })

  const done = createDone(requestId, history, 5, '最终正文', null)
  if (terminalResponse.responseId) done.response.response_id = terminalResponse.responseId
  delete done.response.choices[0].message.reasoning_content
  await receiveEvent(harness, done)

  const messages = harness.chat.messages.value
  const toolCalls = messages.filter(message => (
    parseMessageContent(message)?.tool_calls?.some(call => call.id === 'call-search')
  ))
  const toolResults = messages.filter(message => (
    parseMessageContent(message)?.tool_call_id === 'call-search'
  ))
  const finalReplies = messages.filter(message => (
    message.role === 'assistant' && message.content === '最终正文'
  ))
  assert.equal(toolCalls.length, 1)
  assert.equal(toolResults.length, 1)
  assert.equal(String(toolCalls[0].db_id), '43')
  assert.equal(String(toolResults[0].db_id), '44')
  assert.equal(toolCalls[0].reasoning_content, '工具完整思考')
  assert.equal(parseMessageContent(toolResults[0])?.content, '完整结果')
  assert.equal(finalReplies.length, 1)
  assert.equal(finalReplies[0].response_id, terminalResponse.expectedResponseId)
  if (!terminalResponse.responseId) assert.notEqual(finalReplies[0].response_id, liveResponseId)
  assert.equal(finalReplies[0].reasoning_content || '', '')
  assert.equal(messages.filter(message => message.role === 'reasoning').length, 0)
  assert.equal(messages.filter(message => message.role === 'thinking').length, 0)
})
}

test('preserves a stream through reconnect and finalizes one reasoning source', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const requestId = await sendInput(harness, '断线重连')
  const responseId = 'response-reconnect'
  const start = {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 1
  }
  const output = {
    type: 'agent_loop_output',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 2
  }
  const firstReasoning = {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '前思',
    event_sequence_no: 3
  }
  const firstContent = {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '前文',
    event_sequence_no: 4
  }
  await receiveEvent(harness, start)
  await receiveEvent(harness, output)
  await receiveEvent(harness, firstReasoning)
  await receiveEvent(harness, firstContent)

  harness.receiveWs({ type: 'connection_closed', session_id: SESSION_A })
  harness.receiveWs({ type: 'connection_reopened', session_id: SESSION_A })
  await harness.flush()
  assert.ok(harness.wsManager.sent.some(message => message.type === 'resume'))

  await receiveEvent(harness, start, true)
  await receiveEvent(harness, output, true)
  await receiveEvent(harness, firstReasoning, true)
  await receiveEvent(harness, firstContent, true)
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '后思',
    event_sequence_no: 5
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '后文',
    event_sequence_no: 6
  })
  await receiveEvent(harness, {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '前文后文',
    reasoning_content: '前思后思',
    message_id: 50,
    finish_reason: 'stop',
    event_sequence_no: 7
  })

  const messages = harness.chat.messages.value
  const assistants = messages.filter(message => message.role === 'assistant')
  const reasoningSources = messages.filter(message => (
    typeof message.reasoning_content === 'string' && message.reasoning_content.trim()
  ))
  assert.equal(assistants.length, 1)
  assert.equal(assistants[0].content, '前文后文')
  assert.equal(assistants[0].reasoning_content, '前思后思')
  assert.equal(reasoningSources.length, 1)
  assert.equal(messages.filter(message => message.role === 'reasoning').length, 0)
  assert.equal(messages.filter(message => message.role === 'thinking').length, 0)
})

const weakTerminalCases = [
  {
    name: 'with final reasoning',
    reasoningContent: '最终思考',
    expectedReasoning: '最终思考'
  },
  {
    name: 'without final reasoning',
    reasoningContent: null,
    expectedReasoning: ''
  }
].flatMap(weakTerminalCase => [false, true].map(historyBeforeDone => ({
  ...weakTerminalCase,
  historyBeforeDone,
  name: `${weakTerminalCase.name}, ${historyBeforeDone ? 'history-before-done' : 'no-history'}`
})))

for (const weakTerminalCase of weakTerminalCases) {
  test(`does not reuse earlier reasoning identity for newer content in weak terminal state (${weakTerminalCase.name})`, async t => {
    const harness = weakTerminalCase.historyBeforeDone
      ? await createWsHarness(t, [
          persistedMessage({ id: 41, role: 'user', content: '弱终态' }),
          persistedMessage({ id: 50, role: 'assistant', content: FINAL_CONTENT })
        ])
      : await createWsHarness(t)
    await harness.flush()

    const requestId = await sendInput(harness, '弱终态')
    await receiveEvent(harness, {
      type: 'agent_loop_start',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: WORK_ID,
      response_id: 'response-early',
      turn: 1,
      event_sequence_no: 1
    })
    await receiveEvent(harness, {
      type: 'reasoning',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: 'response-early',
      turn: 1,
      content: '较早思考',
      event_sequence_no: 2
    })
    await receiveEvent(harness, {
      type: 'agent_loop_start',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: WORK_ID,
      response_id: 'response-latest',
      turn: 1,
      event_sequence_no: 3
    })
    await receiveEvent(harness, {
      type: 'content',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: 'response-latest',
      turn: 1,
      content: FINAL_CONTENT,
      event_sequence_no: 4
    })

    if (weakTerminalCase.reasoningContent) {
      await receiveEvent(harness, {
        type: 'reasoning',
        session_id: SESSION_A,
        request_id: requestId,
        work_id: WORK_ID,
        response_id: 'response-latest',
        turn: 1,
        content: weakTerminalCase.reasoningContent,
        event_sequence_no: 5
      })
    }

    if (weakTerminalCase.historyBeforeDone) await receiveEvent(harness, auditHistoryUpdate())

    const done = createDone(
      requestId,
      [],
      weakTerminalCase.reasoningContent ? 6 : 5,
      FINAL_CONTENT,
      null
    )
    delete done.response.choices[0].message.reasoning_content
    await receiveEvent(harness, done)
    await receiveEvent(harness, done, true)

    const messages = harness.chat.messages.value
    const finalReplies = messages.filter(message => (
      message.role === 'assistant' && message.content === FINAL_CONTENT
    ))
    const earlyReasoning = messages.filter(message => (
      message.response_id === 'response-early'
      && message.reasoning_content === '较早思考'
    ))
    const leakedEarlyReasoning = messages.filter(message => (
      message.response_id === 'response-latest'
      && message.reasoning_content === '较早思考'
    ))
    const visibleReasoning = messages.filter(message => (
      typeof message.reasoning_content === 'string' && message.reasoning_content.trim()
    ))
    const collapseNames = Array.from(visibleReasoning, message => getReasoningCollapseName(message))

    assert.equal(finalReplies.length, 1)
    assert.equal(finalReplies[0].response_id, 'response-latest')
    assert.equal(String(finalReplies[0].db_id), '50')
    assert.equal(finalReplies[0].reasoning_content || '', weakTerminalCase.expectedReasoning)
    assert.notEqual(finalReplies[0].reasoning_content, '较早思考')
    assert.equal(earlyReasoning.length, 1)
    assert.equal(leakedEarlyReasoning.length, 0)
    assert.equal(new Set(collapseNames).size, collapseNames.length)
  })
}
