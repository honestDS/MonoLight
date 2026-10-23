import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import * as assistantResponseIdentity from '../src/utils/assistantResponseIdentity.js'
import { truncateErrorMessage } from '../src/utils/errorMessage.js'
import { getReasoningCollapseName, resolveChatActivityNotice } from '../src/utils/chatPresentation.js'
import { MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME } from '../src/constants/index.js'
import { test } from 'node:test'

import { createHarness, createSession } from './chatDraftHarness.js'

const stripImports = sourceText => {
  const keptLines = []
  let inImport = false

  for (const line of sourceText.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const stripExports = sourceText => sourceText
  .replace(/export\s*\{[\s\S]*?\}\s*;?/g, '')
  .replace(/\bexport\s+default\s+/g, '')
  .replace(/\bexport\s+(?=(?:async\s+)?function\b|const\b|let\b|var\b|class\b)/g, '')

const loadChatMessagePresentation = () => {
  const context = vm.createContext({
    ...assistantResponseIdentity,
    computed,
    MarkdownIt,
    hljs,
    truncateErrorMessage,
    resolveChatActivityNotice,
    MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
    i18n: { global: { t: key => key } }
  })
  const utilsSource = readFileSync(new URL('../src/utils/index.js', import.meta.url), 'utf8')
  const utilsBody = stripExports(stripImports(utilsSource))
  vm.runInContext(`(() => {
    ${utilsBody}
    globalThis.__utils = {
      getToolCallContent,
      getToolCallName,
      getToolCalls,
      getToolResultName,
      isToolCall,
      isToolResult
    }
  })()`, context, { filename: 'src/utils/index.js' })
  Object.assign(context, context.__utils)

  const presentationSource = readFileSync(
    new URL('../src/composables/chat/useChatMessagePresentation.js', import.meta.url),
    'utf8'
  )
  const presentationBody = stripExports(stripImports(presentationSource))
  vm.runInContext(`(() => {
    ${presentationBody}
    globalThis.__presentation = { useChatMessagePresentation }
  })()`, context, { filename: 'src/composables/chat/useChatMessagePresentation.js' })
  return context.__presentation.useChatMessagePresentation
}

const useChatMessagePresentation = loadChatMessagePresentation()

const normalizeContent = content => {
  if (typeof content !== 'string') {
    return content === undefined ? null : JSON.parse(JSON.stringify(content))
  }
  try {
    return JSON.parse(content)
  } catch {
    return content
  }
}

const summarizeDisplay = messages => JSON.parse(JSON.stringify(Array.from(messages, message => ({
  id: message.id ?? null,
  type: message.type ?? null,
  role: message.role ?? null,
  response_id: message.response_id ?? null,
  content: normalizeContent(message.content),
  reasoning_content: message.reasoning_content ?? null,
  ...(message.type === 'tool_group' ? {
    pairs: Array.from(message.pairs || [], pair => ({
      id: pair.id ?? null,
      toolCall: pair.toolCall ? {
        id: pair.toolCall.id ?? pair.toolCall.function?.id ?? null,
        name: pair.toolCall.name ?? pair.toolCall.function?.name ?? null,
        arguments: normalizeContent(pair.toolCall.arguments ?? pair.toolCall.function?.arguments)
      } : null,
      resultMessage: pair.resultMessage ? {
        id: pair.resultMessage.id ?? null,
        db_id: pair.resultMessage.db_id ?? null,
        type: pair.resultMessage.type ?? null,
        role: pair.resultMessage.role ?? null,
        response_id: pair.resultMessage.response_id ?? null,
        content: normalizeContent(pair.resultMessage.content)
      } : null
    }))
  } : {})
}))))

const storedToolCall = (id, toolCall, reasoning) => ({
  id,
  created_at: id - 200,
  type: 'tool_call',
  role: 'assistant',
  content: JSON.stringify({ role: 'assistant', tool_calls: [toolCall] }),
  ...(reasoning ? { reasoning_content: reasoning } : {})
})

const storedToolResult = (id, toolCallId, result) => ({
  id,
  created_at: id - 200,
  type: 'tool_result',
  role: 'tool',
  content: JSON.stringify({ role: 'tool', tool_call_id: toolCallId, content: result })
})

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
      new URL('../../tests/memory_recall_fixture.json', import.meta.url),
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
test('恢复取消终态不污染客户端历史或启动思考', async t => {
  const visibleToolCall = {
    id: 'call-visible',
    name: 'fixture_read',
    arguments: { operation: 'read', path: 'fixture.txt' }
  }
  const fixtureHistory = [
    storedToolCall(695, visibleToolCall, ''),
    storedToolResult(696, visibleToolCall.id, 'fixture result'),
    { id: 697, created_at: 497, role: 'user', content: 'fixture user message' },
    {
      id: 718,
      created_at: 518,
      role: 'assistant',
      content: '后续最终答复',
      reasoning_content: '最新思考'
    }
  ]
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', {
        source: 'ws',
        show_reasoning: true,
        show_tool_calls: true,
        is_reply_running: false
      }),
      createSession('B', {
        source: 'ws',
        show_reasoning: true,
        show_tool_calls: true,
        is_reply_running: false
      })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } },
    sessionsHistory: ({ sessionId }) => sessionId === 'A' ? fixtureHistory : []
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
  const cachedPresentation = useChatMessagePresentation({ props: presentationProps, t: key => key })
  cachedPresentation.displayMessages.value

  const recoveryEvents = [
    { type: 'cancelled', session_id: 'A', work_id: 58, request_ids: ['r58'] },
    { type: 'cancelled', session_id: 'A', work_id: 60, request_ids: ['r60'] },
    { type: 'resume_complete', session_id: 'A' }
  ]
  const expectedHistoryIds = ['695', '696', '697', '718']
  const historyIdentity = messages => messages.map(message => String(message.db_id ?? message.id))
  const summarizeHistorySemantics = messages => Array.from(messages, message => {
    const parsedContent = normalizeContent(message.content)
    const contentObject = parsedContent && typeof parsedContent === 'object' && !Array.isArray(parsedContent)
      ? parsedContent
      : {}
    const rawToolCalls = message.tool_calls?.length ? message.tool_calls : contentObject.tool_calls || []
    const toolCalls = Array.from(rawToolCalls, call => ({
      id: call.id ?? call.function?.id ?? null,
      name: call.name ?? call.function?.name ?? null,
      arguments: normalizeContent(call.arguments ?? call.function?.arguments)
    }))
    const isToolResult = message.type === 'tool_result' || message.role === 'tool'
    const isToolCall = message.type === 'tool_call' || toolCalls.length > 0

    return {
      db_id: String(message.db_id ?? message.id),
      role: message.role ?? null,
      body: isToolCall || isToolResult ? null : message.content ?? null,
      reasoning_content: message.reasoning_content ?? null,
      tool_calls: toolCalls,
      tool_call_id: isToolResult ? message.tool_call_id ?? contentObject.tool_call_id ?? null : null,
      tool_result: isToolResult ? contentObject.content ?? parsedContent : null
    }
  })

  for (const [entryIndex] of ['首次进入', '再次进入'].entries()) {
    if (entryIndex === 1) {
      const sessionB = chat.sessions.value.find(session => session.session_id === 'B')
      const sessionA = chat.sessions.value.find(session => session.session_id === 'A')
      harness.module.handleSelectSession(sessionB)
      await harness.flush()
      harness.module.handleSelectSession(sessionA)
      await harness.flush()
    }

    const cleanHistory = JSON.parse(JSON.stringify(Array.from(chat.messages.value)))
    const cleanHistoryIds = historyIdentity(cleanHistory)
    assert.deepEqual(cleanHistoryIds, expectedHistoryIds, '进入时应保留原四项历史身份及顺序')
    assert.equal(new Set(cleanHistoryIds).size, expectedHistoryIds.length, '进入时历史身份不得重复')
    assert.equal(Number(cleanHistory.at(-1)?.db_id), 718, '最新历史项应为 718')
    assert.equal(cachedPresentation.activityNotice.value, null, '恢复尾流前不应显示活动提示')

    const resumeRequest = harness.wsManager.sent.filter(item => item.type === 'resume').at(-1)
    assert.ok(resumeRequest, '会话选择应通过恢复通道发送 resume')
    assert.equal(resumeRequest.history_message_id, 718, '恢复游标应取最近历史消息 718')
    assert.equal(harness.wsManager.sent.some(item => item.type === 'chat'), false, '恢复不发送 chat 请求')

    for (const event of recoveryEvents) {
      harness.receiveWs(event)
      await harness.flush()
      assert.equal(cachedPresentation.activityNotice.value, null, '接收取消终态和恢复完成事件时不应启动思考提示')
    }

    const rawMessages = Array.from(chat.messages.value)
    const displayedMessages = Array.from(cachedPresentation.displayMessages.value)
    const rawHistorySemantics = summarizeHistorySemantics(rawMessages)
    const rawHistoryIds = historyIdentity(rawMessages)
    assert.deepEqual(
      rawHistorySemantics,
      summarizeHistorySemantics(cleanHistory),
      '取消恢复事件不得改变原始历史的可见语义'
    )
    assert.deepEqual(rawHistoryIds, expectedHistoryIds, '恢复后四项历史身份及顺序应保持不变')
    assert.equal(new Set(rawHistoryIds).size, expectedHistoryIds.length, '恢复后历史身份不得重复')
    const finalAnswers = rawMessages.filter(message => (
      message.role === 'assistant'
      && message.content === '后续最终答复'
      && Number(message.db_id) === 718
    ))
    assert.equal(finalAnswers.length, 1, '历史答复 718 应只出现一次')
    const containsTransientAssistant = message => (
      message.role === 'reasoning'
      || message.role === 'thinking'
      || message.type === 'thinking'
      || message.type === 'reasoning'
      || (
        message.type !== 'tool_group'
        && message.type !== 'tool_call'
        && !message.tool_calls?.length
        && message.role === 'assistant'
        && !message.content
      )
    )
    assert.equal(rawMessages.some(containsTransientAssistant), false, '原始历史不得包含思考或空助手占位')
    assert.equal(displayedMessages.some(containsTransientAssistant), false, '展示列表不得包含思考或空助手占位')

    const displayedSummary = summarizeDisplay(displayedMessages)
    const displayedToolPairs = displayedSummary
      .filter(message => message.type === 'tool_group')
      .flatMap(message => message.pairs)
    assert.equal(displayedToolPairs.length, 1, '历史工具调用与结果应保留为一组配对')
    const [displayedToolPair] = displayedToolPairs
    assert.equal(displayedToolPair.toolCall?.id, visibleToolCall.id, '工具组应保留原调用身份')
    assert.equal(displayedToolPair.toolCall?.name, visibleToolCall.name, '工具组应保留原调用名称')
    assert.deepEqual(displayedToolPair.toolCall?.arguments, visibleToolCall.arguments, '工具组应保留原调用参数')
    assert.equal(
      String(displayedToolPair.resultMessage?.db_id ?? displayedToolPair.resultMessage?.id),
      '696',
      '工具组应保留对应结果身份'
    )
    const displayedResultContent = normalizeContent(displayedToolPair.resultMessage?.content)
    assert.equal(
      displayedResultContent && typeof displayedResultContent === 'object'
        ? displayedResultContent.content
        : displayedResultContent,
      'fixture result',
      '工具组应保留对应结果正文'
    )

    const finalDisplayIndex = displayedMessages.findIndex(message => (
      message.role === 'assistant' && message.content === '后续最终答复' && Number(message.id) === 718
    ))
    assert.notEqual(finalDisplayIndex, -1, '展示列表应包含历史答复 718')
    assert.equal(finalDisplayIndex, displayedMessages.length - 1, '最新答复应是展示列表尾项')

    const rawHistoryAfterRecovery = JSON.stringify(rawHistorySemantics)
    const displayAfterRecovery = JSON.stringify(displayedSummary)
    harness.receiveWs(recoveryEvents[1])
    await harness.flush()
    assert.equal(cachedPresentation.activityNotice.value, null, '重复取消终态不得启动思考提示')
    assert.equal(
      JSON.stringify(summarizeHistorySemantics(Array.from(chat.messages.value))),
      rawHistoryAfterRecovery,
      '重复取消终态必须保持规范化原始历史幂等'
    )
    assert.equal(
      JSON.stringify(summarizeDisplay(Array.from(cachedPresentation.displayMessages.value))),
      displayAfterRecovery,
      '重复取消终态必须保持展示列表幂等'
    )
    assert.equal(harness.wsManager.sent.some(item => item.type === 'chat'), false, '恢复和重复终态均不应发送 chat 请求')
  }
})
