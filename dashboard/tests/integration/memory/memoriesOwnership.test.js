import assert from 'node:assert/strict'
import test from 'node:test'
import { renderToString } from 'vue/server-renderer'
import enCommon from '../../../src/i18n/locales/en/common.js'
import enMemories from '../../../src/i18n/locales/en/memories.js'

import {
  confirmationTranslate,
  createDeferred,
  createDetailHarness,
  createHarness,
  dataResponse,
  detailCases,
  detailRow,
  fillEditor,
  lastRequest,
  normalize,
  pageResponse,
} from './memoriesViewHarness.js'

const ownershipRoleCases = [
  {
    name: 'ordinary owner',
    superuser: false,
    currentUid: 'user-a',
    ownerUid: 'user-a',
  },
  {
    name: 'administrator external owner',
    superuser: true,
    currentUid: 'admin-user',
    ownerUid: 'user-b',
  },
]

const createOwnershipRows = ownerUid => {
  const withOwner = row => ownerUid === undefined ? row : { ...row, owner_uid: ownerUid }
  return {
    memory: withOwner({
      id: 'ownership-memory',
      version: 1,
      is_active: true,
      memory_key: 'ownership-key',
      memory_type: 'fact',
      content: 'ownership-content',
      change_evidence: 'ownership-evidence',
      pinned: false,
    }),
    deletedHistory: withOwner({
      id: 'ownership-delete-cleanup',
      operation: 'delete_cleanup',
      memory_id: 'ownership-deleted-memory',
      result: {
        record_snapshot: {
          version: 1,
          memory_key: 'deleted-key',
          memory_type: 'fact',
          content: 'deleted-content',
        },
      },
    }),
    resume: withOwner({
      id: 'ownership-resume',
      version: 1,
      is_active: true,
      suppress_recall: true,
    }),
    retryJob: withOwner({
      id: 'ownership-retry-job',
      operation: 'create',
      status: 'failed',
    }),
    cancelJob: withOwner({
      id: 'ownership-cancel-job',
      operation: 'create',
      status: 'pending',
    }),
    retryMigration: withOwner({
      id: 'ownership-retry-migration',
      status: 'failed',
    }),
    cancelMigration: withOwner({
      id: 'ownership-cancel-migration',
      status: 'building',
    }),
  }
}

const createOwnershipHarness = (t, role) => createHarness(t, {
  superuser: role.superuser,
  currentUid: role.currentUid,
  apiOverrides: {
    memoryApi: {
      get: id => dataResponse({ id, owner_uid: role.ownerUid }),
      history: id => pageResponse([{ id: `history-${id}`, version: 1 }], 1),
      job: id => dataResponse({ id, owner_uid: role.ownerUid }),
      migration: id => dataResponse({ id, owner_uid: role.ownerUid }),
    },
  },
})

test('allows owner and administrator row actions with the correct target ids', async t => {
  for (const role of ownershipRoleCases) {
    const harness = createOwnershipHarness(t, role)
    const rows = createOwnershipRows(role.ownerUid)
    await harness.mount()

    await harness.module.showDetails(rows.memory)
    assert.equal(harness.calls('memoryApi.get').at(-1).args[0], rows.memory.id, role.name)
    assert.equal(harness.module.selectedMemory.value.id, rows.memory.id, role.name)
    assert.equal(harness.module.selectedMemory.value.owner_uid, role.ownerUid, role.name)
    assert.equal(harness.module.detailsVisible.value, true, role.name)

    await harness.module.showHistory(rows.memory)
    assert.equal(harness.calls('memoryApi.history').at(-1).args[0], rows.memory.id, role.name)
    assert.deepEqual(normalize(harness.calls('memoryApi.history').at(-1).args[1]), { page: 1, size: 100 }, role.name)
    assert.equal(harness.module.selectedHistoryMemory.value.id, rows.memory.id, role.name)
    assert.equal(harness.module.historyVisible.value, true, role.name)
    assert.equal(harness.module.history.value.length, 1, role.name)

    await harness.module.showDeletedHistory(rows.deletedHistory)
    assert.equal(harness.calls('memoryApi.history').at(-1).args[0], rows.deletedHistory.memory_id, role.name)
    assert.equal(harness.module.selectedHistoryMemory.value.id, rows.deletedHistory.memory_id, role.name)
    assert.equal(harness.module.selectedHistoryMemory.value.owner_uid, role.ownerUid, role.name)
    assert.equal(harness.module.historyVisible.value, true, role.name)

    await harness.module.showJob(rows.retryJob)
    assert.equal(harness.calls('memoryApi.job').at(-1).args[0], rows.retryJob.id, role.name)
    assert.equal(harness.module.selectedJob.value.id, rows.retryJob.id, role.name)
    assert.equal(harness.module.selectedJob.value.owner_uid, role.ownerUid, role.name)
    assert.equal(harness.module.jobVisible.value, true, role.name)

    await harness.module.showMigration(rows.retryMigration)
    assert.equal(harness.calls('memoryApi.migration').at(-1).args[0], rows.retryMigration.id, role.name)
    assert.equal(harness.module.selectedMigration.value.id, rows.retryMigration.id, role.name)
    assert.equal(harness.module.selectedMigration.value.owner_uid, role.ownerUid, role.name)
    assert.equal(harness.module.migrationVisible.value, true, role.name)

    harness.module.openEditor(rows.memory)
    assert.equal(harness.module.editorVisible.value, true, role.name)
    harness.module.form.content = `${role.name}-updated`
    await harness.module.submitMemory()
    const updatePayload = harness.calls('memoryApi.update').at(-1).args[0]
    assert.equal(updatePayload.memory_id, rows.memory.id, role.name)
    assert.equal(updatePayload.expected_version, rows.memory.version, role.name)

    await harness.module.deleteMemory(rows.memory)
    const deletePayload = harness.calls('memoryApi.delete').at(-1).args[0]
    assert.equal(deletePayload.memory_id, rows.memory.id, role.name)
    assert.equal(deletePayload.expected_version, rows.memory.version, role.name)

    await harness.module.togglePin({ ...rows.memory, pinned: false })
    await harness.module.togglePin({ ...rows.memory, pinned: true })
    assert.equal(harness.calls('memoryApi.pin').at(-1).args[0], rows.memory.id, role.name)
    assert.equal(harness.calls('memoryApi.unpin').at(-1).args[0], rows.memory.id, role.name)

    await harness.module.resumeCurrent(rows.resume)
    assert.deepEqual(normalize(harness.calls('memoryApi.resumeCurrent').at(-1).args), [rows.resume.id, { expected_version: 1 }], role.name)

    await harness.module.retryJob(rows.retryJob)
    await harness.module.cancelJob(rows.cancelJob)
    assert.equal(harness.calls('memoryApi.retryJob').at(-1).args[0], rows.retryJob.id, role.name)
    assert.equal(harness.calls('memoryApi.cancelJob').at(-1).args[0], rows.cancelJob.id, role.name)

    await harness.module.retryMigration(rows.retryMigration)
    await harness.module.cancelMigration(rows.cancelMigration)
    assert.equal(harness.calls('memoryApi.retryMigration').at(-1).args[0], rows.retryMigration.id, role.name)
    assert.equal(harness.calls('memoryApi.cancelMigration').at(-1).args[0], rows.cancelMigration.id, role.name)
  }
})

const ownershipProtectedApiNames = [
  'memoryApi.get',
  'memoryApi.history',
  'memoryApi.job',
  'memoryApi.migration',
  'memoryApi.create',
  'memoryApi.update',
  'memoryApi.delete',
  'memoryApi.pin',
  'memoryApi.unpin',
  'memoryApi.resumeCurrent',
  'memoryApi.retryJob',
  'memoryApi.cancelJob',
  'memoryApi.retryMigration',
  'memoryApi.cancelMigration',
]

const assertNoOwnershipActionRequests = (harness, label) => {
  for (const name of ownershipProtectedApiNames) assert.equal(harness.calls(name).length, 0, `${label}:${name}`)
  assert.equal(harness.confirmCalls.length, 0, `${label}:confirm`)
  assert.equal(harness.module.editorVisible.value, false, `${label}:editor`)
  assert.equal(harness.module.detailsVisible.value, false, `${label}:details`)
  assert.equal(harness.module.historyVisible.value, false, `${label}:history`)
  assert.equal(harness.module.jobVisible.value, false, `${label}:job`)
  assert.equal(harness.module.migrationVisible.value, false, `${label}:migration`)
}

const exerciseBlockedOwnershipEntries = async (harness, rows, label) => {
  const entries = [
    ['details', () => harness.module.showDetails(rows.memory)],
    ['history', () => harness.module.showHistory(rows.memory)],
    ['deleted history', () => harness.module.showDeletedHistory(rows.deletedHistory)],
    ['job details', () => harness.module.showJob(rows.retryJob)],
    ['migration details', () => harness.module.showMigration(rows.retryMigration)],
    ['open editor', () => harness.module.openEditor(rows.memory)],
    ['delete', () => harness.module.deleteMemory(rows.memory)],
    ['pin', () => harness.module.togglePin(rows.memory)],
    ['resume', () => harness.module.resumeCurrent(rows.resume)],
    ['retry job', () => harness.module.retryJob(rows.retryJob)],
    ['cancel job', () => harness.module.cancelJob(rows.cancelJob)],
    ['retry migration', () => harness.module.retryMigration(rows.retryMigration)],
    ['cancel migration', () => harness.module.cancelMigration(rows.cancelMigration)],
    ['menu history', () => harness.module.handleMemoryMoreAction('history', rows.memory)],
    ['menu pin', () => harness.module.handleMemoryMoreAction('toggle-pin', rows.memory)],
    ['menu delete', () => harness.module.handleMemoryMoreAction('delete', rows.memory)],
  ]
  for (const [name, invoke] of entries) {
    await invoke()
    await harness.flush()
    assertNoOwnershipActionRequests(harness, `${label}:${name}`)
  }

  harness.module.selectedMemory.value = rows.memory
  harness.module.detailsVisible.value = true
  harness.module.editSelectedMemory()
  assert.equal(harness.module.editorVisible.value, false, `${label}:selected edit`)
  harness.module.detailsVisible.value = false

  Object.assign(harness.module.form, {
    id: rows.memory.id,
    version: rows.memory.version,
    owner_uid: rows.memory.owner_uid,
    memory_key: rows.memory.memory_key,
    memory_type: rows.memory.memory_type,
    content: rows.memory.content,
    change_evidence: rows.memory.change_evidence,
  })
  harness.module.editorMode.value = 'edit'
  harness.module.editorVisible.value = true
  await harness.module.submitMemory()
  harness.module.editorVisible.value = false
  assertNoOwnershipActionRequests(harness, `${label}:submit update`)
}

const assertOwnershipGuards = (harness, rows, label) => {
  assert.equal(harness.module.canAccessMemoryData(rows.memory), false, `${label}:access`)
  assert.equal(harness.module.canMutateRecord(rows.memory), false, `${label}:mutate`)
  assert.equal(harness.module.canPin(rows.memory), false, `${label}:pin`)
  assert.equal(harness.module.canResume(rows.resume), false, `${label}:resume`)
  assert.equal(harness.module.canShowDeletedHistory(rows.deletedHistory), false, `${label}:deleted history`)
  assert.equal(harness.module.canRetry(rows.retryJob), false, `${label}:retry job`)
  assert.equal(harness.module.canCancel(rows.cancelJob), false, `${label}:cancel job`)
  assert.equal(harness.module.canRetryMigration(rows.retryMigration), false, `${label}:retry migration`)
  assert.equal(harness.module.canCancelMigration(rows.cancelMigration), false, `${label}:cancel migration`)
}

test('blocks ordinary-user foreign or ownerless row actions and unavailable scopes', async t => {
  for (const ownerUid of ['user-b', undefined]) {
    const role = { superuser: false, currentUid: 'user-a', ownerUid: 'user-a' }
    const harness = createOwnershipHarness(t, role)
    const rows = createOwnershipRows(ownerUid)
    await harness.mount()

    assertOwnershipGuards(harness, rows, ownerUid ?? 'missing owner')
    await exerciseBlockedOwnershipEntries(harness, rows, ownerUid ?? 'missing owner')
  }

  for (const lifecycleCase of ['scope not ready', 'unmounted']) {
    const role = { superuser: false, currentUid: 'user-a', ownerUid: 'user-a' }
    const harness = createOwnershipHarness(t, role)
    const rows = createOwnershipRows(role.ownerUid)
    await harness.mount()
    if (lifecycleCase === 'scope not ready') harness.module.memoryScopeReady.value = false
    else harness.unmount()

    assertOwnershipGuards(harness, rows, lifecycleCase)
    await exerciseBlockedOwnershipEntries(harness, rows, lifecycleCase)
  }
})

const delayedOwnershipMutationCases = [
  {
    name: 'delete',
    row: 'memory',
    request: 'memoryApi.delete',
    confirmationKey: 'delete_confirm',
    successKey: 'delete_success',
    invoke: (module, row) => module.deleteMemory(row),
    assertRequest: (request, row) => {
      assert.equal(request.args[0].memory_id, row.id)
      assert.equal(request.args[0].expected_version, row.version)
      assert.equal(typeof request.args[0].dedupe_key, 'string')
      assert.ok(request.args[0].dedupe_key.length > 0)
    },
  },
  {
    name: 'retry job',
    row: 'retryJob',
    request: 'memoryApi.retryJob',
    confirmationKey: 'retry_confirm',
    successKey: 'retry_success',
    invoke: (module, row) => module.retryJob(row),
    assertRequest: (request, row) => assert.deepEqual(normalize(request.args), [row.id]),
  },
  {
    name: 'cancel job',
    row: 'cancelJob',
    request: 'memoryApi.cancelJob',
    confirmationKey: 'cancel_confirm',
    successKey: 'cancel_success',
    invoke: (module, row) => module.cancelJob(row),
    assertRequest: (request, row) => assert.deepEqual(normalize(request.args), [row.id]),
  },
]

const assertMemoryMutationConfirmation = async (harness, mutationCase) => {
  const [message, title, options] = harness.confirmCalls.at(-1)
  const html = await renderToString(message)
  assert.ok(html.includes(enMemories[mutationCase.confirmationKey]), mutationCase.name)
  assert.equal(title, enCommon.warning, mutationCase.name)
  assert.equal(options.confirmButtonText, enCommon.confirm, mutationCase.name)
  assert.equal(options.cancelButtonText, enCommon.cancel, mutationCase.name)
  assert.equal(options.type, 'warning', mutationCase.name)
  assert.equal(options.showCancelButton, true, mutationCase.name)
  assert.equal(options.dangerouslyUseHTMLString, false, mutationCase.name)
  assert.equal(options.customClass, 'app-confirm-dialog', mutationCase.name)
}

test('uses the shared confirmation dialog for delete and job mutations', async t => {
  for (const mutationCase of delayedOwnershipMutationCases) {
    const confirmation = createDeferred()
    const method = mutationCase.request.split('.')[1]
    const harness = createHarness(t, {
      currentUid: 'user-a',
      translate: confirmationTranslate,
      confirm: () => confirmation.promise,
      apiOverrides: {
        memoryApi: {
          [method]: (...args) => args.at(-1).deferred.promise,
        },
      },
    })
    const rows = createOwnershipRows('user-a')
    await harness.mount()
    const row = rows[mutationCase.row]
    const listCountBefore = harness.calls('memoryApi.list').length

    const pending = mutationCase.invoke(harness.module, row)
    assert.equal(harness.confirmCalls.length, 1, mutationCase.name)
    await assertMemoryMutationConfirmation(harness, mutationCase)
    assert.equal(harness.calls(mutationCase.request).length, 0, mutationCase.name)
    assert.equal(harness.messages.filter(message => message.type === 'info').length, 0, mutationCase.name)

    confirmation.resolve()
    await harness.flush()
    const request = harness.calls(mutationCase.request)[0]
    mutationCase.assertRequest(request, row)
    assert.equal(harness.messages.filter(message => message.type === 'info').length, 0, mutationCase.name)

    request.deferred.resolve(dataResponse({ accepted: true }))
    await pending
    await harness.flush()

    assert.ok(harness.calls('memoryApi.list').length > listCountBefore, mutationCase.name)
    assert.equal(
      harness.messages.filter(message => message.type === 'info' && message.value === enMemories[mutationCase.successKey]).length,
      1,
      mutationCase.name,
    )
  }
})

test('does not submit delete or job mutations after confirmation cancellation, closure, or error', async t => {
  const outcomes = [
    { name: 'cancel', rejection: 'cancel' },
    { name: 'close', rejection: 'close' },
    { name: 'error', rejection: new Error('confirmation failed') },
  ]

  for (const mutationCase of delayedOwnershipMutationCases) {
    for (const outcome of outcomes) {
      const confirmation = createDeferred()
      const method = mutationCase.request.split('.')[1]
      const harness = createHarness(t, {
        currentUid: 'user-a',
        translate: confirmationTranslate,
        confirm: () => confirmation.promise,
        apiOverrides: {
          memoryApi: {
            [method]: (...args) => args.at(-1).deferred.promise,
          },
        },
      })
      const rows = createOwnershipRows('user-a')
      await harness.mount()
      const listCountBefore = harness.calls('memoryApi.list').length
      const messagesBefore = normalize(harness.messages)

      const pending = mutationCase.invoke(harness.module, rows[mutationCase.row])
      assert.equal(harness.confirmCalls.length, 1, `${mutationCase.name}:${outcome.name}`)
      confirmation.reject(outcome.rejection)
      await pending
      await harness.flush()

      assert.equal(harness.calls(mutationCase.request).length, 0, `${mutationCase.name}:${outcome.name}`)
      assert.equal(harness.calls('memoryApi.list').length, listCountBefore, `${mutationCase.name}:${outcome.name}`)
      assert.equal(harness.messages.some(message => message.type === 'info'), false, `${mutationCase.name}:${outcome.name}`)
      if (outcome.name === 'error') {
        assert.equal(harness.messages.at(-1).type, 'error', `${mutationCase.name}:${outcome.name}`)
        assert.equal(harness.messages.at(-1).value, 'confirmation failed', `${mutationCase.name}:${outcome.name}`)
      } else {
        assert.deepEqual(normalize(harness.messages), messagesBefore, `${mutationCase.name}:${outcome.name}`)
      }
    }
  }
})

test('rechecks row ownership after delayed destructive confirmations', async t => {
  const revocationCases = [
    {
      name: 'ordinary user loses ownership',
      role: { superuser: false, currentUid: 'user-a', ownerUid: 'user-a' },
      revoke: module => { module.currentUid.value = 'user-b' },
    },
    {
      name: 'administrator loses superuser role',
      role: { superuser: true, currentUid: 'admin-user', ownerUid: 'user-b' },
      revoke: module => { module.isSuperuser.value = false },
    },
  ]

  for (const revocationCase of revocationCases) {
    for (const mutationCase of delayedOwnershipMutationCases) {
      const confirmation = createDeferred()
      const harness = createHarness(t, {
        ...revocationCase.role,
        confirm: () => confirmation.promise,
      })
      const rows = createOwnershipRows(revocationCase.role.ownerUid)
      await harness.mount()

      const pending = mutationCase.invoke(harness.module, rows[mutationCase.row])
      assert.equal(harness.confirmCalls.length, 1, `${revocationCase.name}:${mutationCase.name}`)
      assert.equal(harness.calls(mutationCase.request).length, 0, `${revocationCase.name}:${mutationCase.name}`)

      revocationCase.revoke(harness.module)
      confirmation.resolve(true)
      await pending
      await harness.flush()

      assert.equal(harness.calls(mutationCase.request).length, 0, `${revocationCase.name}:${mutationCase.name}`)
    }
  }
})

test('does not keep ordinary-user details whose response owner is foreign or missing', async t => {
  for (const responseOwner of ['user-b', undefined]) {
    for (const detailCase of detailCases) {
      const harness = createDetailHarness(t, detailCase)
      await harness.mount()
      const row = detailRow(detailCase, 'owner-a', `ownership-${responseOwner ?? 'missing'}-${detailCase.name}`)
      const pending = harness.module[detailCase.show](row)
      const request = harness.calls(detailCase.requestName)[0]
      assert.equal(request.args[0], row.id, detailCase.name)

      const response = { id: row.id, marker: 'foreign-response' }
      if (responseOwner !== undefined) response.owner_uid = responseOwner
      request.deferred.resolve(dataResponse(response))
      await pending

      assert.equal(harness.module[detailCase.selected].value, null, detailCase.name)
      assert.equal(harness.module[detailCase.visible].value, false, detailCase.name)
      assert.equal(harness.messages.length, 0, detailCase.name)
    }
  }
})

test('rejects list responses without a valid authorization scope', async t => {
  const invalidCases = [
    { name: 'missing meta', response: pageResponse([{ id: 'invalid-meta' }], 1) },
    {
      name: 'string false is_superuser',
      meta: { is_superuser: 'false', current_uid: 'user-a', current_username: 'Alice' },
    },
    {
      name: 'string true is_superuser',
      meta: { is_superuser: 'true', current_uid: 'user-a', current_username: 'Alice' },
    },
    {
      name: 'missing is_superuser',
      meta: { current_uid: 'user-a', current_username: 'Alice' },
    },
    {
      name: 'blank current_uid',
      meta: { is_superuser: false, current_uid: '  ', current_username: 'Alice' },
    },
    {
      name: 'missing current_uid',
      meta: { is_superuser: false, current_username: 'Alice' },
    },
    {
      name: 'non-string current_uid',
      meta: { is_superuser: false, current_uid: 42, current_username: 'Alice' },
    },
  ]

  for (const invalidCase of invalidCases) {
    const response = invalidCase.response ?? pageResponse(
      [{ id: `invalid-${invalidCase.name}` }],
      1,
      invalidCase.meta,
    )
    const harness = createHarness(t, { apiOverrides: { memoryApi: { list: () => response } } })
    await harness.mount()

    await harness.module.loadJobs()
    await harness.module.loadMigrations()
    harness.module.openEditor()
    harness.module.openRuntimeDialog('status')
    await harness.flush()

    assert.deepEqual(normalize(harness.module.memories.value), [], invalidCase.name)
    assert.equal(harness.module.memoryTotal.value, 0, invalidCase.name)
    assert.deepEqual(normalize(harness.module.jobs.value), [], invalidCase.name)
    assert.equal(harness.module.jobTotal.value, 0, invalidCase.name)
    assert.deepEqual(normalize(harness.module.migrations.value), [], invalidCase.name)
    assert.equal(harness.module.migrationTotal.value, 0, invalidCase.name)
    assert.equal(harness.module.memoryScopeReady.value, false, invalidCase.name)
    assert.equal(harness.module.isSuperuser.value, false, invalidCase.name)
    assert.equal(harness.module.currentUid.value, null, invalidCase.name)
    assert.equal(harness.calls('adminApi.userList').length, 0, invalidCase.name)
    assert.equal(harness.calls('memoryApi.jobs').length, 0, invalidCase.name)
    assert.equal(harness.calls('memoryApi.migrations').length, 0, invalidCase.name)
    assert.equal(harness.calls('memoryApi.settings').length, 0, invalidCase.name)
    assert.equal(harness.module.editorVisible.value, false, invalidCase.name)
    assert.equal(harness.module.runtimeDialogVisible.value, false, invalidCase.name)
  }
})

test('clears loaded identity-scoped state when list metadata changes identity', async t => {
  const transitions = [
    {
      name: 'superuser demotion with the same uid',
      superuser: true,
      currentUid: 'admin-user',
      currentUsername: 'Administrator',
      initialMeta: { is_superuser: true, current_uid: 'admin-user', current_username: 'Administrator' },
      nextMeta: { is_superuser: false, current_uid: 'admin-user', current_username: 'Admin User' },
      oldOwner: 'owner-a',
      nextOwner: 'admin-user',
    },
    {
      name: 'ordinary uid change',
      superuser: false,
      currentUid: 'user-a',
      currentUsername: 'Alice',
      initialMeta: { is_superuser: false, current_uid: 'user-a', current_username: 'Alice' },
      nextMeta: { is_superuser: false, current_uid: 'user-b', current_username: 'Bob' },
      oldOwner: 'user-a',
      nextOwner: 'user-b',
    },
  ]

  for (const transition of transitions) {
    for (const staleOutcome of ['success', 'failure']) {
      let listCallCount = 0
      let ownerCallCount = 0
      let migrationCallCount = 0
      let settingsCallCount = 0
      const oldMemory = {
        id: `${transition.name}-memory`,
        version: 2,
        owner_uid: transition.oldOwner,
        is_active: true,
        memory_key: 'old-key',
        memory_type: 'fact',
        content: 'old-content',
        change_evidence: 'old-evidence',
      }
      const oldJob = { id: `${transition.name}-job`, owner_uid: transition.oldOwner, status: 'failed' }
      const oldMigration = { id: `${transition.name}-migration`, owner_uid: transition.oldOwner, status: 'failed' }
      const oldSettings = {
        configured: true,
        current_job: { id: `${transition.name}-runtime`, operation: 'organize', status: 'running' },
      }
      const apiOverrides = {
        memoryApi: {
          list: () => {
            listCallCount += 1
            return listCallCount === 1
              ? pageResponse([{ id: 'old-list', owner_uid: transition.oldOwner, marker: 'old' }], 1, transition.initialMeta)
              : pageResponse([{ id: 'new-list', owner_uid: transition.nextOwner, marker: 'new' }], 1, transition.nextMeta)
          },
          jobs: () => pageResponse([oldJob], 1),
          migrations: (...args) => {
            migrationCallCount += 1
            return migrationCallCount === 1
              ? pageResponse([oldMigration], 1)
              : args.at(-1).deferred.promise
          },
          get: () => dataResponse(oldMemory),
          history: () => pageResponse([{ version: 1, content: 'old-history' }], 1),
          job: () => dataResponse(oldJob),
          migration: () => dataResponse(oldMigration),
          settings: (...args) => {
            settingsCallCount += 1
            return settingsCallCount === 1 ? dataResponse(oldSettings) : args.at(-1).deferred.promise
          },
        },
      }
      if (transition.superuser) {
        apiOverrides.adminApi = {
          userList: (...args) => {
            ownerCallCount += 1
            return ownerCallCount === 1
              ? pageResponse([{ uid: 'owner-a', username: 'Owner A' }], 1)
              : args.at(-1).deferred.promise
          },
        }
      }

      const harness = createHarness(t, {
        superuser: transition.superuser,
        currentUid: transition.currentUid,
        currentUsername: transition.currentUsername,
        apiOverrides,
      })
      await harness.mount()
      await harness.flush()

      harness.module.ownerFilter.value = 'user-b'
      await harness.module.loadJobs()
      await harness.module.loadMigrations()
      await harness.module.showDetails(oldMemory)
      harness.module.editSelectedMemory()
      fillEditor(harness.module, {
        memory_key: 'old-draft-key',
        content: 'old-draft-content',
        change_evidence: 'old-draft-evidence',
      })
      await harness.module.showHistory(oldMemory)
      await harness.module.showJob(oldJob)
      await harness.module.showMigration(oldMigration)
      harness.module.editorVisible.value = false
      harness.module.historyVisible.value = false
      harness.module.jobVisible.value = false
      harness.module.migrationVisible.value = false

      harness.module.openRuntimeDialog('status')
      await harness.flush()
      const oldSettingsPending = harness.module.loadSettings()
      const oldSettingsRequest = lastRequest(harness.calls('memoryApi.settings'))
      const oldMigrationsPending = harness.module.loadMigrations()
      const oldMigrationsRequest = lastRequest(harness.calls('memoryApi.migrations'))
      let oldOwnersPending = Promise.resolve()
      let oldOwnersRequest = null
      if (transition.superuser) {
        oldOwnersPending = harness.module.loadOwners()
        oldOwnersRequest = lastRequest(harness.calls('adminApi.userList'))
      }

      assert.equal(harness.module.ownerFilter.value, 'user-b', transition.name)
      assert.equal(harness.module.jobs.value[0].id, oldJob.id, transition.name)
      assert.equal(harness.module.migrations.value[0].id, oldMigration.id, transition.name)
      assert.equal(harness.module.selectedMemory.value.id, oldMemory.id, transition.name)
      assert.equal(harness.module.selectedHistoryMemory.value.id, oldMemory.id, transition.name)
      assert.equal(harness.module.selectedJob.value.id, oldJob.id, transition.name)
      assert.equal(harness.module.selectedMigration.value.id, oldMigration.id, transition.name)
      assert.equal(harness.module.form.content, 'old-draft-content', transition.name)
      assert.equal(harness.module.runtimeDialogVisible.value, true, transition.name)
      assert.equal(harness.module.settings.current_job.id, oldSettings.current_job.id, transition.name)

      const identityPending = harness.module.loadMemories()
      await identityPending
      await harness.flush()

      const assertCleared = () => {
        assert.equal(harness.module.isSuperuser.value, transition.nextMeta.is_superuser, transition.name)
        assert.equal(harness.module.currentUid.value, transition.nextMeta.current_uid, transition.name)
        assert.equal(harness.module.memoryScopeReady.value, true, transition.name)
        assert.equal(harness.module.ownerFilter.value, '', transition.name)
        assert.deepEqual(normalize(harness.module.owners.value), [], transition.name)
        assert.equal(harness.module.ownersLoaded.value, false, transition.name)
        assert.deepEqual(normalize(harness.module.memories.value), [{ id: 'new-list', owner_uid: transition.nextOwner, marker: 'new' }], transition.name)
        assert.equal(harness.module.memoryTotal.value, 1, transition.name)
        assert.deepEqual(normalize(harness.module.jobs.value), [], transition.name)
        assert.equal(harness.module.jobTotal.value, 0, transition.name)
        assert.deepEqual(normalize(harness.module.migrations.value), [], transition.name)
        assert.equal(harness.module.migrationTotal.value, 0, transition.name)
        assert.equal(harness.module.selectedMemory.value, null, transition.name)
        assert.equal(harness.module.selectedHistoryMemory.value, null, transition.name)
        assert.equal(harness.module.selectedJob.value, null, transition.name)
        assert.equal(harness.module.selectedMigration.value, null, transition.name)
        assert.equal(harness.module.detailsVisible.value, false, transition.name)
        assert.equal(harness.module.historyVisible.value, false, transition.name)
        assert.equal(harness.module.jobVisible.value, false, transition.name)
        assert.equal(harness.module.migrationVisible.value, false, transition.name)
        assert.equal(harness.module.editorVisible.value, false, transition.name)
        assert.deepEqual(normalize(harness.module.form), {
          id: null,
          version: 0,
          owner_uid: '',
          memory_key: '',
          memory_type: 'fact',
          content: '',
          change_evidence: '',
          suppress_current: false,
        }, transition.name)
        assert.deepEqual(normalize(harness.module.settings), {}, transition.name)
        assert.equal(harness.module.settingsLoaded.value, false, transition.name)
        assert.equal(harness.module.settingsLoading.value, false, transition.name)
        assert.equal(harness.module.runtimeDialogVisible.value, false, transition.name)
        assert.equal(harness.module.runtimeOwnerFilter.value, '', transition.name)
        assert.equal(harness.module.currentMemoryTask.value, null, transition.name)
      }
      assertCleared()
      const messagesAfterIdentityChange = normalize(harness.messages)

      if (staleOutcome === 'success') {
        oldSettingsRequest.deferred.resolve(dataResponse({ configured: false, current_job: { id: 'stale-runtime' } }))
        oldMigrationsRequest.deferred.resolve(pageResponse([{ id: 'stale-migration' }], 1))
        oldOwnersRequest?.deferred.resolve(pageResponse([{ uid: 'stale-owner', username: 'Stale Owner' }], 1))
      } else {
        oldSettingsRequest.deferred.reject(new Error('stale settings response'))
        oldMigrationsRequest.deferred.reject(new Error('stale migrations response'))
        oldOwnersRequest?.deferred.reject(new Error('stale owners response'))
      }
      await Promise.all([oldSettingsPending, oldMigrationsPending, oldOwnersPending])
      await harness.flush()

      assertCleared()
      assert.deepEqual(normalize(harness.messages), messagesAfterIdentityChange, transition.name)
    }
  }
})

test('clears authorization state after unauthorized list responses and recovers with an unscoped ordinary request', async t => {
  const authorizationCases = [
    { name: 'http status', response: { status: 401 } },
    { name: 'response data code', response: { data: { code: 403 } } },
  ]

  for (const authorizationCase of authorizationCases) {
    let listCallCount = 0
    const harness = createHarness(t, {
      superuser: true,
      apiOverrides: {
        memoryApi: {
          list: () => {
            listCallCount += 1
            if (listCallCount === 1) return pageResponse([{ id: 'authorized-old', owner_uid: 'user-b' }], 1, {
              is_superuser: true,
              current_uid: 'admin-user',
              current_username: 'Administrator',
            })
            if (listCallCount === 2) {
              return Promise.reject(Object.assign(new Error('authorization expired'), {
                response: authorizationCase.response,
              }))
            }
            return pageResponse([{ id: 'ordinary-own', owner_uid: 'admin-user' }], 1, {
              is_superuser: false,
              current_uid: 'admin-user',
              current_username: 'Admin User',
            })
          },
        },
      },
    })
    await harness.mount()
    harness.module.ownerFilter.value = 'user-b'
    harness.module.openRuntimeDialog('status')
    await harness.flush()
    assert.equal(harness.module.runtimeDialogVisible.value, true, authorizationCase.name)

    await harness.module.loadMemories()

    assert.equal(harness.module.isSuperuser.value, false, authorizationCase.name)
    assert.equal(harness.module.currentUid.value, null, authorizationCase.name)
    assert.equal(harness.module.memoryScopeReady.value, false, authorizationCase.name)
    assert.equal(harness.module.ownerFilter.value, '', authorizationCase.name)
    assert.deepEqual(normalize(harness.module.memories.value), [], authorizationCase.name)
    assert.equal(harness.module.memoryTotal.value, 0, authorizationCase.name)
    assert.deepEqual(normalize(harness.module.settings), {}, authorizationCase.name)
    assert.equal(harness.module.runtimeDialogVisible.value, false, authorizationCase.name)

    await harness.module.loadMemories()
    const recoveredRequest = lastRequest(harness.calls('memoryApi.list'))
    assert.equal(recoveredRequest.args[0].uid, undefined, authorizationCase.name)
    assert.equal(harness.module.isSuperuser.value, false, authorizationCase.name)
    assert.equal(harness.module.currentUid.value, 'admin-user', authorizationCase.name)
    assert.equal(harness.module.memoryScopeReady.value, true, authorizationCase.name)
    assert.deepEqual(normalize(harness.module.memories.value), [{ id: 'ordinary-own', owner_uid: 'admin-user' }], authorizationCase.name)
    assert.equal(harness.module.memoryTotal.value, 1, authorizationCase.name)
  }
})
