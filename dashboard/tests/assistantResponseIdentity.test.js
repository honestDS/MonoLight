import assert from 'node:assert/strict'
import test from 'node:test'

import {
  findAssistantResponseReplacementIndex,
  isAssistantResponse,
  isPlainAssistantResponse,
  mergeAssistantResponseIntoList
} from '../src/utils/assistantResponseIdentity.js'

const liveResponse = (overrides = {}) => ({
  id: 'assistant-live',
  role: 'assistant',
  content: 'final body',
  request_id: 'request-1',
  response_id: 'llm-response-1',
  work_id: 7,
  ...overrides
})

test('done response merges into the live message by work identity and adds db identity', () => {
  const doneResponse = liveResponse({
    id: 'assistant-done',
    db_id: 9,
    response_id: 'session-reply-work:7'
  })

  const merged = mergeAssistantResponseIntoList([liveResponse()], doneResponse)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].id, 'assistant-live')
  assert.equal(merged[0].db_id, '9')
  assert.equal(merged[0].content, 'final body')
})


test('terminal or history merge preserves archived reasoning when the incoming copy has no reasoning', () => {
  const local = liveResponse({ reasoning_content: 'persist this reasoning' })
  const incoming = liveResponse({
    id: 'assistant-history',
    db_id: 9,
    reasoning_content: null
  })

  const merged = mergeAssistantResponseIntoList([local], incoming)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].reasoning_content, 'persist this reasoning')
})

test('repeated done responses remain idempotent', () => {
  const doneResponse = liveResponse({
    id: 'assistant-done',
    message_id: 9,
    response_id: 'session-reply-work:7'
  })

  const firstMerge = mergeAssistantResponseIntoList([liveResponse()], doneResponse)
  const secondMerge = mergeAssistantResponseIntoList(firstMerge, doneResponse)

  assert.equal(secondMerge.length, 1)
  assert.equal(secondMerge[0].db_id, '9')
})

test('legacy work identity merges the terminal response into the last matching assistant message', () => {
  const firstResponse = liveResponse({
    id: 'assistant-first',
    content: 'first body',
    response_id: 'response-turn-1'
  })
  const lastResponse = liveResponse({
    id: 'assistant-last',
    content: 'last body',
    response_id: 'response-turn-2'
  })
  const doneResponse = liveResponse({
    id: 'assistant-done',
    db_id: 9,
    response_id: 'session-reply-work:7',
    content: 'final body'
  })

  const merged = mergeAssistantResponseIntoList([firstResponse, lastResponse], doneResponse)

  assert.equal(merged.length, 2)
  assert.equal(merged[0].content, 'first body')
  assert.equal(merged[0].response_id, 'response-turn-1')
  assert.equal(merged[1].content, 'final body')
  assert.equal(merged[1].response_id, 'response-turn-2')
  assert.equal(merged[1].db_id, '9')
})

test('done response with the final response identity only merges the last of three work turns', () => {
  const firstResponse = liveResponse({
    id: 'assistant-first',
    content: 'first body',
    response_id: 'response-turn-1'
  })
  const secondResponse = liveResponse({
    id: 'assistant-second',
    content: 'second body',
    response_id: 'response-turn-2'
  })
  const lastResponse = liveResponse({
    id: 'assistant-last',
    content: 'last body',
    response_id: 'response-turn-3'
  })
  const doneResponse = liveResponse({
    id: 'assistant-done',
    db_id: 9,
    content: 'final body',
    response_id: 'response-turn-3'
  })

  const merged = mergeAssistantResponseIntoList(
    [firstResponse, secondResponse, lastResponse],
    doneResponse
  )

  assert.equal(merged.length, 3)
  assert.equal(merged[0].content, 'first body')
  assert.equal(merged[1].content, 'second body')
  assert.equal(merged[2].content, 'final body')
  assert.equal(merged[2].response_id, 'response-turn-3')
  assert.equal(merged[2].db_id, '9')
})

test('replayed legacy work terminal responses only update the last turn idempotently', () => {
  const firstResponse = liveResponse({
    id: 'assistant-first',
    content: 'first body',
    response_id: 'response-turn-1'
  })
  const secondResponse = liveResponse({
    id: 'assistant-second',
    content: 'second body',
    response_id: 'response-turn-2'
  })
  const lastResponse = liveResponse({
    id: 'assistant-last',
    content: 'last body',
    response_id: 'response-turn-3'
  })
  const legacyDoneResponse = liveResponse({
    id: 'assistant-done',
    db_id: 9,
    content: 'final body',
    response_id: 'session-reply-work:7'
  })
  const replayedLegacyDoneResponse = {
    ...legacyDoneResponse,
    id: 'assistant-done-replayed'
  }

  const firstMerge = mergeAssistantResponseIntoList(
    [firstResponse, secondResponse, lastResponse],
    legacyDoneResponse
  )
  const replayedMerge = mergeAssistantResponseIntoList(firstMerge, replayedLegacyDoneResponse)

  assert.equal(replayedMerge.length, 3)
  assert.equal(replayedMerge[0].content, 'first body')
  assert.equal(replayedMerge[1].content, 'second body')
  assert.equal(replayedMerge[2].content, 'final body')
  assert.equal(replayedMerge[2].response_id, 'response-turn-3')
  assert.equal(replayedMerge[2].db_id, '9')
})

test('done and turn_end responses converge by response and database identity in either arrival order', () => {
  const turnEndResponse = liveResponse({
    id: 'assistant-turn-end',
    db_id: 9,
    content: 'turn end body',
    response_id: 'response-turn-3'
  })
  const doneResponse = liveResponse({
    id: 'assistant-done',
    db_id: 9,
    content: 'done body',
    response_id: 'response-turn-3'
  })

  const doneThenTurnEnd = mergeAssistantResponseIntoList(
    mergeAssistantResponseIntoList([], doneResponse),
    turnEndResponse
  )
  const turnEndThenDone = mergeAssistantResponseIntoList(
    mergeAssistantResponseIntoList([], turnEndResponse),
    doneResponse
  )

  for (const merged of [doneThenTurnEnd, turnEndThenDone]) {
    assert.equal(merged.length, 1)
    assert.equal(merged[0].response_id, 'response-turn-3')
    assert.equal(merged[0].db_id, '9')
  }
})

test('responses with the same request identity but different work identities remain isolated', () => {
  const firstResponse = {
    id: 'assistant-work-7',
    role: 'assistant',
    content: 'work 7 body',
    request_id: 'request-shared',
    work_id: 7
  }
  const secondResponse = {
    id: 'assistant-work-8',
    role: 'assistant',
    content: 'work 8 body',
    request_id: 'request-shared',
    work_id: 8
  }

  assert.equal(findAssistantResponseReplacementIndex([firstResponse], secondResponse), -1)
  assert.equal(mergeAssistantResponseIntoList([firstResponse], secondResponse).length, 2)
})

test('legacy same-work responses without response or database identity only merge into the last turn', () => {
  const firstResponse = {
    id: 'assistant-first',
    role: 'assistant',
    content: 'first body',
    request_id: 'request-1',
    work_id: 7
  }
  const lastResponse = {
    id: 'assistant-last',
    role: 'assistant',
    content: 'last body',
    request_id: 'request-1',
    work_id: 7
  }
  const legacyResponse = {
    id: 'assistant-legacy',
    role: 'assistant',
    content: 'legacy final body',
    request_id: 'request-1',
    work_id: 7
  }

  const merged = mergeAssistantResponseIntoList(
    [firstResponse, lastResponse],
    legacyResponse
  )

  assert.equal(merged.length, 2)
  assert.equal(merged[0].content, 'first body')
  assert.equal(merged[1].content, 'legacy final body')
})

test('different database messages never merge through shared work or request identity', () => {
  const first = liveResponse({ db_id: 9, content: 'same body' })
  const second = liveResponse({ db_id: 10, content: 'same body' })

  assert.equal(findAssistantResponseReplacementIndex([first], second), -1)
  assert.equal(mergeAssistantResponseIntoList([first], second).length, 2)
})

test('empty done response preserves content already received from the stream', () => {
  const doneResponse = liveResponse({
    db_id: 9,
    response_id: 'session-reply-work:7',
    content: ''
  })

  const merged = mergeAssistantResponseIntoList([liveResponse()], doneResponse)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].content, 'final body')
  assert.equal(merged[0].db_id, '9')
})

test('late content with an old response identity cannot replace a different persisted final message by work', () => {
  const persistedFinal = liveResponse({
    id: 'assistant-final',
    db_id: 9,
    response_id: 'response-turn-3',
    content: 'persisted final body'
  })
  const lateOldContent = liveResponse({
    id: 'assistant-old-content',
    response_id: 'response-turn-1',
    content: 'late old body'
  })

  const merged = mergeAssistantResponseIntoList([persistedFinal], lateOldContent)

  assert.equal(merged.length, 2)
  assert.equal(merged[0].content, 'persisted final body')
  assert.equal(merged[0].response_id, 'response-turn-3')
  assert.equal(merged[1].content, 'late old body')
})

test('content, turn_end, done, and history events converge without duplicate final responses', () => {
  const content = liveResponse({
    id: 'assistant-content',
    response_id: 'response-turn-3',
    content: 'stream body'
  })
  const turnEnd = liveResponse({
    id: 'assistant-turn-end',
    db_id: 9,
    response_id: 'response-turn-3',
    content: 'turn end body'
  })
  const done = liveResponse({
    id: 'assistant-done',
    db_id: 9,
    response_id: 'session-reply-work:7',
    content: 'done body'
  })
  const history = liveResponse({
    id: 'assistant-history',
    db_id: 9,
    response_id: 'response-turn-3',
    content: ''
  })

  const merged = [content, turnEnd, done, history].reduce(
    (messages, incoming) => mergeAssistantResponseIntoList(messages, incoming),
    []
  )
  const replayed = mergeAssistantResponseIntoList(merged, done)

  assert.equal(replayed.length, 1)
  assert.equal(replayed[0].db_id, '9')
  assert.equal(replayed[0].response_id, 'response-turn-3')
  assert.equal(replayed[0].content, 'done body')
})

test('late old turns and different requests in one work remain isolated by response identity', () => {
  const firstTurn = liveResponse({
    id: 'assistant-first',
    request_id: 'request-a',
    response_id: 'response-turn-1',
    content: 'first body'
  })
  const finalTurn = liveResponse({
    id: 'assistant-final',
    request_id: 'request-b',
    db_id: 9,
    response_id: 'response-turn-3',
    content: 'final body'
  })
  const lateFirstTurn = liveResponse({
    id: 'assistant-first-replayed',
    request_id: 'request-a',
    response_id: 'response-turn-1',
    content: 'first body replayed'
  })

  const merged = mergeAssistantResponseIntoList([firstTurn, finalTurn], lateFirstTurn)

  assert.equal(merged.length, 2)
  assert.equal(merged[0].content, 'first body replayed')
  assert.equal(merged[1].content, 'final body')
  assert.equal(merged[1].request_id, 'request-b')
})

test('three interleaved turns keep one message per response while legacy done updates only the final turn', () => {
  const turns = [1, 2, 3].map(turn => liveResponse({
    id: `assistant-turn-${turn}`,
    response_id: `response-turn-${turn}`,
    content: `turn ${turn} body`
  }))
  const legacyDone = liveResponse({
    id: 'assistant-done',
    db_id: 9,
    response_id: 'session-reply-work:7',
    content: 'final turn body'
  })
  const lateSecondTurn = liveResponse({
    id: 'assistant-turn-2-late',
    response_id: 'response-turn-2',
    content: 'turn 2 replayed'
  })

  const merged = mergeAssistantResponseIntoList(
    mergeAssistantResponseIntoList(turns, legacyDone),
    lateSecondTurn
  )

  assert.equal(merged.length, 3)
  assert.deepEqual(merged.map(message => message.response_id), [
    'response-turn-1',
    'response-turn-2',
    'response-turn-3'
  ])
  assert.equal(merged[1].content, 'turn 2 replayed')
  assert.equal(merged[2].content, 'final turn body')
  assert.equal(merged[2].db_id, '9')
})

test('confirmation cards and tool calls are excluded from plain response merging', () => {
  const confirmation = liveResponse({ type: 'audit_confirmation' })
  const toolCall = liveResponse({
    type: 'tool_call',
    content: JSON.stringify({
      role: 'assistant',
      tool_calls: [{ id: 'call-1', name: 'tool', arguments: {} }]
    })
  })

  assert.equal(isPlainAssistantResponse(confirmation), false)
  assert.equal(isPlainAssistantResponse(toolCall), false)
  assert.equal(isAssistantResponse(toolCall), true)
})

test('tool assistant merges terminal plain response while preserving serialized tool calls', () => {
  const toolAssistant = liveResponse({
    id: 'assistant-tool',
    content: JSON.stringify({
      role: 'assistant',
      content: '调用前正文',
      tool_calls: [{ id: 'call-1', type: 'function' }]
    })
  })
  const terminalAssistant = liveResponse({
    id: 'assistant-done',
    db_id: 12,
    content: '最终正文'
  })

  const merged = mergeAssistantResponseIntoList([toolAssistant], terminalAssistant)
  const content = JSON.parse(merged[0].content)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].id, 'assistant-tool')
  assert.equal(merged[0].db_id, '12')
  assert.equal(content.role, 'assistant')
  assert.equal(content.content, '最终正文')
  assert.deepEqual(content.tool_calls, [{ id: 'call-1', type: 'function' }])
})

test('plain streamed assistant becomes serialized tool assistant when top-level tool calls arrive', () => {
  const plainAssistant = liveResponse({
    id: 'assistant-stream',
    content: '流式正文'
  })
  const toolAssistant = liveResponse({
    id: 'assistant-tool',
    tool_calls: [{ id: 'call-2', type: 'function' }],
    content: '流式正文'
  })

  const merged = mergeAssistantResponseIntoList([plainAssistant], toolAssistant)
  const content = JSON.parse(merged[0].content)

  assert.equal(merged.length, 1)
  assert.equal(isPlainAssistantResponse(merged[0]), false)
  assert.equal(isAssistantResponse(merged[0]), true)
  assert.equal(content.role, 'assistant')
  assert.equal(content.content, '流式正文')
  assert.deepEqual(content.tool_calls, [{ id: 'call-2', type: 'function' }])
})

test('explicit tool assistant types remain assistant responses but not plain responses', () => {
  const explicitMessageToolCall = liveResponse({ type: 'tool_call' })
  const explicitContentToolCall = liveResponse({
    content: JSON.stringify({
      role: 'assistant',
      type: 'tool_call',
      content: '',
      tool_calls: [{ id: 'call-3' }]
    })
  })
  const confirmation = liveResponse({ type: 'audit_confirmation' })
  const toolResult = liveResponse({ type: 'tool_result' })
  const toolRole = liveResponse({ role: 'tool' })

  assert.equal(isAssistantResponse(explicitMessageToolCall), true)
  assert.equal(isPlainAssistantResponse(explicitMessageToolCall), false)
  assert.equal(isAssistantResponse(explicitContentToolCall), true)
  assert.equal(isPlainAssistantResponse(explicitContentToolCall), false)
  assert.equal(isAssistantResponse(confirmation), false)
  assert.equal(isAssistantResponse(toolResult), false)
  assert.equal(isAssistantResponse(toolRole), false)
})

test('tool and plain assistant turns with the same work remain isolated by response identity', () => {
  const toolAssistant = liveResponse({
    id: 'assistant-tool-turn',
    response_id: 'response-tool-turn',
    content: JSON.stringify({
      role: 'assistant',
      content: '工具回合',
      tool_calls: [{ id: 'call-4' }]
    })
  })
  const plainAssistant = liveResponse({
    id: 'assistant-plain-turn',
    response_id: 'response-plain-turn',
    content: '普通回合'
  })

  const merged = mergeAssistantResponseIntoList([toolAssistant], plainAssistant)

  assert.equal(merged.length, 2)
  assert.deepEqual(merged.map(message => message.response_id), [
    'response-tool-turn',
    'response-plain-turn'
  ])
})

test('stream and identity-less history converge through a real turn_end in either arrival order', () => {
  const streamResponse = liveResponse({
    id: 'assistant-stream-bridge',
    db_id: undefined,
    response_id: 'response-bridge',
    work_id: 7,
    content: 'stream body'
  })
  const historyResponse = liveResponse({
    id: 'assistant-history-bridge',
    db_id: 9,
    response_id: undefined,
    work_id: undefined,
    content: 'persisted body'
  })
  const turnEnd = liveResponse({
    id: 'assistant-turn-end-bridge',
    type: 'turn_end',
    db_id: 9,
    response_id: 'response-bridge',
    work_id: 7,
    content: 'final body'
  })

  for (const [first, second] of [[streamResponse, historyResponse], [historyResponse, streamResponse]]) {
    const merged = [first, second, turnEnd].reduce(
      (messages, incoming) => mergeAssistantResponseIntoList(messages, incoming),
      []
    )
    const replayed = mergeAssistantResponseIntoList(merged, {
      ...turnEnd,
      id: 'assistant-turn-end-bridge-replayed'
    })

    assert.equal(replayed.length, 1)
    assert.equal(replayed[0].db_id, '9')
    assert.equal(replayed[0].response_id, 'response-bridge')
    assert.equal(replayed[0].content, 'final body')
  }
})

test('persisted history content wins over partial stream content when turn_end is empty', () => {
  const streamResponse = liveResponse({
    id: 'assistant-partial-bridge',
    db_id: undefined,
    response_id: 'response-content-bridge',
    work_id: 7,
    content: 'partial'
  })
  const historyResponse = liveResponse({
    id: 'assistant-persisted-final',
    db_id: 9,
    response_id: undefined,
    work_id: undefined,
    content: 'persisted final'
  })
  const emptyTurnEnd = liveResponse({
    id: 'assistant-empty-turn-end',
    type: 'turn_end',
    db_id: 9,
    response_id: 'response-content-bridge',
    work_id: 7,
    content: ''
  })

  for (const [first, second] of [[streamResponse, historyResponse], [historyResponse, streamResponse]]) {
    const merged = [first, second, emptyTurnEnd].reduce(
      (messages, incoming) => mergeAssistantResponseIntoList(messages, incoming),
      []
    )

    assert.equal(merged.length, 1)
    assert.equal(merged[0].content, 'persisted final')
    assert.equal(merged[0].db_id, '9')
    assert.equal(merged[0].response_id, 'response-content-bridge')
  }
})

test('proactive and synthetic done bridges fold history into only the final same-work turn', () => {
  const firstResponse = liveResponse({
    id: 'assistant-work-first',
    response_id: 'response-work-first',
    content: 'first body'
  })
  const finalResponse = liveResponse({
    id: 'assistant-work-final',
    response_id: 'response-work-final',
    content: 'final streamed body'
  })
  const historyResponse = liveResponse({
    id: 'assistant-work-history',
    db_id: 9,
    response_id: undefined,
    work_id: undefined,
    content: 'history body'
  })
  const bridgeCases = [
    {
      type: 'proactive_reply',
      response_id: undefined,
      content: 'proactive final body'
    },
    {
      type: 'done',
      response_id: 'session-reply-work:7',
      content: 'synthetic final body'
    }
  ]

  for (const bridge of bridgeCases) {
    const merged = mergeAssistantResponseIntoList(
      [firstResponse, finalResponse, historyResponse],
      liveResponse({
        id: `assistant-${bridge.type}-bridge`,
        type: bridge.type,
        db_id: 9,
        work_id: 7,
        response_id: bridge.response_id,
        content: bridge.content
      })
    )

    assert.equal(merged.length, 2)
    assert.equal(merged[0].response_id, 'response-work-first')
    assert.equal(merged[0].content, 'first body')
    assert.equal(merged[1].response_id, 'response-work-final')
    assert.equal(merged[1].content, bridge.content)
    assert.equal(merged[1].db_id, '9')
  }
})

test('a db identity prevents a bridge from merging a different database message with the same response and work', () => {
  const historyResponse = liveResponse({
    id: 'assistant-db-9-history',
    db_id: 9,
    response_id: undefined,
    work_id: undefined,
    content: 'db 9 history'
  })
  const otherDatabaseResponse = liveResponse({
    id: 'assistant-db-10',
    db_id: 10,
    response_id: 'response-shared',
    work_id: 7,
    content: 'db 10 body'
  })
  const bridge = liveResponse({
    id: 'assistant-db-9-bridge',
    type: 'turn_end',
    db_id: 9,
    response_id: 'response-shared',
    work_id: 7,
    content: 'db 9 final'
  })

  const merged = mergeAssistantResponseIntoList([historyResponse, otherDatabaseResponse], bridge)

  assert.equal(merged.length, 2)
  assert.equal(merged[0].db_id, '9')
  assert.equal(merged[0].response_id, 'response-shared')
  assert.equal(merged[0].content, 'db 9 final')
  assert.equal(merged[1].db_id, 10)
  assert.equal(merged[1].response_id, 'response-shared')
  assert.equal(merged[1].work_id, 7)
  assert.equal(merged[1].content, 'db 10 body')
})

test('tool-call stream and persisted history converge through turn_end while retaining tool calls and final content', () => {
  const toolCalls = [{ id: 'call-bridge', type: 'function', function: { name: 'lookup' } }]
  const toolStream = liveResponse({
    id: 'assistant-tool-bridge',
    db_id: undefined,
    response_id: 'response-tool-bridge',
    work_id: 7,
    tool_calls: toolCalls,
    content: 'partial tool response'
  })
  const historyResponse = liveResponse({
    id: 'assistant-tool-history',
    db_id: 9,
    response_id: undefined,
    work_id: undefined,
    content: 'persisted final'
  })
  const turnEnd = liveResponse({
    id: 'assistant-tool-turn-end',
    type: 'turn_end',
    db_id: 9,
    response_id: 'response-tool-bridge',
    work_id: 7,
    content: ''
  })

  const merged = [toolStream, historyResponse, turnEnd].reduce(
    (messages, incoming) => mergeAssistantResponseIntoList(messages, incoming),
    []
  )
  const content = JSON.parse(merged[0].content)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].db_id, '9')
  assert.equal(merged[0].response_id, 'response-tool-bridge')
  assert.deepEqual(merged[0].tool_calls, toolCalls)
  assert.equal(content.content, 'persisted final')
  assert.deepEqual(content.tool_calls, toolCalls)
})

test('repeated history merges collapse all database-id duplicates while preserving unrelated messages', () => {
  const databaseCopies = [
    liveResponse({
      id: 'assistant-db-number',
      db_id: 21,
      response_id: undefined,
      work_id: undefined,
      content: 'first body'
    }),
    liveResponse({
      id: 'assistant-db-string-1',
      db_id: '21',
      response_id: undefined,
      work_id: undefined,
      content: 'second body'
    }),
    liveResponse({
      id: 'assistant-db-string-2',
      db_id: '21',
      response_id: undefined,
      work_id: undefined,
      content: 'third body'
    }),
    liveResponse({
      id: 'assistant-db-other',
      db_id: 22,
      response_id: undefined,
      work_id: undefined,
      content: 'unrelated body'
    })
  ]
  const history = liveResponse({
    id: 'assistant-db-history',
    db_id: 21,
    response_id: undefined,
    work_id: undefined,
    content: 'history body'
  })

  const firstMerge = mergeAssistantResponseIntoList(databaseCopies, history)
  const replayed = mergeAssistantResponseIntoList(firstMerge, {
    ...history,
    id: 'assistant-db-history-replayed'
  })
  const matching = replayed.filter(message => String(message.db_id) === '21')
  const unrelated = replayed.filter(message => String(message.db_id) === '22')

  assert.equal(replayed.length, 2)
  assert.equal(matching.length, 1)
  assert.equal(unrelated.length, 1)
  assert.equal(unrelated[0].content, 'unrelated body')
})

test('turn_end collapses all copies of one real response without merging another same-work turn', () => {
  const responseCopies = [1, 2, 3].map(copy => liveResponse({
    id: `assistant-response-shared-${copy}`,
    db_id: undefined,
    response_id: 'response-shared',
    work_id: 7,
    turn: 'turn-1',
    content: `shared body ${copy}`
  }))
  const otherTurn = liveResponse({
    id: 'assistant-response-other',
    db_id: undefined,
    response_id: 'response-other',
    work_id: 7,
    turn: 'turn-1',
    content: 'other body'
  })
  const turnEnd = liveResponse({
    id: 'assistant-turn-end-shared',
    type: 'turn_end',
    db_id: 31,
    response_id: 'response-shared',
    work_id: 7,
    turn: 'turn-1',
    content: 'final shared body'
  })

  const merged = mergeAssistantResponseIntoList(
    [...responseCopies, otherTurn],
    turnEnd
  )
  const shared = merged.filter(message => message.response_id === 'response-shared')
  const other = merged.filter(message => message.response_id === 'response-other')

  assert.equal(merged.length, 2)
  assert.equal(shared.length, 1)
  assert.equal(shared[0].db_id, '31')
  assert.equal(shared[0].content, 'final shared body')
  assert.equal(other.length, 1)
  assert.equal(other[0].content, 'other body')
})

test('synthetic done bridges db-only history and live response while retaining the real response identity', () => {
  const history = liveResponse({
    id: 'assistant-history-db-only',
    db_id: 42,
    response_id: undefined,
    work_id: undefined,
    content: 'persisted body'
  })
  const live = liveResponse({
    id: 'assistant-live-real',
    db_id: undefined,
    response_id: 'response-real-42',
    work_id: 7,
    content: 'stream body'
  })
  const syntheticDone = liveResponse({
    id: 'assistant-done-synthetic',
    type: 'done',
    db_id: 42,
    response_id: 'session-reply-work:7',
    work_id: 7,
    content: 'final body'
  })

  const merged = mergeAssistantResponseIntoList([history, live], syntheticDone)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].db_id, '42')
  assert.equal(merged[0].response_id, 'response-real-42')
  assert.notEqual(merged[0].response_id, 'session-reply-work:7')
  assert.equal(merged[0].content, 'final body')
})

test('weak database history merge preserves the real response and complete tool message over a synthetic duplicate', () => {
  const toolCalls = [{ id: 'call-db-duplicate', type: 'function', function: { name: 'lookup' } }]
  const completeToolContent = JSON.stringify({
    role: 'assistant',
    content: 'complete body',
    tool_calls: toolCalls
  })
  const synthetic = liveResponse({
    id: 'assistant-db-synthetic',
    db_id: '55',
    response_id: 'session-reply-work:7',
    work_id: 7,
    content: JSON.stringify({ role: 'assistant', content: 'partial body' }),
    reasoning_content: null
  })
  const real = liveResponse({
    id: 'assistant-db-real',
    db_id: 55,
    response_id: 'response-real-tools',
    work_id: 7,
    content: completeToolContent,
    reasoning_content: 'complete reasoning'
  })
  const weakHistory = liveResponse({
    id: 'assistant-db-weak-history',
    db_id: '55',
    response_id: undefined,
    work_id: undefined,
    content: '',
    reasoning_content: null
  })

  const merged = mergeAssistantResponseIntoList([synthetic, real], weakHistory)
  const content = JSON.parse(merged[0].content)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].db_id, '55')
  assert.equal(merged[0].response_id, 'response-real-tools')
  assert.equal(merged[0].reasoning_content, 'complete reasoning')
  assert.equal(content.content, 'complete body')
  assert.deepEqual(content.tool_calls, toolCalls)
})
