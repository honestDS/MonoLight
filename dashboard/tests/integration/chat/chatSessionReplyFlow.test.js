import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import { receiveWs } from './sessionCompositionHarness.js'

test('composed websocket send merges turn_end into the existing tool message', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  const sendPromise = chat.wsSend('request')
  await harness.flush()

  const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
  assert.ok(request)
  const requestId = request.request_id
  await receiveWs(harness, {
    type: 'input_accepted',
    session_id: 'A',
    request_id: requestId
  })
  await sendPromise

  chat.messages.value.push(
    { id: 'plain-1', role: 'assistant', response_id: 'r1', content: 'partial one' },
    { id: 'plain-2', role: 'assistant', response_id: 'r1', content: 'partial two' },
    {
      id: 'tool-1',
      role: 'assistant',
      response_id: 'r1',
      content: JSON.stringify({
        role: 'assistant',
        tool_calls: [{ id: 'tool-1', function: { name: 'tool', arguments: '{}' } }],
        content: 'tool stream'
      })
    },
    { id: 'plain-3', role: 'assistant', response_id: 'r2', content: 'other response' }
  )

  const turnEnd = {
    type: 'turn_end',
    session_id: 'A',
    request_id: requestId,
    response_id: 'r1',
    work_id: 'w1',
    content: 'final body',
    message_id: 77,
    finish_reason: 'stop',
    reasoning_content: 'reasoning'
  }
  await receiveWs(harness, turnEnd)

  let r1Messages = chat.messages.value.filter(message => message.response_id === 'r1')
  assert.equal(r1Messages.length, 1)
  assert.equal(JSON.parse(r1Messages[0].content).content, 'final body')
  assert.equal(String(r1Messages[0].db_id), '77')
  assert.equal(r1Messages[0].reasoning_content, 'reasoning')
  assert.equal(
    chat.messages.value.find(message => message.response_id === 'r2').content,
    'other response'
  )

  await receiveWs(harness, { ...turnEnd })
  r1Messages = chat.messages.value.filter(message => message.response_id === 'r1')
  assert.equal(r1Messages.length, 1)
})

test('composed websocket reuses a work sequence without accepting late prior events', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws', show_tool_calls: true })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const stream = {
    session_id: 'A',
    request_id: 'request-reused',
    request_ids: ['request-reused'],
    work_id: 'work-reused',
    turn: 1
  }
  const toolCallId = 'call-reused'
  const receive = fields => receiveWs(harness, { ...stream, ...fields })

  await harness.flush()
  await receive({
    type: 'agent_loop_start',
    response_id: 'response-old',
    work_sequence_no: 5,
    event_sequence_no: 1
  })
  await receive({
    type: 'done',
    response_id: 'response-old',
    message_id: 101,
    work_sequence_no: 5,
    event_sequence_no: 2,
    response: {
      work_id: 'work-reused',
      response_id: 'response-old',
      message_id: 101,
      choices: [{ message: { role: 'assistant', content: '旧回复' }, finish_reason: 'stop' }]
    }
  })

  await receive({
    type: 'agent_loop_start',
    response_id: 'response-new-tool',
    work_sequence_no: 6,
    event_sequence_no: 3
  })
  await receive({ type: 'cancelled', work_sequence_no: 5, event_sequence_no: 4 })
  assert.equal(chat.messages.value.filter(message => message.role === 'thinking').length, 1)

  await receive({
    type: 'tool_start',
    response_id: 'response-new-tool',
    tool_call_id: toolCallId,
    name: 'lookup',
    arguments: '{}',
    work_sequence_no: 6,
    event_sequence_no: 5
  })
  await receive({
    type: 'content',
    response_id: 'response-old-late',
    content: '旧迟到正文',
    work_sequence_no: 5,
    event_sequence_no: 6
  })
  await receive({
    type: 'tool_end',
    response_id: 'response-new-tool',
    tool_call_id: toolCallId,
    name: 'lookup',
    result: { status: 'queued', task_id: 42 },
    work_sequence_no: 6,
    event_sequence_no: 7
  })
  await receive({
    type: 'tool_end',
    response_id: 'response-old-late',
    tool_call_id: toolCallId,
    name: 'lookup',
    result: { status: 'queued', task_id: 99 },
    work_sequence_no: 5,
    event_sequence_no: 8
  })
  const queuedResult = JSON.parse(chat.messages.value.find(message => message.role === 'tool').content).content
  assert.deepEqual(queuedResult, { status: 'queued', task_id: 42 })

  await receive({
    type: 'tool_end',
    response_id: 'response-new-tool',
    tool_call_id: toolCallId,
    name: 'lookup',
    result: '最终工具结果',
    work_sequence_no: 6,
    event_sequence_no: 9
  })
  await receive({
    type: 'done',
    response_id: 'response-new-final',
    message_id: 102,
    work_sequence_no: 6,
    event_sequence_no: 10,
    response: {
      work_id: 'work-reused',
      response_id: 'response-new-final',
      message_id: 102,
      choices: [{ message: { role: 'assistant', content: '新回复' }, finish_reason: 'stop' }]
    }
  })

  const messages = chat.messages.value
  const toolCalls = messages.filter(message => message.role === 'assistant' && chat.isToolCall(message))
  const toolResults = messages.filter(message => message.role === 'tool')
  assert.equal(toolCalls.length, 1)
  assert.equal(toolResults.length, 1)
  assert.equal(JSON.parse(toolResults[0].content).tool_call_id, toolCallId)
  assert.equal(JSON.parse(toolResults[0].content).content, '最终工具结果')
  assert.equal(messages.filter(message => message.response_id === 'response-old' && message.content === '旧回复').length, 1)
  assert.equal(messages.filter(message => message.response_id === 'response-new-final' && message.content === '新回复').length, 1)
  assert.equal(messages.some(message => message.content === '旧迟到正文'), false)
  assert.equal(messages.some(message => message.role === 'err'), false)
  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('composed websocket done respects background result visibility and preserves complete response', async t => {
  for (const showToolCalls of [true, false]) {
    await t.test(`show_tool_calls=${showToolCalls}`, async nested => {
      const files = [{ name: 'answer.txt', url: '/files/answer.txt' }]
      const backgroundHistory = [
        {
          id: 101,
          type: 'background_result',
          role: 'user',
          content: JSON.stringify({
            type: 'background_tool_result',
            task: 'visible user background result',
            tool_call: { id: 'background-user-call', name: 'background_lookup', arguments: {} }
          })
        },
        { id: 102, type: 'background_result', role: 'user', content: '{broken-json' },
        {
          id: 103,
          type: 'background_result',
          role: 'assistant',
          content: JSON.stringify({
            type: 'background_tool_result',
            task: 'visible assistant background result',
            tool_call: { id: 'background-assistant-call', name: 'background_lookup', arguments: {} }
          })
        },
        { id: 104, type: 'background_result', role: 'assistant', content: '{broken-json' }
      ]
      const internalHistory = [
        ...backgroundHistory,
        { id: 105, role: 'system', content: 'hidden system' },
        { id: 106, type: 'message', role: 'background_system', content: 'hidden legacy system' }
      ]
      const toolCall = {
        id: 201,
        role: 'assistant',
        content: '',
        tool_calls: [{
          id: 'call-1',
          type: 'function',
          function: { name: 'lookup', arguments: '{}' }
        }]
      }
      const toolResult = {
        id: 202,
        role: 'tool',
        tool_call_id: 'call-1',
        content: 'lookup result'
      }
      const finalHistoryMessage = { id: 203, role: 'assistant', content: 'complete reply' }
      const backgroundIds = new Set(backgroundHistory.map(message => String(message.id)))
      const hiddenIds = new Set(internalHistory
        .filter(message => ['system', 'background_system'].includes(message.role))
        .map(message => String(message.id)))
      const harness = await createHarness(nested, {
        sessions: [createSession('A', { source: 'ws', show_tool_calls: showToolCalls })],
        initialRoute: { path: '/', query: { session_id: 'A' } }
      })
      const { chat } = harness

      await harness.flush()
      const sendPromise = chat.wsSend('request')
      await harness.flush()
      const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
      assert.ok(request)
      await receiveWs(harness, {
        type: 'input_accepted',
        session_id: 'A',
        request_id: request.request_id
      })
      await sendPromise

      const doneEvent = {
        type: 'done',
        session_id: 'A',
        request_id: request.request_id,
        work_id: 'done-work',
        response_id: 'done-response',
        response: {
          work_id: 'done-work',
          response_id: 'done-response',
          message_id: 204,
          choices: [{
            message: { role: 'assistant', content: 'complete reply' },
            finish_reason: 'stop'
          }],
          history: [...internalHistory, toolCall, toolResult, finalHistoryMessage],
          files
        }
      }

      const assertDoneMessages = (expectedBackgroundCount = showToolCalls ? backgroundIds.size : 0) => {
        const messages = chat.messages.value
        assert.equal(
          messages.filter(message => backgroundIds.has(String(message.db_id ?? message.id))).length,
          expectedBackgroundCount
        )
        assert.equal(
          messages.some(message => hiddenIds.has(String(message.db_id ?? message.id))),
          false
        )
        assert.equal(
          messages.some(message => ['system', 'background_system'].includes(message.role)),
          false
        )

        const finalMessages = messages.filter(
          message => message.role === 'assistant' && message.content === 'complete reply'
        )
        assert.equal(finalMessages.length, 1)
        assert.deepEqual(finalMessages[0].files, files)
        assert.equal(String(finalMessages[0].db_id), '204')
        assert.equal(
          messages.filter(message => String(message.db_id ?? message.id) === '201').length,
          showToolCalls ? 1 : 0
        )
        assert.equal(
          messages.filter(message => String(message.db_id ?? message.id) === '202').length,
          showToolCalls ? 1 : 0
        )
      }

      const responseHistorySnapshot = JSON.stringify(doneEvent.response.history)
      await receiveWs(harness, doneEvent)
      assertDoneMessages()
      const nonBackgroundMessageCount = chat.messages.value.filter(
        message => !backgroundIds.has(String(message.db_id ?? message.id))
      ).length

      await receiveWs(harness, doneEvent)
      assert.equal(
        chat.messages.value.filter(
          message => !backgroundIds.has(String(message.db_id ?? message.id))
        ).length,
        nonBackgroundMessageCount
      )
      assertDoneMessages(
        chat.messages.value.filter(message => backgroundIds.has(String(message.db_id ?? message.id))).length
      )
      assert.equal(JSON.stringify(doneEvent.response.history), responseHistorySnapshot)
    })
  }
})

test('composed HTTP send does not resubmit an outcome with unknown confirmation', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'http' })],
    initialRoute: { path: '/', query: { session_id: 'A' } },
    httpResponses: [{}]
  })
  const { chat } = harness

  await harness.flush()
  chat.inputMsg.value = 'once'
  await chat.httpSend()
  await harness.flush()

  assert.equal(harness.apiState.completionRequests.length, 1)
  assert.equal(chat.messages.value.filter(message => (
    message.role === 'user' && message.content === 'once'
  )).length, 1)
  assert.ok(harness.messages.some(entry => (
    entry.type === 'warning' && entry.value === 'chat.submit_outcome_unknown'
  )))
  assert.equal(chat.inputMsg.value, '')
  assert.equal(chat.messages.value.some(message => message.role === 'thinking'), false)

  await chat.loadSessions()
  await harness.flush()
  assert.equal(harness.apiState.completionRequests.length, 1)
})
