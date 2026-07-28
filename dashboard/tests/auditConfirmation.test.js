import assert from 'node:assert/strict'
import test from 'node:test'
import { isAuditConfirmationActionable } from '../src/utils/auditConfirmation.js'

const now = Date.parse('2026-07-29T00:00:00.000Z')

test('returns true for a pending confirmation with a future expiry', () => {
  assert.equal(
    isAuditConfirmationActionable(
      { status: 'pending', expires_at: '2026-07-29T00:01:00.000Z' },
      now
    ),
    true
  )
})

test('returns false when a pending confirmation expires exactly at now', () => {
  assert.equal(
    isAuditConfirmationActionable(
      { status: 'pending', expires_at: '2026-07-29T00:00:00.000Z' },
      now
    ),
    false
  )
})

test('returns false for a pending confirmation with a past expiry', () => {
  assert.equal(
    isAuditConfirmationActionable(
      { status: 'pending', expires_at: '2026-07-28T23:59:59.999Z' },
      now
    ),
    false
  )
})

test('returns false for non-pending confirmations despite a future expiry', () => {
  for (const status of ['expired', 'executing', 'cancelled', 'rejected']) {
    assert.equal(
      isAuditConfirmationActionable(
        { status, expires_at: '2026-07-29T00:01:00.000Z' },
        now
      ),
      false
    )
  }
})

test('returns false for confirmations with a missing or invalid expiry', () => {
  for (const expiresAt of [undefined, null, '', 'not-a-date']) {
    assert.equal(
      isAuditConfirmationActionable({ status: 'pending', expires_at: expiresAt }, now),
      false
    )
  }
})

test('returns false for a null confirmation', () => {
  assert.equal(isAuditConfirmationActionable(null, now), false)
})

test('returns false when now is not finite', () => {
  for (const invalidNow of [NaN, Infinity, -Infinity]) {
    assert.equal(
      isAuditConfirmationActionable(
        { status: 'pending', expires_at: '2026-07-29T00:01:00.000Z' },
        invalidNow
      ),
      false
    )
  }
})
