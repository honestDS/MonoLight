import assert from 'node:assert/strict'
import test from 'node:test'

import { getStreamEventIdentity } from '../src/composables/chat/streamEventIdentity.js'

test('prefers event ids and normalizes numeric and string values', () => {
  assert.equal(getStreamEventIdentity({ event_id: 42, work_id: 'work-a', event_sequence_no: 3 }), '42')
  assert.equal(getStreamEventIdentity({ event_id: 'event-42', work_id: 'work-a', event_sequence_no: 3 }), 'event-42')
})

test('generates stable identities from work and event sequence', () => {
  const event = { work_id: 'work-a', event_sequence_no: 3 }

  assert.equal(getStreamEventIdentity(event), 'work:work-a:event:3')
  assert.equal(getStreamEventIdentity({ ...event }), getStreamEventIdentity(event))
  assert.notEqual(getStreamEventIdentity(event), getStreamEventIdentity({ ...event, event_sequence_no: 4 }))
  assert.notEqual(getStreamEventIdentity(event), getStreamEventIdentity({ ...event, work_id: 'work-b' }))
})

test('accepts zero and rejects invalid event sequences or work identities', () => {
  assert.equal(getStreamEventIdentity({ work_id: 'work-a', event_sequence_no: 0 }), 'work:work-a:event:0')
  assert.equal(getStreamEventIdentity({ work_id: 'work-a', event_sequence_no: -1 }), null)
  assert.equal(getStreamEventIdentity({ work_id: 'work-a', event_sequence_no: 1.5 }), null)
  assert.equal(getStreamEventIdentity({ work_id: 'work-a', event_sequence_no: Number.NaN }), null)
  assert.equal(getStreamEventIdentity({ work_id: 'work-a', event_sequence_no: Number.POSITIVE_INFINITY }), null)
  assert.equal(getStreamEventIdentity({ work_id: 'work-a', event_sequence_no: '1' }), null)
  assert.equal(getStreamEventIdentity({ work_id: '', event_sequence_no: 1 }), null)
  assert.equal(getStreamEventIdentity({ work_id: null, event_sequence_no: 1 }), null)
})

test('returns null when no usable event identity is available', () => {
  assert.equal(getStreamEventIdentity({}), null)
  assert.equal(getStreamEventIdentity({ event_id: '' }), null)
  assert.equal(getStreamEventIdentity({ event_id: null }), null)
  assert.equal(getStreamEventIdentity({ event_id: undefined }), null)
  assert.equal(getStreamEventIdentity({ work_id: 'work-a' }), null)
  assert.equal(getStreamEventIdentity({ event_sequence_no: 1 }), null)
})
