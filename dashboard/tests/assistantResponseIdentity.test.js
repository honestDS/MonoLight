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
