import test from 'node:test'
import assert from 'node:assert/strict'

import {
  assertPrivateStateCleared,
  createHarness,
  dataResponse,
  deferred,
  flushPromises,
  normalize,
} from './knowledgeBaseEntriesHarness.js'

test('route A-B-A and identity changes isolate old GET and list results', async t => {
  const oldAGet = deferred()
  const oldBGet = deferred()
  const oldAList = deferred()
  const staleIdentityList = deferred()
  const currentIdentityList = deferred()
  let getCount = 0
  let listCount = 0
  const harness = createHarness(t, {
    routeId: 1,
    get: id => {
      getCount += 1
      if (getCount === 1) return oldAGet.promise
      if (getCount === 2) return oldBGet.promise
      if (getCount === 3) {
        return Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' }))
      }
      return Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' }))
    },
    documents: () => {
      listCount += 1
      if (listCount === 1) return oldAList.promise
      if (listCount === 2) {
        return Promise.resolve(dataResponse({ items: [{ id: 2, filename: 'current-b.md' }], total: 1 }))
      }
      if (listCount === 3) return staleIdentityList.promise
      if (listCount === 4) return currentIdentityList.promise
      return Promise.resolve(dataResponse({ items: [], total: 0 }))
    },
  })
  assert.equal(harness.api.get.length, 1)

  harness.routeId.value = 2
  harness.routeId.value = 1
  assert.deepEqual(harness.api.get.map(call => call.id), [1, 2, 1])
  await flushPromises()
  assert.equal(harness.module.selectedKb.value.id, 1)
  assert.equal(harness.api.documents.length, 1)

  harness.routeId.value = 2
  await flushPromises()
  assert.equal(harness.module.selectedKb.value.id, 2)
  assert.deepEqual(harness.module.items.value.map(item => item.filename), ['current-b.md'])

  oldAGet.reject(new Error('stale A get'))
  oldBGet.resolve(dataResponse({ id: 2, knowledge_base_type: 'user', name: 'stale B' }))
  oldAList.resolve(dataResponse({ items: [{ id: 1, filename: 'stale-a.md' }], total: 1 }))
  await flushPromises()
  assert.equal(harness.module.selectedKb.value.id, 2)
  assert.deepEqual(harness.module.items.value.map(item => item.filename), ['current-b.md'])
  assert.equal(harness.messages.length, 0)

  const staleIdentityLoad = harness.module.fetchEntries()
  assert.equal(harness.api.documents.length, 3)
  harness.permissionsStore.currentUser = {
    uid: 'user-c',
    username: 'User C',
    is_superuser: false,
  }
  assert.deepEqual(normalize(harness.module.items.value), [])
  assert.equal(harness.module.selectedKb.value, null)
  await flushPromises()
  assert.equal(harness.api.get.at(-1).id, 2)
  currentIdentityList.resolve(dataResponse({ items: [{ id: 92, filename: 'new-identity.md' }], total: 1 }))
  await flushPromises()
  staleIdentityList.reject(new Error('stale list failure'))
  await staleIdentityLoad
  assert.deepEqual(harness.module.items.value.map(item => item.filename), ['new-identity.md'])
  assert.equal(harness.messages.length, 0)
})

test('identity reset clears every private field and timer, while display-name changes preserve state', async t => {
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => Promise.resolve(dataResponse({
      items: [{ id: 31, knowledge_key: 'private.policy', pending_job_id: 81 }],
      total: 1,
    })),
    managedItem: () => Promise.resolve(dataResponse({ content: 'private detail' })),
    managedHistory: () => Promise.resolve(dataResponse([{ version: 1 }])),
  })
  await flushPromises()
  await harness.module.viewEntry({ id: 31, knowledge_key: 'private.policy' })
  await harness.module.showHistory({ id: 31, knowledge_key: 'private.policy' })
  harness.module.page.value = 4
  harness.module.pageSize.value = 50
  harness.module.query.value = 'private query'
  assert.equal(harness.activeTimers().length, 1)
  const getCount = harness.api.get.length
  const itemSnapshot = normalize(harness.module.items.value)

  harness.permissionsStore.currentUser = {
    uid: 'user-a',
    username: 'Renamed user',
    is_superuser: false,
  }
  await flushPromises()
  assert.equal(harness.api.get.length, getCount)
  assert.equal(harness.module.page.value, 4)
  assert.deepEqual(normalize(harness.module.items.value), itemSnapshot)
  assert.equal(harness.module.detailVisible.value, true)
  assert.equal(harness.module.historyVisible.value, true)
  assert.equal(harness.activeTimers().length, 1)

  harness.permissionsStore.currentUser = {
    uid: 'user-a',
    username: 'Renamed admin',
    is_superuser: true,
  }
  assertPrivateStateCleared(harness.module, { loading: true })
  assert.equal(harness.activeTimers().length, 0)
  await flushPromises()
  assert.equal(harness.api.get.length, getCount + 1)

  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: true,
  }
  assertPrivateStateCleared(harness.module, { loading: true })
  await flushPromises()
  assert.equal(harness.api.get.at(-1).id, 1)

  harness.permissionsStore.clear()
  assertPrivateStateCleared(harness.module)
  assert.equal(harness.activeTimers().length, 0)
  const getsAtLogout = harness.api.get.length
  await flushPromises()
  assert.equal(harness.api.get.length, getsAtLogout)

  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: true,
  }
  await flushPromises()
  await harness.module.viewEntry({ id: 31, knowledge_key: 'private.policy' })
  await harness.module.showHistory({ id: 31, knowledge_key: 'private.policy' })
  assert.equal(harness.activeTimers().length, 1)
  harness.dispose()
  assertPrivateStateCleared(harness.module)
  assert.equal(harness.activeTimers().length, 0)
})

test('old detail failures and history successes cannot populate a new identity scope', async t => {
  const oldDetail = deferred()
  const oldHistory = deferred()
  let detailCount = 0
  let historyCount = 0
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => Promise.resolve(dataResponse({
      items: [{ id: 40, knowledge_key: 'scope.private', pending_job_id: 90 }],
      total: 1,
    })),
    managedItem: () => {
      detailCount += 1
      return detailCount === 1
        ? oldDetail.promise
        : Promise.resolve(dataResponse({ content: 'new identity detail' }))
    },
    managedHistory: () => {
      historyCount += 1
      return historyCount === 1
        ? oldHistory.promise
        : Promise.resolve(dataResponse([{ version: 2, content: 'new identity history' }]))
    },
  })
  await flushPromises()

  const row = { id: 40, knowledge_key: 'scope.private' }
  const oldDetailLoad = harness.module.viewEntry(row)
  const oldHistoryLoad = harness.module.showHistory(row)
  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: false,
  }
  await flushPromises()
  assert.equal(harness.module.detailVisible.value, false)
  assert.equal(harness.module.historyVisible.value, false)
  assert.equal(harness.activeTimers().length, 1)

  await harness.module.viewEntry(row)
  await harness.module.showHistory(row)
  assert.equal(harness.module.detailContent.value, 'new identity detail')
  assert.deepEqual(normalize(harness.module.historyItems.value), [
    { version: 2, content: 'new identity history' },
  ])
  const messagesBeforeStaleResults = harness.messages.length

  oldDetail.reject(new Error('old detail failed'))
  oldHistory.resolve(dataResponse([{ version: 1, content: 'old identity history' }]))
  await Promise.all([oldDetailLoad, oldHistoryLoad])
  assert.equal(harness.module.detailVisible.value, true)
  assert.equal(harness.module.detailContent.value, 'new identity detail')
  assert.equal(harness.module.historyVisible.value, true)
  assert.deepEqual(normalize(harness.module.historyItems.value), [
    { version: 2, content: 'new identity history' },
  ])
  assert.equal(harness.messages.length, messagesBeforeStaleResults)
})
