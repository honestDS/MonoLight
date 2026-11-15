import assert from 'node:assert/strict'
import test from 'node:test'

import {
  getBackgroundToolResultPayload,
  getOriginalToolResultMessageId,
  getToolResultCallId,
  isToolResultCancelled,
  isToolResultPending,
  mergeToolResultIntoList,
  mergeToolResultMessage,
  projectToolResultMessage
} from '../src/utils/toolResultState.js'

const makeToolResult = ({
  callId = 'call-1',
  result = 'result',
  serialized = false,
  id,
  responseId,
  workId,
  dbId,
  toolCancelled,
  toolExecutionStatus
} = {}) => {
  const toolPayload = {
    role: 'tool',
    tool_call_id: callId,
    content: result
  }
  if (toolCancelled !== undefined) toolPayload.tool_cancelled = toolCancelled
  if (toolExecutionStatus !== undefined) toolPayload.tool_execution_status = toolExecutionStatus

  const serializedResult = JSON.stringify(toolPayload)
  const message = serialized
    ? { role: 'assistant', content: serializedResult }
    : {
        role: 'tool',
        type: 'tool_result',
        tool_call_id: callId,
        content: result,
        ...(toolCancelled === undefined ? {} : { tool_cancelled: toolCancelled }),
        ...(toolExecutionStatus === undefined ? {} : { tool_execution_status: toolExecutionStatus })
      }

  return {
    ...message,
    ...(id === undefined ? {} : { id }),
    ...(responseId === undefined ? {} : { response_id: responseId }),
    ...(workId === undefined ? {} : { work_id: workId }),
    ...(dbId === undefined ? {} : { db_id: dbId })
  }
}

const makeQueued = ({
  callId = 'call-1',
  taskId = 7,
  id,
  responseId,
  workId,
  dbId
} = {}) => makeToolResult({
  callId,
  result: JSON.stringify({ status: 'queued', task_id: taskId }),
  id,
  responseId,
  workId,
  dbId
})

const makeMarker = ({
  callId = 'call-1',
  task = 'completed',
  dbId,
  originalId,
  toolCancelled,
  executionStatus
} = {}) => {
  const payload = {
    type: 'background_tool_result',
    tool_call: {
      id: callId,
      name: 'search',
      arguments: '{"query":"MonoLight"}'
    },
    task
  }
  if (toolCancelled !== undefined) payload.tool_cancelled = toolCancelled
  if (executionStatus !== undefined) payload.execution_status = executionStatus
  if (originalId !== undefined) payload.original_tool_result_message_id = originalId

  return {
    type: 'background_result',
    role: 'assistant',
    ...(dbId === undefined ? {} : { db_id: dbId }),
    content: JSON.stringify(payload)
  }
}

const parseProjectedContent = message => JSON.parse(message.content)

test('recognizes top-level and serialized tool results and markers by one call id', () => {
  const topLevel = makeToolResult({ callId: 'call-shared' })
  const serialized = makeToolResult({ callId: 'call-shared', serialized: true })
  const marker = makeMarker({ callId: 'call-shared' })

  assert.equal(getToolResultCallId(topLevel), 'call-shared')
  assert.equal(getToolResultCallId(serialized), 'call-shared')
  assert.equal(getToolResultCallId(marker), 'call-shared')
  assert.ok(getBackgroundToolResultPayload(marker))
})

test('keeps one result and first UI metadata for queued/stream/marker races', () => {
  const queued = makeQueued({
    callId: 'call-race',
    id: 'ui-first',
    responseId: 'response-first',
    workId: 'work-first'
  })
  const streamFinal = makeToolResult({
    callId: 'call-race',
    result: 'stream final',
    id: 'stream-id',
    responseId: 'stream-response',
    workId: 'stream-work'
  })
  const marker = makeMarker({ callId: 'call-race', task: 'marker final', dbId: 900 })

  for (const events of [
    [queued, streamFinal, marker],
    [marker, queued, streamFinal]
  ]) {
    const messages = events.reduce(mergeToolResultIntoList, [])
    assert.equal(messages.length, 1)
    assert.equal(messages[0].db_id, 900)
    assert.equal(messages[0].id, 'ui-first')
    assert.equal(messages[0].response_id, 'response-first')
    assert.equal(messages[0].work_id, 'work-first')
    assert.equal(getToolResultCallId(messages[0]), 'call-race')
  }
})

test('associates persisted queued results with a marker original message id in either order', () => {
  const queued = makeQueued({ callId: 'call-persisted', dbId: 410 })
  const marker = makeMarker({
    callId: 'call-persisted',
    dbId: 411,
    originalId: 410,
    task: 'persisted final'
  })

  assert.equal(getOriginalToolResultMessageId(marker), '410')
  for (const events of [[queued, marker], [marker, queued]]) {
    const messages = events.reduce(mergeToolResultIntoList, [])
    assert.equal(messages.length, 1)
    assert.equal(messages[0].db_id, 411)
    assert.equal(getOriginalToolResultMessageId(messages[0]), '410')
  }
})

test('is idempotent for repeated marker, stream, and history messages', () => {
  const stream = makeToolResult({
    callId: 'call-repeat',
    result: 'stream result',
    id: 'ui-repeat',
    responseId: 'response-repeat',
    workId: 'work-repeat'
  })
  const marker = makeMarker({ callId: 'call-repeat', dbId: 700, task: 'history result' })
  const history = makeToolResult({
    callId: 'call-repeat',
    result: 'history result',
    serialized: true,
    dbId: 700
  })
  const events = [stream, stream, marker, marker, history, history]
  const once = events.reduce(mergeToolResultIntoList, [])
  const twice = events.reduce(mergeToolResultIntoList, once)

  assert.equal(once.length, 1)
  assert.deepEqual(twice, once)
  assert.equal(once[0].db_id, 700)
  assert.equal(parseProjectedContent(projectToolResultMessage(once[0])).content, 'history result')
})

test('prefers newer high-db-id markers without mixing calls', () => {
  let messages = mergeToolResultIntoList([], makeMarker({ callId: 'call-a', dbId: 20, task: 'current' }))
  messages = mergeToolResultIntoList(messages, makeMarker({ callId: 'call-a', dbId: 10, task: 'old' }))
  assert.equal(messages.length, 1)
  assert.equal(messages[0].db_id, 20)
  assert.equal(parseProjectedContent(projectToolResultMessage(messages[0])).content, 'current')

  messages = mergeToolResultIntoList(messages, makeMarker({ callId: 'call-a', dbId: 30, task: 'newest' }))
  assert.equal(messages.length, 1)
  assert.equal(messages[0].db_id, 30)
  assert.equal(parseProjectedContent(projectToolResultMessage(messages[0])).content, 'newest')

  messages = mergeToolResultIntoList(messages, makeMarker({ callId: 'call-b', dbId: 31, task: 'newest' }))
  assert.equal(messages.length, 2)
  assert.deepEqual(
    new Set(messages.map(getToolResultCallId)),
    new Set(['call-a', 'call-b'])
  )
})

test('accepts empty and null marker tasks but rejects damaged or unrelated payloads', () => {
  for (const task of ['', null]) {
    const marker = makeMarker({ callId: 'call-empty', task })
    assert.ok(getBackgroundToolResultPayload(marker))
    assert.equal(getToolResultCallId(marker), 'call-empty')
    assert.equal(parseProjectedContent(projectToolResultMessage(marker)).content, task === null ? 'null' : '')
  }

  const damaged = [
    undefined,
    null,
    { type: 'background_result', content: undefined },
    { type: 'background_result', content: '{bad json' },
    {
      type: 'background_result',
      content: JSON.stringify({ type: 'wrong', tool_call: { id: 'call-x' }, task: 'x' })
    },
    {
      type: 'background_result',
      content: JSON.stringify({ type: 'background_tool_result', task: 'x' })
    },
    {
      type: 'background_result',
      content: JSON.stringify({
        type: 'background_tool_result',
        tool_call: { id: '   ' },
        task: 'x'
      })
    },
    {
      type: 'background_result',
      content: {
        type: 'background_tool_result',
        tool_call: { id: 'call-x' },
        task: undefined
      }
    }
  ]
  for (const message of damaged) {
    assert.equal(getBackgroundToolResultPayload(message), null)
    assert.equal(getOriginalToolResultMessageId(message), null)
  }

  assert.equal(
    getToolResultCallId({
      role: 'user',
      content: JSON.stringify({ type: 'tool_result', tool_call_id: 'fake-call', content: 'fake' })
    }),
    null
  )
  assert.equal(
    getToolResultCallId({
      type: 'tool_call',
      role: 'assistant',
      content: JSON.stringify({ role: 'tool', tool_call_id: 'fake-call', content: 'fake' })
    }),
    null
  )
})

test('projects marker body and tool metadata, while pending requires a valid task id', () => {
  const marker = makeMarker({ callId: 'call-project', task: 'actual body' })
  const projected = projectToolResultMessage(marker)
  assert.equal(projected.role, 'tool')
  assert.equal(projected.type, 'tool_result')
  assert.deepEqual(parseProjectedContent(projected), {
    role: 'tool',
    tool_call_id: 'call-project',
    content: 'actual body'
  })
  assert.deepEqual(projected.tool_call, {
    id: 'call-project',
    name: 'search',
    arguments: '{"query":"MonoLight"}'
  })

  for (const taskId of [1, Number.MAX_SAFE_INTEGER]) {
    assert.equal(isToolResultPending(makeQueued({ taskId })), true)
  }
  for (const taskId of [0, -1, 1.5, '1', Number.MAX_SAFE_INTEGER + 1, null, NaN]) {
    assert.equal(isToolResultPending(makeQueued({ taskId })), false)
  }
  assert.equal(
    isToolResultPending({
      type: 'tool_result',
      role: 'tool',
      tool_call_id: 'call-project',
      content: JSON.stringify({ status: 'queued', task_id: undefined })
    }),
    false
  )
  assert.equal(
    isToolResultPending(makeMarker({
      callId: 'call-project',
      task: { status: 'queued', task_id: 1 }
    })),
    false
  )
})

test('recognizes only cancelled tool result statuses across supported message shapes', () => {
  const cancelledResult = JSON.stringify({
    type: 'tool_cancellation',
    status: 'cancelled',
    error: 'cancelled'
  })
  const cancelledMessages = [
    makeToolResult({
      callId: 'call-cancelled-top-level',
      result: cancelledResult,
      toolCancelled: true,
      toolExecutionStatus: 'execution_unknown'
    }),
    makeToolResult({
      callId: 'call-cancelled-serialized',
      result: cancelledResult,
      serialized: true,
      toolCancelled: true,
      toolExecutionStatus: 'execution_unknown'
    }),
    makeMarker({
      callId: 'call-cancelled-background',
      task: { status: 'cancelled', error: 'cancelled' },
      toolCancelled: true,
      executionStatus: 'execution_unknown'
    })
  ]

  for (const message of cancelledMessages) assert.equal(isToolResultCancelled(message), true)
  assert.equal(isToolResultCancelled(projectToolResultMessage(cancelledMessages[2])), true)

  const successfulToolBody = makeToolResult({
    callId: 'call-successful-body',
    result: JSON.stringify({ status: 'succeeded', tool_cancelled: true }),
    serialized: true,
    toolExecutionStatus: 'succeeded'
  })
  const notCancelledMessages = [
    makeToolResult({
      callId: 'call-body-cancelled',
      result: cancelledResult,
      toolExecutionStatus: 'execution_unknown'
    }),
    successfulToolBody,
    makeMarker({
      callId: 'call-background-task-body',
      task: { status: 'cancelled', error: 'cancelled', tool_cancelled: true },
      executionStatus: 'execution_unknown'
    }),
    makeToolResult({ callId: 'call-string-true', result: cancelledResult, toolCancelled: 'true' }),
    makeToolResult({ callId: 'call-false', result: cancelledResult, toolCancelled: false }),
    makeMarker({
      callId: 'call-background-string-true',
      task: { status: 'cancelled', error: 'cancelled' },
      toolCancelled: 'true'
    }),
    makeMarker({
      callId: 'call-background-false',
      task: { status: 'cancelled', error: 'cancelled' },
      toolCancelled: false
    }),
    makeToolResult({
      callId: 'call-execution-unknown',
      result: JSON.stringify({ status: 'execution_unknown' }),
      toolExecutionStatus: 'execution_unknown'
    })
  ]
  for (const message of notCancelledMessages) assert.equal(isToolResultCancelled(message), false)

  assert.equal(
    isToolResultCancelled({
      role: 'user',
      content: JSON.stringify({ status: 'cancelled' })
    }),
    false
  )
  assert.equal(isToolResultCancelled(makeToolResult({ result: '{bad json' })), false)
  assert.equal(isToolResultCancelled(makeMarker({ task: '{bad json' })), false)
})

test('keeps repeated cancelled updates idempotent and protects them from stale queued results', () => {
  const cancelledResult = JSON.stringify({
    type: 'tool_cancellation',
    status: 'cancelled',
    error: 'cancelled'
  })
  const toolResultsUpdate = makeToolResult({
    callId: 'call-cancelled',
    result: cancelledResult,
    id: 'ui-cancelled',
    responseId: 'response-cancelled',
    workId: 'work-cancelled',
    toolCancelled: true,
    toolExecutionStatus: 'execution_unknown'
  })
  const history = makeToolResult({
    callId: 'call-cancelled',
    result: cancelledResult,
    serialized: true,
    dbId: 700,
    toolCancelled: true,
    toolExecutionStatus: 'execution_unknown'
  })
  const other = makeToolResult({
    callId: 'call-other',
    result: JSON.stringify({ status: 'succeeded' })
  })
  const source = [toolResultsUpdate, history, other]
  const before = structuredClone(source)
  const merged = [toolResultsUpdate, toolResultsUpdate, history, history, other, other]
    .reduce(mergeToolResultIntoList, [])
  const cancelled = merged.find(message => getToolResultCallId(message) === 'call-cancelled')
  const otherResult = merged.find(message => getToolResultCallId(message) === 'call-other')

  assert.equal(merged.length, 2)
  assert.equal(isToolResultCancelled(cancelled), true)
  assert.equal(isToolResultCancelled(otherResult), false)
  assert.equal(JSON.parse(otherResult.content).status, 'succeeded')
  assert.deepEqual(source, before)

  const queued = makeQueued({
    callId: 'call-order',
    id: 'ui-order',
    responseId: 'response-order',
    workId: 'work-order',
    dbId: 11
  })
  const cancelledUpdate = makeToolResult({
    callId: 'call-order',
    result: cancelledResult,
    id: 'ui-order',
    responseId: 'response-order',
    workId: 'work-order',
    dbId: 12,
    toolCancelled: true,
    toolExecutionStatus: 'execution_unknown'
  })
  const orderInputs = [queued, cancelledUpdate]
  const orderBefore = structuredClone(orderInputs)
  const cancelledThenQueued = [cancelledUpdate, queued].reduce(mergeToolResultIntoList, [])
  const queuedThenCancelled = [queued, cancelledUpdate].reduce(mergeToolResultIntoList, [])

  assert.deepEqual(cancelledThenQueued, queuedThenCancelled)
  assert.equal(cancelledThenQueued.length, 1)
  assert.equal(isToolResultCancelled(cancelledThenQueued[0]), true)
  assert.deepEqual(orderInputs, orderBefore)
})

test('does not mutate input messages and keeps same output from different calls separate', () => {
  const queued = makeQueued({
    callId: 'call-a',
    id: 'ui-a',
    responseId: 'response-a',
    workId: 'work-a'
  })
  const marker = makeMarker({ callId: 'call-a', dbId: 800, task: 'same output' })
  const messages = [queued]
  const before = structuredClone({ messages, queued, marker })

  const merged = mergeToolResultIntoList(messages, marker)
  assert.notStrictEqual(merged, messages)
  assert.deepEqual({ messages, queued, marker }, before)

  const local = makeToolResult({ callId: 'call-b', result: 'local' })
  const remote = makeMarker({ callId: 'call-b', dbId: 801, task: 'remote' })
  const localBefore = structuredClone(local)
  const remoteBefore = structuredClone(remote)
  mergeToolResultMessage(local, remote)
  projectToolResultMessage(remote)
  assert.deepEqual(local, localBefore)
  assert.deepEqual(remote, remoteBefore)

  const separate = mergeToolResultIntoList(
    mergeToolResultIntoList([], makeToolResult({ callId: 'call-x', result: 'same output' })),
    makeToolResult({ callId: 'call-y', result: 'same output' })
  )
  assert.equal(separate.length, 2)
  assert.deepEqual(new Set(separate.map(getToolResultCallId)), new Set(['call-x', 'call-y']))
})
