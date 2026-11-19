import test from 'node:test'
import assert from 'node:assert/strict'
import { renderToString } from 'vue/server-renderer'

import {
  createHarness,
  dataResponse,
  normalize,
  escapeHtml,
  deferred,
  flushPromises,
} from './knowledgeBaseManagementHarness.js'

test('uses the list-wide repair count for the warning and clears it at zero', async t => {
  const responses = [
    dataResponse({ items: [], total: 0, embedding_models: [], repair_required_count: 4 }),
    dataResponse({ items: [], total: 0, embedding_models: [], repair_required_count: 0 }),
  ]
  const harness = await createHarness(t, { list: () => Promise.resolve(responses.shift()) })

  assert.equal(harness.module.repairRequiredCount.value, 0)
  await harness.module.fetchData()
  assert.equal(harness.module.tableData.value.length, 0)
  assert.equal(harness.module.repairRequiredCount.value, 4)

  await harness.module.fetchData()
  assert.equal(harness.module.repairRequiredCount.value, 0)
})

test('clears the repair count on identity changes and ignores an old list response', async t => {
  for (const changedField of ['uid', 'superuser']) {
    const stale = deferred()
    const fresh = deferred()
    let listCalls = 0
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      list: () => {
        listCalls += 1
        if (listCalls === 1) {
          return Promise.resolve(dataResponse({
            items: [], total: 0, embedding_models: [], repair_required_count: 6,
          }))
        }
        return listCalls === 2 ? stale.promise : fresh.promise
      },
    })

    await harness.module.fetchData()
    const staleLoad = harness.module.fetchData()

    harness.permissionsStore.currentUser = changedField === 'uid'
      ? { uid: 'user-b', username: 'Bob', is_superuser: false }
      : { uid: 'user-a', username: 'Alice', is_superuser: true }
    await flushPromises()
    assert.equal(harness.module.repairRequiredCount.value, 0)

    const freshLoad = harness.api.lists.length === 2 ? harness.module.fetchData() : null
    fresh.resolve(dataResponse({
      items: [], total: 0, embedding_models: [], repair_required_count: 3,
    }))
    if (freshLoad) await freshLoad
    else await flushPromises()

    stale.resolve(dataResponse({
      items: [], total: 0, embedding_models: [], repair_required_count: 99,
    }))
    await staleLoad
    await flushPromises()
    assert.equal(harness.module.repairRequiredCount.value, 3, changedField)
  }
})

test('clears the repair count when the list request is unauthorized', async t => {
  for (const status of [401, 403]) {
    let listCalls = 0
    const harness = await createHarness(t, {
      list: () => {
        listCalls += 1
        if (listCalls === 1) {
          return Promise.resolve(dataResponse({
            items: [], total: 0, embedding_models: [], repair_required_count: 5,
          }))
        }
        return Promise.reject(Object.assign(new Error('unauthorized'), {
          response: { status },
        }))
      },
    })

    await harness.module.fetchData()
    assert.equal(harness.module.repairRequiredCount.value, 5)
    await harness.module.fetchData()
    assert.equal(harness.module.repairRequiredCount.value, 0, String(status))
  }
})

test('repairs a selected owner from the complete admin user list without opening an arbitrary migration', async t => {
  const users = Array.from({ length: 205 }, (_, index) => ({
    uid: `owner-${index}`,
    username: `Owner ${index}`,
  }))
  users[100] = { uid: 'admin-user', username: 'Administrator' }
  const selectedOwner = users[204]
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users,
    knowledgeBases: [{
      id: 'kb-current-page',
      uid: 'owner-0',
      name: 'Current page KB',
      knowledge_base_type: 'user',
      embedding_channel_id: 1,
      embedding_model_id: 'embedding-model',
    }],
  })

  await harness.module.fetchData()
  await harness.module.showRepairDialog()

  assert.equal(harness.module.repairDialogVisible.value, true)
  assert.equal(harness.api.userLists.length, 3)
  assert.deepEqual(harness.api.userLists.map(request => request.params), [
    { page: 1, size: 100 },
    { page: 2, size: 100 },
    { page: 3, size: 100 },
  ])
  assert.deepEqual(normalize(harness.module.repairOwners.value), users)
  assert.equal(harness.module.repairOwnerUid.value, 'admin-user')
  assert.equal(harness.module.tableData.value.some(row => row.uid === selectedOwner.uid), false)

  harness.module.repairOwnerUid.value = selectedOwner.uid
  await harness.module.submitUserRepair()

  assert.equal(harness.api.repairs.length, 1)
  assert.deepEqual(harness.api.repairs[0].params, { uid: selectedOwner.uid })
  assert.equal(typeof harness.api.repairs[0].payload.dedupe_key, 'string')
  assert.ok(harness.api.repairs[0].payload.dedupe_key.length > 0)
  assert.equal(harness.module.repairDialogVisible.value, false)
  assert.equal(harness.module.migrationDialogVisible.value, false)
  assert.equal(harness.api.gets.length, 0)
  assert.ok(harness.api.lists.length >= 2)
})

test('repairs an ordinary user only for the logged-in uid without loading admin users', async t => {
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    users: [{ uid: 'other-user', username: 'Other user' }],
  })

  await harness.module.showRepairDialog()

  assert.equal(harness.module.repairDialogVisible.value, true)
  assert.equal(harness.module.repairOwnerUid.value, 'user-a')
  assert.equal(harness.api.userLists.length, 0)

  await harness.module.submitUserRepair()

  assert.equal(harness.api.repairs.length, 1)
  assert.deepEqual(harness.api.repairs[0].params, { uid: 'user-a' })
  assert.equal(harness.api.userLists.length, 0)
  assert.equal(harness.module.repairDialogVisible.value, false)
  assert.equal(harness.module.migrationDialogVisible.value, false)
})

test('blocks user repair while owners load, after owner-list failure, or without a valid selection', async t => {
  const pendingUsers = deferred()
  const loadingHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    userList: () => pendingUsers.promise,
  })
  const loading = loadingHarness.module.showRepairDialog()

  assert.equal(loadingHarness.module.repairOwnersLoading.value, true)
  await loadingHarness.module.submitUserRepair()
  assert.equal(loadingHarness.api.repairs.length, 0)

  pendingUsers.resolve(dataResponse({
    items: [
      { uid: 'admin-user', username: 'Administrator' },
      { uid: 'owner-b', username: 'Bob' },
    ],
    total: 2,
  }))
  await loading
  loadingHarness.module.repairOwnerUid.value = 'not-in-loaded-list'
  await loadingHarness.module.submitUserRepair()
  assert.equal(loadingHarness.api.repairs.length, 0)

  const failedHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    userList: () => Promise.reject(new Error('users unavailable')),
  })
  await failedHarness.module.showRepairDialog()
  assert.equal(failedHarness.module.repairOwnersLoading.value, false)
  assert.notEqual(failedHarness.module.repairOwnersError.value, null)
  await failedHarness.module.submitUserRepair()
  assert.equal(failedHarness.api.repairs.length, 0)

  const emptyHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users: [],
  })
  await emptyHarness.module.showRepairDialog()
  assert.deepEqual(normalize(emptyHarness.module.repairOwners.value), [])
  assert.equal(emptyHarness.module.repairOwnerUid.value, null)
  await emptyHarness.module.submitUserRepair()
  assert.equal(emptyHarness.api.repairs.length, 0)
})

test('keeps user repair open and retryable after an API failure, and blocks duplicate submissions', async t => {
  const request = deferred()
  const knowledgeBase = {
    id: 'kb-user-a',
    uid: 'user-a',
    username: 'Alice',
    name: 'Alice KB',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  let repairCalls = 0
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    knowledgeBases: [knowledgeBase],
    repair: (payload, params) => {
      repairCalls += 1
      if (repairCalls === 1) return Promise.reject(new Error('repair unavailable'))
      if (repairCalls === 2) return request.promise
      return Promise.resolve(dataResponse({
        items: [{ ...knowledgeBase, migration_job_id: 103 }],
        total: 1,
      }))
    },
  })

  await harness.module.showRepairDialog()
  await harness.module.submitUserRepair()
  assert.equal(harness.module.repairDialogVisible.value, true)
  assert.equal(harness.module.repairSubmitting.value, false)

  const retry = harness.module.submitUserRepair()
  await harness.module.submitUserRepair()
  assert.equal(harness.api.repairs.length, 2)
  assert.equal(harness.module.repairSubmitting.value, true)
  request.resolve(dataResponse({
    items: [{ ...knowledgeBase, migration_job_id: 103 }],
    total: 1,
  }))
  await retry

  assert.equal(harness.module.repairDialogVisible.value, false)
  assert.equal(harness.api.repairs.length, 2)
  assert.ok(harness.api.repairs.every(item => item.params.uid === 'user-a'))
})

test('confirms per-knowledge-base repair with literal owner identity and skips cancel or close', async t => {
  const row = {
    id: 'kb-repair-special',
    uid: 'opaque-owner-id',
    username: 'Alice <admin>',
    name: 'Knowledge base <alpha> & "quotes"',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }

  for (const action of ['cancel', 'close']) {
    const harness = await createHarness(t, {
      confirm: async () => {
        if (action === 'close') throw 'close'
        return false
      },
    })

    await harness.module.repairKnowledgeBase(row)

    assert.equal(harness.confirms.length, 1)
    const [message, , options] = harness.confirms[0]
    const html = await renderToString(message)
    assert.ok(html.includes(escapeHtml(row.name)))
    assert.ok(html.includes(escapeHtml(row.username)))
    assert.ok(html.includes(escapeHtml(row.id)))
    assert.doesNotMatch(html, /<(?:alpha|admin|script)\b/i)
    assert.equal(options.dangerouslyUseHTMLString, false)
    assert.equal(harness.api.repairs.length, 0)
  }
})

test('does not confirm or submit a second knowledge-base repair while one is pending', async t => {
  const confirmation = deferred()
  const repair = deferred()
  const firstRow = {
    id: 'kb-repair-first',
    name: 'First KB',
    username: 'Alice',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const secondRow = {
    id: 'kb-repair-second',
    name: 'Second KB',
    username: 'Bob',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const harness = await createHarness(t, {
    confirm: () => confirmation.promise,
    repair: () => repair.promise,
    get: id => Promise.resolve(dataResponse({ id, migration_status: 'succeeded' })),
  })

  const first = harness.module.repairKnowledgeBase(firstRow)
  await harness.module.repairKnowledgeBase(secondRow)
  assert.equal(harness.confirms.length, 1)
  assert.equal(harness.api.repairs.length, 0)

  confirmation.resolve(true)
  await flushPromises()
  assert.equal(harness.api.repairs.length, 1)
  const duplicateDuringRequest = harness.module.repairKnowledgeBase(secondRow)
  await duplicateDuringRequest
  assert.equal(harness.confirms.length, 1)
  assert.equal(harness.api.repairs.length, 1)

  repair.resolve(dataResponse({ items: [{ id: firstRow.id, migration_job_id: 101 }], total: 1 }))
  await first
  assert.deepEqual(harness.api.repairs[0].params, { kb_id: firstRow.id })
  assert.equal(harness.module.selectedKb.value.id, firstRow.id)
  assert.equal(harness.module.migrationDialogVisible.value, true)
  assert.deepEqual(harness.api.gets.map(request => request.id), [firstRow.id])
})

test('releases a failed per-knowledge-base repair so it can be retried', async t => {
  let repairCalls = 0
  const row = {
    id: 'kb-retry',
    name: 'Retry KB',
    username: 'Alice',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const harness = await createHarness(t, {
    repair: () => {
      repairCalls += 1
      if (repairCalls === 1) return Promise.reject(new Error('repair unavailable'))
      return Promise.resolve(dataResponse({ items: [{ id: row.id, migration_job_id: 102 }], total: 1 }))
    },
    get: id => Promise.resolve(dataResponse({ id, migration_status: 'succeeded' })),
  })

  await harness.module.repairKnowledgeBase(row)
  assert.equal(harness.api.repairs.length, 1)
  await harness.module.repairKnowledgeBase(row)

  assert.equal(harness.api.repairs.length, 2)
  assert.ok(harness.api.repairs.every(request => request.params.kb_id === row.id))
  assert.equal(harness.module.migrationDialogVisible.value, true)
})

test('routes the more-menu repair action through confirmation and repairs only that knowledge base', async t => {
  const row = {
    id: 'kb-more-repair',
    uid: 'owner-a',
    username: 'Alice',
    name: 'More menu KB',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const cancelled = await createHarness(t, { confirm: async () => false })

  await cancelled.module.handleKnowledgeBaseMoreAction('repair', row)

  assert.equal(cancelled.confirms.length, 1)
  const cancelledHtml = await renderToString(cancelled.confirms[0][0])
  assert.ok(cancelledHtml.includes(escapeHtml(row.name)))
  assert.ok(cancelledHtml.includes(escapeHtml(row.username)))
  assert.ok(cancelledHtml.includes(escapeHtml(row.id)))
  assert.equal(cancelled.confirms[0][2].dangerouslyUseHTMLString, false)
  assert.equal(cancelled.api.repairs.length, 0)

  const repaired = await createHarness(t, {
    repair: () => Promise.resolve(dataResponse({
      items: [{ ...row, migration_job_id: 501 }],
      total: 1,
    })),
    get: id => Promise.resolve(dataResponse({ id, migration_status: 'succeeded' })),
  })

  await repaired.module.handleKnowledgeBaseMoreAction('repair', row)
  await flushPromises()

  assert.equal(repaired.confirms.length, 1)
  const repairedHtml = await renderToString(repaired.confirms[0][0])
  assert.ok(repairedHtml.includes(escapeHtml(row.name)))
  assert.ok(repairedHtml.includes(escapeHtml(row.username)))
  assert.ok(repairedHtml.includes(escapeHtml(row.id)))
  assert.equal(repaired.api.repairs.length, 1)
  assert.deepEqual(repaired.api.repairs[0].params, { kb_id: row.id })
  assert.deepEqual(repaired.api.gets.map(request => request.id), [row.id])
  assert.equal(repaired.module.selectedKb.value.id, row.id)
})
