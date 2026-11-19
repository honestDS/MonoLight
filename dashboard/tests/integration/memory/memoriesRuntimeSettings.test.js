import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createDetailHarness,
  createHarness,
  dataResponse,
  detailCases,
  detailResponse,
  detailRow,
  normalize,
  pageResponse,
} from './memoriesViewHarness.js'

test('loads each admin runtime action for the selected owner without changing list state', async t => {
  for (const action of ['organize', 'reindex', 'status']) {
    const settingsByOwner = {
      'user-a': {
        configured: true,
        capacity: { active_record_count: 11, max_active_records: 50 },
        current_job: { id: 'job-a', operation: 'organize', status: 'running' },
      },
      'user-b': {
        configured: true,
        capacity: { active_record_count: 29, max_active_records: 60 },
        current_job: { id: 'job-b', operation: 'reindex', status: 'running' },
      },
    }
    const harness = createHarness(t, {
      superuser: true,
      apiOverrides: {
        adminApi: {
          userList: () => pageResponse([
            { uid: 'user-a', username: 'Alice' },
            { uid: 'user-b', username: 'Bob' },
          ], 2),
        },
        memoryApi: {
          settings: ({ params }) => dataResponse(settingsByOwner[params.uid]),
        },
      },
    })
    await harness.mount()
    await harness.flush()

    harness.module.ownerFilter.value = 'user-a'
    harness.module.filters.keyword = 'keep-this-keyword'
    harness.module.filters.memory_type = 'preference'
    harness.module.jobFilters.status = 'running'
    harness.module.jobFilters.operation = 'organize'
    harness.module.jobFilters.memory_id = 'memory-7'
    harness.module.memoryPage.value = 4
    harness.module.memoryPageSize.value = 50
    harness.module.memoryTotal.value = 12
    harness.module.memories.value = [{ id: 'list-a', owner_uid: 'user-a' }]
    harness.module.jobPage.value = 3
    harness.module.jobPageSize.value = 50
    harness.module.jobTotal.value = 8
    harness.module.jobs.value = [{ id: 'job-list-a', owner_uid: 'user-a' }]
    harness.module.migrationPage.value = 2
    harness.module.migrationPageSize.value = 50
    harness.module.migrationTotal.value = 5
    harness.module.migrations.value = [{ id: 'migration-a', owner_uid: 'user-a' }]

    harness.module.openRuntimeDialog(action)
    await harness.flush()

    assert.equal(harness.module.runtimeDialogVisible.value, true, action)
    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a', action)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-a', action)
    assert.equal(harness.calls('memoryApi.settings').at(-1).args[0].params.uid, 'user-a', action)
    assert.equal(harness.module.settings.capacity.active_record_count, 11, action)
    assert.equal(harness.module.currentMemoryTask.value.id, 'job-a', action)
    assert.equal(harness.module.ownerLabel(harness.module.runtimeOwnerUid.value), 'Alice', action)

    const stateBeforeRuntimeSwitch = {
      ownerFilter: harness.module.ownerFilter.value,
      filters: normalize(harness.module.filters),
      jobFilters: normalize(harness.module.jobFilters),
      memoryPage: harness.module.memoryPage.value,
      memoryPageSize: harness.module.memoryPageSize.value,
      memoryTotal: harness.module.memoryTotal.value,
      memories: normalize(harness.module.memories.value),
      jobPage: harness.module.jobPage.value,
      jobPageSize: harness.module.jobPageSize.value,
      jobTotal: harness.module.jobTotal.value,
      jobs: normalize(harness.module.jobs.value),
      migrationPage: harness.module.migrationPage.value,
      migrationPageSize: harness.module.migrationPageSize.value,
      migrationTotal: harness.module.migrationTotal.value,
      migrations: normalize(harness.module.migrations.value),
    }
    const listRequestCount = harness.calls('memoryApi.list').length
    const jobsRequestCount = harness.calls('memoryApi.jobs').length
    const migrationsRequestCount = harness.calls('memoryApi.migrations').length
    const settingsRequestCount = harness.calls('memoryApi.settings').length

    harness.module.handleRuntimeOwnerChange('user-b')

    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-b', action)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-b', action)
    assert.equal(harness.module.settingsLoaded.value, false, action)
    assert.deepEqual(normalize(harness.module.settings), {}, action)
    assert.equal(harness.module.currentMemoryTask.value, null, action)

    await harness.flush()

    assert.equal(harness.calls('memoryApi.settings').length, settingsRequestCount + 1, action)
    assert.equal(harness.calls('memoryApi.settings').at(-1).args[0].params.uid, 'user-b', action)
    assert.equal(harness.module.settings.capacity.active_record_count, 29, action)
    assert.equal(harness.module.currentMemoryTask.value.id, 'job-b', action)
    assert.equal(harness.module.ownerLabel(harness.module.runtimeOwnerUid.value), 'Bob', action)
    assert.equal(harness.calls('memoryApi.list').length, listRequestCount, action)
    assert.equal(harness.calls('memoryApi.jobs').length, jobsRequestCount, action)
    assert.equal(harness.calls('memoryApi.migrations').length, migrationsRequestCount, action)
    assert.deepEqual({
      ownerFilter: harness.module.ownerFilter.value,
      filters: normalize(harness.module.filters),
      jobFilters: normalize(harness.module.jobFilters),
      memoryPage: harness.module.memoryPage.value,
      memoryPageSize: harness.module.memoryPageSize.value,
      memoryTotal: harness.module.memoryTotal.value,
      memories: normalize(harness.module.memories.value),
      jobPage: harness.module.jobPage.value,
      jobPageSize: harness.module.jobPageSize.value,
      jobTotal: harness.module.jobTotal.value,
      jobs: normalize(harness.module.jobs.value),
      migrationPage: harness.module.migrationPage.value,
      migrationPageSize: harness.module.migrationPageSize.value,
      migrationTotal: harness.module.migrationTotal.value,
      migrations: normalize(harness.module.migrations.value),
    }, stateBeforeRuntimeSwitch, action)

    harness.module.handleRuntimeOwnerChange('')
    assert.equal(harness.module.runtimeOwnerFilter.value, '', action)
    assert.equal(harness.module.runtimeOwnerUid.value, null, action)
    assert.equal(harness.module.settingsLoaded.value, false, action)
    assert.deepEqual(normalize(harness.module.settings), {}, action)
    assert.equal(harness.module.currentMemoryTask.value, null, action)
    assert.equal(harness.module.organizeBlocked.value, true, action)
    assert.equal(harness.module.reindexBlocked.value, true, action)

    await harness.flush()

    assert.equal(harness.calls('memoryApi.settings').length, settingsRequestCount + 1, action)
    assert.equal(harness.calls('memoryApi.organize').length, 0, action)
    assert.equal(harness.calls('memoryApi.reindex').length, 0, action)
  }
})

test('keeps ordinary-user runtime settings bound to the current user for every action', async t => {
  for (const action of ['organize', 'reindex', 'status']) {
    const requestedOwners = []
    const harness = createHarness(t, {
      currentUid: 'user-a',
      currentUsername: 'Alice',
      apiOverrides: {
        memoryApi: {
          settings: ({ params }) => {
            requestedOwners.push(params.uid)
            return dataResponse({
              configured: true,
              capacity: { active_record_count: 7, max_active_records: 40 },
            })
          },
        },
      },
    })
    await harness.mount()
    await harness.flush()

    assert.equal(harness.calls('adminApi.userList').length, 0, action)
    harness.module.ownerFilter.value = 'user-b'
    harness.module.openRuntimeDialog(action)
    await harness.flush()
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-a', action)
    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a', action)
    assert.equal(harness.module.ownerLabel(harness.module.runtimeOwnerUid.value), 'Alice', action)

    harness.module.runtimeOwnerFilter.value = 'user-b'
    await harness.module.loadSettings()
    harness.module.handleRuntimeOwnerChange('user-b')
    await harness.flush()

    assert.ok(requestedOwners.length >= 3, action)
    assert.equal(requestedOwners.every(uid => uid === 'user-a'), true, action)
    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a', action)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-a', action)
    assert.equal(harness.module.ownerLabel('user-a'), 'Alice', action)
    assert.equal(harness.module.ownerLabel('user-b'), 'user-b', action)
  }
})

test('does not open or query runtime status when entry is blocked or another dialog is open', async t => {
  const blockedEntryCases = [
    {
      name: 'invalid action',
      prepare: () => {},
      action: 'unknown',
    },
    {
      name: 'scope is not ready',
      prepare: harness => { harness.module.memoryScopeReady.value = false },
      action: 'status',
    },
    {
      name: 'another action is loading',
      prepare: harness => { harness.module.actionLoading.value = 'organize' },
      action: 'status',
    },
  ]

  for (const blockedCase of blockedEntryCases) {
    const harness = createHarness(t)
    await harness.mount()
    blockedCase.prepare(harness)
    harness.module.openRuntimeDialog(blockedCase.action)

    assert.equal(harness.module.runtimeDialogVisible.value, false, blockedCase.name)
    assert.equal(harness.calls('memoryApi.settings').length, 0, blockedCase.name)
  }

  const unmountedHarness = createHarness(t)
  await unmountedHarness.mount()
  unmountedHarness.unmount()
  unmountedHarness.module.openRuntimeDialog('status')
  assert.equal(unmountedHarness.module.runtimeDialogVisible.value, false)
  assert.equal(unmountedHarness.calls('memoryApi.settings').length, 0)

  for (const dialogKey of ['editorVisible', 'detailsVisible', 'historyVisible', 'jobVisible', 'migrationVisible']) {
    const harness = createHarness(t)
    await harness.mount()
    harness.module[dialogKey].value = true
    Object.assign(harness.module.form, {
      id: 901,
      version: 3,
      owner_uid: 'user-a',
      memory_key: 'draft-key',
      memory_type: 'preference',
      content: 'draft-content',
      change_evidence: 'draft-evidence',
      suppress_current: true,
    })
    const draftBeforeOpen = normalize(harness.module.form)

    harness.module.openRuntimeDialog('status')

    assert.equal(harness.module.runtimeDialogVisible.value, false, dialogKey)
    assert.equal(harness.module[dialogKey].value, true, dialogKey)
    assert.deepEqual(normalize(harness.module.form), draftBeforeOpen, dialogKey)
    assert.equal(harness.calls('memoryApi.settings').length, 0, dialogKey)
  }
})

test('invalidates pending memory, job, and migration details when runtime status opens first', async t => {
  for (const detailCase of detailCases) {
    for (const staleOutcome of ['success', 'failure']) {
      const harness = createDetailHarness(t, detailCase)
      await harness.mount()
      const row = detailRow(detailCase, 'owner-a', `runtime-${staleOutcome}`)
      const pending = harness.module[detailCase.show](row)
      const request = harness.calls(detailCase.requestName)[0]

      assert.equal(harness.module[detailCase.visible].value, false, detailCase.name)
      harness.module.openRuntimeDialog('status')
      await harness.flush()
      const messagesBeforeStaleResponse = normalize(harness.messages)

      if (staleOutcome === 'success') request.deferred.resolve(detailResponse(row, 'stale'))
      else request.deferred.reject(new Error(`stale ${detailCase.name} response`))
      await pending

      assert.equal(harness.module.runtimeDialogVisible.value, true, detailCase.name)
      assert.equal(harness.module[detailCase.selected].value, null, detailCase.name)
      assert.equal(harness.module[detailCase.visible].value, false, detailCase.name)
      assert.deepEqual(normalize(harness.messages), messagesBeforeStaleResponse, detailCase.name)
    }
  }
})

const runtimeSettingsData = (marker, activeRecordCount, maxActiveRecords = activeRecordCount + 40) => dataResponse({
  configured: true,
  capacity: { active_record_count: activeRecordCount, max_active_records: maxActiveRecords },
  current_job: { id: `runtime-${marker}`, operation: 'create', status: 'running' },
})

const createRuntimeSettingsHarness = t => createHarness(t, {
  superuser: true,
  apiOverrides: {
    memoryApi: {
      settings: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

test('keeps the newest runtime settings after an administrator owner switch race', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createRuntimeSettingsHarness(t)
    await harness.mount()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.openRuntimeDialog('status')
    const oldRequest = harness.calls('memoryApi.settings')[0]

    harness.module.handleRuntimeOwnerChange('user-b')
    const newRequest = harness.calls('memoryApi.settings')[1]
    assert.equal(oldRequest.args[0].params.uid, 'user-a')
    assert.equal(newRequest.args[0].params.uid, 'user-b')
    assert.equal(oldRequest.args[0].signal.aborted, true)

    newRequest.deferred.resolve(runtimeSettingsData('B', 29, 60))
    await harness.flush()
    const settingsAfterB = normalize(harness.module.settings)
    const taskAfterB = normalize(harness.module.currentMemoryTask.value)
    const messagesAfterB = normalize(harness.messages)
    assert.equal(harness.module.settings.capacity.active_record_count, 29)
    assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-B')
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')

    if (staleOutcome === 'success') oldRequest.deferred.resolve(runtimeSettingsData('A-stale', 11, 50))
    else oldRequest.deferred.reject(new Error('stale A settings response'))
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), settingsAfterB)
    assert.deepEqual(normalize(harness.module.currentMemoryTask.value), taskAfterB)
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.deepEqual(normalize(harness.messages), messagesAfterB)
  }
})

test('keeps the final A runtime read authoritative across a rapid A to B to A cycle', async t => {
  for (const firstAOutcome of ['success', 'failure']) {
    for (const bOutcome of ['success', 'failure']) {
      const harness = createRuntimeSettingsHarness(t)
      await harness.mount()
      harness.module.ownerFilter.value = 'user-a'
      harness.module.openRuntimeDialog('status')
      const firstARequest = harness.calls('memoryApi.settings')[0]

      harness.module.handleRuntimeOwnerChange('user-b')
      const bRequest = harness.calls('memoryApi.settings')[1]
      harness.module.handleRuntimeOwnerChange('user-a')
      const finalARequest = harness.calls('memoryApi.settings')[2]
      assert.equal(firstARequest.args[0].signal.aborted, true)
      assert.equal(bRequest.args[0].signal.aborted, true)
      assert.equal(finalARequest.args[0].params.uid, 'user-a')

      finalARequest.deferred.resolve(runtimeSettingsData('A-final', 37, 70))
      await harness.flush()
      const finalSnapshot = {
        settings: normalize(harness.module.settings),
        currentMemoryTask: normalize(harness.module.currentMemoryTask.value),
        settingsLoaded: harness.module.settingsLoaded.value,
        settingsLoading: harness.module.settingsLoading.value,
        settingsLoadError: harness.module.settingsLoadError.value,
        messages: normalize(harness.messages),
      }
      assert.equal(harness.module.settings.capacity.active_record_count, 37)
      assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-A-final')
      assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')

      if (firstAOutcome === 'success') firstARequest.deferred.resolve(runtimeSettingsData('A-first-stale', 11, 50))
      else firstARequest.deferred.reject(new Error('stale first A settings response'))
      await harness.flush()
      if (bOutcome === 'success') bRequest.deferred.resolve(runtimeSettingsData('B-stale', 29, 60))
      else bRequest.deferred.reject(new Error('stale B settings response'))
      await harness.flush()

      assert.deepEqual(normalize(harness.module.settings), finalSnapshot.settings)
      assert.deepEqual(normalize(harness.module.currentMemoryTask.value), finalSnapshot.currentMemoryTask)
      assert.equal(harness.module.settingsLoaded.value, finalSnapshot.settingsLoaded)
      assert.equal(harness.module.settingsLoading.value, finalSnapshot.settingsLoading)
      assert.equal(harness.module.settingsLoadError.value, finalSnapshot.settingsLoadError)
      assert.deepEqual(normalize(harness.messages), finalSnapshot.messages)
    }
  }
})

test('cancels and clears runtime settings on close before reopening the same owner', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createRuntimeSettingsHarness(t)
    await harness.mount()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.openRuntimeDialog('status')
    const oldRequest = harness.calls('memoryApi.settings')[0]

    harness.module.closeRuntimeDialog()
    assert.equal(oldRequest.args[0].signal.aborted, true)
    assert.equal(harness.module.runtimeDialogVisible.value, false)
    assert.deepEqual(normalize(harness.module.settings), {})
    assert.equal(harness.module.settingsLoaded.value, false)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')
    for (const name of [
      'memoryApi.cancelJob',
      'memoryApi.cancelMigration',
      'memoryApi.organize',
      'memoryApi.reindex',
      'memoryApi.retryJob',
      'memoryApi.retryMigration',
      'memoryApi.retryCleanup',
      'memoryApi.updateSettings',
      'memoryApi.create',
      'memoryApi.update',
      'memoryApi.delete',
      'memoryApi.pin',
      'memoryApi.unpin',
      'memoryApi.resumeCurrent',
    ]) assert.equal(harness.calls(name).length, 0, name)

    harness.module.openRuntimeDialog('status')
    const newRequest = harness.calls('memoryApi.settings')[1]
    assert.equal(newRequest.args[0].params.uid, 'user-a')
    if (staleOutcome === 'success') oldRequest.deferred.resolve(runtimeSettingsData('old-late', 7, 40))
    else oldRequest.deferred.reject(new Error('old closed settings response'))
    await harness.flush()
    assert.deepEqual(normalize(harness.module.settings), {})
    assert.equal(harness.module.settingsLoaded.value, false)
    assert.equal(harness.module.settingsLoading.value, true)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.equal(harness.messages.length, 0)

    newRequest.deferred.resolve(runtimeSettingsData('reopened', 43, 80))
    await harness.flush()
    assert.equal(harness.module.settings.capacity.active_record_count, 43)
    assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-reopened')
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.equal(harness.messages.length, 0)
  }
})

test('blocks runtime confirmation during an organize or reindex refresh failure and restores it after success', async t => {
  for (const action of ['organize', 'reindex']) {
    const harness = createRuntimeSettingsHarness(t)
    await harness.mount()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.openRuntimeDialog(action)
    const initialRequest = harness.calls('memoryApi.settings')[0]
    initialRequest.deferred.resolve(runtimeSettingsData('before-refresh', 13, 50))
    await harness.flush()
    assert.equal(harness.module.organizeBlocked.value, false)
    assert.equal(harness.module.reindexBlocked.value, false)
    assert.equal(harness.module.settings.capacity.active_record_count, 13)
    assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-before-refresh')
    const messagesBeforeFailure = normalize(harness.messages)

    const failedPending = harness.module.loadSettings()
    const failedRequest = harness.calls('memoryApi.settings')[1]
    assert.equal(harness.module.settingsLoading.value, true)
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.organizeBlocked.value, true)
    assert.equal(harness.module.reindexBlocked.value, true)
    await harness.module.submitRuntimeOperation(action)
    assert.equal(harness.calls('memoryApi.organize').length, 0)
    assert.equal(harness.calls('memoryApi.reindex').length, 0)
    failedRequest.deferred.reject(new Error('runtime refresh failed'))
    await failedPending
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), {})
    assert.equal(harness.module.currentMemoryTask.value, null)
    assert.equal(harness.module.settingsLoaded.value, false)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, 'runtime refresh failed')
    assert.equal(harness.module.organizeBlocked.value, true)
    assert.equal(harness.module.reindexBlocked.value, true)
    await harness.module.submitRuntimeOperation(action)
    assert.equal(harness.calls('memoryApi.organize').length, 0)
    assert.equal(harness.calls('memoryApi.reindex').length, 0)
    assert.deepEqual(normalize(harness.messages), messagesBeforeFailure)

    const successfulPending = harness.module.loadSettings()
    const successfulRequest = harness.calls('memoryApi.settings')[2]
    successfulRequest.deferred.resolve(runtimeSettingsData('after-refresh', 31, 70))
    await successfulPending
    await harness.flush()
    assert.equal(harness.module.settings.capacity.active_record_count, 31)
    assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-after-refresh')
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.equal(harness.module.organizeBlocked.value, false)
    assert.equal(harness.module.reindexBlocked.value, false)
    assert.deepEqual(normalize(harness.messages), messagesBeforeFailure)
  }
})

test('polls runtime settings only for the open owner and keeps silent reads out of loading state', async t => {
  const harness = createRuntimeSettingsHarness(t)
  await harness.mount()
  const closedPollBeforeOpen = [...harness.timers.values()].at(-1)
  const listCountBeforeClosedPoll = harness.calls('memoryApi.list').length
  await closedPollBeforeOpen.callback()
  await harness.flush()
  assert.equal(harness.calls('memoryApi.settings').length, 0)
  assert.ok(harness.calls('memoryApi.list').length > listCountBeforeClosedPoll)

  harness.module.ownerFilter.value = 'user-a'
  harness.module.openRuntimeDialog('status')
  const openRequest = harness.calls('memoryApi.settings')[0]
  openRequest.deferred.resolve(runtimeSettingsData('open', 17, 50))
  await harness.flush()
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  assert.equal(harness.module.settingsLoaded.value, true)
  assert.equal(harness.module.settingsLoading.value, false)

  const pollTimer = [...harness.timers.values()].at(-1)
  const pollPending = pollTimer.callback()
  await harness.flush()
  const settingsRequestsDuringPoll = harness.calls('memoryApi.settings')
  assert.equal(settingsRequestsDuringPoll.length, 2)
  assert.deepEqual(settingsRequestsDuringPoll.map(request => request.args[0].params.uid), ['user-a', 'user-a'])
  assert.equal(harness.module.settingsLoading.value, false)
  assert.equal(harness.module.settingsLoaded.value, true)
  assert.equal(harness.module.settings.capacity.active_record_count, 17)

  settingsRequestsDuringPoll[1].deferred.resolve(runtimeSettingsData('polled', 23, 60))
  await pollPending
  await harness.flush()
  assert.equal(harness.module.settings.capacity.active_record_count, 23)
  assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-polled')
  assert.equal(harness.module.settingsLoading.value, false)

  harness.module.closeRuntimeDialog()
  const settingsCountAfterClose = harness.calls('memoryApi.settings').length
  const listCountBeforeClosedPollAfterOpen = harness.calls('memoryApi.list').length
  const closedPollAfterOpen = [...harness.timers.values()].at(-1)
  await closedPollAfterOpen.callback()
  await harness.flush()
  assert.equal(harness.calls('memoryApi.settings').length, settingsCountAfterClose)
  assert.ok(harness.calls('memoryApi.list').length > listCountBeforeClosedPollAfterOpen)
})

test('preserves the runtime settings snapshot when a pending read settles after unmount', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createRuntimeSettingsHarness(t)
    await harness.mount()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.openRuntimeDialog('status')
    const initialRequest = harness.calls('memoryApi.settings')[0]
    initialRequest.deferred.resolve(runtimeSettingsData('before-unmount', 19, 55))
    await harness.flush()
    harness.module.settingsLoadError.value = 'keep-before-unmount'

    const pending = harness.module.loadSettings()
    const request = harness.calls('memoryApi.settings')[1]
    const snapshot = {
      settings: normalize(harness.module.settings),
      currentMemoryTask: normalize(harness.module.currentMemoryTask.value),
      runtimeDialogVisible: harness.module.runtimeDialogVisible.value,
      runtimeOwnerFilter: harness.module.runtimeOwnerFilter.value,
      runtimeOwnerUid: harness.module.runtimeOwnerUid.value,
      settingsLoaded: harness.module.settingsLoaded.value,
      settingsLoading: harness.module.settingsLoading.value,
      settingsLoadError: harness.module.settingsLoadError.value,
      messages: normalize(harness.messages),
    }
    assert.equal(snapshot.settingsLoading, true)
    harness.unmount()
    assert.equal(request.args[0].signal.aborted, true)

    if (staleOutcome === 'success') request.deferred.resolve(runtimeSettingsData('late-unmount', 99, 120))
    else request.deferred.reject(new Error('late unmount settings failure'))
    await pending
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), snapshot.settings)
    assert.deepEqual(normalize(harness.module.currentMemoryTask.value), snapshot.currentMemoryTask)
    assert.equal(harness.module.runtimeDialogVisible.value, snapshot.runtimeDialogVisible)
    assert.equal(harness.module.runtimeOwnerFilter.value, snapshot.runtimeOwnerFilter)
    assert.equal(harness.module.runtimeOwnerUid.value, snapshot.runtimeOwnerUid)
    assert.equal(harness.module.settingsLoaded.value, snapshot.settingsLoaded)
    assert.equal(harness.module.settingsLoading.value, snapshot.settingsLoading)
    assert.equal(harness.module.settingsLoadError.value, snapshot.settingsLoadError)
    assert.deepEqual(normalize(harness.messages), snapshot.messages)
  }
})
