import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import * as assistantResponseIdentity from '../../src/utils/assistantResponseIdentity.js'
import { truncateErrorMessage } from '../../src/utils/errorMessage.js'
import { resolveChatActivityNotice } from '../../src/utils/chatPresentation.js'
import { MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME } from '../../src/constants/index.js'
import { test } from 'node:test'

import { createHarness, createSession } from '../chatDraftHarness.js'

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
  const utilsSource = readFileSync(new URL('../../src/utils/index.js', import.meta.url), 'utf8')
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
    new URL('../../src/composables/chat/useChatMessagePresentation.js', import.meta.url),
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

const summarizeRawDiagnostic = messages => Array.from(messages, message => ({
  role: message.role ?? null,
  type: message.type ?? null,
  response_id: message.response_id ?? null,
  db_id: message.db_id ?? null,
  thought: message.reasoning_content || (message.role === 'reasoning' ? message.content : '') || ''
}))

const summarizeDisplayDiagnostic = messages => Array.from(messages, message => ({
  role: message.role ?? null,
  type: message.type ?? null,
  response_id: message.response_id ?? null,
  db_id: message.db_id ?? null,
  thought: message.reasoning_content || ''
}))

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
  { name: '最终正文后turn_end前历史合并', mergeAt: 'final-content' }
]) {
  test(`TMP DeepSeek 展示顺序诊断：${scenario.name}`, async t => {
    const committedHistory = []
    const harness = await createHarness(t, {
      sessions: [
        createSession('A', { source: 'ws', show_tool_calls: true, show_reasoning: true }),
        createSession('B', { source: 'ws', show_tool_calls: true, show_reasoning: true })
      ],
      initialRoute: { path: '/', query: { session_id: 'A' } },
      sessionsHistory: () => committedHistory
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

    const memoryCall = {
      id: 'call-memory',
      name: MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
      arguments: { operation: 'recall', query: 'fixture' }
    }
    const memoryResult = 'fixture memory'
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
    await receiveWs(streamEvent('content', 'response-final', 6, { content: '最终答复' }))

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

    await receiveWs(streamEvent('done', 'response-final', 6, {
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
    }))

    const rawMessages = Array.from(chat.messages.value, message => ({ ...message }))
    const displayedMessages = Array.from(cachedPresentation.displayMessages.value)
    const cachedSummary = summarizeDisplay(displayedMessages)
    const freshPresentation = useChatMessagePresentation({ props: presentationProps, t: key => key })
    const freshSummary = summarizeDisplay(freshPresentation.displayMessages.value)
    t.diagnostic(JSON.stringify({
      scenario: scenario.name,
      raw: summarizeRawDiagnostic(rawMessages),
      display: summarizeDisplayDiagnostic(displayedMessages)
    }))

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
