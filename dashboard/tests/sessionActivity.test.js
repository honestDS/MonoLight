import assert from 'node:assert/strict'
import test from 'node:test'

import {
  formatSessionActivityTime,
  withSessionActivity
} from '../src/composables/chat/sessionActivity.js'

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

test('consecutive activity normalization is idempotent without mutating the session', () => {
  const session = {
    session_id: 'session-a',
    last_active: '2026-09-20 10:00:00'
  }
  const originalSession = { ...session }

  const normalized = withSessionActivity(
    session,
    new Date(2026, 8, 22, 5, 42, 7)
  )
  const repeated = withSessionActivity(
    normalized,
    new Date(2026, 8, 23, 6, 43, 8)
  )

  assert.deepEqual(repeated, normalized)
  assert.equal(repeated.last_active, originalSession.last_active)
  assert.deepEqual(session, originalSession)
})

test('session activity formatter matches backend timestamp shape', () => {
  assert.equal(
    formatSessionActivityTime(new Date(2026, 0, 2, 3, 4, 5)),
    '2026-01-02 03:04:05'
  )
})
