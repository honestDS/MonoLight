import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  shouldDeferChatContent,
  shouldExposeChatContent,
  shouldReleaseChatContent,
  shouldReturnToWelcomeAfterSessionDelete
} from '../src/utils/chatContentReveal.js'

const dashboardDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

test('new-session to existing-session transition defers content until the welcome exit finishes', () => {
  assert.equal(shouldDeferChatContent({
    wasWelcome: true,
    deferActive: false,
    sessionId: 'session-b'
  }), true)

  assert.equal(shouldExposeChatContent({
    deferredSessionId: 'session-b',
    currentSessionId: 'session-b'
  }), false)
})

test('existing-session switches do not add an artificial reveal delay', () => {
  assert.equal(shouldDeferChatContent({
    wasWelcome: false,
    deferActive: false,
    sessionId: 'session-b'
  }), false)

  assert.equal(shouldExposeChatContent({
    deferredSessionId: null,
    currentSessionId: 'session-b'
  }), true)
})

test('a second session selection during welcome exit remains deferred', () => {
  assert.equal(shouldDeferChatContent({
    wasWelcome: false,
    deferActive: true,
    sessionId: 'session-c'
  }), true)
})

test('only the current input-area transform transition releases deferred content', () => {
  const base = {
    deferredSessionId: 'session-b',
    currentSessionId: 'session-b'
  }

  assert.equal(shouldReleaseChatContent({ ...base, propertyName: 'transform' }), true)
  assert.equal(shouldReleaseChatContent({ ...base, propertyName: 'padding-left' }), false)
  assert.equal(shouldReleaseChatContent({
    deferredSessionId: 'session-a',
    currentSessionId: 'session-b',
    propertyName: 'transform'
  }), false)
})

test('welcome exit scrolls revealed history to the bottom after rendering', async () => {
  const source = await readFile(path.join(dashboardDirectory, 'src/views/ChatView.vue'), 'utf8')
  const handlerStart = source.indexOf('const handleWelcomeExitTransitionEnd =')
  const handlerEnd = source.indexOf('const guidanceSubmitting', handlerStart)
  const handlerSource = source.slice(handlerStart, handlerEnd)

  const releaseIndex = handlerSource.indexOf('deferredContentSessionId.value = null')
  const renderIndex = handlerSource.indexOf('await nextTick()')
  const scrollIndex = handlerSource.indexOf("await messageList.value?.scrollToBottom('auto')")

  assert.match(handlerSource, /const handleWelcomeExitTransitionEnd = async \(event\) =>/)
  assert.ok(releaseIndex >= 0)
  assert.ok(renderIndex > releaseIndex)
  assert.ok(scrollIndex > renderIndex)
})

test('deleting the active session returns the chat to the welcome state only after a successful delete', () => {
  assert.equal(shouldReturnToWelcomeAfterSessionDelete({
    deleted: true,
    deletedSessionId: 'session-a',
    currentSessionId: 'session-a'
  }), true)

  assert.equal(shouldReturnToWelcomeAfterSessionDelete({
    deleted: true,
    deletedSessionId: 'session-a',
    currentSessionId: 'session-b'
  }), false)

  assert.equal(shouldReturnToWelcomeAfterSessionDelete({
    deleted: false,
    deletedSessionId: 'session-a',
    currentSessionId: 'session-a'
  }), false)
})
