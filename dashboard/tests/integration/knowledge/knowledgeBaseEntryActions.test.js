import test from 'node:test'
import assert from 'node:assert/strict'
import { renderToString } from 'vue/server-renderer'

import {
  createHarness,
  dataResponse,
  deferred,
  escapeHtml,
  flushPromises,
  normalize,
} from './knowledgeBaseEntriesHarness.js'

test('delete confirmation is single-flight, cancellation writes nothing, and mutations use the right API payloads', async t => {
  const confirmation = deferred()
  const cancelHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
    confirm: () => confirmation.promise,
  })
  await flushPromises()
  const userRow = { id: 12, filename: 'manual.md' }
  const cancelListCount = cancelHarness.api.documents.length
  const firstDelete = cancelHarness.module.deleteEntry(userRow)
  const duplicateDelete = cancelHarness.module.deleteEntry(userRow)
  await duplicateDelete
  assert.equal(cancelHarness.confirms.length, 1)
  assert.equal(cancelHarness.module.entryActionId.value, 12)
  confirmation.resolve(false)
  await firstDelete
  assert.equal(cancelHarness.api.deleteDocument.length, 0)
  assert.equal(cancelHarness.api.deleteManagedItem.length, 0)
  assert.equal(cancelHarness.api.documents.length, cancelListCount)
  assert.deepEqual(cancelHarness.messages, [])
  assert.equal(cancelHarness.module.entryActionId.value, null)

  const userHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
  })
  await flushPromises()
  await userHarness.module.deleteEntry(userRow)
  assert.deepEqual(userHarness.api.deleteDocument.map(call => [call.id, call.entryId]), [[1, 12]])
  assert.equal(userHarness.api.deleteManagedItem.length, 0)

  const managedHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
  })
  await flushPromises()
  const managedRow = { id: 18, knowledge_key: 'policy.retention', version: 7 }
  await managedHarness.module.deleteEntry(managedRow)
  assert.equal(managedHarness.api.deleteDocument.length, 0)
  assert.deepEqual(managedHarness.api.deleteManagedItem[0].payload.expected_version, 7)
  assert.match(managedHarness.api.deleteManagedItem[0].payload.dedupe_key, /^managed-ui-delete:/)

  await managedHarness.module.retryEntry({
    id: 19,
    version: 8,
    publication_job_status: 'failed',
    publication_job_id: 402,
  })
  assert.equal(managedHarness.api.retryManagedItem.length, 1)
  const retryPayload = normalize(managedHarness.api.retryManagedItem[0].payload)
  assert.deepEqual(retryPayload, {
    expected_version: 8,
    failed_job_id: 402,
    dedupe_key: retryPayload.dedupe_key,
  })
  assert.match(retryPayload.dedupe_key, /^managed-ui-retry:/)
})

test('delete confirmations render interpolated filenames and keys as escaped SSR text', async t => {
  const translate = (key, params) => {
    if (key === 'knowledgeBase.delete_doc_confirm') {
      return `Delete document "${params.filename}"?`
    }
    if (key === 'knowledgeBase.managed_delete_confirm') {
      return `Delete managed knowledge "${params.key}"?`
    }
    return key
  }
  const filename = '<script>alert("x")</script>& "manual".md'
  const knowledgeKey = '<img src=x onerror=alert(1)>& "managed"'
  const userHarness = createHarness(t, {
    translate,
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
  })
  await flushPromises()
  await userHarness.module.deleteEntry({ id: 32, filename })
  const userHtml = await renderToString(userHarness.confirms[0][0])
  assert.ok(userHtml.includes(escapeHtml(filename)))
  assert.doesNotMatch(userHtml, /<script\b/i)

  const managedHarness = createHarness(t, {
    translate,
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
  })
  await flushPromises()
  await managedHarness.module.deleteEntry({ id: 33, knowledge_key: knowledgeKey, version: 4 })
  const managedHtml = await renderToString(managedHarness.confirms[0][0])
  assert.ok(managedHtml.includes(escapeHtml(knowledgeKey)))
  assert.doesNotMatch(managedHtml, /<img\b/i)
})

test('close and confirmation errors do not submit, refresh, or leak action state', async t => {
  const closeHarness = createHarness(t, {
    confirm: 'close',
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
  })
  await flushPromises()
  const closeListCount = closeHarness.api.documents.length
  await closeHarness.module.deleteEntry({ id: 34, filename: 'closed.md' })
  assert.equal(closeHarness.api.deleteDocument.length, 0)
  assert.equal(closeHarness.api.documents.length, closeListCount)
  assert.deepEqual(closeHarness.messages, [])
  assert.equal(closeHarness.module.entryActionId.value, null)

  const confirmationError = new Error('confirmation unavailable')
  const errorHarness = createHarness(t, {
    confirm: () => Promise.reject(confirmationError),
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
  })
  await flushPromises()
  const errorListCount = errorHarness.api.documents.length
  await errorHarness.module.deleteEntry({ id: 35, filename: 'error.md' })
  assert.equal(errorHarness.api.deleteDocument.length, 0)
  assert.equal(errorHarness.api.documents.length, errorListCount)
  assert.deepEqual(errorHarness.messages, [{ type: 'error', args: [confirmationError.message] }])
  assert.equal(errorHarness.module.entryActionId.value, null)
})

test('identity changes during a delete confirmation prevent the write', async t => {
  const confirmation = deferred()
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
    confirm: () => confirmation.promise,
  })
  await flushPromises()

  const deletion = harness.module.deleteEntry({ id: 21, filename: 'private.md' })
  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: false,
  }
  await deletion
  confirmation.resolve(true)
  await flushPromises()
  assert.equal(harness.api.deleteDocument.length, 0)
  assert.equal(harness.module.entryActionId.value, null)
  assert.equal(harness.confirmationCloses.length, 1)
})

test('stale delete and retry responses cannot clear a new action, loading state, or trigger refresh', async t => {
  for (const operation of ['deleteManagedItem', 'retryManagedItem']) {
    const oldResponse = deferred()
    const newResponse = deferred()
    const pendingList = deferred()
    let actionCount = 0
    let listCount = 0
    const harness = createHarness(t, {
      get: id => Promise.resolve(dataResponse({
        id,
        knowledge_base_type: 'llm_managed',
        index_status: 'ready',
      })),
      managedItems: () => {
        listCount += 1
        if (listCount === 3) return pendingList.promise
        return Promise.resolve(dataResponse({
          items: [{ id: listCount === 1 ? 10 : 20, knowledge_key: 'private.item', version: 2 }],
          total: 1,
        }))
      },
      [operation]: () => {
        actionCount += 1
        return actionCount === 1 ? oldResponse.promise : newResponse.promise
      },
    })
    await flushPromises()

    const oldRow = operation === 'retryManagedItem'
      ? { id: 10, version: 2, publication_job_status: 'failed', publication_job_id: 501 }
      : { id: 10, version: 2, knowledge_key: 'private.item' }
    const oldAction = operation === 'retryManagedItem'
      ? harness.module.retryEntry(oldRow)
      : harness.module.deleteEntry(oldRow)
    await flushPromises()
    assert.equal(actionCount, 1)

    harness.permissionsStore.currentUser = {
      uid: 'user-b',
      username: 'User B',
      is_superuser: false,
    }
    await flushPromises()
    const pendingRefresh = harness.module.fetchEntries()
    await flushPromises()
    assert.equal(harness.module.loading.value, true)

    const newRow = operation === 'retryManagedItem'
      ? { id: 20, version: 3, publication_job_status: 'failed', publication_job_id: 601 }
      : { id: 20, version: 3, knowledge_key: 'private.item' }
    const newAction = operation === 'retryManagedItem'
      ? harness.module.retryEntry(newRow)
      : harness.module.deleteEntry(newRow)
    await flushPromises()
    assert.equal(harness.module.entryActionId.value, 20)
    assert.equal(harness.module.loading.value, true)
    const callCountBeforeOldResponse = listCount
    const messageCountBeforeOldResponse = harness.messages.length

    if (operation === 'retryManagedItem') oldResponse.reject(new Error('old retry failed'))
    else oldResponse.resolve(dataResponse({ status: 'deleted' }))
    await oldAction
    assert.equal(harness.module.entryActionId.value, 20)
    assert.equal(harness.module.loading.value, true)
    assert.equal(listCount, callCountBeforeOldResponse)
    assert.equal(harness.messages.length, messageCountBeforeOldResponse)

    newResponse.resolve(dataResponse({
      status: operation === 'retryManagedItem' ? 'retry_submitted' : 'deleted',
    }))
    await newAction
    pendingList.resolve(dataResponse({ items: [], total: 0 }))
    await pendingRefresh
    await flushPromises()
    assert.equal(harness.module.entryActionId.value, null)
    assert.equal(harness.module.loading.value, false)
    assert.equal(listCount, callCountBeforeOldResponse + 1)
  }
})
