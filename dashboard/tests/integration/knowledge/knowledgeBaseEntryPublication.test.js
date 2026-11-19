import test from 'node:test'
import assert from 'node:assert/strict'

import {
  createHarness,
  dataResponse,
  flushPromises,
  normalize,
} from './knowledgeBaseEntriesHarness.js'

test('refresh failures report once and preserve entries for both knowledge base types', async t => {
  const failures = [
    Object.assign(new Error('server error'), { response: { status: 500 } }),
    new Error('network unavailable'),
  ]

  for (const type of ['user', 'llm_managed']) {
    for (const failedStage of ['metadata', 'list']) {
      for (const failure of failures) {
        let getCount = 0
        let listCount = 0
        const listMethod = type === 'user' ? 'documents' : 'managedItems'
        const oldItem = type === 'user'
          ? { id: 31, filename: 'before.md' }
          : { id: 31, knowledge_key: 'before.key' }
        const newItem = type === 'user'
          ? { id: 32, filename: 'after.md' }
          : { id: 32, knowledge_key: 'after.key' }
        const harness = createHarness(t, {
          get: id => {
            getCount += 1
            if (failedStage === 'metadata' && getCount === 2) return Promise.reject(failure)
            return Promise.resolve(dataResponse({
              id,
              knowledge_base_type: type,
              index_status: 'ready',
              name: `knowledge base ${getCount}`,
            }))
          },
          [listMethod]: () => {
            listCount += 1
            if (failedStage === 'list' && listCount === 2) return Promise.reject(failure)
            return Promise.resolve(dataResponse({
              items: listCount === 1 ? [oldItem] : [newItem],
              total: listCount === 1 ? 4 : 8,
            }))
          },
        })
        await flushPromises()

        harness.module.page.value = 3
        harness.module.pageSize.value = 25
        harness.module.query.value = 'preserved query'
        await harness.module.refresh()

        assert.equal(harness.module.loading.value, false)
        assert.deepEqual(normalize(harness.module.selectedKb.value), {
          id: 1,
          knowledge_base_type: type,
          index_status: 'ready',
          name: failedStage === 'metadata' ? 'knowledge base 1' : 'knowledge base 2',
        })
        assert.deepEqual(normalize(harness.module.items.value), [oldItem])
        assert.equal(harness.module.total.value, 4)
        assert.equal(harness.module.page.value, 3)
        assert.equal(harness.module.pageSize.value, 25)
        assert.equal(harness.module.query.value, 'preserved query')
        assert.deepEqual(harness.messages, [{
          type: 'error',
          args: ['knowledgeBase.entries_load_failed'],
        }])

        await harness.module.refresh()
        assert.equal(harness.module.loading.value, false)
        assert.deepEqual(normalize(harness.module.selectedKb.value), {
          id: 1,
          knowledge_base_type: type,
          index_status: 'ready',
          name: `knowledge base ${getCount}`,
        })
        assert.deepEqual(normalize(harness.module.items.value), [newItem])
        assert.equal(harness.module.total.value, 8)
        assert.equal(harness.module.page.value, 3)
        assert.equal(harness.module.pageSize.value, 25)
        assert.equal(harness.module.query.value, 'preserved query')
        assert.equal(harness.messages.length, 1)
      }
    }
  }
})

test('silent refresh and list failures preserve entries while explicit refresh reports them', async t => {
  let getCount = 0
  let listCount = 0
  let failNextGet = false
  let failNextList = false
  const harness = createHarness(t, {
    get: id => {
      getCount += 1
      if (failNextGet) {
        failNextGet = false
        return Promise.reject(new Error('metadata unavailable'))
      }
      return Promise.resolve(dataResponse({
        id,
        knowledge_base_type: 'llm_managed',
        index_status: 'ready',
        name: `knowledge base ${getCount}`,
      }))
    },
    managedItems: () => {
      listCount += 1
      if (failNextList) {
        failNextList = false
        return Promise.reject(new Error('list unavailable'))
      }
      return Promise.resolve(dataResponse({
        items: [{ id: 41, knowledge_key: `entry-${listCount}` }],
        total: listCount,
      }))
    },
  })
  await flushPromises()

  harness.module.page.value = 2
  harness.module.pageSize.value = 25
  harness.module.query.value = 'saved query'
  const savedEntries = {
    items: normalize(harness.module.items.value),
    total: harness.module.total.value,
    page: harness.module.page.value,
    pageSize: harness.module.pageSize.value,
    query: harness.module.query.value,
  }
  const assertEntriesPreserved = () => {
    assert.deepEqual(normalize(harness.module.items.value), savedEntries.items)
    assert.equal(harness.module.total.value, savedEntries.total)
    assert.equal(harness.module.page.value, savedEntries.page)
    assert.equal(harness.module.pageSize.value, savedEntries.pageSize)
    assert.equal(harness.module.query.value, savedEntries.query)
  }

  failNextGet = true
  await harness.module.refresh(false)
  assert.equal(harness.module.loading.value, false)
  assertEntriesPreserved()
  assert.equal(harness.module.selectedKb.value.name, 'knowledge base 1')
  assert.equal(harness.messages.length, 0)

  failNextGet = true
  await harness.module.refresh()
  assert.equal(harness.module.loading.value, false)
  assertEntriesPreserved()
  assert.equal(harness.messages.length, 1)
  assert.deepEqual(harness.messages[0], {
    type: 'error',
    args: ['knowledgeBase.entries_load_failed'],
  })

  failNextList = true
  await harness.module.refresh(false)
  assert.equal(harness.module.loading.value, false)
  assertEntriesPreserved()
  assert.equal(harness.module.selectedKb.value.name, 'knowledge base 4')
  assert.equal(harness.messages.length, 1)

  failNextList = true
  await harness.module.refresh()
  assert.equal(harness.module.loading.value, false)
  assertEntriesPreserved()
  assert.equal(harness.messages.length, 2)
  assert.deepEqual(harness.messages[1], {
    type: 'error',
    args: ['knowledgeBase.entries_load_failed'],
  })

  failNextList = true
  await harness.module.fetchEntries(false)
  assert.equal(harness.module.loading.value, false)
  assertEntriesPreserved()
  assert.equal(harness.messages.length, 2)

  failNextList = true
  await harness.module.refresh()
  assert.equal(harness.module.loading.value, false)
  assertEntriesPreserved()
  assert.equal(harness.messages.length, 3)
  assert.deepEqual(harness.messages[2], {
    type: 'error',
    args: ['knowledgeBase.entries_load_failed'],
  })
})

test('prioritizes index and publication statuses and requires an indexed version match', async t => {
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
  })
  await flushPromises()
  const status = row => harness.module.getEntryStatus(row)

  assert.equal(status({
    publication_job_status: 'pending',
    version: 4,
    is_recallable: false,
    indexed_version: 4,
  }),
    'knowledgeBase.entries_status_processing')
  assert.equal(status({
    publication_job_status: 'succeeded',
    version: 4,
    is_recallable: true,
    indexed_version: 4,
  }),
    'knowledgeBase.entries_status_ready')
  assert.equal(status({
    publication_job_status: 'succeeded',
    version: 5,
    is_recallable: true,
    indexed_version: 4,
  }),
    'knowledgeBase.entries_status_pending')

  harness.module.selectedKb.value.index_status = 'pending'
  assert.equal(status({ publication_job_status: 'failed' }), 'knowledgeBase.entries_status_failed')
  assert.equal(status({ pending_job_id: 10 }), 'knowledgeBase.entries_status_processing')
  assert.equal(status({ publication_job_status: 'cancelled' }), 'knowledgeBase.entries_status_cancelled')
  assert.equal(status({}), 'knowledgeBase.entries_status_indexing')

  harness.module.selectedKb.value.index_status = 'failed'
  assert.equal(status({ publication_job_status: 'failed' }),
    'knowledgeBase.entries_status_index_unavailable')
  assert.equal(status({ pending_job_id: 10 }), 'knowledgeBase.entries_status_index_unavailable')

  harness.module.selectedKb.value = { id: 1, knowledge_base_type: 'user', index_status: 'ready' }
  assert.equal(status({ version: 1, is_recallable: false }), 'knowledgeBase.entries_status_ready')
  harness.module.selectedKb.value.index_status = 'pending'
  assert.equal(status({}), 'knowledgeBase.entries_status_indexing')
})

test('polls only pending managed entries on the current page and stops at a terminal result', async t => {
  let managedListCount = 0
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => {
      managedListCount += 1
      return Promise.resolve(dataResponse(managedListCount === 1
        ? { items: [{ id: 5, pending_job_id: 8 }], total: 1 }
        : { items: [{ id: 5, publication_job_status: 'succeeded' }], total: 1 }))
    },
  })
  await flushPromises()
  assert.equal(harness.activeTimers().length, 1)
  assert.equal(harness.activeTimers()[0].delay, 2000)

  assert.equal(await harness.runNextTimer(), true)
  assert.equal(managedListCount, 2)
  assert.equal(harness.activeTimers().length, 0)

  harness.module.items.value = [{ id: 6, publication_job_status: 'pending' }]
  harness.module.page.value = 2
  await harness.module.handlePageChange()
  assert.deepEqual(harness.api.managedItems.at(-1).params, { page: 2, size: 10, query: '' })
  assert.equal(harness.activeTimers().length, 0)

  const userHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'user',
      index_status: 'ready',
    })),
    documents: () => Promise.resolve(dataResponse({
      items: [{ id: 6, pending_job_id: 8, publication_job_status: 'pending' }],
      total: 1,
    })),
  })
  await flushPromises()
  assert.equal(userHarness.activeTimers().length, 0)

  const scopedHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => Promise.resolve(dataResponse({
      items: [{ id: 8, pending_job_id: 9 }],
      total: 1,
    })),
  })
  await flushPromises()
  assert.equal(scopedHarness.activeTimers().length, 1)
  scopedHarness.routeId.value = 2
  assert.equal(scopedHarness.activeTimers().length, 0)
  await flushPromises()
  assert.equal(scopedHarness.activeTimers().length, 1)
  scopedHarness.permissionsStore.clear()
  assert.equal(scopedHarness.activeTimers().length, 0)
  scopedHarness.permissionsStore.currentUser = {
    uid: 'user-a',
    username: 'User A',
    is_superuser: false,
  }
  await flushPromises()
  assert.equal(scopedHarness.activeTimers().length, 1)
  scopedHarness.dispose()
  assert.equal(scopedHarness.activeTimers().length, 0)
})

test('poll refreshes managed metadata and moves the pending entry to ready', async t => {
  let getCount = 0
  let listCount = 0
  const harness = createHarness(t, {
    get: id => {
      getCount += 1
      return Promise.resolve(dataResponse({
        id,
        knowledge_base_type: 'llm_managed',
        index_status: getCount === 1 ? 'pending' : 'ready',
      }))
    },
    managedItems: () => {
      listCount += 1
      return Promise.resolve(dataResponse({
        items: [listCount === 1
          ? { id: 15, version: 4, pending_job_id: 27 }
          : {
            id: 15,
            version: 4,
            publication_job_status: 'succeeded',
            is_recallable: true,
            indexed_version: 4,
          }],
        total: 1,
      }))
    },
  })
  await flushPromises()

  assert.equal(harness.module.selectedKb.value.index_status, 'pending')
  assert.equal(harness.module.getEntryStatus(harness.module.items.value[0]),
    'knowledgeBase.entries_status_processing')
  assert.equal(harness.activeTimers().length, 1)

  assert.equal(await harness.runNextTimer(), true)
  assert.equal(getCount, 2)
  assert.equal(listCount, 2)
  assert.equal(harness.module.selectedKb.value.index_status, 'ready')
  assert.equal(harness.module.getEntryStatus(harness.module.items.value[0]),
    'knowledgeBase.entries_status_ready')
  assert.equal(harness.activeTimers().length, 0)
})
