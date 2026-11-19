import assert from 'node:assert/strict'
import { test } from 'node:test'

import * as toolResultState from '../../../src/utils/toolResultState.js'
import { createHarness, createSession } from '../../chatDraftHarness.js'
import { normalizeContent, useChatMessagePresentation } from './recoveredPresentationHarness.js'

const concurrentToolCases = [
  {
    key: 'shell',
    label: '普通 execute_shell',
    name: 'execute_shell',
    arguments: index => ({
      command: `printf concurrent-${index}`,
      execution_mode: 'non_interactive'
    })
  },
  {
    key: 'interactive-shell',
    label: '交互 execute_shell',
    name: 'execute_shell',
    arguments: index => ({
      command: `printf interactive-${index}`,
      execution_mode: 'interactive',
      interactive: true
    })
  },
  {
    key: 'terminal-watch',
    label: 'AI 主动 terminal_watch',
    name: 'terminal_watch',
    arguments: index => ({
      terminal_session_id: String.fromCharCode(97 + index).repeat(32),
      after_offset: index,
      condition: 'process_exit'
    })
  }
]

const concurrentResultOrders = [
  { key: 'tool-end-first', label: '最终 tool_end 后 background_result 通知' },
  { key: 'background-first', label: 'background_result 通知后最终 tool_end' }
]

for (const toolCase of concurrentToolCases) {
  for (const resultOrder of concurrentResultOrders) {
    test(`并发${toolCase.label}结果归入原工具组：${resultOrder.label}`, async t => {
      const responseId = `response-concurrent-${toolCase.key}`
      const bodyResponseId = `${responseId}-body`
      const workId = `work-concurrent-${toolCase.key}`
      const toolCalls = [0, 1].map(index => ({
        id: `call-${toolCase.key}-${index + 1}`,
        name: toolCase.name,
        arguments: toolCase.arguments(index)
      }))
      const queuedResults = toolCalls.map((_toolCall, index) => ({
        status: 'queued',
        task_id: 700 + index
      }))
      const queuedMessageIds = [701, 702]
      const markerIds = [703, 704]
      const finalResults = toolCalls.map((_toolCall, index) => `final-${toolCase.key}-${index + 1}`)
      const bodyText = `独立助手正文-${toolCase.key}`
      let historyRows = []
      const sessionsHistory = ({ sessionId }) => sessionId === 'A' ? historyRows : []
      const harness = await createHarness(t, {
        sessions: [createSession('A', { source: 'ws', show_tool_calls: true, show_reasoning: true })],
        initialRoute: { path: '/', query: { session_id: 'A' } },
        sessionsHistory
      })
      const { chat } = harness
      await harness.flush()

      const presentationProps = {
        get messages() {
          return chat.messages.value
        },
        currentSessionShowReasoning: true,
        historyLoading: false,
        contextSummarizing: false
      }
      const presentation = useChatMessagePresentation({ props: presentationProps, t: key => key })
      presentation.displayMessages.value

      const receiveWs = async event => {
        harness.receiveWs(event)
        await harness.flush()
      }
      const sendPromise = chat.wsSend(`并发${toolCase.key}`)
      await harness.flush()
      const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
      assert.ok(request)
      await receiveWs({
        type: 'input_accepted',
        session_id: 'A',
        request_id: request.request_id,
        work_id: workId,
        submission_status: 'accepted'
      })
      await sendPromise

      let eventSequence = 0
      const streamEvent = (type, eventResponseId, fields = {}) => ({
        type,
        session_id: 'A',
        request_id: request.request_id,
        work_id: workId,
        response_id: eventResponseId,
        event_sequence_no: ++eventSequence,
        ...fields
      })

      await receiveWs(streamEvent('agent_loop_start', responseId, { turn: 1 }))
      for (const toolCall of toolCalls) {
        await receiveWs(streamEvent('tool_start', responseId, {
          tool_call_id: toolCall.id,
          name: toolCall.name,
          arguments: toolCall.arguments
        }))
      }
      for (const [index, toolCall] of toolCalls.entries()) {
        await receiveWs(streamEvent('tool_end', responseId, {
          tool_call_id: toolCall.id,
          name: toolCall.name,
          result: queuedResults[index]
        }))
      }

      await receiveWs(streamEvent('agent_loop_start', bodyResponseId, { turn: 2 }))
      await receiveWs(streamEvent('content', bodyResponseId, { turn: 2, content: bodyText }))

      const finalToolEnds = toolCalls.map((toolCall, index) => streamEvent('tool_end', responseId, {
        tool_call_id: toolCall.id,
        name: toolCall.name,
        result: finalResults[index]
      }))
      const queuedHistoryRows = toolCalls.map((toolCall, index) => ({
        id: queuedMessageIds[index],
        created_at: 100 + index,
        type: 'tool_result',
        role: 'tool',
        tool_call_id: toolCall.id,
        content: JSON.stringify({
          role: 'tool',
          tool_call_id: toolCall.id,
          content: queuedResults[index]
        })
      }))
      const backgroundMarkers = toolCalls.map((toolCall, index) => ({
        id: markerIds[index],
        created_at: 102 + index,
        type: 'background_result',
        role: 'assistant',
        content: JSON.stringify({
          type: 'background_tool_result',
          original_tool_result_message_id: queuedMessageIds[index],
          task: finalResults[index],
          tool_call: {
            id: toolCall.id,
            name: toolCall.name,
            arguments: toolCall.arguments
          }
        })
      }))
      historyRows = [...queuedHistoryRows, ...backgroundMarkers]
      const backgroundUpdate = () => ({
        type: 'tool_results_update',
        session_id: 'A',
        work_id: workId,
        event_id: `background-result-${toolCase.key}`,
        messages: backgroundMarkers.map(marker => ({ ...marker }))
      })

      const updates = resultOrder.key === 'tool-end-first'
        ? [...finalToolEnds, backgroundUpdate(), backgroundUpdate()]
        : [backgroundUpdate(), ...finalToolEnds, backgroundUpdate()]
      for (const update of updates) await receiveWs(update)

      const displayedMessages = Array.from(presentation.displayMessages.value)
      const toolGroups = displayedMessages.filter(message => message.type === 'tool_group')
      assert.equal(toolGroups.length, 1, '展示应只有一个原工具组')
      const [toolGroup] = toolGroups
      assert.equal(toolGroup.response_id, responseId)
      assert.equal(toolGroup.pairs.length, 2, '原工具组应包含两个调用配对')
      assert.equal(presentation.getToolGroupStateClass(toolGroup), 'is-result')
      assert.equal(presentation.getToolGroupStatus(toolGroup), 'chat.tool_status_returned')

      const expectedResults = new Map([
        [toolCalls[0].id, finalResults[0]],
        [toolCalls[1].id, finalResults[1]]
      ])
      assert.deepEqual(
        new Set(toolGroup.pairs.map(pair => pair.id)),
        new Set(toolCalls.map(toolCall => toolCall.id))
      )
      for (const toolCall of toolCalls) {
        const pair = toolGroup.pairs.find(item => item.id === toolCall.id)
        assert.ok(pair)
        assert.equal(pair.toolCall.name, toolCall.name)
        assert.deepEqual(normalizeContent(pair.toolCall.arguments), toolCall.arguments)
        assert.equal(presentation.isToolPairReturned(pair), true)
        const resultPayload = normalizeContent(pair.resultMessage?.content)
        assert.equal(resultPayload?.content, expectedResults.get(toolCall.id))
      }

      const rawToolResults = chat.messages.value.filter(message => (
        toolResultState.getToolResultCallId(message) !== null
      ))
      assert.equal(rawToolResults.length, 2, '原始列表中两个调用各自只能保留一条结果')
      for (const toolCall of toolCalls) {
        assert.equal(
          rawToolResults.filter(message => toolResultState.getToolResultCallId(message) === toolCall.id).length,
          1,
          `原始列表中调用 ${toolCall.id} 只能保留一条结果`
        )
      }

      assert.equal(
        displayedMessages.filter(message => message.role === 'assistant' && message.content === bodyText).length,
        1,
        '独立助手正文不得重复'
      )
      assert.equal(
        chat.messages.value.filter(message => message.role === 'assistant' && message.content === bodyText).length,
        1,
        '原始消息中的独立助手正文不得重复'
      )
    })
  }
}
