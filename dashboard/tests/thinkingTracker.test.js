import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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


test('ThinkingBlock is controlled by the shared collapse model and starts collapsed from the parent default', () => {
  const componentSource = readFileSync(new URL('../src/components/ThinkingBlock.vue', import.meta.url), 'utf8')
  const trackerSource = readFileSync(new URL('../src/composables/chat/thinkingTracker.js', import.meta.url), 'utf8')
  const lifecycleSource = readFileSync(new URL('../src/composables/chat/workLifecycleTracker.js', import.meta.url), 'utf8')
  const listSource = readFileSync(new URL('../src/components/ChatMessageList.vue', import.meta.url), 'utf8')

  assert.match(componentSource, /modelValue/)
  assert.match(componentSource, /update:modelValue/)
  assert.doesNotMatch(componentSource, /const activeNames = ref\(\[\]\)/)
  assert.match(componentSource, /<el-collapse[\s\S]*?<el-collapse-item :name="name"/)
  assert.match(listSource, /const reasoningCollapseModel = ref\(\[\]\)/)
  assert.match(listSource, /v-model="reasoningCollapseModel"/)
  assert.match(listSource, /getReasoningCollapseName\(msg\)/)
  assert.equal(trackerSource.includes("content: 'Thinking...'"), false)
  assert.equal(lifecycleSource.includes("content: 'Thinking...'"), false)
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


test('chat rendering keeps Thinking as lifecycle state, hides its row, and gates reasoning by the session switch', () => {
  const listSource = readFileSync(new URL('../src/components/ChatMessageList.vue', import.meta.url), 'utf8')
  const viewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')

  assert.doesNotMatch(listSource, /msg\.role === 'thinking'[\s\S]*?thinking-status/)
  assert.match(listSource, /message\.role !== 'thinking'/)
  assert.match(listSource, /msg\.role === 'reasoning'[\s\S]*?currentSessionShowReasoning[\s\S]*?<ThinkingBlock/)
  assert.match(listSource, /currentSessionShowReasoning:\s*\{ type: Boolean, default: true \}/)
  assert.match(viewSource, /chat\.show_reasoning/)
  assert.match(viewSource, /:current-session-show-reasoning="currentSessionShowReasoning"/)
  const sessionSource = readFileSync(new URL('../src/composables/chat/useChatSession.js', import.meta.url), 'utf8')
  assert.match(sessionSource, /performHttpSend\([\s\S]*?currentSessionShowToolCalls\.value,\s*currentSessionShowReasoning\.value\s*\)/)
})

test('top activity notice uses the compression notice presentation and reasoning uses the existing markdown renderer', () => {
  const componentSource = readFileSync(new URL('../src/components/ThinkingBlock.vue', import.meta.url), 'utf8')
  const styleSource = readFileSync(new URL('../src/assets/css/ThinkingBlock.scss', import.meta.url), 'utf8')
  const viewStyleSource = readFileSync(new URL('../src/assets/css/ChatView.scss', import.meta.url), 'utf8')
  const listSource = readFileSync(new URL('../src/components/ChatMessageList.vue', import.meta.url), 'utf8')

  assert.match(listSource, /activity-status-notice/)
  assert.match(listSource, /resolveChatActivityNotice/)
  assert.match(viewStyleSource, /\.activity-status-notice\s*\{[\s\S]*?box-shadow:/)
  assert.doesNotMatch(viewStyleSource, /\.activity-status-notice\s*\{[\s\S]*?z-index:/)
  assert.match(componentSource, /v-html="renderedContent"/)
  assert.match(componentSource, /class="thinking-block-content markdown-body"/)
  assert.match(listSource, /:rendered-content="renderMarkdown\(getReasoningContent\(msg\)\)"/)
  assert.match(styleSource, /background:\s*transparent/)
  assert.match(styleSource, /border-radius:\s*8px/)
})
