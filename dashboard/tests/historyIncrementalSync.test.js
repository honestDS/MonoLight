import assert from 'node:assert/strict'
import test from 'node:test'
import {
  getIncrementalHistoryCursor,
  getLatestPersistedMessageId,
  syncIncrementalHistory
} from '../src/composables/chat/historyIncrementalSync.js'

test('incremental history sync advances by persisted message id and continues across bounded runs', async () => {
  const allMessages = Array.from({ length: 45 }, (_, index) => ({
    id: 101 + index,
    content: `message-${101 + index}`
  }))
  const merged = [{ db_id: 100, content: 'existing' }]
  const requests = []

  const fetchPage = async ({ afterId, limit }) => {
    requests.push([afterId, limit])
    return allMessages.filter(message => message.id > afterId).slice(0, limit)
  }
  const mergePage = messages => {
    merged.push(...messages.map(message => ({ ...message, db_id: message.id })))
  }

  const first = await syncIncrementalHistory({
    initialAfterId: getLatestPersistedMessageId(merged),
    fetchPage,
    mergePage,
    pageSize: 20,
    maxPages: 2
  })
  assert.deepEqual(first, {
    lastMessageId: 140,
    hasMore: true,
    pagesFetched: 2
  })
  assert.deepEqual(requests, [[100, 20], [120, 20]])

  const second = await syncIncrementalHistory({
    initialAfterId: getLatestPersistedMessageId(merged),
    fetchPage,
    mergePage,
    pageSize: 20,
    maxPages: 2
  })
  assert.deepEqual(second, {
    lastMessageId: 145,
    hasMore: false,
    pagesFetched: 1
  })
  assert.equal(getLatestPersistedMessageId(merged), 145)
})

test('persisted cursor ignores local transient ids', () => {
  assert.equal(getLatestPersistedMessageId([
    { id: 1710000000000, role: 'user' },
    { id: 1710000000001, db_id: 41 },
    { id: 42, role: 'assistant' }
  ]), 41)
  assert.equal(getLatestPersistedMessageId([{ id: 1710000000000 }]), 0)
})

test('incremental cursor keeps server progress even when fetched messages are hidden from the UI', () => {
  const visibleMessages = [{ db_id: 100, role: 'assistant' }]
  assert.equal(getIncrementalHistoryCursor(visibleMessages, 140), 140)

  visibleMessages.push({ db_id: 145, role: 'assistant' })
  assert.equal(getIncrementalHistoryCursor(visibleMessages, 140), 140)
  assert.equal(getIncrementalHistoryCursor(visibleMessages, undefined), 145)
})

test('incremental history sync stops without declaring completion after invalidation', async () => {
  let current = true
  let mergeCalls = 0

  const result = await syncIncrementalHistory({
    initialAfterId: 10,
    pageSize: 20,
    maxPages: 4,
    isCurrent: () => current,
    fetchPage: async () => {
      current = false
      return [{ id: 11 }]
    },
    mergePage: () => {
      mergeCalls += 1
    }
  })

  assert.deepEqual(result, {
    lastMessageId: 10,
    hasMore: true,
    pagesFetched: 0,
    cancelled: true
  })
  assert.equal(mergeCalls, 0)
})
