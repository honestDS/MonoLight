import test from 'node:test'
import assert from 'node:assert/strict'

import {
  createHarness,
  dataResponse,
  deferred,
  flushPromises,
  normalize,
} from './knowledgeBaseEntriesHarness.js'

test('loads numeric routes immediately and uses the USER or MANAGED API and labels', async t => {
  const harness = createHarness(t, {
    routeId: 11,
    get: id => Promise.resolve(dataResponse(id === 11
      ? { id, knowledge_base_type: 'user', index_status: 'ready' }
      : { id, knowledge_base_type: 'llm_managed', index_status: 'ready' })),
  })

  assert.deepEqual(harness.api.get.map(call => call.id), [11])
  assert.equal(harness.api.documents.length, 0)
  await flushPromises()
  assert.deepEqual(harness.api.documents[0].params, { page: 1, size: 10 })
  assert.equal(harness.module.getEntryName({ filename: 'handbook.md' }), 'handbook.md')
  assert.equal(harness.module.getEntrySourceLabel({}), 'knowledgeBase.entries_source_manual')

  harness.module.page.value = 3
  harness.module.pageSize.value = 25
  await harness.module.handlePageChange()
  assert.deepEqual(harness.api.documents.at(-1).params, { page: 3, size: 25 })

  harness.routeId.value = 22
  assert.deepEqual(harness.api.get.map(call => call.id), [11, 22])
  await flushPromises()
  assert.deepEqual(harness.api.managedItems.at(-1).params, { page: 1, size: 10, query: '' })
  assert.equal(harness.module.getEntryName({ knowledge_key: 'policy.retention' }), 'policy.retention')
  assert.equal(
    harness.module.getEntrySourceLabel({ source_type: 'user_api' }),
    'knowledgeBase.managed_source_user_api',
  )

  harness.module.page.value = 4
  harness.module.pageSize.value = 30
  harness.module.query.value = 'retention'
  await harness.module.handleSearch()
  assert.deepEqual(harness.api.managedItems.at(-1).params, {
    page: 1,
    size: 30,
    query: 'retention',
  })
})

test('keeps selectedKb empty after get failure and lets refresh retry', async t => {
  let getCount = 0
  const harness = createHarness(t, {
    get: id => {
      getCount += 1
      return getCount === 1
        ? Promise.reject(new Error('offline'))
        : Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' }))
    },
  })

  await flushPromises()
  assert.equal(harness.module.selectedKb.value, null)
  assert.equal(harness.module.loading.value, false)
  assert.deepEqual(harness.messages, [{
    type: 'error',
    args: ['knowledgeBase.entries_load_failed'],
  }])
  assert.equal(harness.api.documents.length, 0)

  await harness.module.refresh()
  assert.equal(harness.module.selectedKb.value.id, 1)
  assert.equal(harness.api.get.length, 2)
  assert.equal(harness.api.documents.length, 1)
  assert.deepEqual(harness.messages, [{
    type: 'error',
    args: ['knowledgeBase.entries_load_failed'],
  }])
})

test('latest list request wins and a failed page request releases loading', async t => {
  const stale = deferred()
  const latest = deferred()
  let listCount = 0
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: (_id, _params, _config) => {
      listCount += 1
      if (listCount === 2) return stale.promise
      if (listCount === 3) return latest.promise
      if (listCount === 4) return Promise.reject(new Error('page unavailable'))
      return Promise.resolve(dataResponse({ items: [], total: 0 }))
    },
  })
  await flushPromises()

  harness.module.query.value = 'older term'
  const olderLoad = harness.module.handleSearch()
  harness.module.query.value = 'latest term'
  const latestLoad = harness.module.handleSearch()
  assert.equal(harness.api.managedItems[1].config.signal.aborted, true)
  assert.deepEqual(harness.api.managedItems[1].params, {
    page: 1,
    size: 10,
    query: 'older term',
  })
  assert.deepEqual(harness.api.managedItems[2].params, {
    page: 1,
    size: 10,
    query: 'latest term',
  })

  latest.resolve(dataResponse({ items: [{ id: 2, knowledge_key: 'current' }], total: 1 }))
  await latestLoad
  stale.resolve(dataResponse({ items: [{ id: 1, knowledge_key: 'stale' }], total: 1 }))
  await olderLoad
  assert.deepEqual(harness.module.items.value.map(item => item.knowledge_key), ['current'])
  assert.equal(harness.module.loading.value, false)

  await harness.module.handlePageChange()
  assert.equal(harness.module.loading.value, false)
  assert.deepEqual(harness.module.items.value.map(item => item.knowledge_key), ['current'])
  assert.equal(harness.module.total.value, 1)
  assert.deepEqual(harness.messages, [{
    type: 'error',
    args: ['knowledgeBase.entries_load_failed'],
  }])
})

test('preserves raw detail content and closing invalidates pending detail and history reads', async t => {
  const lateDocument = deferred()
  let documentCount = 0
  const userHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
    document: () => {
      documentCount += 1
      return documentCount === 1
        ? Promise.resolve(dataResponse({ content: '  <article>\nraw & exact\n</article>  ' }))
        : lateDocument.promise
    },
  })
  await flushPromises()
  const userRow = { id: 7, filename: 'raw.md' }
  await userHarness.module.viewEntry(userRow)
  assert.equal(userHarness.module.detailContent.value, '  <article>\nraw & exact\n</article>  ')
  assert.equal(userHarness.module.detailTitle.value, 'raw.md')
  assert.equal(userHarness.api.document.length, 1)
  assert.equal(userHarness.api.managedHistory.length, 0)

  userHarness.module.closeDetail()
  const lateView = userHarness.module.viewEntry(userRow)
  assert.equal(userHarness.module.detailVisible.value, true)
  userHarness.module.closeDetail()
  lateDocument.resolve(dataResponse({ content: 'late private content' }))
  await lateView
  assert.equal(userHarness.module.detailVisible.value, false)
  assert.equal(userHarness.module.detailContent.value, '')
  assert.equal(userHarness.module.detailLoading.value, false)

  const lateHistory = deferred()
  let historyCount = 0
  const managedHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItem: () => Promise.resolve(dataResponse({ content: '\n  managed raw\t' })),
    managedHistory: () => {
      historyCount += 1
      return historyCount === 1
        ? lateHistory.promise
        : Promise.resolve(dataResponse([{ version: 3, content: 'current history' }]))
    },
  })
  await flushPromises()
  const managedRow = { id: 9, knowledge_key: 'policy.access' }
  await managedHarness.module.viewEntry(managedRow)
  assert.equal(managedHarness.module.detailContent.value, '\n  managed raw\t')
  assert.equal(managedHarness.module.detailTitle.value, 'policy.access')

  const historyLoad = managedHarness.module.showHistory(managedRow)
  assert.equal(managedHarness.module.historyVisible.value, true)
  assert.equal(managedHarness.module.historyLoading.value, true)
  assert.equal(managedHarness.module.historyTitle.value, 'knowledgeBase.managed_history_title')
  managedHarness.module.closeHistory()
  lateHistory.resolve(dataResponse([{ version: 2, content: 'stale history' }]))
  await historyLoad
  assert.equal(managedHarness.module.historyVisible.value, false)
  assert.equal(managedHarness.module.historyLoading.value, false)
  assert.deepEqual(normalize(managedHarness.module.historyItems.value), [])
  assert.equal(managedHarness.api.managedHistory.length, 1)

  await managedHarness.module.showHistory(managedRow)
  assert.deepEqual(normalize(managedHarness.module.historyItems.value), [
    { version: 3, content: 'current history' },
  ])
  managedHarness.module.closeHistory()
  assert.equal(managedHarness.module.historyVisible.value, false)
  assert.deepEqual(normalize(managedHarness.module.historyItems.value), [])
})

test('USER entries do not request managed history or retry operations', async t => {
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
  })
  await flushPromises()

  await harness.module.showHistory({ id: 4, knowledge_key: 'not-managed' })
  await harness.module.retryEntry({
    id: 4,
    publication_job_status: 'failed',
    publication_job_id: 12,
    version: 3,
  })
  assert.equal(harness.module.historyVisible.value, false)
  assert.equal(harness.api.managedHistory.length, 0)
  assert.equal(harness.api.retryManagedItem.length, 0)
})
