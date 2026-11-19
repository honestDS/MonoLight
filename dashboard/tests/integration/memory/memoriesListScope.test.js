import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createHarness,
  dataResponse,
  normalize,
  pageResponse,
} from './memoriesViewHarness.js'

test('derives ordinary-user scope from list metadata without cross-user uid requests', async t => {
  const harness = createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
  })
  await harness.mount()

  assert.equal(harness.module.isSuperuser.value, false)
  assert.equal(harness.module.currentUid.value, 'user-a')
  assert.equal(harness.module.currentUsername.value, 'Alice')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  assert.equal(harness.module.ownerLabel('user-a'), 'Alice')
  assert.equal(harness.calls('memoryApi.settings').length, 0)

  harness.module.openRuntimeDialog('status')
  await harness.flush()
  assert.deepEqual(normalize(harness.calls('memoryApi.settings')[0].args[0].params), { uid: 'user-a' })

  harness.module.ownerFilter.value = 'user-b'
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  await harness.module.loadJobs()
  await harness.module.loadMigrations()

  assert.equal(harness.calls('adminApi.userList').length, 0)
  for (const name of ['memoryApi.list', 'memoryApi.jobs', 'memoryApi.migrations']) {
    for (const request of harness.calls(name)) assert.equal(request.args[0].uid, undefined)
  }
})

test('loads ordinary-user channel model names from paginated profile options without channel-list access', async t => {
  const firstPage = Array.from({ length: 100 }, (_value, index) => ({
    id: index + 1,
    name: `Channel ${index}`,
    is_active: true,
    model_ids: [{
      model_id: `model-${index}`,
      usage: 'EMBEDDING',
      protocol: 'OPENAI_EMBEDDING',
      is_enabled: true,
    }],
  }))
  const secondPage = [{
    id: 101,
    name: 'Channel 100',
    is_active: true,
    model_ids: [{
      model_id: 'model-100',
      usage: 'EMBEDDING',
      protocol: 'OPENAI_EMBEDDING',
      is_enabled: true,
    }],
  }]
  const harness = createHarness(t, {
    apiOverrides: {
      profileApi: {
        modelOptions: params => pageResponse(params.page === 1 ? firstPage : secondPage, 101),
      },
    },
  })
  await harness.mount()
  await harness.flush()

  assert.deepEqual(normalize(harness.calls('profileApi.modelOptions').map(request => request.args[0])), [
    { page: 1, size: 100 },
    { page: 2, size: 100 },
  ])
  assert.equal(harness.module.channels.value.length, 101)
  assert.equal(harness.module.channelName(101), 'Channel 100')
  assert.equal(harness.calls('channelApi.list').length, 0)
  assert.equal(harness.messages.some(message => message.type === 'error'), false)

  await harness.module.loadChannels()
  await harness.flush()

  assert.deepEqual(normalize(harness.calls('profileApi.modelOptions').map(request => request.args[0])), [
    { page: 1, size: 100 },
    { page: 2, size: 100 },
    { page: 1, size: 100 },
    { page: 2, size: 100 },
  ])
  assert.equal(harness.module.channels.value.length, 101)
  assert.equal(new Set(harness.module.channels.value.map(channel => channel.id)).size, 101)
  assert.equal(harness.calls('channelApi.list').length, 0)
  assert.equal(harness.messages.some(message => message.type === 'error'), false)
})

test('loads all superuser owner labels across paginated user-list responses', async t => {
  const firstPage = Array.from({ length: 100 }, (_value, index) => ({
    uid: `user-${index}`,
    username: `User ${index}`,
  }))
  const secondPage = [{ uid: 'user-100', username: 'User 100' }]
  const harness = createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    apiOverrides: {
      adminApi: {
        userList: params => pageResponse(params.page === 1 ? firstPage : secondPage, 101),
      },
    },
  })
  await harness.mount()
  await harness.flush()

  assert.deepEqual(normalize(harness.calls('adminApi.userList').map(request => request.args[0])), [
    { page: 1, size: 100 },
    { page: 2, size: 100 },
  ])
  assert.equal(harness.module.owners.value.length, 101)
  assert.equal(harness.module.ownerLabel('user-100'), 'User 100')
  assert.equal(harness.module.ownerLabel('unknown-owner'), 'unknown-owner')
  assert.equal(harness.module.ownerLabel(''), 'owner_unknown')
})

test('uses the selected superuser owner for list, jobs, and migrations while independently pre-filling the runtime dialog', async t => {
  const harness = createHarness(t, {
    superuser: true,
    apiOverrides: {
      adminApi: {
        userList: () => pageResponse([
          { uid: 'user-a', username: 'Alice' },
          { uid: 'user-b', username: 'Bob' },
        ], 2),
      },
    },
  })
  await harness.mount()
  await harness.flush()
  assert.equal(harness.calls('memoryApi.settings').length, 0)
  const requestCountBeforeSelection = harness.apiState.requests.length

  harness.module.ownerFilter.value = 'user-b'
  harness.module.handleOwnerChange()
  await harness.flush()
  await harness.module.loadJobs()
  await harness.module.loadMigrations()

  const selectedRequests = harness.apiState.requests.slice(requestCountBeforeSelection)
  for (const name of ['memoryApi.list', 'memoryApi.jobs', 'memoryApi.migrations']) {
    const requests = harness.calls(name).filter(request => selectedRequests.includes(request))
    assert.ok(requests.length > 0)
    for (const request of requests) assert.equal(request.args[0].uid, 'user-b')
  }
  assert.equal(harness.calls('memoryApi.settings').length, 0)

  harness.module.openRuntimeDialog('status')
  await harness.flush()
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-b')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-b')
  assert.equal(harness.calls('memoryApi.settings').length, 1)
  assert.equal(harness.calls('memoryApi.settings')[0].args[0].params.uid, 'user-b')
})

test('blocks runtime actions without an owner in the all-user scope', async t => {
  const harness = createHarness(t, { superuser: true })
  await harness.mount()
  await harness.flush()

  for (const action of ['status', 'organize', 'reindex']) {
    harness.module.openRuntimeDialog(action)
    await harness.flush()

    assert.equal(harness.module.runtimeDialogVisible.value, true)
    assert.equal(harness.module.runtimeDialogAction.value, action)
    assert.equal(harness.module.runtimeOwnerUid.value, null)
    assert.equal(harness.module.organizeBlocked.value, true)
    assert.equal(harness.module.reindexBlocked.value, true)

    await harness.module.submitRuntimeOperation('organize')
    await harness.module.submitRuntimeOperation('reindex')
    assert.equal(harness.calls('memoryApi.settings').length, 0)
    assert.equal(harness.calls('memoryApi.organize').length, 0)
    assert.equal(harness.calls('memoryApi.reindex').length, 0)

    harness.module.closeRuntimeDialog()
    assert.equal(harness.module.runtimeDialogVisible.value, false)
  }
})

test('keeps runtime state independent while switching the list owner', async t => {
  const harness = createHarness(t, {
    superuser: true,
    apiOverrides: {
      memoryApi: {
        settings: () => dataResponse({
          configured: true,
          current_job: { id: 7, operation: 'organize', status: 'running' },
        }),
      },
    },
  })
  await harness.mount()
  await harness.flush()

  harness.module.ownerFilter.value = 'user-a'
  harness.module.openRuntimeDialog('status')
  await harness.flush()
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')

  harness.module.activeTab.value = 'jobs'
  Object.assign(harness.module.settings, {
    configured: true,
    current_job: { id: 7, operation: 'organize', status: 'running' },
  })
  harness.module.memories.value = [{ id: 1 }]
  harness.module.memoryTotal.value = 1
  harness.module.memoryPage.value = 4
  harness.module.jobs.value = [{ id: 2 }]
  harness.module.jobTotal.value = 2
  harness.module.jobPage.value = 5
  harness.module.migrations.value = [{ id: 3 }]
  harness.module.migrationTotal.value = 3
  harness.module.migrationPage.value = 6
  harness.module.selectedMemory.value = { id: 1 }
  harness.module.selectedHistoryMemory.value = { id: 4 }
  harness.module.detailsVisible.value = true
  harness.module.history.value = [{ version: 1 }]
  harness.module.historyVisible.value = true
  harness.module.selectedJob.value = { id: 2 }
  harness.module.jobVisible.value = true
  harness.module.selectedMigration.value = { id: 3 }
  harness.module.migrationVisible.value = true
  assert.equal(harness.module.currentMemoryTask.value.status, 'running')

  harness.module.ownerFilter.value = 'user-b'
  const settingsBeforeSwitch = normalize(harness.module.settings)
  const currentTaskBeforeSwitch = normalize(harness.module.currentMemoryTask.value)
  const requestCountBeforeSwitch = harness.apiState.requests.length
  harness.module.handleOwnerChange()

  assert.deepEqual(normalize(harness.module.settings), settingsBeforeSwitch)
  assert.deepEqual(normalize(harness.module.memories.value), [])
  assert.equal(harness.module.memoryTotal.value, 0)
  assert.equal(harness.module.memoryPage.value, 1)
  assert.deepEqual(normalize(harness.module.jobs.value), [])
  assert.equal(harness.module.jobTotal.value, 0)
  assert.equal(harness.module.jobPage.value, 1)
  assert.deepEqual(normalize(harness.module.migrations.value), [])
  assert.equal(harness.module.migrationTotal.value, 0)
  assert.equal(harness.module.migrationPage.value, 1)
  assert.equal(harness.module.selectedMemory.value, null)
  assert.equal(harness.module.selectedHistoryMemory.value, null)
  assert.equal(harness.module.detailsVisible.value, false)
  assert.deepEqual(normalize(harness.module.history.value), [])
  assert.equal(harness.module.historyVisible.value, false)
  assert.equal(harness.module.selectedJob.value, null)
  assert.equal(harness.module.jobVisible.value, false)
  assert.equal(harness.module.selectedMigration.value, null)
  assert.equal(harness.module.migrationVisible.value, false)
  assert.deepEqual(normalize(harness.module.currentMemoryTask.value), currentTaskBeforeSwitch)
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')

  await harness.flush()
  const refreshedRequests = harness.apiState.requests.slice(requestCountBeforeSwitch)
  const settingsRequests = refreshedRequests.filter(request => request.name === 'memoryApi.settings')
  const listRequests = refreshedRequests.filter(request => request.name === 'memoryApi.list')
  const jobRequests = refreshedRequests.filter(request => request.name === 'memoryApi.jobs')
  const migrationRequests = refreshedRequests.filter(request => request.name === 'memoryApi.migrations')

  assert.equal(settingsRequests.length, 1)
  assert.equal(settingsRequests[0].args[0].params.uid, 'user-a')
  assert.equal(listRequests.length, 1)
  assert.equal(listRequests[0].args[0].uid, 'user-b')
  assert.equal(jobRequests.length, 1)
  assert.equal(jobRequests[0].args[0].uid, 'user-b')
  assert.equal(migrationRequests.length, 0)
  assert.deepEqual(normalize(harness.module.settings), settingsBeforeSwitch)
  assert.deepEqual(normalize(harness.module.currentMemoryTask.value), currentTaskBeforeSwitch)
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
})
