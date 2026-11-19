import test from 'node:test'
import assert from 'node:assert/strict'

import {
  createHarness,
  dataResponse,
  normalize,
  deferred,
  flushPromises,
} from './knowledgeBaseManagementHarness.js'

test('submits an embedding migration and displays the returned state', async t => {
  const row = {
    id: 'kb-migrate',
    uid: 'user-a',
    name: 'Migration KB',
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
    embedding_channel_id: 1,
    embedding_model_id: 'old-model',
  }
  const updated = {
    ...row,
    migration_status: 'preparing',
    target_embedding_channel_id: 2,
    target_embedding_model_id: 'new-model',
  }
  const harness = await createHarness(t, {
    list: () => Promise.resolve(dataResponse({
      items: [row],
      total: 1,
      embedding_models: [{ channel_id: 2, model_id: 'new-model', channel_name: 'New channel' }],
    })),
    get: () => Promise.resolve(dataResponse(row)),
    migrateEmbedding: () => Promise.resolve(dataResponse(updated)),
  })

  await harness.module.fetchData()
  await harness.module.showMigrationDialog(row)
  harness.module.migrationTargetKey.value = '2::new-model'
  await harness.module.submitEmbeddingMigration()

  assert.deepEqual(normalize(harness.api.migrations.map(({ id, payload }) => ({ id, payload }))), [{
    id: row.id,
    payload: { embedding_channel_id: 2, embedding_model_id: 'new-model' },
  }])
  assert.equal(harness.module.selectedKb.value.migration_status, 'preparing')
  assert.equal(harness.module.tableData.value[0].migration_status, 'preparing')
  assert.equal(harness.module.tableData.value[0].target_embedding_model_id, 'new-model')
  assert.equal(harness.messages.filter(message => message.type === 'success').length, 1)
  assert.equal(harness.activeTimers().length, 1)
})

test('does not submit a migration confirmation after the identity and selected KB change', async t => {
  const confirmation = deferred()
  const oldKb = {
    id: 'kb-confirm-old',
    uid: 'user-a',
    name: 'Old confirmation KB',
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
    target_embedding_channel_id: 2,
    target_embedding_model_id: 'old-target',
  }
  const newKb = {
    id: 'kb-confirm-new',
    uid: 'user-b',
    name: 'New confirmation KB',
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
  }
  let listCalls = 0
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    list: () => {
      listCalls += 1
      return Promise.resolve(dataResponse({
        items: [listCalls === 1 ? oldKb : newKb],
        total: 1,
        embedding_models: listCalls === 1
          ? [{ channel_id: 2, model_id: 'old-target' }]
          : [{ channel_id: 3, model_id: 'new-target' }],
      }))
    },
    get: id => Promise.resolve(dataResponse(id === oldKb.id ? oldKb : newKb)),
    confirm: () => confirmation.promise,
  })

  await harness.module.fetchData()
  await harness.module.showMigrationDialog(oldKb)
  harness.module.migrationTargetKey.value = '2::old-target'
  const pendingConfirmation = harness.module.submitEmbeddingMigration()
  await flushPromises()
  assert.equal(harness.confirms.length, 1)
  assert.equal(harness.api.migrations.length, 0)

  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'Bob',
    is_superuser: false,
  }
  await flushPromises()
  assert.deepEqual(harness.module.tableData.value.map(item => item.id), [newKb.id])
  await harness.module.showMigrationDialog(newKb)
  harness.module.migrationTargetKey.value = '3::new-target'

  confirmation.resolve(true)
  await pendingConfirmation

  assert.equal(harness.api.migrations.length, 0)
  assert.equal(harness.module.selectedKb.value.id, newKb.id)
  assert.equal(harness.module.migrationDialogVisible.value, true)
  assert.equal(harness.module.migrationTargetKey.value, '3::new-target')
  assert.equal(harness.messages.length, 0)
})

test('ignores late migration success or failure without disturbing the new identity dialog', async t => {
  for (const outcome of ['success', 'failure']) {
    const oldMigration = deferred()
    const newMigration = deferred()
    const oldKb = {
      id: 'kb-submit-old',
      uid: 'user-a',
      name: 'Old submitted KB',
      knowledge_base_type: 'user',
      migration_status: 'succeeded',
    }
    const newKb = {
      id: 'kb-submit-new',
      uid: 'user-b',
      name: 'New submitted KB',
      knowledge_base_type: 'user',
      migration_status: 'succeeded',
    }
    let listCalls = 0
    let migrationCalls = 0
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      list: () => {
        listCalls += 1
        const isOldScope = listCalls === 1
        return Promise.resolve(dataResponse({
          items: [isOldScope ? oldKb : newKb],
          total: 1,
          embedding_models: isOldScope
            ? [{ channel_id: 2, model_id: 'old-target' }]
            : [{ channel_id: 3, model_id: 'new-target' }],
        }))
      },
      get: id => Promise.resolve(dataResponse(id === oldKb.id ? oldKb : newKb)),
      migrateEmbedding: () => {
        migrationCalls += 1
        return migrationCalls === 1 ? oldMigration.promise : newMigration.promise
      },
    })

    await harness.module.fetchData()
    await harness.module.showMigrationDialog(oldKb)
    harness.module.migrationTargetKey.value = '2::old-target'
    const oldSubmission = harness.module.submitEmbeddingMigration()
    await flushPromises()
    assert.equal(harness.api.migrations.length, 1)
    assert.equal(harness.module.migrationSubmitting.value, true)

    harness.permissionsStore.currentUser = {
      uid: 'user-b',
      username: 'Bob',
      is_superuser: false,
    }
    await flushPromises()
    assert.equal(harness.module.migrationSubmitting.value, false)
    assert.deepEqual(harness.module.tableData.value.map(item => item.id), [newKb.id])
    await harness.module.showMigrationDialog(newKb)
    harness.module.migrationTargetKey.value = '3::new-target'
    const newSubmission = harness.module.submitEmbeddingMigration()
    await flushPromises()
    assert.equal(harness.api.migrations.length, 2)
    assert.deepEqual(normalize(harness.api.migrations[1]), {
      method: 'migrateEmbedding',
      id: newKb.id,
      payload: { embedding_channel_id: 3, embedding_model_id: 'new-target' },
    })
    assert.equal(harness.module.migrationSubmitting.value, true)
    const newTableBeforeLateResponse = normalize(harness.module.tableData.value)
    const newSelectionBeforeLateResponse = normalize(harness.module.selectedKb.value)
    const messageCountBeforeLateResponse = harness.messages.length

    if (outcome === 'success') {
      oldMigration.resolve(dataResponse({
        ...oldKb,
        migration_status: 'preparing',
        target_embedding_channel_id: 2,
        target_embedding_model_id: 'old-target',
      }))
    } else {
      oldMigration.reject(new Error('old migration failed'))
    }
    await oldSubmission
    await flushPromises()

    assert.deepEqual(normalize(harness.module.tableData.value), newTableBeforeLateResponse, outcome)
    assert.deepEqual(normalize(harness.module.selectedKb.value), newSelectionBeforeLateResponse, outcome)
    assert.equal(harness.module.migrationSubmitting.value, true, outcome)
    assert.equal(harness.module.migrationDialogVisible.value, true, outcome)
    assert.equal(harness.messages.length, messageCountBeforeLateResponse, outcome)
    assert.equal(harness.activeTimers().length, 0, outcome)

    newMigration.resolve(dataResponse({
      ...newKb,
      migration_status: 'preparing',
      target_embedding_channel_id: 3,
      target_embedding_model_id: 'new-target',
    }))
    await newSubmission
    assert.equal(harness.module.selectedKb.value.id, newKb.id)
    assert.equal(harness.module.selectedKb.value.migration_status, 'preparing')
    assert.equal(harness.module.tableData.value[0].migration_status, 'preparing')
    assert.equal(harness.module.migrationSubmitting.value, false)
    assert.equal(harness.messages.filter(message => message.type === 'success').length, 1)
  }
})

test('immediately refreshes migration state by id and polls a KB absent from the current page', async t => {
  const row = {
    id: 'kb-not-in-page',
    name: 'Finished in the table',
    migration_status: 'succeeded',
    migration_total_count: 5,
    migration_success_count: 5,
  }
  let getCalls = 0
  const harness = await createHarness(t, {
    get: id => {
      getCalls += 1
      return Promise.resolve(dataResponse(getCalls === 1
        ? {
            ...row,
            migration_status: 'building',
            migration_total_count: 5,
            migration_success_count: 2,
          }
        : {
            ...row,
            migration_status: 'succeeded',
            migration_total_count: 5,
            migration_success_count: 5,
          }))
    },
  })

  await harness.module.showMigrationDialog(row)

  assert.equal(harness.module.tableData.value.some(item => item.id === row.id), false)
  assert.equal(harness.module.migrationDialogVisible.value, true)
  assert.deepEqual(harness.api.gets.map(request => request.id), [row.id])
  assert.equal(harness.module.selectedKb.value.migration_status, 'building')
  assert.equal(harness.module.selectedKb.value.migration_total_count, 5)
  assert.equal(harness.module.selectedKb.value.migration_success_count, 2)
  assert.equal(harness.module.migrationProgress.value, 40)
  assert.equal(harness.activeTimers().length, 1)

  await harness.runNextTimer()
  assert.deepEqual(harness.api.gets.map(request => request.id), [row.id, row.id])
  assert.equal(harness.module.selectedKb.value.migration_status, 'succeeded')
  assert.equal(harness.module.selectedKb.value.migration_total_count, 5)
  assert.equal(harness.module.selectedKb.value.migration_success_count, 5)
  assert.equal(harness.module.migrationProgress.value, 100)
  assert.equal(harness.activeTimers().length, 0)
})

test('ignores stale migration responses after close, switching KB, unmount, or identity change', async t => {
  for (const action of ['close', 'switch', 'unmount', 'identity']) {
    const stale = deferred()
    const firstRow = {
      id: 'kb-stale',
      name: 'Stale KB',
      migration_status: 'succeeded',
      migration_total_count: 5,
      migration_success_count: 5,
    }
    const secondRow = {
      id: 'kb-current',
      name: 'Current KB',
      migration_status: 'succeeded',
      migration_total_count: 5,
      migration_success_count: 5,
    }
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      currentUsername: 'Alice',
      get: id => id === firstRow.id
        ? stale.promise
        : Promise.resolve(dataResponse({ ...secondRow, id })),
    })
    const pending = harness.module.showMigrationDialog(firstRow)
    assert.deepEqual(harness.api.gets.map(request => request.id), [firstRow.id])

    if (action === 'close') {
      harness.module.closeMigrationDialog()
    } else if (action === 'switch') {
      await harness.module.showMigrationDialog(secondRow)
    } else if (action === 'unmount') {
      for (const callback of harness.unmountedCallbacks) callback()
    } else {
      harness.permissionsStore.currentUser = {
        uid: 'user-b',
        username: 'Bob',
        is_superuser: false,
      }
      await flushPromises()
    }

    const selectedBeforeStaleResponse = normalize(harness.module.selectedKb.value ?? null)
    stale.resolve(dataResponse({
      ...firstRow,
      migration_status: 'building',
      migration_total_count: 5,
      migration_success_count: 2,
    }))
    await pending
    await flushPromises()

    assert.deepEqual(normalize(harness.module.selectedKb.value ?? null), selectedBeforeStaleResponse, action)
    assert.equal(harness.activeTimers().length, 0, action)
    if (action === 'close') assert.equal(harness.module.migrationDialogVisible.value, false)
    if (action === 'switch') assert.equal(harness.module.selectedKb.value.id, secondRow.id)
  }
})
