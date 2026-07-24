import assert from 'node:assert/strict'
import test from 'node:test'

import { clearThinkingRequestCallbacks, ensureActiveThinkingMessage, findThinkingIndex, insertMessageBeforeThinking, removeThinkingMessageByIdentity } from '../src/composables/chat/thinkingTracker.js'

test('creates an active Thinking message for the first request', () => {
  const messages = []

  const thinkingId = ensureActiveThinkingMessage(messages, 'thinking-1', 'request-1')

  assert.equal(thinkingId, 'thinking-1')
  assert.deepEqual(messages, [{
    id: 'thinking-1',
    role: 'thinking',
    content: 'Thinking...',
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
  const thinkingMessage = { id: 'thinking-1', role: 'thinking', content: 'Thinking...', request_id: 'request-1' }
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
