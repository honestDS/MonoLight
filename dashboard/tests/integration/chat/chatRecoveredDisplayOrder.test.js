import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import { getReasoningCollapseName } from '../../../src/utils/chatPresentation.js'
import { MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME } from '../../../src/constants/index.js'
import { createHarness, createSession } from '../../chatDraftHarness.js'
import {
  normalizeContent,
  storedToolCall,
  storedToolResult,
  summarizeDisplay,
  useChatMessagePresentation
} from './recoveredPresentationHarness.js'

for (const scenario of [
  { name: '无历史并发', mergeAt: null },
  { name: '工具turn_end后tool_start前历史合并', mergeAt: 'first-tool-turn' },
  { name: '最终正文后turn_end前历史合并', mergeAt: 'final-content' },
  { name: '最终输出前重入', reenterAt: 'before-content' },
  { name: '最终输出期间连续历史分页', reenterAt: 'before-content', paginate: true },
  { name: '最终正文后重入', reenterAt: 'after-content' }
]) {
  test(`DeepSeek 展示顺序：${scenario.name}`, async t => {
    const committedHistory = scenario.paginate
      ? Array.from({ length: 120 }, (_, index) => {
          const id = index + 1
          return {
            id,
            created_at: id / 1000,
            role: 'user',
            content: `fixture old history ${id}`
          }
        })
      : []
    const harness = await createHarness(t, {
      sessions: [
        createSession('A', { source: 'ws', show_tool_calls: true, show_reasoning: true }),
        createSession('B', { source: 'ws', show_tool_calls: true, show_reasoning: true })
      ],
      initialRoute: { path: '/', query: { session_id: 'A' } },
      sessionsHistory: ({ sessionId, page, size }) => {
        if (sessionId !== 'A') return []
        if (!scenario.paginate) return committedHistory
        const end = committedHistory.length - ((page - 1) * size)
        return committedHistory.slice(Math.max(0, end - size), end)
      }
    })
    const { chat } = harness
    await harness.flush()
    const oldHistoryIds = messages => Array.from(messages)
      .filter(message => (
        message.role === 'user'
        && Number(message.db_id ?? message.id) >= 1
        && Number(message.db_id ?? message.id) <= 120
      ))
      .map(message => String(message.db_id ?? message.id))
    const initiallyLoadedOldHistoryIds = scenario.paginate
      ? oldHistoryIds(chat.messages.value)
      : []
    if (scenario.paginate) {
      assert.ok(initiallyLoadedOldHistoryIds.length > 0, '初始历史加载应包含旧用户消息')
    }
    let oldestOldHistoryIdBeforePagination = null
    let paginatedOldHistoryIds = []

    const presentationProps = {
      get messages() {
        return chat.messages.value
      },
      currentSessionShowReasoning: true,
      historyLoading: false,
      contextSummarizing: false
    }
    const cachedPresentation = useChatMessagePresentation({ props: presentationProps, t: key => key })
    cachedPresentation.displayMessages.value

    const receiveWs = async event => {
      harness.receiveWs(event)
      await harness.flush()
      const currentDisplay = cachedPresentation.displayMessages.value
      return Array.from(currentDisplay)
    }

    const sendPromise = chat.wsSend('fixture request')
    await harness.flush()
    const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
    assert.ok(request)

    let eventSequence = 0
    const streamEvent = (type, responseId = null, turn = null, fields = {}) => ({
      type,
      session_id: 'A',
      work_id: 'work7',
      request_id: request.request_id,
      event_sequence_no: ++eventSequence,
      ...(responseId ? { response_id: responseId } : {}),
      ...(turn === null || turn === undefined ? {} : { turn }),
      ...fields
    })

    committedHistory.push({
      id: 201,
      created_at: 1,
      role: 'user',
      content: 'fixture request',
      request_id: request.request_id
    })
    await receiveWs(streamEvent('input_accepted'))
    await sendPromise

    const responseHistory = []
    const commitAssistantCall = (id, call, reasoning) => {
      const message = storedToolCall(id, call, reasoning)
      committedHistory.push(message)
      responseHistory.push({
        id,
        created_at: id - 200,
        role: 'assistant',
        content: '',
        tool_calls: [call],
        ...(reasoning ? { reasoning_content: reasoning } : {})
      })
    }
    const commitToolResult = (id, callId, result) => {
      const message = storedToolResult(id, callId, result)
      committedHistory.push(message)
      responseHistory.push({
        id,
        created_at: id - 200,
        role: 'tool',
        tool_call_id: callId,
        content: result
      })
    }
    const mergeCommittedHistory = async () => {
      await receiveWs(streamEvent('audit_tool_results_update', null, null, { messages: [] }))
    }
    const reenterFinalStream = async ({ replayContent = false } = {}) => {
      const sessionB = chat.sessions.value.find(session => session.session_id === 'B')
      const sessionA = chat.sessions.value.find(session => session.session_id === 'A')
      harness.module.handleSelectSession(sessionB)
      await harness.flush()
      harness.module.handleSelectSession(sessionA)
      await harness.flush()

      await receiveWs(streamEvent('agent_loop_start', 'response-final', 6))
      await receiveWs(streamEvent('reasoning', 'response-final', 6, { content: '最终思考' }))
      await receiveWs(streamEvent('agent_loop_output', 'response-final', 6))
      if (replayContent) {
        await receiveWs(streamEvent('content', 'response-final', 6, { content: '最终答复' }))
      }
    }
    const paginateOldHistory = async () => {
      const previousOldIds = oldHistoryIds(cachedPresentation.displayMessages.value)
      assert.ok(previousOldIds.length > 0, '分页前展示应包含已加载的旧历史')
      oldestOldHistoryIdBeforePagination = Math.min(...previousOldIds.map(Number))
      const previousHistoryRequestCount = harness.apiState.historyRequests.length
      let captureScrollAnchorCount = 0
      chat.messageList.value = {
        scrollTop: 0,
        canFollowOutput: () => false,
        captureScrollAnchor: () => {
          captureScrollAnchorCount += 1
          return { captureScrollAnchorCount }
        },
        restoreScrollAnchor: async () => {},
        scrollToBottom: async () => {},
        addEventListener: () => {},
        removeEventListener: () => {}
      }

      let paginatedDisplay = []
      for (let page = 0; page < 3; page += 1) {
        await chat.handleScroll()
        await harness.flush()
        paginatedDisplay = Array.from(cachedPresentation.displayMessages.value)
      }

      assert.equal(
        harness.apiState.historyRequests.length - previousHistoryRequestCount,
        3,
        '连续滚动应实际请求三页历史'
      )
      assert.equal(captureScrollAnchorCount, 3, '每次历史分页都应捕获滚动锚点')
      paginatedOldHistoryIds = oldHistoryIds(paginatedDisplay)
      assert.ok(paginatedOldHistoryIds.length > 0, '分页后展示应保留旧用户历史')
      assert.ok(
        Math.min(...paginatedOldHistoryIds.map(Number)) < oldestOldHistoryIdBeforePagination,
        '连续分页应扩展到更早的旧历史'
      )
      assert.ok(
        initiallyLoadedOldHistoryIds.every(id => paginatedOldHistoryIds.includes(id)),
        '连续分页不得丢失最初已加载的旧消息'
      )
    }

    const memoryCall = {
      id: 'call-memory',
      name: MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
      arguments: { operation: 'recall', query: 'fixture' }
    }
    const memoryPayload = JSON.parse(readFileSync(
      new URL('../../../../tests/memory_recall_fixture.json', import.meta.url),
      'utf8'
    ))
    memoryPayload.current_session_id = 'A'
    const memoryResult = JSON.stringify(memoryPayload)
    await receiveWs(streamEvent('agent_loop_start', 'response-memory', 0))
    commitAssistantCall(202, memoryCall, '')
    await receiveWs(streamEvent('turn_end', 'response-memory', null, {
      message_id: 202,
      finish_reason: 'tool_calls'
    }))
    await receiveWs(streamEvent('tool_start', 'response-memory', null, {
      tool_call_id: 'call-memory',
      name: MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
      arguments: { operation: 'recall', query: 'fixture' }
    }))
    commitToolResult(203, 'call-memory', memoryResult)
    await receiveWs(streamEvent('tool_end', 'response-memory', null, {
      tool_call_id: 'call-memory',
      name: MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
      result: memoryResult
    }))

    const toolRounds = [
      { id: 204, callId: 'call-1', name: 'execute_shell', arguments: { command: 'printf fixture' } },
      { id: 206, callId: 'call-2', name: 'execute_shell', arguments: { command: 'printf fixture-two' } },
      { id: 208, callId: 'call-3', name: 'manage_todo', arguments: { operation: 'add', task: 'fixture task' } },
      { id: 210, callId: 'call-4', name: 'manage_todo', arguments: { operation: 'complete', task_id: 'fixture-task' } }
    ]

    for (const [index, toolRound] of toolRounds.entries()) {
      const turn = index + 1
      const responseId = `response-tool-${turn}`
      const thought = `工具轮${turn}思考`
      const result = `fixture result ${turn}`
      const call = {
        id: toolRound.callId,
        name: toolRound.name,
        arguments: toolRound.arguments
      }

      await receiveWs(streamEvent('agent_loop_start', responseId, turn))
      await receiveWs(streamEvent('reasoning', responseId, turn, { content: thought }))
      await receiveWs(streamEvent('agent_loop_output', responseId, turn))
      commitAssistantCall(toolRound.id, call, thought)
      await receiveWs(streamEvent('turn_end', responseId, turn, {
        message_id: toolRound.id,
        finish_reason: 'tool_calls',
        reasoning_content: thought
      }))

      if (index === 0 && scenario.mergeAt === 'first-tool-turn') await mergeCommittedHistory()

      await receiveWs(streamEvent('tool_start', responseId, null, {
        tool_call_id: toolRound.callId,
        name: toolRound.name,
        arguments: toolRound.arguments
      }))
      commitToolResult(toolRound.id + 1, toolRound.callId, result)
      await receiveWs(streamEvent('tool_end', responseId, null, {
        tool_call_id: toolRound.callId,
        name: toolRound.name,
        result
      }))
    }

    await receiveWs(streamEvent('agent_loop_start', 'response-goal', 5))
    await receiveWs(streamEvent('agent_loop_output', 'response-goal', 5))

    await receiveWs(streamEvent('agent_loop_start', 'response-final', 6))
    await receiveWs(streamEvent('reasoning', 'response-final', 6, { content: '最终思考' }))
    await receiveWs(streamEvent('agent_loop_output', 'response-final', 6))
    if (scenario.reenterAt === 'before-content') {
      await reenterFinalStream()
      if (scenario.paginate) await paginateOldHistory()
    }
    await receiveWs(streamEvent('content', 'response-final', 6, { content: '最终答复' }))
    if (scenario.reenterAt === 'after-content') await reenterFinalStream({ replayContent: true })

    const finalAssistant = {
      id: 212,
      created_at: 12,
      role: 'assistant',
      content: '最终答复',
      reasoning_content: '最终思考'
    }
    committedHistory.push(finalAssistant)
    responseHistory.push(finalAssistant)
    if (scenario.mergeAt === 'final-content') await mergeCommittedHistory()

    await receiveWs(streamEvent('turn_end', 'response-final', 6, {
      message_id: 212,
      content: '最终答复',
      finish_reason: 'stop',
      reasoning_content: '最终思考'
    }))

    const doneEvent = streamEvent('done', 'response-final', 6, {
      message_id: 212,
      response: {
        work_id: 'work7',
        message_id: 212,
        response_id: 'response-final',
        provider_metadata: {
          source: {
            channel_id: 1,
            model_id: 'deepseek-flash',
            protocol: 'deepseek_responses'
          }
        },
        choices: [{
          message: { role: 'assistant', content: '最终答复', reasoning_content: '最终思考' },
          finish_reason: 'stop'
        }],
        history: responseHistory
      }
    })
    await receiveWs(doneEvent)
    const rawAfterDone = JSON.stringify(Array.from(chat.messages.value))
    const displayAfterDone = JSON.stringify(summarizeDisplay(cachedPresentation.displayMessages.value))
    await receiveWs(doneEvent)
    await receiveWs(doneEvent)
    assert.equal(
      JSON.stringify(Array.from(chat.messages.value)),
      rawAfterDone,
      '重复完成事件必须保持原始消息状态幂等'
    )
    assert.equal(
      JSON.stringify(summarizeDisplay(cachedPresentation.displayMessages.value)),
      displayAfterDone,
      '重复完成事件必须保持展示状态幂等'
    )

    const rawMessages = Array.from(chat.messages.value, message => ({ ...message }))
    const displayedMessages = Array.from(cachedPresentation.displayMessages.value)
    const cachedSummary = summarizeDisplay(displayedMessages)
    const freshPresentation = useChatMessagePresentation({ props: presentationProps, t: key => key })
    const freshSummary = summarizeDisplay(freshPresentation.displayMessages.value)
    const displayKeys = displayedMessages.map(cachedPresentation.getDisplayKey)
    assert.equal(new Set(displayKeys).size, displayedMessages.length, '最终展示项 getDisplayKey 不得重复')
    const reasoningCollapseNames = displayedMessages
      .filter(message => message.reasoning_content !== undefined && message.reasoning_content !== null)
      .map(getReasoningCollapseName)
    assert.equal(
      new Set(reasoningCollapseNames).size,
      reasoningCollapseNames.length,
      '有思考内容的展示项 getReasoningCollapseName 不得重复'
    )
    if (scenario.paginate) {
      const displayedOldHistoryIds = oldHistoryIds(displayedMessages)
      assert.ok(
        initiallyLoadedOldHistoryIds.every(id => displayedOldHistoryIds.includes(id)),
        '最终展示不得丢失最初已加载的旧消息'
      )
      assert.ok(
        Math.min(...displayedOldHistoryIds.map(Number)) < oldestOldHistoryIdBeforePagination,
        '最终展示应保留已扩展到更早位置的旧历史'
      )
      assert.ok(
        paginatedOldHistoryIds.every(id => displayedOldHistoryIds.includes(id)),
        '最终输出不得丢失分页后已展示的旧历史'
      )
      const displayedToolPairs = cachedSummary
        .filter(message => message.type === 'tool_group')
        .flatMap(message => message.pairs)
      for (const [index, toolRound] of toolRounds.entries()) {
        const pair = displayedToolPairs.find(item => item.toolCall?.id === toolRound.callId)
        assert.ok(pair, `分页后不得丢失工具调用 ${toolRound.callId}`)
        const resultContent = normalizeContent(pair.resultMessage?.content)
        assert.equal(
          resultContent && typeof resultContent === 'object' ? resultContent.content : resultContent,
          `fixture result ${index + 1}`,
          `分页后不得丢失工具结果 ${toolRound.callId}`
        )
      }
    }
    const memoryToolPair = cachedSummary
      .filter(message => message.type === 'tool_group')
      .flatMap(message => message.pairs)
      .find(pair => pair.toolCall?.id === memoryCall.id)
    assert.ok(memoryToolPair, '恢复展示应保留记忆工具调用与结果配对')
    const displayedMemoryResult = normalizeContent(memoryToolPair.resultMessage?.content)
    const displayedMemoryPayload = normalizeContent(displayedMemoryResult?.content)
    assert.deepEqual(displayedMemoryPayload, memoryPayload, '恢复展示应完整保留记忆响应内容')
    assert.equal(Object.hasOwn(displayedMemoryPayload, 'items'), false, '记忆响应不应包含 items')
    assert.equal(displayedMemoryPayload.current_session_id, 'A', '记忆响应应保留当前会话标识')
    assert.ok(Array.isArray(displayedMemoryPayload.memories), '记忆响应应保留 memories 数组')
    assert.equal(displayedMemoryPayload.memories[0]?.content, 'fixture memory', '记忆响应应保留首条记忆内容')
    const finalAnswers = rawMessages.filter(message => (
      message.role === 'assistant' && message.content === '最终答复'
    ))
    assert.equal(finalAnswers.length, 1, '最终答复在原始消息中应只出现一次')
    assert.equal(rawMessages.some(message => (
      message.role === 'thinking'
      || message.role === 'reasoning'
      || message.type === 'thinking'
    )), false, '原始消息中不应残留临时思考消息')

    const displayedFinalAnswers = cachedSummary.filter(message => (
      message.role === 'assistant' && message.content === '最终答复'
    ))
    assert.equal(displayedFinalAnswers.length, 1, '展示列表中的最终答复应只出现一次')
    assert.equal(
      cachedSummary.at(-1)?.content,
      '最终答复',
      '最终答复必须是展示列表最后一项，不应在其后出现工具卡片或独立思考'
    )

    const displayedThoughts = cachedSummary
      .map(message => message.reasoning_content || (message.role === 'reasoning' ? message.content : ''))
      .filter(Boolean)
    assert.deepEqual(displayedThoughts, [
      '工具轮1思考',
      '工具轮2思考',
      '工具轮3思考',
      '工具轮4思考',
      '最终思考'
    ], '展示思考应按四个工具轮和最终回复各出现一次并保持顺序')
    assert.equal(JSON.stringify(cachedSummary), JSON.stringify(freshSummary), '增量展示缓存应与全量重算一致')
  })
}

// 旧工具和未结束思考事件的抑制由后端数据库测试负责；此处仅验证客户端处理取消终态。
