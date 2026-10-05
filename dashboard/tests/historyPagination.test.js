import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveHistoryRequest } from '../src/composables/chat/historyPagination.js'

test('initial two logical pages are fetched as one forty-message request', () => {
  assert.deepEqual(resolveHistoryRequest(1, 2, 20), {
    page: 1,
    size: 40,
    nextPage: 3
  })
})

test('pagination continues with twenty messages after the initial forty', () => {
  assert.deepEqual(resolveHistoryRequest(3, 1, 20), {
    page: 3,
    size: 20,
    nextPage: 4
  })
})

test('normalizes negative and fractional pagination inputs', () => {
  assert.deepEqual(resolveHistoryRequest(-2, 0, -1), {
    page: 1,
    size: 1,
    nextPage: 2
  })
  assert.deepEqual(resolveHistoryRequest(3.9, 2.9, 20.9), {
    page: 2,
    size: 40,
    nextPage: 5
  })
})
