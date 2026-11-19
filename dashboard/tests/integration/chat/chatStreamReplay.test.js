import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SESSION_A,
  WORK_ID,
  createWsHarness,
  replayEvent,
  receiveEvent,
  sendInput,
  createDone
} from './messageReconciliationHarness.js'

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
