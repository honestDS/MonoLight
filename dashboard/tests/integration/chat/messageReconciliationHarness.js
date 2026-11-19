import assert from 'node:assert/strict'

import { getReasoningCollapseName } from '../../../src/utils/chatPresentation.js'
import { createHarness, createSession } from '../../chatDraftHarness.js'

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

const selectRealSession = async (harness, sessionId) => {
  const session = harness.chat.sessions.value.find(item => item.session_id === sessionId)
  assert.ok(session)
  harness.chat.selectSession(session)
  await harness.flush()
}

export {
  SESSION_A,
  SESSION_B,
  WORK_ID,
  FINAL_CONTENT,
  FINAL_REASONING,
  persistedMessage,
  createPersistedHistory,
  createWsHarness,
  replayEvent,
  parseMessageContent,
  receiveEvent,
  sendInput,
  streamEvents,
  createTurnEnd,
  createDone,
  createProactiveReply,
  auditHistoryUpdate,
  assertSingleReconciledReply,
  createToolHistory,
  selectRealSession
}
