import test from 'node:test'
import assert from 'node:assert/strict'

import {
  createHarness,
  dataResponse,
  normalize,
  deferred,
  flushPromises,
} from './knowledgeBaseManagementHarness.js'

test('clears list and migration state on identity changes, reloads page one, and ignores stale list/get responses', async t => {
  for (const scenario of [
    {
      superuser: true,
      currentUid: 'admin-user',
      nextUser: { uid: 'admin-user', username: 'Alice', is_superuser: false },
    },
    {
      currentUid: 'user-a',
      nextUser: { uid: 'user-b', username: 'Bob', is_superuser: false },
    },
  ]) {
    const oldList = deferred()
    const newList = deferred()
    const staleGet = deferred()
    const oldKb = {
      id: 'kb-old-scope',
      uid: scenario.currentUid,
      name: 'Old scope KB',
      knowledge_base_type: 'user',
      migration_status: 'building',
      target_embedding_channel_id: 9,
      target_embedding_model_id: 'old-target',
    }
    const newKb = {
      id: 'kb-new-scope',
      uid: scenario.nextUser.uid,
      name: 'New scope KB',
      knowledge_base_type: 'user',
    }
    let listCalls = 0
    let getCalls = 0
    const harness = await createHarness(t, {
      ...scenario,
      list: () => {
        listCalls += 1
        if (listCalls === 1) return Promise.resolve(dataResponse({
          items: [oldKb],
          total: 1,
          embedding_models: [{ channel_id: 1, model_id: 'old-model' }],
          repair_required_count: 2,
        }))
        return listCalls === 2 ? oldList.promise : newList.promise
      },
      get: () => {
        getCalls += 1
        return getCalls === 1
          ? Promise.resolve(dataResponse(oldKb))
          : staleGet.promise
      },
    })

    await harness.module.fetchData()
    harness.module.currentPage.value = 4
    const staleListLoad = harness.module.fetchData()
    await harness.module.showMigrationDialog(oldKb)
    const staleGetLoad = harness.module.refreshMigrationState()
    assert.equal(harness.activeTimers().length, 1)

    harness.permissionsStore.currentUser = scenario.nextUser

    assert.deepEqual(normalize(harness.module.tableData.value), [])
    assert.equal(harness.module.total.value, 0)
    assert.deepEqual(normalize(harness.module.embeddingModels.value), [])
    assert.equal(harness.module.selectedKb.value, null)
    assert.equal(harness.module.currentPage.value, 1)
    assert.equal(harness.module.migrationDialogVisible.value, false)
    assert.equal(harness.module.migrationTargetKey.value, '')
    assert.equal(harness.module.migrationStateError.value, null)
    assert.equal(harness.module.migrationStateLoading.value, false)
    assert.equal(harness.activeTimers().length, 0)
    assert.deepEqual(harness.api.lists.map(request => request.params.page), [1, 4, 1])

    newList.resolve(dataResponse({
      items: [newKb],
      total: 1,
      embedding_models: [{ channel_id: 2, model_id: 'new-model' }],
      repair_required_count: 0,
    }))
    await flushPromises()
    assert.deepEqual(harness.module.tableData.value.map(item => item.id), [newKb.id])
    assert.equal(harness.module.total.value, 1)
    assert.deepEqual(normalize(harness.module.embeddingModels.value), [
      { channel_id: 2, model_id: 'new-model' },
    ])

    staleGet.resolve(dataResponse({ ...oldKb, migration_status: 'succeeded' }))
    await staleGetLoad
    oldList.resolve(dataResponse({
      items: [{ ...oldKb, name: 'Late old KB' }],
      total: 1,
      embedding_models: [{ channel_id: 1, model_id: 'old-model' }],
      repair_required_count: 99,
    }))
    await staleListLoad
    await flushPromises()
    assert.deepEqual(harness.module.tableData.value.map(item => item.id), [newKb.id])
    assert.equal(harness.module.total.value, 1)
    assert.deepEqual(normalize(harness.module.embeddingModels.value), [
      { channel_id: 2, model_id: 'new-model' },
    ])
    assert.equal(harness.module.selectedKb.value, null)
    assert.equal(harness.activeTimers().length, 0)
  }
})

test('clears permissions without reloading and keeps cleared state after a failed refresh', async t => {
  const row = {
    id: 'kb-before-clear',
    name: 'Before clear',
    knowledge_base_type: 'user',
    target_embedding_channel_id: 2,
    target_embedding_model_id: 'target-model',
  }
  let listCalls = 0
  const harness = await createHarness(t, {
    list: () => {
      listCalls += 1
      if (listCalls === 1) return Promise.resolve(dataResponse({
        items: [row],
        total: 1,
        embedding_models: [{ channel_id: 1, model_id: 'old-model' }],
      }))
      return Promise.reject(new Error('refresh unavailable'))
    },
    get: () => Promise.reject(new Error('status unavailable')),
  })

  await harness.module.fetchData()
  harness.module.currentPage.value = 3
  await harness.module.showMigrationDialog(row)
  assert.notEqual(harness.module.migrationStateError.value, null)
  assert.equal(harness.activeTimers().length, 1)

  harness.permissionsStore.clear()
  assert.deepEqual(normalize(harness.module.tableData.value), [])
  assert.equal(harness.module.total.value, 0)
  assert.deepEqual(normalize(harness.module.embeddingModels.value), [])
  assert.equal(harness.module.selectedKb.value, null)
  assert.equal(harness.module.currentPage.value, 1)
  assert.equal(harness.module.migrationDialogVisible.value, false)
  assert.equal(harness.module.migrationTargetKey.value, '')
  assert.equal(harness.module.migrationStateError.value, null)
  assert.equal(harness.activeTimers().length, 0)
  assert.equal(harness.api.lists.length, 1)

  harness.permissionsStore.clear()
  assert.equal(harness.api.lists.length, 1)
  await harness.module.fetchData()
  assert.deepEqual(normalize(harness.module.tableData.value), [])
  assert.equal(harness.module.selectedKb.value, null)
  assert.equal(harness.module.total.value, 0)
  assert.deepEqual(normalize(harness.module.embeddingModels.value), [])
})

test('preserves knowledge-base view state when the same uid and role are replaced', async t => {
  const row = {
    id: 'kb-same-identity',
    uid: 'user-a',
    name: 'Same identity KB',
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
    target_embedding_channel_id: 2,
    target_embedding_model_id: 'target-model',
  }
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    list: () => Promise.resolve(dataResponse({
      items: [row],
      total: 1,
      embedding_models: [{ channel_id: 1, model_id: 'current-model' }],
    })),
    get: () => Promise.resolve(dataResponse(row)),
  })

  await harness.module.fetchData()
  harness.module.currentPage.value = 3
  await harness.module.showMigrationDialog(row)
  const before = {
    tableData: normalize(harness.module.tableData.value),
    total: harness.module.total.value,
    currentPage: harness.module.currentPage.value,
    embeddingModels: normalize(harness.module.embeddingModels.value),
    selectedKb: normalize(harness.module.selectedKb.value),
    migrationTargetKey: harness.module.migrationTargetKey.value,
    migrationDialogVisible: harness.module.migrationDialogVisible.value,
  }

  harness.permissionsStore.currentUser = {
    uid: 'user-a',
    username: 'Updated display name',
    is_superuser: false,
  }

  assert.deepEqual({
    tableData: normalize(harness.module.tableData.value),
    total: harness.module.total.value,
    currentPage: harness.module.currentPage.value,
    embeddingModels: normalize(harness.module.embeddingModels.value),
    selectedKb: normalize(harness.module.selectedKb.value),
    migrationTargetKey: harness.module.migrationTargetKey.value,
    migrationDialogVisible: harness.module.migrationDialogVisible.value,
  }, before)
  assert.equal(harness.api.lists.length, 1)
  assert.equal(harness.api.gets.length, 1)
})

test('clears loaded list, owner, repair, migration, and query state on scope changes', async t => {
  const oldKb = {
    id: 'kb-old-owner-scope',
    uid: 'admin-user',
    username: 'Administrator',
    name: 'Old owner knowledge base',
    description: 'Old description',
    knowledge_base_type: 'user',
    embedding_channel_id: 2,
    embedding_model_id: 'embedding-v2',
    migration_status: 'building',
    target_embedding_channel_id: 3,
    target_embedding_model_id: 'embedding-v3',
  }
  const users = [
    { uid: 'admin-user', username: 'Administrator' },
    { uid: 'owner-b', username: 'Bob' },
  ]

  for (const action of ['uid', 'role', 'clear', 'unmount']) {
    let listCalls = 0
    const confirmation = deferred()
    const harness = await createHarness(t, {
      superuser: true,
      currentUid: 'admin-user',
      currentUsername: 'Administrator',
      users,
      confirm: () => confirmation.promise,
      list: () => {
        listCalls += 1
        const items = listCalls === 1 ? [oldKb] : []
        return Promise.resolve(dataResponse({
          items,
          total: items.length,
          embedding_models: items.length
            ? [{ channel_id: 2, model_id: 'embedding-v2' }, { channel_id: 3, model_id: 'embedding-v3' }]
            : [],
          repair_required_count: items.length ? 5 : 0,
        }))
      },
      get: () => Promise.resolve(dataResponse(oldKb)),
      queryTest: () => Promise.resolve(dataResponse({
        items: [{ id: 'query-result-1', content: 'secret query result' }],
        retrieval_mode: 'hybrid',
        rerank_error: 'secret rerank diagnostic',
      })),
    })
    const module = harness.module
    module.formRef.value = { resetFields() {}, clearValidate() {} }
    module.queryTestFormRef.value = {
      clearValidate() {},
      validate: async callback => {
        if (callback) await callback(true)
      },
    }

    await module.fetchData()
    await module.showDialog()
    assert.deepEqual(normalize(module.ownerUsers.value), users)
    module.showEditDialog(oldKb)
    await module.showRepairDialog()
    await module.showMigrationDialog(oldKb)
    module.showQueryTestDialog(oldKb)
    module.queryTestForm.query = 'secret query text'
    module.queryTestForm.top_k = 7
    await module.submitQueryTest()
    const pendingDelete = module.handleDelete(oldKb)
    await flushPromises()

    const dialogs = [
      module.dialogVisible,
      module.repairDialogVisible,
      module.migrationDialogVisible,
      module.queryTestDialogVisible,
    ]
    assert.deepEqual(dialogs.map(dialog => dialog.value), Array(4).fill(true), action)
    assert.equal(harness.confirms.length, 1, action)
    assert.equal(module.selectedKb.value.id, oldKb.id)
    assert.equal(module.total.value, 1)
    assert.equal(module.repairRequiredCount.value, 5)
    assert.equal(module.form.name, oldKb.name)
    assert.equal(module.editingId.value, oldKb.id)
    assert.deepEqual(normalize(module.ownerUsers.value), [users[0]])
    assert.deepEqual(normalize(module.repairOwners.value), users)
    assert.equal(module.queryTestForm.query, 'secret query text')
    assert.equal(module.queryTestForm.top_k, 7)
    assert.equal(module.queryTestResults.value[0].content, 'secret query result')
    assert.equal(module.retrievalMode.value, 'hybrid')
    assert.equal(module.rerankError.value, 'secret rerank diagnostic')
    assert.equal(module.queryTested.value, true)
    assert.ok(harness.activeTimers().length > 0, action)

    const assertCleared = () => {
      assert.deepEqual(dialogs.map(dialog => dialog.value), Array(4).fill(false), action)
      assert.equal(module.selectedKb.value, null, action)
      assert.deepEqual(normalize(module.tableData.value), [], action)
      assert.deepEqual(normalize(module.embeddingModels.value), [], action)
      assert.equal(module.total.value, 0, action)
      assert.equal(module.currentPage.value, 1, action)
      assert.equal(module.repairRequiredCount.value, 0, action)
      assert.deepEqual(normalize(module.ownerUsers.value), [], action)
      assert.equal(module.ownerUsersLoading.value, false, action)
      assert.equal(module.ownerUsersError.value, null, action)
      assert.deepEqual(normalize(module.repairOwners.value), [], action)
      assert.equal(module.repairOwnersLoading.value, false, action)
      assert.equal(module.repairOwnersError.value, null, action)
      assert.equal(module.repairOwnerUid.value, null, action)
      assert.equal(module.repairSubmitting.value, false, action)
      assert.equal(module.form.name, '', action)
      assert.equal(module.form.description, '', action)
      assert.equal(module.form.embedding_model_key, '', action)
      assert.equal(module.form.uid, null, action)
      assert.equal(module.form.username, '', action)
      assert.equal(module.isEditing.value, false, action)
      assert.equal(module.editingId.value, null, action)
      assert.equal(module.migrationTargetKey.value, '', action)
      assert.equal(module.migrationSubmitting.value, false, action)
      assert.equal(module.migrationStateLoading.value, false, action)
      assert.equal(module.migrationStateError.value, null, action)
      assert.deepEqual(normalize(module.queryTestForm), { query: '', top_k: 5 }, action)
      assert.deepEqual(normalize(module.queryTestResults.value), [], action)
      assert.equal(module.retrievalMode.value, null, action)
      assert.equal(module.rerankError.value, null, action)
      assert.equal(module.queryTested.value, false, action)
      assert.equal(module.queryTesting.value, false, action)
      assert.equal(harness.activeTimers().length, 0, action)
      assert.equal(harness.closedConfirmations, 1, action)
      assert.equal(harness.api.deletes.length, 0, action)
    }

    if (action === 'uid') {
      harness.permissionsStore.currentUser = {
        uid: 'user-b',
        username: 'Bob',
        is_superuser: true,
      }
    } else if (action === 'role') {
      harness.permissionsStore.currentUser = {
        uid: 'admin-user',
        username: 'Administrator',
        is_superuser: false,
      }
    } else if (action === 'clear') {
      harness.permissionsStore.clear()
    } else {
      for (const callback of harness.unmountedCallbacks) callback()
    }

    assertCleared()
    await pendingDelete
    confirmation.resolve(true)
    await flushPromises()
    assert.equal(harness.api.deletes.length, 0, action)
    if (action === 'clear') {
      harness.permissionsStore.clear()
      assertCleared()
    } else if (action === 'unmount') {
      for (const callback of harness.unmountedCallbacks) callback()
      assertCleared()
      harness.permissionsStore.clear()
      for (const callback of harness.unmountedCallbacks) callback()
      assertCleared()
    }

    if (action === 'uid' || action === 'role') {
      assert.equal(harness.api.lists.length, 2, action)
      await flushPromises()
      assert.equal(module.tableData.value.length, 0, action)
      assert.equal(module.embeddingModels.value.length, 0, action)
      assert.equal(module.total.value, 0, action)
      assert.equal(module.repairRequiredCount.value, 0, action)
    } else {
      assert.equal(harness.api.lists.length, 1, action)
    }
  }
})

test('ignores late query-test success or failure from the previous identity', async t => {
  for (const outcome of ['success', 'failure']) {
    const oldRequest = deferred()
    const freshRequest = deferred()
    const oldKb = { id: 'kb-query-old', name: 'Old query KB' }
    const freshKb = { id: 'kb-query-new', name: 'New query KB' }
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      queryTest: () => harness.api.queryTest.length === 1
        ? oldRequest.promise
        : freshRequest.promise,
    })
    const module = harness.module
    module.queryTestFormRef.value = {
      clearValidate() {},
      validate: async callback => callback(true),
    }
    const startQuery = (kb, query) => {
      module.showQueryTestDialog(kb)
      module.queryTestForm.query = query
      module.queryTestForm.top_k = 6
      return module.submitQueryTest()
    }

    const oldQuery = startQuery(oldKb, 'old scope query')
    await flushPromises()
    assert.equal(harness.api.queryTest.length, 1)
    assert.deepEqual(harness.api.queryTest.map(request => request.id), [oldKb.id])
    assert.deepEqual(harness.api.queryTest[0].params, { query: 'old scope query', top_k: 6 })

    harness.permissionsStore.currentUser = { uid: 'user-b', username: 'Bob', is_superuser: false }
    await flushPromises()
    assert.equal(module.selectedKb.value, null, `${outcome}: reset selection`)
    assert.deepEqual(normalize(module.queryTestResults.value), [], `${outcome}: reset results`)
    assert.equal(module.queryTested.value, false, `${outcome}: reset completion`)

    const freshQuery = startQuery(freshKb, 'fresh scope query')
    await flushPromises()
    assert.equal(harness.api.queryTest.length, 2)
    assert.deepEqual(harness.api.queryTest.map(request => request.id), [oldKb.id, freshKb.id])
    assert.deepEqual(harness.api.queryTest[1].params, { query: 'fresh scope query', top_k: 6 })
    const freshState = normalize({
      selectedKb: module.selectedKb.value,
      query: module.queryTestForm.query,
      results: module.queryTestResults.value,
      retrievalMode: module.retrievalMode.value,
      rerankError: module.rerankError.value,
      tested: module.queryTested.value,
      testing: module.queryTesting.value,
    })

    if (outcome === 'success') {
      oldRequest.resolve(dataResponse({
        items: [{ id: 'old-result', content: 'old private result' }],
        retrieval_mode: 'vector',
        rerank_error: 'old diagnostic',
      }))
    } else {
      oldRequest.reject(new Error('old scope query failed'))
    }
    await oldQuery
    await flushPromises()

    assert.deepEqual(normalize({
      selectedKb: module.selectedKb.value,
      query: module.queryTestForm.query,
      results: module.queryTestResults.value,
      retrievalMode: module.retrievalMode.value,
      rerankError: module.rerankError.value,
      tested: module.queryTested.value,
      testing: module.queryTesting.value,
    }), freshState, `${outcome}: old result leaves fresh query unchanged`)
    assert.deepEqual(harness.messages, [], `${outcome}: stale message`)

    freshRequest.resolve(dataResponse({
      items: [{ id: 'fresh-result', content: 'fresh private result' }],
      retrieval_mode: 'hybrid',
      rerank_error: null,
    }))
    await freshQuery
    assert.equal(module.selectedKb.value.id, freshKb.id, `${outcome}: selected KB`)
    assert.deepEqual(normalize(module.queryTestResults.value), [
      { id: 'fresh-result', content: 'fresh private result' },
    ], `${outcome}: result`)
    assert.equal(module.retrievalMode.value, 'hybrid', `${outcome}: retrieval mode`)
    assert.equal(module.rerankError.value, null, `${outcome}: rerank error`)
    assert.equal(module.queryTested.value, true, `${outcome}: completion`)
    assert.equal(module.queryTesting.value, false, `${outcome}: loading state`)
    assert.deepEqual(harness.messages, [], `${outcome}: final messages`)
  }
})

test('closes and invalidates pending confirmations on identity changes', async t => {
  const userKb = {
    id: 'kb-confirm-user',
    uid: 'owner-user',
    username: 'Owner',
    name: 'User KB',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
    migration_status: 'succeeded',
  }
  const cases = [
    { api: 'deletes', kb: userKb, start: module => module.handleDelete(userKb) },
    { api: 'repairs', kb: userKb, start: module => module.repairKnowledgeBase(userKb) },
    {
      api: 'migrations',
      kb: userKb,
      prepare: async module => {
        await module.showMigrationDialog(userKb)
        module.embeddingModels.value = [{ channel_id: 2, model_id: 'target-model' }]
        module.migrationTargetKey.value = '2::target-model'
      },
      start: module => module.submitEmbeddingMigration(),
    },
  ]

  for (const scenario of cases) {
    for (const identityChange of ['role', 'clear']) {
      for (const outcome of ['resolve', 'reject']) {
        const confirmation = deferred()
        const harness = await createHarness(t, {
          superuser: true,
          currentUid: 'admin-user',
          confirm: () => confirmation.promise,
          list: () => Promise.resolve(dataResponse({
            items: [userKb],
            total: 1,
            embedding_models: [{ channel_id: 2, model_id: 'target-model' }],
            repair_required_count: 0,
          })),
          get: id => Promise.resolve(dataResponse({ ...userKb, id, migration_status: 'succeeded' })),
        })
        harness.module.selectedKb.value = scenario.kb
        if (scenario.prepare) await scenario.prepare(harness.module)

        const operation = scenario.start(harness.module)
        const label = `${scenario.api}:${identityChange}:${outcome}`
        await flushPromises()
        assert.equal(harness.confirms.length, 1, label)
        assert.equal(harness.api[scenario.api].length, 0, label)

        if (identityChange === 'role') {
          harness.permissionsStore.currentUser = {
            uid: 'admin-user',
            username: 'Administrator',
            is_superuser: false,
          }
        } else {
          harness.permissionsStore.clear()
        }
        assert.equal(harness.closedConfirmations, 1, label)
        if (identityChange === 'clear') {
          harness.permissionsStore.clear()
          assert.equal(harness.closedConfirmations, 1, `${label}: repeated clear`)
        }

        await operation
        assert.equal(harness.api[scenario.api].length, 0, `${label}: after cancellation`)
        assert.deepEqual(harness.messages, [], `${label}: messages after cancellation`)
        assert.equal(harness.activeTimers().length, 0, `${label}: timers after cancellation`)

        if (outcome === 'resolve') confirmation.resolve(true)
        else confirmation.reject(new Error('old-secret confirmation failure'))
        await flushPromises()

        assert.equal(harness.api[scenario.api].length, 0, `${label}: late confirmation`)
        assert.deepEqual(harness.messages, [], `${label}: late messages`)
        assert.equal(harness.activeTimers().length, 0, `${label}: late timers`)
        assert.equal(harness.closedConfirmations, 1, `${label}: close count`)
      }
    }
  }
})

test('ignores pending form validation after switching users', async t => {
  const userKbA = {
    id: 'kb-user-a-validation', uid: 'user-a', name: 'Alice KB', description: 'Alice description',
    knowledge_base_type: 'user', embedding_channel_id: 1, embedding_model_id: 'embedding-model',
  }
  const userKbB = {
    id: 'kb-user-b-validation', uid: 'user-b', name: 'Bob KB', description: 'Bob description',
    knowledge_base_type: 'user', embedding_channel_id: 1, embedding_model_id: 'embedding-model',
  }
  const cases = [
    {
      api: 'creates', method: 'create', ref: 'formRef',
      open: module => module.showDialog(),
      fill: (module, value) => {
        module.form.name = `${value} name`
        module.form.description = `${value} description`
        module.form.embedding_model_key = '1::embedding-model'
      },
      busy: module => module.submitting.value,
      dialogs: module => [module.dialogVisible.value], completedDialogs: [false],
      snapshot: module => ({ name: module.form.name, description: module.form.description, dialog: module.dialogVisible.value, busy: module.submitting.value }),
      assertRequest: request => {
        assert.equal(Object.hasOwn(request, 'id'), false)
        assert.equal(normalize(request.payload).name, 'fresh name')
      },
      result: { id: 'created-fresh-kb' },
    },
    {
      api: 'updates', method: 'update', ref: 'formRef', oldTarget: userKbA, freshTarget: userKbB,
      open: (module, kb) => module.showEditDialog(kb),
      fill: (module, value) => { module.form.name = `${value} name`; module.form.description = `${value} description` },
      busy: module => module.submitting.value,
      dialogs: module => [module.dialogVisible.value], completedDialogs: [false],
      snapshot: module => ({ id: module.editingId.value, name: module.form.name, description: module.form.description, dialog: module.dialogVisible.value, busy: module.submitting.value }),
      assertRequest: request => {
        assert.equal(request.id, userKbB.id)
        assert.deepEqual(normalize(request.payload), { name: 'fresh name', description: 'fresh description' })
      },
      result: { id: userKbB.id },
    },
  ]

  const installValidationRef = (module, refName, validate) => {
    const formRef = { resetFields() {}, clearValidate() {}, validateField() {}, validate }
    module[refName].value = formRef
    return formRef
  }
  const hasSuccessMessage = messages => messages.some(message => (
    message.type === 'success' || message.args?.some(argument => argument?.type === 'success')
  ))

  for (const scenario of cases) {
    const oldValidation = deferred()
    const freshApi = deferred()
    let validationCalls = 0
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      currentUsername: 'Alice',
      superuser: false,
      [scenario.method]: () => freshApi.promise,
    })
    const formRef = installValidationRef(harness.module, scenario.ref, () => {
      validationCalls += 1
      return oldValidation.promise
    })

    await scenario.open(harness.module, scenario.oldTarget)
    scenario.fill(harness.module, 'old')
    const oldSubmit = harness.module.submitForm()
    await flushPromises()
    assert.equal(validationCalls, 1, scenario.api)
    assert.equal(scenario.busy(harness.module), true, scenario.api)
    assert.equal(harness.api[scenario.api].length, 0, scenario.api)

    harness.permissionsStore.currentUser = { uid: 'user-b', username: 'Bob', is_superuser: false }
    await flushPromises()
    formRef.validate = () => Promise.resolve(true)
    await scenario.open(harness.module, scenario.freshTarget)
    scenario.fill(harness.module, 'fresh')
    const freshSubmit = harness.module.submitForm()
    await flushPromises()

    assert.equal(harness.api[scenario.api].length, 1, scenario.api)
    await scenario.assertRequest(harness.api[scenario.api][0])
    assert.equal(scenario.busy(harness.module), true, `${scenario.api}: fresh busy`)
    assert.ok(scenario.dialogs(harness.module).every(Boolean), `${scenario.api}: fresh dialogs`)
    const freshSnapshot = normalize(scenario.snapshot(harness.module))

    oldValidation.resolve(true)
    await oldSubmit
    await flushPromises()

    assert.equal(harness.api[scenario.api].length, 1, `${scenario.api}: stale validation`)
    assert.equal(scenario.busy(harness.module), true, `${scenario.api}: busy after stale validation`)
    assert.ok(scenario.dialogs(harness.module).every(Boolean), `${scenario.api}: dialogs after stale validation`)
    assert.deepEqual(normalize(scenario.snapshot(harness.module)), freshSnapshot, `${scenario.api}: fresh form preserved`)
    assert.deepEqual(harness.messages, [], `${scenario.api}: stale messages`)

    freshApi.resolve(dataResponse(scenario.result))
    await freshSubmit
    assert.equal(scenario.busy(harness.module), false, `${scenario.api}: completed busy`)
    assert.deepEqual(scenario.dialogs(harness.module), scenario.completedDialogs, `${scenario.api}: completed dialogs`)
    assert.ok(hasSuccessMessage(harness.messages), `${scenario.api}: success feedback`)
  }
})

test('keeps new-identity mutations isolated from late old mutation results', async t => {
  const userKbA = { id: 'kb-user-a-mutation', uid: 'user-a', username: 'Alice', name: 'Alice KB', description: 'Alice description', knowledge_base_type: 'user', embedding_channel_id: 1, embedding_model_id: 'embedding-model' }
  const userKbB = { id: 'kb-user-b-mutation', uid: 'user-b', username: 'Bob', name: 'Bob KB', description: 'Bob description', knowledge_base_type: 'user', embedding_channel_id: 1, embedding_model_id: 'embedding-model' }
  const targets = {
    create: [{ label: 'old Alice' }, { label: 'fresh Bob' }],
    update: [{ kb: userKbA, label: 'old Alice' }, { kb: userKbB, label: 'fresh Bob' }],
    delete: [{ kb: userKbA }, { kb: userKbB }],
  }
  const rootForm = (module, target) => {
    module.form.name = `${target.label} knowledge`
    module.form.description = `${target.label} description`
    module.form.embedding_model_key = '1::embedding-model'
  }
  const cases = [
    {
      key: 'create', array: 'creates', method: 'create', ref: 'formRef', busy: module => module.submitting.value,
      open: module => module.showDialog(), fill: rootForm, submit: module => module.submitForm(), refresh: 'lists',
      assertRequest: (request, target) => {
        assert.equal(Object.hasOwn(request, 'id'), false)
        assert.deepEqual(normalize(request.payload), {
          name: `${target.label} knowledge`, description: `${target.label} description`,
          embedding_channel_id: 1, embedding_model_id: 'embedding-model',
        })
      }, result: {},
    },
    {
      key: 'update', array: 'updates', method: 'update', ref: 'formRef', busy: module => module.submitting.value,
      open: (module, target) => module.showEditDialog(target.kb), fill: rootForm, submit: module => module.submitForm(), refresh: 'lists',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.deepEqual(normalize(request.payload), { name: `${target.label} knowledge`, description: `${target.label} description` })
      }, result: {},
    },
    {
      key: 'delete', array: 'deletes', method: 'delete', open: async () => {},
      submit: (module, target) => module.handleDelete(target.kb), refresh: 'lists',
      assertRequest: (request, target) => assert.equal(request.id, target.kb.id), result: {},
    },
  ]
  const snapshot = harness => {
    const module = harness.module
    return normalize({
      selectedKb: module.selectedKb.value,
      dialogs: [module.dialogVisible.value],
      root: { loading: module.loading.value, table: module.tableData.value, total: module.total.value, submitting: module.submitting.value, editing: module.editingId.value, form: { ...module.form } },
      timers: harness.activeTimers().length,
    })
  }
  const successMessage = messages => messages.some(message => (
    message.type === 'success' || message.args?.some(argument => argument?.type === 'success')
  ))

  for (const scenario of cases) {
    for (const oldOutcome of ['success', 'failure']) {
      const [oldTarget, freshTarget] = targets[scenario.key]
      const oldMutation = deferred()
      const freshMutation = deferred()
      let mutationCalls = 0
      const harness = await createHarness(t, {
        currentUid: 'user-a',
        currentUsername: 'Alice',
        superuser: false,
        [scenario.method]: () => {
          mutationCalls += 1
          return mutationCalls === 1 ? oldMutation.promise : freshMutation.promise
        },
      })
      if (scenario.ref) {
        harness.module[scenario.ref].value = { resetFields() {}, clearValidate() {}, validateField() {}, validate: () => Promise.resolve(true) }
      }

      await scenario.open(harness.module, oldTarget)
      scenario.fill?.(harness.module, oldTarget)
      const oldSubmit = scenario.submit(harness.module, oldTarget)
      await flushPromises()
      assert.equal(harness.api[scenario.array].length, 1, `${scenario.key}: old request`)
      await scenario.assertRequest(harness.api[scenario.array][0], oldTarget)

      harness.permissionsStore.currentUser = { uid: 'user-b', username: 'Bob', is_superuser: false }
      await flushPromises()
      await scenario.open(harness.module, freshTarget)
      scenario.fill?.(harness.module, freshTarget)
      const freshSubmit = scenario.submit(harness.module, freshTarget)
      await flushPromises()

      assert.equal(harness.api[scenario.array].length, 2, `${scenario.key}: fresh request`)
      await scenario.assertRequest(harness.api[scenario.array][1], freshTarget)
      if (scenario.busy) assert.equal(scenario.busy(harness.module, freshTarget), true, `${scenario.key}: fresh busy`)
      const freshSnapshot = snapshot(harness)
      const requestCount = harness.api.requests.length
      const messageBaseline = normalize(harness.messages)
      const refreshCount = scenario.refresh ? harness.api[scenario.refresh].length : null
      const result = scenario.result

      if (oldOutcome === 'success') oldMutation.resolve(dataResponse(result))
      else oldMutation.reject(new Error('old-secret failure'))
      await oldSubmit
      await flushPromises()

      assert.deepEqual(snapshot(harness), freshSnapshot, `${scenario.key}:${oldOutcome}:new UI`)
      assert.equal(harness.api[scenario.array].length, 2, `${scenario.key}:${oldOutcome}:mutation count`)
      assert.equal(harness.api.requests.length, requestCount, `${scenario.key}:${oldOutcome}:request count`)
      assert.deepEqual(normalize(harness.messages), messageBaseline, `${scenario.key}:${oldOutcome}:messages`)
      if (scenario.busy) assert.equal(scenario.busy(harness.module, freshTarget), true, `${scenario.key}:${oldOutcome}:fresh busy`)

      freshMutation.resolve(dataResponse(result))
      await freshSubmit
      if (scenario.busy) assert.equal(scenario.busy(harness.module, freshTarget), false, `${scenario.key}: completed busy`)
      assert.ok(successMessage(harness.messages), `${scenario.key}: success feedback`)
      if (scenario.refresh) {
        assert.ok(harness.api[scenario.refresh].length > refreshCount, `${scenario.key}: refresh`)
      }
    }
  }
})
