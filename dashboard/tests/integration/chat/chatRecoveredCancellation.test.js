import assert from 'node:assert/strict'
import { test } from 'node:test'

import * as toolResultState from '../../../src/utils/toolResultState.js'
import { createHarness, createSession } from '../../chatDraftHarness.js'
import {
  normalizeContent,
  storedToolCall,
  storedToolResult,
  summarizeDisplay,
  useChatMessagePresentation
} from './recoveredPresentationHarness.js'

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

const recoveredVerificationPrompt = 'The execution of this tool was interrupted (possibly due to a new user message or system restart). The result is unknown. Please check if the action was completed and decide the next step.'

const recoveredToolCall = id => ({ id, name: 'fixture_tool', arguments: { value: id } })

const recoveredToolCallMessage = (id, responseId, workId, calls) => ({
  id, created_at: id, type: 'tool_call', role: 'assistant', response_id: responseId, work_id: workId,
  content: JSON.stringify({ role: 'assistant', tool_calls: calls })
})

const recoveredToolResult = (id, callId, result, {
  role = 'tool',
  toolExecutionStatus = null,
  toolCancelled = false
} = {}) => ({
  id, created_at: id, type: 'tool_result', role, tool_call_id: callId,
  content: JSON.stringify({
    role: 'tool',
    id,
    tool_call_id: callId,
    ...(toolExecutionStatus ? { tool_execution_status: toolExecutionStatus } : {}),
    ...(toolCancelled ? { tool_cancelled: true } : {}),
    content: JSON.stringify(result)
  })
})

const recoveredCancelledResult = (id, callId) => recoveredToolResult(
  id,
  callId,
  { status: 'cancelled', error: recoveredVerificationPrompt },
  { toolExecutionStatus: 'execution_unknown', toolCancelled: true }
)

const recoveredBackgroundResult = (id, call, taskId, originalResultId, role) => ({
  id, created_at: id, type: 'background_result', role,
  content: JSON.stringify({
    type: 'background_tool_result',
    task_id: taskId,
    original_tool_result_message_id: originalResultId,
    tool_call: call,
    task: { status: 'cancelled', error: recoveredVerificationPrompt },
    tool_cancelled: true,
    execution_status: 'execution_unknown',
    ui_only: true
  })
})

const recoveredHistoryRows = (id, responseId, workId, calls, results) => [
  recoveredToolCallMessage(id, responseId, workId, calls),
  ...results
]

const recoveredPresentation = chat => {
  const props = { get messages() { return chat.messages.value }, currentSessionShowReasoning: true, historyLoading: false, contextSummarizing: false }
  const presentation = useChatMessagePresentation({ props, t: key => key })
  presentation.displayMessages.value
  return presentation
}

const recoveredGroups = presentation => Array.from(presentation.displayMessages.value).filter(message => message.type === 'tool_group')
const recoveredResultMessages = chat => chat.messages.value.filter(message => toolResultState.getToolResultCallId(message) !== null)
const recoveredPairStatuses = (presentation, group) => Array.from(group.pairs, pair => presentation.getToolPairStatus(pair))
const recoveredToolResultPayload = message => JSON.parse(message.content)
const recoveredToolResultBody = message => JSON.parse(recoveredToolResultPayload(message).content)
const createRecoveredHarness = (t, sessionsHistory) => createHarness(t, {
  sessions: [createSession('A', { source: 'ws', show_tool_calls: true, show_reasoning: true })],
  initialRoute: { path: '/', query: { session_id: 'A' } }, sessionsHistory
})
const receiveRecovered = async (harness, event) => {
  harness.receiveWs({ session_id: 'A', ...event })
  await harness.flush()
}
const assertRecoveredGroup = (presentation, pairStatuses, groupStatus) => {
  const groups = recoveredGroups(presentation)
  assert.equal(groups.length, 1)
  const [group] = groups
  assert.deepEqual(recoveredPairStatuses(presentation, group), pairStatuses)
  assert.equal(presentation.getToolGroupStatus(group), groupStatus)
  return group
}

test('工具结果取消通知与历史恢复保持工具组状态', async t => {
  const status = {
    calling: 'chat.tool_status_calling', cancelled: 'chat.tool_status_cancelled',
    returned: 'chat.tool_status_returned', partial: 'chat.tool_status_partially_cancelled'
  }
  const workId = 'work-recovered-cancel'
  const responseId = 'response-recovered-cancel'
  const calls = ['call-cancelled', 'call-success', 'call-queued'].map(recoveredToolCall)
  const historyCall = recoveredToolCallMessage(901, responseId, workId, calls)
  const cancelledResult = recoveredCancelledResult(902, calls[0].id)
  const queuedResult = recoveredToolResult(903, calls[2].id, { status: 'queued', task_id: 17 })
  const successResult = recoveredToolResult(
    904,
    calls[1].id,
    { status: 'cancelled', content: 'fixture success' },
    { toolExecutionStatus: 'succeeded' }
  )
  const backgroundHistoryResult = recoveredBackgroundResult(905, calls[2], 17, queuedResult.id, 'user')
  const backgroundEventResult = recoveredBackgroundResult(905, calls[2], 17, queuedResult.id, 'assistant')
  let historyRows = []
  const harness = await createRecoveredHarness(t, () => historyRows)
  await harness.flush()
  const { chat } = harness
  const presentation = recoveredPresentation(chat)
  const streamEvent = (type, fields = {}) => ({ type, work_id: workId, response_id: responseId, ...fields })

  await receiveRecovered(harness, streamEvent('agent_loop_start'))
  for (const call of calls) await receiveRecovered(harness, streamEvent('tool_start', {
    tool_call_id: call.id, name: call.name, arguments: call.arguments
  }))
  assertRecoveredGroup(presentation, [status.calling, status.calling, status.calling], status.calling)

  historyRows = [historyCall, cancelledResult]
  await receiveRecovered(harness, { type: 'tool_results_update', work_id: workId, event_id: 'cancel-1', messages: [cancelledResult] })
  assertRecoveredGroup(presentation, [status.cancelled, status.calling, status.calling], status.calling)

  historyRows = [historyCall, cancelledResult, queuedResult]
  const queuedUpdate = { type: 'tool_results_update', work_id: workId, event_id: 'queued-1', messages: [queuedResult] }
  await receiveRecovered(harness, queuedUpdate)
  await receiveRecovered(harness, queuedUpdate)
  assertRecoveredGroup(presentation, [status.cancelled, status.calling, status.calling], status.calling)
  assert.equal(recoveredResultMessages(chat).length, 2, '重复通知不得产生重复结果')
  await chat.reloadCurrentSessionHistory()
  await harness.flush()
  assert.equal(recoveredResultMessages(chat).length, 2, '刷新历史不得产生重复结果')

  await receiveRecovered(harness, { type: 'cancelled', work_id: workId, request_ids: ['request-recovered-cancel'] })
  historyRows = [historyCall, cancelledResult, successResult, backgroundHistoryResult]
  const backgroundUpdate = {
    type: 'tool_results_update',
    work_id: workId,
    event_id: 'cancel-2',
    messages: [backgroundEventResult]
  }
  assert.equal(backgroundUpdate.messages.some(message => toolResultState.getToolResultCallId(message) === calls[1].id), false, '后台取消事件不得伪造成功回执')
  await receiveRecovered(harness, backgroundUpdate)
  await receiveRecovered(harness, backgroundUpdate)
  const group = assertRecoveredGroup(presentation, [status.cancelled, status.returned, status.cancelled], status.partial)
  assert.equal(presentation.getToolGroupStateClass(group), 'is-result')
  assert.equal(recoveredResultMessages(chat).length, 3, '通知和历史合并后每个调用只能有一条结果')
  const wsResults = recoveredResultMessages(chat)
  assert.equal(recoveredToolResultBody(wsResults.find(message => toolResultState.getToolResultCallId(message) === calls[1].id)).status, 'cancelled', '正常返回正文的 cancelled 不得显示取消')
  assert.equal(wsResults.find(message => toolResultState.getToolResultCallId(message) === calls[2].id).type, 'background_result', '后台标记不得被旧 queued 覆盖')

  await chat.reloadCurrentSessionHistory()
  await harness.flush()
  assertRecoveredGroup(presentation, [status.cancelled, status.returned, status.cancelled], status.partial)
  assert.equal(recoveredResultMessages(chat).find(message => toolResultState.getToolResultCallId(message) === calls[2].id).role, 'user', '刷新后应使用历史后台标记')

  const historyCalls = {
    all: ['history-cancelled-1', 'history-cancelled-2'].map(recoveredToolCall),
    partial: ['history-partial-cancelled', 'history-partial-success'].map(recoveredToolCall),
    unknown: [recoveredToolCall('history-unknown')]
  }
  const historyRowsOnly = [
    ...recoveredHistoryRows(1001, 'response-history-all', 'work-history-all', historyCalls.all, [
      recoveredCancelledResult(1002, historyCalls.all[0].id),
      recoveredCancelledResult(1003, historyCalls.all[1].id)
    ]),
    ...recoveredHistoryRows(1011, 'response-history-partial', 'work-history-partial', historyCalls.partial, [
      recoveredCancelledResult(1012, historyCalls.partial[0].id),
      recoveredToolResult(1013, historyCalls.partial[1].id, { status: 'cancelled', content: 'normal returned body' }, { toolExecutionStatus: 'succeeded' })
    ]),
    ...recoveredHistoryRows(1021, 'response-history-unknown', 'work-history-unknown', historyCalls.unknown, [
      recoveredToolResult(1022, historyCalls.unknown[0].id, { status: 'execution_unknown', error: recoveredVerificationPrompt }, { toolExecutionStatus: 'execution_unknown' })
    ])
  ]
  const historyHarness = await createRecoveredHarness(t, () => historyRowsOnly)
  await historyHarness.flush()
  const historyPresentation = recoveredPresentation(historyHarness.chat)
  const historyStatus = () => new Map(recoveredGroups(historyPresentation).map(item => [item.response_id, historyPresentation.getToolGroupStatus(item)]))
  const expectedHistoryStatus = new Map([
    ['response-history-all', status.cancelled], ['response-history-partial', status.partial], ['response-history-unknown', status.returned]
  ])
  assert.deepEqual(historyStatus(), expectedHistoryStatus)
  const unknownGroup = recoveredGroups(historyPresentation).find(item => item.response_id === 'response-history-unknown')
  assert.equal(historyPresentation.getToolPairStatus(unknownGroup.pairs[0]), status.returned, '未知但非 cancelled 结果不得显示取消')
  assert.equal(recoveredResultMessages(historyHarness.chat).length, 5)
  await historyHarness.chat.reloadCurrentSessionHistory()
  await historyHarness.flush()
  assert.deepEqual(historyStatus(), expectedHistoryStatus, '仅历史加载和刷新后状态应一致')
  assert.equal(recoveredResultMessages(historyHarness.chat).length, 5, '重复历史加载不得产生重复结果')

  historyHarness.chat.currentSessionShowToolCalls.value = false
  await historyHarness.chat.reloadCurrentSessionHistory()
  await historyHarness.flush()
  await receiveRecovered(historyHarness, { type: 'tool_results_update', work_id: 'work-history-all', messages: [historyRowsOnly[1]] })
  assert.equal(recoveredGroups(historyPresentation).length, 0, '关闭工具调用展示时不得显示工具组')
  assert.equal(recoveredResultMessages(historyHarness.chat).length, 0, '关闭工具调用展示时不得接收工具结果')
})

test('取消结果通知先到且工具调用随后恢复时保持原工具组', async t => {
  const status = {
    cancelled: 'chat.tool_status_cancelled', returned: 'chat.tool_status_returned', partial: 'chat.tool_status_partially_cancelled'
  }
  const workId = 'work-recovered-notification-first'
  const responseId = 'response-recovered-notification-first'
  const calls = [
    recoveredToolCall('notification-cancelled'),
    recoveredToolCall('notification-background'),
    recoveredToolCall('notification-unknown')
  ]
  const historyCall = recoveredToolCallMessage(1101, responseId, workId, calls)
  const cancelledResult = recoveredCancelledResult(1102, calls[0].id)
  const backgroundEventResult = recoveredBackgroundResult(1103, calls[1], 31, 1104, 'assistant')
  const backgroundHistoryResult = recoveredBackgroundResult(1103, calls[1], 31, 1104, 'user')
  const unknownResult = recoveredToolResult(
    1105,
    calls[2].id,
    { status: 'execution_unknown', error: recoveredVerificationPrompt },
    { toolExecutionStatus: 'execution_unknown' }
  )
  const notificationResults = [backgroundEventResult, unknownResult, cancelledResult]
  for (const result of notificationResults) {
    assert.equal(Object.hasOwn(result, 'response_id'), false, '通知结果不得虚构 response_id')
    assert.equal(Object.hasOwn(result, 'work_id'), false, '通知结果不得携带 work_id')
  }

  let historyRows = []
  const harness = await createRecoveredHarness(t, () => historyRows)
  await harness.flush()
  const { chat } = harness
  const presentation = recoveredPresentation(chat)
  const notificationUpdate = {
    type: 'tool_results_update',
    work_id: workId,
    event_id: 'notification-first-cancel',
    messages: notificationResults
  }

  await receiveRecovered(harness, notificationUpdate)
  assert.equal(historyRows.length, 0, '结果通知先到时历史应暂为空')
  const cachedBeforeToolStarts = Array.from(presentation.displayMessages.value)
  assert.ok(cachedBeforeToolStarts.length > 0, '结果通知后应先计算展示缓存')
  assert.deepEqual(
    recoveredResultMessages(chat).map(message => toolResultState.getToolResultCallId(message)),
    notificationResults.map(result => toolResultState.getToolResultCallId(result)),
    '通知先到时应保留三条原始结果'
  )

  for (const call of calls) {
    await receiveRecovered(harness, {
      type: 'tool_start',
      work_id: workId,
      response_id: responseId,
      tool_call_id: call.id,
      name: call.name,
      arguments: call.arguments
    })
  }

  const assertNotificationFirstState = () => {
    const group = assertRecoveredGroup(
      presentation,
      [status.cancelled, status.cancelled, status.returned],
      status.partial
    )
    assert.equal(presentation.getToolGroupStateClass(group), 'is-result')
    assert.equal(group.response_id, responseId)
    assert.equal(group.pairs.length, calls.length, '原工具组应只包含对应的三条调用')
    for (const call of calls) {
      const pair = group.pairs.find(item => item.toolCall?.id === call.id)
      assert.ok(pair, `工具组应包含调用 ${call.id}`)
      assert.equal(pair.toolCall.name, call.name)
      assert.deepEqual(normalizeContent(pair.toolCall.arguments), call.arguments, `调用 ${call.id} 应保留精确参数`)
    }
    return group
  }

  assertNotificationFirstState()
  const resultMessagesBeforeDuplicate = recoveredResultMessages(chat)
  for (const result of resultMessagesBeforeDuplicate) {
    assert.equal(result.response_id, undefined, '结果消息不得被补写 response_id')
    assert.equal(result.work_id, undefined, '事件 work_id 不得下沉到结果消息')
  }
  assert.equal(recoveredToolResultBody(resultMessagesBeforeDuplicate.find(message => (
    toolResultState.getToolResultCallId(message) === calls[0].id
  ))).status, 'cancelled')
  assert.equal(
    resultMessagesBeforeDuplicate.find(message => toolResultState.getToolResultCallId(message) === calls[1].id).type,
    'background_result',
    '后台取消结果应保留原后台结果类型'
  )
  assert.equal(
    recoveredToolResultBody(resultMessagesBeforeDuplicate.find(message => toolResultState.getToolResultCallId(message) === calls[2].id)).status,
    'execution_unknown',
    '中性 execution_unknown 结果正文应保留'
  )

  await receiveRecovered(harness, notificationUpdate)
  assertNotificationFirstState()
  assert.equal(recoveredResultMessages(chat).length, 3, '重复通知不得产生新的结果或工具组')

  historyRows = [historyCall, cancelledResult, backgroundHistoryResult, unknownResult]
  await chat.reloadCurrentSessionHistory()
  await harness.flush()
  assertNotificationFirstState()
  assert.equal(recoveredResultMessages(chat).length, 3, '历史恢复不得增加结果')

  await chat.reloadCurrentSessionHistory()
  await harness.flush()
  assertNotificationFirstState()
  assert.equal(recoveredGroups(presentation).length, 1, '重复历史恢复不得产生新工具组')
})

test('HTTP 工作取消查询合并明确取消标记并在历史刷新后保持展示', async t => {
  const status = {
    calling: 'chat.tool_status_calling', cancelled: 'chat.tool_status_cancelled',
    returned: 'chat.tool_status_returned', partial: 'chat.tool_status_partially_cancelled'
  }
  const workId = 314
  const responseId = 'response-http-recovered-cancel'
  const requestId = 'request-http-recovered-cancel'
  const calls = ['http-cancelled', 'http-success', 'http-background'].map(recoveredToolCall)
  const historyCall = recoveredToolCallMessage(2001, responseId, workId, calls)
  const cancelledResult = recoveredCancelledResult(2002, calls[0].id)
  const successResult = recoveredToolResult(
    2003,
    calls[1].id,
    { status: 'cancelled', content: 'fixture HTTP success' },
    { toolExecutionStatus: 'succeeded' }
  )
  const queuedResult = recoveredToolResult(2004, calls[2].id, { status: 'queued', task_id: 71 })
  const backgroundHistoryResult = recoveredBackgroundResult(2005, calls[2], 71, queuedResult.id, 'user')
  const backgroundEventResult = recoveredBackgroundResult(2005, calls[2], 71, queuedResult.id, 'assistant')
  let historyRows = [historyCall, successResult, queuedResult]
  const statusCalls = []
  const harness = await createHarness(t, {
    sessions: [createSession('A', {
      source: 'http',
      show_tool_calls: true,
      show_reasoning: true,
      is_reply_running: true,
      reply_works: [{ work_id: workId, status: 'waiting_external_work', request_ids: [requestId] }]
    })],
    initialRoute: { path: '/', query: { session_id: 'A' } },
    sessionsHistory: () => historyRows,
    replyWorkStatus: ({ workId: requestedWorkId }) => {
      statusCalls.push(requestedWorkId)
      return {
        work_id: workId,
        resolved_work_id: workId,
        session_id: 'A',
        status: 'cancelled',
        request_ids: [requestId],
        result_message_id: backgroundEventResult.id,
        response: null,
        session_events: [{
          type: 'tool_results_update',
          session_id: 'A',
          work_id: workId,
          messages: [cancelledResult, backgroundEventResult]
        }]
      }
    }
  })
  await harness.flush()
  const { chat } = harness
  const presentation = recoveredPresentation(chat)
  assert.equal(chat.loading.value, true, '活动工作状态应先保持加载中')
  assert.deepEqual(statusCalls, [], '活动工作不应提前查询终态')
  assertRecoveredGroup(presentation, [status.calling, status.returned, status.calling], status.calling)

  Object.assign(harness.sessions[0], {
    is_reply_running: false,
    is_loading: false,
    reply_works: [{ work_id: workId, status: 'cancelled', request_ids: [requestId] }]
  })
  await chat.loadSessions()
  await harness.flush()

  assert.deepEqual(statusCalls, [String(workId)], '终态刷新应通过真实工作状态 API 查询')
  assert.equal(chat.loading.value, false)
  const group = assertRecoveredGroup(presentation, [status.cancelled, status.returned, status.cancelled], status.partial)
  assert.equal(presentation.getToolGroupStateClass(group), 'is-result')
  assert.equal(recoveredResultMessages(chat).length, 3)
  const httpResults = recoveredResultMessages(chat)
  const httpSuccess = httpResults.find(message => toolResultState.getToolResultCallId(message) === calls[1].id)
  const httpBackground = httpResults.find(message => toolResultState.getToolResultCallId(message) === calls[2].id)
  assert.equal(recoveredToolResultPayload(httpSuccess).tool_execution_status, 'succeeded', '历史成功回执应保留')
  assert.equal(recoveredToolResultBody(httpSuccess).status, 'cancelled', '正常返回正文的 cancelled 仍应视为已返回')
  assert.equal(httpBackground.type, 'background_result', '事件后台标记不得被历史 queued 覆盖')
  assert.equal(recoveredToolResultPayload(httpBackground).task.status, 'cancelled')

  historyRows = [historyCall, cancelledResult, successResult, backgroundHistoryResult]
  await chat.reloadCurrentSessionHistory()
  await harness.flush()
  assertRecoveredGroup(presentation, [status.cancelled, status.returned, status.cancelled], status.partial)
  assert.equal(recoveredResultMessages(chat).length, 3)
  const refreshedBackground = recoveredResultMessages(chat).find(message => toolResultState.getToolResultCallId(message) === calls[2].id)
  assert.equal(refreshedBackground.role, 'user', '刷新后应使用持久化历史后台标记')
  assert.equal(statusCalls.length, 1, '刷新历史不得重复查询已解决工作')

  await chat.loadSessions()
  await harness.flush()
  assertRecoveredGroup(presentation, [status.cancelled, status.returned, status.cancelled], status.partial)
  assert.equal(statusCalls.length, 1, '重复会话刷新不得重复查询已解决工作')
})
