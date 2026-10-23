import assert from 'node:assert/strict'
import test from 'node:test'

import { appendStreamReasoning, finalizeStreamReasoning } from '../src/composables/chat/reasoningTracker.js'
import { clearThinkingRequestCallbacks, ensureActiveThinkingMessage, findThinkingIndex, insertMessageBeforeThinking, removeThinkingMessageByIdentity } from '../src/composables/chat/thinkingTracker.js'

test('creates an active Thinking message for the first request', () => {
  const messages = []

  const thinkingId = ensureActiveThinkingMessage(messages, 'thinking-1', 'request-1')

  assert.equal(thinkingId, 'thinking-1')
  assert.deepEqual(messages, [{
    id: 'thinking-1',
    role: 'thinking',
    content: '',
    request_id: 'request-1',
    request_ids: ['request-1']
  }])
})

test('shares and moves Thinking after an appended request', () => {
  const messages = [{ id: 'user-1', role: 'user', content: 'first' }]
  const firstThinkingId = ensureActiveThinkingMessage(messages, 'thinking-1', 'request-1')
  messages.push({ id: 'user-2', role: 'user', content: 'second' })

  const secondThinkingId = ensureActiveThinkingMessage(messages, 'thinking-2', 'request-2')
  const thinkingMessages = messages.filter(message => message.role === 'thinking')

  assert.equal(secondThinkingId, firstThinkingId)
  assert.equal(thinkingMessages.length, 1)
  assert.equal(messages.at(-2).id, 'user-2')
  assert.equal(messages.at(-1).id, firstThinkingId)
  assert.deepEqual(messages.at(-1).request_ids, ['request-1', 'request-2'])
})

test('finds a shared Thinking message by its exact id', () => {
  const messages = []
  const thinkingId = ensureActiveThinkingMessage(messages, 'thinking-1', 'request-1')
  ensureActiveThinkingMessage(messages, 'thinking-2', 'request-2')

  assert.equal(findThinkingIndex(messages, thinkingId, 'request-1'), 0)
  assert.equal(findThinkingIndex(messages, null, 'request-1'), 0)
  assert.equal(findThinkingIndex(messages, null, 'request-2'), 0)
})

test('does not match another request Thinking without an exact id', () => {
  const messages = []
  ensureActiveThinkingMessage(messages, 'thinking-1', 'request-1')

  assert.equal(findThinkingIndex(messages, null, 'wrong-request'), -1)
})

test('inserts streaming content before Thinking without replacing it', () => {
  const thinkingMessage = { id: 'thinking-1', role: 'thinking', content: '', request_id: 'request-1' }
  const messages = [
    { id: 'user-1', role: 'user', content: 'request' },
    thinkingMessage
  ]

  assert.equal(insertMessageBeforeThinking(messages, { id: 'assistant-1', role: 'assistant', content: 'first content' }, 'thinking-1', 'request-1'), true)
  assert.deepEqual(messages.map(message => message.role), ['user', 'assistant', 'thinking'])
  assert.equal(messages[2], thinkingMessage)
  assert.equal(messages[2].id, 'thinking-1')
})

test('keeps Thinking after an appended user message until terminal completion', () => {
  const activeRequestIds = new Set(['request-a'])
  const messages = [{ id: 'user-a', role: 'user', content: 'first request' }]
  const thinkingId = ensureActiveThinkingMessage(messages, 'thinking-1', 'request-a', activeRequestIds)
  insertMessageBeforeThinking(messages, { id: 'assistant-a', role: 'assistant', content: 'first content' }, thinkingId, 'request-a')
  const thinkingMessage = messages.at(-1)

  messages.push({ id: 'user-b', role: 'user', content: 'appended request' })
  activeRequestIds.add('request-b')
  ensureActiveThinkingMessage(messages, 'thinking-2', 'request-b', activeRequestIds)
  insertMessageBeforeThinking(messages, { id: 'assistant-next', role: 'assistant', content: 'next content' }, thinkingId, 'request-a')

  assert.equal(messages.at(-3).id, 'user-b')
  assert.equal(messages.at(-2).id, 'assistant-next')
  assert.equal(messages.at(-1), thinkingMessage)
  assert.equal(removeThinkingMessageByIdentity(messages, thinkingId, 'request-a'), true)
  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('keeps absorbed request identities after the original Thinking is replaced', () => {
  const messages = []
  const activeRequestIds = new Set(['request-a'])
  const firstThinkingId = ensureActiveThinkingMessage(messages, 'thinking-1', 'request-a', activeRequestIds)
  messages.splice(0, 1, { id: firstThinkingId, role: 'assistant', content: 'first response' })
  messages.push({ id: 'user-2', role: 'user', content: 'second request' })
  activeRequestIds.add('request-b')

  const secondThinkingId = ensureActiveThinkingMessage(messages, 'thinking-2', 'request-b', activeRequestIds)
  const thinkingMessage = messages.find(message => message.id === secondThinkingId)

  assert.deepEqual(thinkingMessage.request_ids, ['request-a', 'request-b'])
  assert.notEqual(findThinkingIndex(messages, null, 'request-a'), -1)
  assert.equal(removeThinkingMessageByIdentity(messages, firstThinkingId, 'request-a'), true)
  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('clears callbacks for all requests absorbed by a Thinking work', () => {
  const callbacksMap = new Map([
    ['request-a', { thinkingId: 'thinking-1' }],
    ['request-b', { thinkingId: 'thinking-2' }],
    ['request-c', { thinkingId: 'thinking-other' }]
  ])
  const relatedRequestIds = new Set(['request-a', 'request-b'])

  clearThinkingRequestCallbacks(callbacksMap, 'request-a', 'thinking-1', relatedRequestIds)

  assert.equal(callbacksMap.has('request-a'), false)
  assert.equal(callbacksMap.has('request-b'), false)
  assert.equal(callbacksMap.has('request-c'), true)
})


test('creates a reasoning block only after the first real reasoning chunk and leaves Thinking as lifecycle state', () => {
  let messages = [
    { id: 'user-1', role: 'user', content: 'question', request_id: 'request-1' },
    { id: 'thinking-1', role: 'thinking', content: '', request_id: 'request-1', work_id: 'work-1', response_id: 'response-1', turn: 1 }
  ]

  assert.equal(messages.some(message => message.role === 'reasoning'), false)
  messages = appendStreamReasoning(messages, 'first ', { turn: 1, responseId: 'response-1', requestId: 'request-1', workId: 'work-1' })
  messages = appendStreamReasoning(messages, 'second', { turn: 1, responseId: 'response-1', requestId: 'request-1', workId: 'work-1' })

  assert.equal(messages.filter(message => message.role === 'thinking').length, 1)
  assert.equal(messages.find(message => message.role === 'thinking').reasoning_content, undefined)
  assert.equal(messages.filter(message => message.role === 'reasoning').length, 1)
  assert.equal(messages.find(message => message.role === 'reasoning').reasoning_content, 'first second')
})


test('keeps accumulating in the transient reasoning block when an assistant tool message appears mid-turn', () => {
  let messages = [
    { id: 'thinking-1', role: 'thinking', content: '', request_id: 'request-1', work_id: 'work-1', response_id: 'response-1', turn: 1 }
  ]

  messages = appendStreamReasoning(messages, 'first ', { turn: 1, responseId: 'response-1', requestId: 'request-1', workId: 'work-1' })
  messages.splice(1, 0, { id: 'assistant-tool', role: 'assistant', content: '{"tool_calls":[]}', response_id: 'response-1', work_id: 'work-1', turn: 1 })
  messages = appendStreamReasoning(messages, 'second', { turn: 1, responseId: 'response-1', requestId: 'request-1', workId: 'work-1' })
  messages = finalizeStreamReasoning(messages, null, { turn: 1, responseId: 'response-1', requestId: 'request-1', workId: 'work-1' })

  assert.equal(messages.find(message => message.id === 'assistant-tool').reasoning_content, 'first second')
  assert.equal(messages.some(message => message.role === 'reasoning'), false)
})

test('turn end archives streamed reasoning on the assistant and removes only the transient reasoning block', () => {
  const messages = [
    { id: 'assistant-1', role: 'assistant', content: 'answer', response_id: 'response-1', work_id: 'work-1', turn: 1 },
    { id: 'reasoning-1', role: 'reasoning', content: '', reasoning_content: 'streamed reasoning', response_id: 'response-1', work_id: 'work-1', turn: 1 },
    { id: 'thinking-1', role: 'thinking', content: '', response_id: 'response-1', work_id: 'work-1', turn: 1 }
  ]

  const finalized = finalizeStreamReasoning(messages, null, { turn: 1, responseId: 'response-1', workId: 'work-1' })

  assert.equal(finalized.some(message => message.role === 'reasoning'), false)
  assert.equal(finalized.some(message => message.role === 'thinking'), true)
  assert.equal(finalized.find(message => message.id === 'assistant-1').reasoning_content, 'streamed reasoning')
})

test('finalizes historical reasoning by normalized database identity and remains idempotent', () => {
  const messages = [
    { id: 'assistant-1', db_id: 11, role: 'assistant', request_id: 'shared-request', content: 'first body' },
    { id: 'assistant-2', db_id: '22', role: 'assistant', request_id: 'shared-request', content: 'second body' }
  ]
  const original = messages.map(message => ({ ...message }))

  const firstFinalized = finalizeStreamReasoning(messages, 'first reasoning', { requestId: 'shared-request', dbId: '11' })
  const finalized = finalizeStreamReasoning(firstFinalized, 'second reasoning', { requestId: 'shared-request', dbId: 22 })
  const replayed = finalizeStreamReasoning(finalized, 'first reasoning', { requestId: 'shared-request', dbId: 11 })

  assert.deepEqual(finalized.map(({ id, content, reasoning_content }) => ({ id, content, reasoning_content })), [
    { id: 'assistant-1', content: 'first body', reasoning_content: 'first reasoning' },
    { id: 'assistant-2', content: 'second body', reasoning_content: 'second reasoning' }
  ])
  assert.deepEqual(replayed, finalized)
  assert.deepEqual(messages, original)
})

test('database-targeted finalization does not consume request-matched transient reasoning', () => {
  for (const reasoningContent of [null, '   ', '历史思考']) {
    const messages = [
      { id: 'assistant-history', db_id: 51, role: 'assistant', request_id: 'shared-request', content: 'history body' },
      { id: 'reasoning-active', role: 'reasoning', request_id: 'shared-request', reasoning_content: 'active thought' },
      { id: 'reasoning-other-work', role: 'reasoning', request_id: 'shared-request', response_id: 'other-response', work_id: 'other-work', reasoning_content: 'other work thought' }
    ]
    const original = messages.map(message => ({ ...message }))

    const finalized = finalizeStreamReasoning(messages, reasoningContent, { requestId: 'shared-request', dbId: 51 })

    assert.deepEqual(finalized.map(message => message.id), original.map(message => message.id))
    assert.equal(finalized[0].reasoning_content, reasoningContent === '历史思考' ? reasoningContent : undefined)
    assert.equal(finalized[1].reasoning_content, 'active thought')
    assert.equal(finalized[2].reasoning_content, 'other work thought')
    assert.deepEqual(messages, original)
  }
})

test('creates separate database-targeted assistants from an empty list idempotently', () => {
  const firstFinalized = finalizeStreamReasoning([], 'first reasoning', { requestId: 'shared-request', dbId: 81 })
  const finalized = finalizeStreamReasoning(firstFinalized, 'second reasoning', { requestId: 'shared-request', dbId: 82 })
  const replayed = finalizeStreamReasoning(finalized, 'first reasoning', { requestId: 'shared-request', dbId: 81 })

  assert.deepEqual(finalized.map(({ role, db_id }) => ({ role, db_id: String(db_id) })), [
    { role: 'assistant', db_id: '81' },
    { role: 'assistant', db_id: '82' }
  ])
  assert.notEqual(finalized[0].id, finalized[1].id)
  assert.equal(finalized[0].reasoning_content, 'first reasoning')
  assert.equal(finalized[1].reasoning_content, 'second reasoning')
  assert.deepEqual(replayed, finalized)
})

test('explicit database identity wins over matching response and work identities', () => {
  const messages = [
    { id: 'assistant-target', db_id: 31, role: 'assistant', request_id: 'shared-request', response_id: 'response-shared', work_id: 7, turn: 1, content: 'target body' },
    { id: 'assistant-collision', db_id: 32, role: 'assistant', request_id: 'shared-request', response_id: 'response-shared', work_id: 7, turn: 1, content: 'other body' },
    { id: 'reasoning-shared', role: 'reasoning', request_id: 'shared-request', response_id: 'response-shared', work_id: 7, turn: 1, reasoning_content: 'streamed thought' }
  ]

  const finalized = finalizeStreamReasoning(messages, 'target reasoning', {
    requestId: 'shared-request',
    responseId: 'response-shared',
    workId: 7,
    turn: 1,
    dbId: 31
  })

  assert.equal(finalized[0].reasoning_content, 'target reasoning')
  assert.equal(finalized[1].reasoning_content, undefined)
  assert.equal(finalized.some(message => message.id === 'reasoning-shared'), false)
  assert.deepEqual(finalized.map(message => message.id), ['assistant-target', 'assistant-collision'])
})

test('database-targeted finalization collects only transient reasoning for the target response', () => {
  const messages = [
    { id: 'assistant-target', db_id: '41', role: 'assistant', request_id: 'shared-request', response_id: 'response-target', content: 'target body' },
    { id: 'reasoning-target', role: 'reasoning', request_id: 'shared-request', response_id: 'response-target', reasoning_content: 'target thought' },
    { id: 'reasoning-other', role: 'reasoning', request_id: 'shared-request', response_id: 'response-other', reasoning_content: 'other thought' }
  ]

  const finalized = finalizeStreamReasoning(messages, null, { requestId: 'shared-request', dbId: 41 })

  assert.equal(finalized[0].reasoning_content, 'target thought')
  assert.deepEqual(finalized.map(message => message.id), ['assistant-target', 'reasoning-other'])
  assert.equal(finalized[1].reasoning_content, 'other thought')
  assert.deepEqual(messages.map(message => message.id), ['assistant-target', 'reasoning-target', 'reasoning-other'])
})
