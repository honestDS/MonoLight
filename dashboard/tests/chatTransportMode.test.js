import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const transportSource = readFileSync(
  new URL('../src/composables/chat/useChatTransport.js', import.meta.url),
  'utf8'
)
const chatSessionSource = readFileSync(
  new URL('../src/composables/chat/useChatSession.js', import.meta.url),
  'utf8'
)

test('http transport uses a true non-stream request and never opens a websocket', () => {
  const httpSendStart = transportSource.indexOf('const httpSend = async')
  const wsSendStart = transportSource.indexOf('const wsSend = async')
  assert.ok(httpSendStart >= 0 && wsSendStart > httpSendStart)

  const httpSendSource = transportSource.slice(httpSendStart, wsSendStart)
  assert.match(httpSendSource, /chatApi\.completions\(\{ \.\.\.payload, stream: false \}\)/)
  assert.doesNotMatch(httpSendSource, /completionsStream|wsManager|createWebSocket/)
})

test('non-stream session handling consumes only the final response lifecycle', () => {
  const performHttpSendStart = chatSessionSource.indexOf('const performHttpSend = async')
  const wsSendStart = chatSessionSource.indexOf('const wsSend = async', performHttpSendStart)
  assert.ok(performHttpSendStart >= 0 && wsSendStart > performHttpSendStart)

  const performHttpSendSource = chatSessionSource.slice(performHttpSendStart, wsSendStart)
  assert.doesNotMatch(performHttpSendSource, /callbacks:\s*\{/)
  assert.match(
    performHttpSendSource,
    /if \(shouldProcessCompletedWork\(response\)\) \{[\s\S]*?processAiResponse\(response, null, requestId\)[\s\S]*?finishRequestLifecycle\(requestId, isCurrentRequestSession\)/
  )
})

test('todo bootstrap is session-scoped instead of reply-scoped polling', () => {
  assert.equal((chatSessionSource.match(/chatApi\.sessionTodo\(/g) || []).length, 1)
  assert.match(
    chatSessionSource,
    /watch\(\s*\(\) => sessionManager\.currentSessionId\.value,/
  )
  assert.doesNotMatch(chatSessionSource, /\[currentSessionId,\s*loading\]/)
})

test('websocket transport routes todo updates independently from tool visibility', () => {
  assert.match(
    transportSource,
    /if \(type === 'todo_update'\) \{\s*if \(onTodoUpdate\) onTodoUpdate\(data\)/
  )
  assert.match(
    chatSessionSource,
    /onTodoUpdate: event => \{\s*if \(isCurrentRequestSession\(\)\) applyTodoTransportPayload\(event\)/
  )
})
