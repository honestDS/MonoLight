import assert from 'node:assert/strict'
import test from 'node:test'

import {
  shouldDeferChatContent,
  shouldExposeChatContent,
  shouldReleaseChatContent
} from '../src/utils/chatContentReveal.js'

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
