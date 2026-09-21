import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  formatSessionActivityTime,
  withSessionActivity
} from '../src/composables/chat/sessionActivity.js'

const chatSessionSource = readFileSync(
  new URL('../src/composables/chat/useChatSession.js', import.meta.url),
  'utf8'
)

test('new local sessions receive the current activity time for grouping', () => {
  const now = new Date(2026, 8, 22, 5, 42, 7)

  assert.deepEqual(
    withSessionActivity({ session_id: 'session-a', title: 'New session' }, now),
    {
      session_id: 'session-a',
      title: 'New session',
      last_active: '2026-09-22 05:42:07'
    }
  )
})

test('existing session activity time is preserved', () => {
  const session = {
    session_id: 'session-a',
    last_active: '2026-09-20 10:00:00'
  }

  assert.equal(
    withSessionActivity(session, new Date(2026, 8, 22, 5, 42, 7)).last_active,
    '2026-09-20 10:00:00'
  )
})

test('session activity formatter matches backend timestamp shape', () => {
  assert.equal(
    formatSessionActivityTime(new Date(2026, 0, 2, 3, 4, 5)),
    '2026-01-02 03:04:05'
  )
})

test('new HTTP and WebSocket sessions share the activity-normalizing insertion path', () => {
  const selectStart = chatSessionSource.indexOf('const selectNewSession =')
  const selectEnd = chatSessionSource.indexOf('const applyLifecycleEvent =', selectStart)
  const selectSource = chatSessionSource.slice(selectStart, selectEnd)

  assert.match(selectSource, /const activeSession = withSessionActivity\(session\)/)
  assert.match(selectSource, /sessions\.value\.unshift\(activeSession\)/)
  assert.match(selectSource, /selectSession\(activeSession, null, false, false\)/)
})
