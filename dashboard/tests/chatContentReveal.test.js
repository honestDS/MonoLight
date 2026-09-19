import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  shouldDeferChatContent,
  shouldExposeChatContent,
  shouldReleaseChatContent
} from '../src/utils/chatContentReveal.js'

const chatViewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')

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

test('ChatView gates both message data and layout-ready state behind the reveal gate', () => {
  assert.match(chatViewSource, /:messages="renderedMessages"/)
  assert.match(chatViewSource, /:initial-history-loaded="renderedInitialHistoryLoaded"/)
  assert.match(chatViewSource, /@transitionend\.self="handleWelcomeExitTransitionEnd"/)
  assert.match(chatViewSource, /@click="handleSelectSession\(session\)"/)
})
