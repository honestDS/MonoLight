import assert from 'node:assert/strict'
import test from 'node:test'
import { createTransportNotifier } from '../src/composables/chat/transportNotifications.js'

test('transport notifier keeps only one transient Element Plus message active', () => {
  const calls = []
  const closed = []
  const notifier = createTransportNotifier({
    translate: key => `translated:${key}`,
    showMessage: options => {
      calls.push(options)
      return {
        close: () => closed.push(options.message)
      }
    }
  })

  notifier.show('fallback_retrying')
  notifier.show('submission_unknown')

  assert.deepEqual(closed, ['translated:chat.ws_fallback_retrying'])
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[0], {
    type: 'warning',
    message: 'translated:chat.ws_fallback_retrying',
    duration: 4000,
    showClose: false
  })
  assert.deepEqual(calls[1], {
    type: 'warning',
    message: 'translated:chat.ws_submission_unknown',
    duration: 7000,
    showClose: true
  })

  notifier.close()
  assert.deepEqual(closed, [
    'translated:chat.ws_fallback_retrying',
    'translated:chat.ws_submission_unknown'
  ])
})
