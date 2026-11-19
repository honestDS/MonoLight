import assert from 'node:assert/strict'
import test from 'node:test'

import { getReasoningCollapseName } from '../../../src/utils/chatPresentation.js'
import {
  SESSION_A,
  WORK_ID,
  FINAL_CONTENT,
  FINAL_REASONING,
  persistedMessage,
  createPersistedHistory,
  createWsHarness,
  parseMessageContent,
  receiveEvent,
  sendInput,
  streamEvents,
  createTurnEnd,
  createDone,
  createProactiveReply,
  auditHistoryUpdate,
  assertSingleReconciledReply,
  createToolHistory
} from './messageReconciliationHarness.js'

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
