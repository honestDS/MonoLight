import assert from 'node:assert/strict'
import test from 'node:test'

import {
  findAssistantResponseReplacementIndex,
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
})
