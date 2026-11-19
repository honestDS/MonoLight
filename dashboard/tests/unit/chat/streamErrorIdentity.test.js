import assert from 'node:assert/strict'
import test from 'node:test'
import * as Vue from 'vue'
import { processStreamError } from '../../../src/composables/chat/terminalHistory.js'

test('processStreamError deduplicates event IDs and omits invalid message IDs', () => {
  const messages = Vue.ref([])

  assert.equal(
    processStreamError(messages, 'first error', null, 'request-A', 'work-A', 'event-A', 'invalid'),
    true
  )
  assert.equal(messages.value[0].role, 'err')
  assert.equal(Object.hasOwn(messages.value[0], 'db_id'), false)

  assert.equal(
    processStreamError(messages, 'duplicate error', null, 'request-B', 'work-B', 'event-A', 17),
    false
  )
  assert.equal(messages.value.length, 1)
})
