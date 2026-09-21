import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { resolveHistoryRequest } from '../src/composables/chat/historyPagination.js'

const messageListSource = readFileSync(
  new URL('../src/components/ChatMessageList.vue', import.meta.url),
  'utf8'
)

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

test('programmatic scrolling does not notify the history pagination listener', () => {
  const handlerStart = messageListSource.indexOf('const handleVirtualScroll =')
  const handlerEnd = messageListSource.indexOf('const captureScrollAnchor =', handlerStart)
  const handlerSource = messageListSource.slice(handlerStart, handlerEnd)

  assert.match(
    handlerSource,
    /if \(userScrolled\) \{[\s\S]*?scrollListeners\.forEach\(listener => listener\(offset\)\)[\s\S]*?\}/
  )
  assert.doesNotMatch(
    handlerSource.replace(/if \(userScrolled\) \{[\s\S]*?\}/, ''),
    /scrollListeners\.forEach/
  )
})
