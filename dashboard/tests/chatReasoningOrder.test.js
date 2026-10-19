import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createHarness, createSession } from './chatDraftHarness.js'

const persistedHistory = [
  { id: 41, role: 'user', content: '复现工具参数预检失败' },
  {
    id: 42,
    type: 'tool_call',
    role: 'assistant',
    content: JSON.stringify({
      role: 'assistant',
      tool_calls: [{
        id: 'invalid-file-call',
        name: 'file_tool',
        arguments: { path: 'fixture.txt', content: 'fixture' }
      }]
    }),
    reasoning_content: '第一轮思考'
  },
  {
    id: 43,
    type: 'tool_result',
    role: 'tool',
    content: JSON.stringify({
      role: 'tool',
      tool_call_id: 'invalid-file-call',
      content: JSON.stringify({
        status: 'failed',
        tool_name: 'file_tool',
        error: 'Missing required argument: operation'
      })
    })
  }
]

const selectSession = async (harness, sessionId) => {
  const session = harness.chat.sessions.value.find(item => item.session_id === sessionId)
  await harness.module.handleSelectSession(session)
  await harness.flush()
}

const receiveWs = async (harness, event) => {
  await harness.receiveWs(event)
  await harness.flush()
}

const parseContent = message => {
  if (typeof message?.content !== 'string') return message?.content
  try {
    return JSON.parse(message.content)
  } catch {
    return message.content
  }
}

const hasToolCall = message => (
  (Array.isArray(message?.tool_calls) && message.tool_calls.length > 0)
  || (Array.isArray(parseContent(message)?.tool_calls) && parseContent(message).tool_calls.length > 0)
)

const thoughtOrder = messages => messages
  .map(message => message.reasoning_content || (message.role === 'reasoning' ? message.content : ''))
  .filter(Boolean)

for (const scenario of [
  { name: '正常工具事件对照', sendsToolEvents: true, recovery: null },
  { name: '预检失败且缺少工具事件', sendsToolEvents: false, recovery: 'reenter' },
  { name: '完整终态遗留孤立思考', sendsToolEvents: false, recovery: 'done' },
  { name: '连续两轮预检失败造成思考回跳', sendsToolEvents: false, recovery: 'done', repeatedPrecheck: true }
]) {
  test(`思考顺序与终态历史：${scenario.name}`, async t => {
    let loadPersistedHistory = false
    let historyForReload = persistedHistory
    const harness = await createHarness(t, {
      sessions: [
        createSession('A', { source: 'ws', show_tool_calls: true, show_reasoning: true }),
        createSession('B', { source: 'ws', show_tool_calls: true, show_reasoning: true })
      ],
      initialRoute: { path: '/', query: { session_id: 'A' } },
      sessionsHistory: ({ sessionId }) => (
        sessionId === 'A' && loadPersistedHistory ? historyForReload : []
      )
    })
    const { chat } = harness

    await harness.flush()
    assert.equal(chat.messages.value.length, 0)

    const sendPromise = chat.wsSend('复现工具参数预检失败')
    await harness.flush()
    const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
    assert.ok(request)
    await receiveWs(harness, {
      type: 'input_accepted',
      session_id: 'A',
      request_id: request.request_id
    })
    await sendPromise

    let eventSequenceNo = 0
    const streamEvent = (type, responseId, turn, fields = {}) => ({
      type,
      session_id: 'A',
      request_id: request.request_id,
      work_id: 7,
      response_id: responseId,
      turn,
      event_sequence_no: ++eventSequenceNo,
      ...fields
    })

    await receiveWs(harness, streamEvent('agent_loop_start', 'response-first', 1))
    await receiveWs(harness, streamEvent('reasoning', 'response-first', 1, {
      content: '第一轮思考'
    }))
    await receiveWs(harness, streamEvent('agent_loop_output', 'response-first', 1))
    await receiveWs(harness, streamEvent('turn_end', 'response-first', 1, {
      message_id: 42,
      finish_reason: 'tool_calls',
      reasoning_content: '第一轮思考'
    }))

    if (scenario.sendsToolEvents) {
      await receiveWs(harness, streamEvent('tool_start', 'response-first', 1, {
        tool_call_id: 'invalid-file-call',
        name: 'file_tool',
        arguments: { operation: 'write', path: 'fixture.txt', content: 'fixture' }
      }))
      await receiveWs(harness, streamEvent('tool_end', 'response-first', 1, {
        tool_call_id: 'invalid-file-call',
        name: 'file_tool',
        result: 'ok'
      }))
    }

    await receiveWs(harness, streamEvent('agent_loop_start', 'response-second', 2))
    await receiveWs(harness, streamEvent('reasoning', 'response-second', 2, {
      content: '第二轮思考'
    }))

    assert.deepEqual(
      thoughtOrder(chat.messages.value),
      ['第一轮思考', '第二轮思考'],
      '普通顺序此时未复现乱序'
    )

    if (scenario.repeatedPrecheck) {
      await receiveWs(harness, streamEvent('agent_loop_output', 'response-second', 2))
      await receiveWs(harness, streamEvent('turn_end', 'response-second', 2, {
        message_id: 44,
        finish_reason: 'tool_calls',
        reasoning_content: '第二轮思考'
      }))
      await receiveWs(harness, streamEvent('agent_loop_start', 'response-third', 3))
      await receiveWs(harness, streamEvent('reasoning', 'response-third', 3, {
        content: '第三轮思考'
      }))
    }

    const firstResponse = chat.messages.value.find(message => (
      message.role === 'assistant' && message.response_id === 'response-first'
    ))
    assert.ok(firstResponse)
    assert.equal(String(firstResponse.db_id), '42')
    assert.equal(firstResponse.reasoning_content, '第一轮思考')
    assert.equal(chat.messages.value.some(message => (
      message.response_id === 'response-second'
      && message.reasoning_content === '第二轮思考'
    )), true)
    assert.equal(chat.messages.value.some(message => (
      message.response_id === 'response-second' && message._stream_finalized === true
    )), Boolean(scenario.repeatedPrecheck))

    if (scenario.recovery === null) {
      assert.equal(hasToolCall(firstResponse), true)
      assert.equal(JSON.parse(firstResponse.content).tool_calls[0].id, 'invalid-file-call')
      return
    }

    if (!scenario.sendsToolEvents) {
      assert.equal(firstResponse.content || '', '')
      assert.equal(firstResponse.type, undefined)
      assert.equal(hasToolCall(firstResponse), false)
    }

    if (scenario.recovery === 'done') {
      const finalTurn = scenario.repeatedPrecheck ? 3 : 2
      const finalResponseId = scenario.repeatedPrecheck ? 'response-third' : 'response-second'
      const finalMessageId = scenario.repeatedPrecheck ? 46 : 44
      const finalReasoning = scenario.repeatedPrecheck ? '第三轮思考' : '第二轮思考'

      await receiveWs(harness, streamEvent('content', finalResponseId, finalTurn, {
        content: '最终答复'
      }))
      await receiveWs(harness, streamEvent('turn_end', finalResponseId, finalTurn, {
        message_id: finalMessageId,
        content: '最终答复',
        finish_reason: 'stop',
        reasoning_content: finalReasoning
      }))

      const persistedToolCall = JSON.parse(persistedHistory[1].content)
      const persistedToolResult = JSON.parse(persistedHistory[2].content)
      const responseHistory = [
        {
          id: 42,
          role: 'assistant',
          content: '',
          tool_calls: persistedToolCall.tool_calls,
          reasoning_content: '第一轮思考'
        },
        {
          id: 43,
          role: 'tool',
          tool_call_id: persistedToolResult.tool_call_id,
          content: persistedToolResult.content
        }
      ]
      if (scenario.repeatedPrecheck) {
        responseHistory.push(
          {
            id: 44,
            role: 'assistant',
            content: '',
            tool_calls: [{
              id: 'invalid-file-call-second',
              name: 'file_tool',
              arguments: { path: 'fixture.txt', content: 'fixture' }
            }],
            reasoning_content: '第二轮思考'
          },
          {
            id: 45,
            role: 'tool',
            tool_call_id: 'invalid-file-call-second',
            content: persistedToolResult.content
          }
        )
      }
      responseHistory.push({
        id: finalMessageId,
        role: 'assistant',
        content: '最终答复',
        reasoning_content: finalReasoning
      })

      const doneEvent = streamEvent('done', finalResponseId, finalTurn, {
        request_ids: [request.request_id],
        message_id: finalMessageId,
        response: {
          work_id: 7,
          message_id: finalMessageId,
          response_id: finalResponseId,
          choices: [{
            message: {
              role: 'assistant',
              content: '最终答复',
              reasoning_content: finalReasoning
            },
            finish_reason: 'stop'
          }],
          history: responseHistory
        }
      })
      await receiveWs(harness, doneEvent)

      const terminalState = JSON.stringify(chat.messages.value)
      await receiveWs(harness, doneEvent)
      assert.equal(
        JSON.stringify(chat.messages.value),
        terminalState,
        '重复回放同一 done 事件不应改变已展示消息'
      )

      const terminalMessages = Array.from(chat.messages.value, message => ({ ...message }))
      const terminalThoughts = Array.from(thoughtOrder(terminalMessages))
      const terminalDbIds = terminalMessages
        .filter(message => message.db_id != null)
        .map(message => String(message.db_id))
      assert.equal(new Set(terminalDbIds).size, terminalDbIds.length)
      assert.deepEqual(
        terminalDbIds.sort(),
        scenario.repeatedPrecheck ? ['42', '43', '44', '45', '46'] : ['42', '43', '44']
      )

      const expectedToolCalls = scenario.repeatedPrecheck
        ? [['42', 'invalid-file-call'], ['44', 'invalid-file-call-second']]
        : [['42', 'invalid-file-call']]
      for (const [dbId, toolCallId] of expectedToolCalls) {
        const message = terminalMessages.find(item => String(item.db_id) === dbId)
        assert.ok(message)
        assert.equal(hasToolCall(message), true)
        const messageContent = parseContent(message)
        const toolCalls = message.tool_calls?.length
          ? message.tool_calls
          : messageContent.tool_calls
        assert.equal(toolCalls[0].id, toolCallId)
      }

      const expectedToolResults = scenario.repeatedPrecheck
        ? [['43', 'invalid-file-call'], ['45', 'invalid-file-call-second']]
        : [['43', 'invalid-file-call']]
      for (const [dbId, toolCallId] of expectedToolResults) {
        const message = terminalMessages.find(item => String(item.db_id) === dbId)
        assert.ok(message)
        assert.equal(message.role, 'tool')
        const resultEnvelope = parseContent(message)
        assert.equal(message.tool_call_id ?? resultEnvelope.tool_call_id, toolCallId)
        const result = resultEnvelope.status
          ? resultEnvelope
          : parseContent({ content: resultEnvelope.content })
        assert.equal(result.status, 'failed')
        assert.equal(result.tool_name, 'file_tool')
        assert.match(result.error, /Missing required argument: operation/)
      }

      assert.equal(terminalMessages.some(message => (
        message.role === 'reasoning' || message.role === 'thinking' || message.type === 'thinking'
      )), false)
      assert.equal(terminalMessages.some(message => (
        message.role === 'assistant' && !hasToolCall(message) && !String(message.content || '').trim()
      )), false)

      historyForReload = [
        persistedHistory[0],
        ...responseHistory.map(message => {
          if (message.role === 'tool') {
            return {
              id: message.id,
              type: 'tool_result',
              role: 'tool',
              content: JSON.stringify({
                role: 'tool',
                tool_call_id: message.tool_call_id,
                content: message.content
              })
            }
          }
          if (Array.isArray(message.tool_calls)) {
            return {
              id: message.id,
              type: 'tool_call',
              role: 'assistant',
              content: JSON.stringify({ role: 'assistant', tool_calls: message.tool_calls }),
              reasoning_content: message.reasoning_content
            }
          }
          return {
            id: message.id,
            role: 'assistant',
            content: message.content,
            reasoning_content: message.reasoning_content
          }
        })
      ]

      await selectSession(harness, 'B')
      loadPersistedHistory = true
      await selectSession(harness, 'A')

      const restoredFirstResponses = chat.messages.value.filter(message => String(message.db_id) === '42')
      assert.equal(restoredFirstResponses.length, 1)
      const restoredFirstResponse = restoredFirstResponses[0]
      assert.equal(restoredFirstResponse.role, 'assistant')
      assert.equal(hasToolCall(restoredFirstResponse), true)
      assert.equal(parseContent(restoredFirstResponse).tool_calls[0].id, 'invalid-file-call')
      const restoredDbIds = Array.from(chat.messages.value)
        .filter(message => message.db_id != null)
        .map(message => String(message.db_id))
      assert.equal(new Set(restoredDbIds).size, restoredDbIds.length)
      assert.deepEqual(
        restoredDbIds.sort(),
        scenario.repeatedPrecheck
          ? ['41', '42', '43', '44', '45', '46']
          : ['41', '42', '43', '44']
      )
      assert.deepEqual(
        Array.from(thoughtOrder(chat.messages.value)),
        scenario.repeatedPrecheck
          ? ['第一轮思考', '第二轮思考', '第三轮思考']
          : ['第一轮思考', '第二轮思考']
      )
      assert.equal(chat.messages.value.some(message => (
        message.role === 'reasoning' || message.role === 'thinking' || message.type === 'thinking'
      )), false)
      assert.equal(chat.messages.value.some(message => (
        message.role === 'assistant' && !hasToolCall(message) && !String(message.content || '').trim()
      )), false)

      assert.deepEqual(
        terminalThoughts,
        scenario.repeatedPrecheck
          ? ['第一轮思考', '第二轮思考', '第三轮思考']
          : ['第一轮思考', '第二轮思考']
      )
      assert.equal(terminalMessages.filter(message => String(message.db_id) === '42').length, 1)

      return
    }

    await selectSession(harness, 'B')
    loadPersistedHistory = true
    await selectSession(harness, 'A')

    const restoredToolCall = chat.messages.value.find(message => String(message.db_id) === '42')
    const restoredToolResult = chat.messages.value.find(message => String(message.db_id) === '43')
    assert.ok(restoredToolCall)
    assert.equal(restoredToolCall.role, 'assistant')
    assert.equal(hasToolCall(restoredToolCall), true)
    assert.ok(restoredToolResult)
    assert.equal(restoredToolResult.role, 'tool')
    assert.match(parseContent(restoredToolResult).content, /Missing required argument: operation/)

    const firstThoughtMessages = chat.messages.value.filter(message => (
      message.reasoning_content === '第一轮思考'
    ))
    assert.equal(firstThoughtMessages.length, 1)
    assert.equal(firstThoughtMessages[0], restoredToolCall)
    assert.equal(chat.messages.value.some(message => (
      message.role === 'reasoning' && message.reasoning_content === '第一轮思考'
    )), false)
    assert.equal(chat.messages.value.some(message => (
      message.role === 'assistant'
      && message.response_id === 'response-first'
      && !hasToolCall(message)
      && !String(message.content || '').trim()
    )), false)

  })
}
