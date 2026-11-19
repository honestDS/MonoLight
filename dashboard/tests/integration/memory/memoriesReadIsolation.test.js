import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createHarness,
  detailResponse,
  lastRequest,
  normalize,
  pageResponse,
} from './memoriesViewHarness.js'

const scopedLoaderMeta = {
  is_superuser: true,
  current_uid: 'admin-user',
  current_username: 'Administrator',
}

const scopedOwner = marker => String(marker).startsWith('A') ? 'owner-a' : 'owner-b'
const scopedId = (marker, ownerAId, ownerBId) => String(marker).startsWith('A') ? ownerAId : ownerBId

const scopedLoaderCases = [
  {
    name: 'memories',
    method: 'list',
    requestName: 'memoryApi.list',
    loader: 'loadMemories',
    activeTab: 'memories',
    params: request => request.args[0],
    signal: request => request.args[1].signal,
    response: marker => pageResponse([{
      id: scopedId(marker, 401, 402),
      owner_uid: scopedOwner(marker),
      marker,
    }], 1, { ...scopedLoaderMeta }),
    assertState: (harness, marker) => {
      assert.equal(harness.module.memories.value[0].id, scopedId(marker, 401, 402))
      assert.equal(harness.module.memories.value[0].owner_uid, scopedOwner(marker))
      assert.equal(harness.module.memories.value[0].marker, marker)
      assert.equal(harness.module.memoryTotal.value, 1)
      assert.equal(harness.module.isSuperuser.value, true)
      assert.equal(harness.module.currentUid.value, 'admin-user')
    },
    prepareState: harness => { harness.module.memories.value = [{ id: 499, owner_uid: 'owner-a' }]; harness.module.memoryTotal.value = 1 },
    snapshot: harness => ({
      memories: normalize(harness.module.memories.value),
      total: harness.module.memoryTotal.value,
      loading: harness.module.memoriesLoading.value,
    }),
    assertSnapshot: (harness, snapshot) => {
      assert.deepEqual(normalize(harness.module.memories.value), snapshot.memories)
      assert.equal(harness.module.memoryTotal.value, snapshot.total)
      assert.equal(harness.module.memoriesLoading.value, snapshot.loading)
    },
  },
  {
    name: 'jobs',
    method: 'jobs',
    requestName: 'memoryApi.jobs',
    loader: 'loadJobs',
    activeTab: 'jobs',
    params: request => request.args[0],
    signal: request => request.args[1].signal,
    response: marker => pageResponse([{
      id: scopedId(marker, 501, 502),
      owner_uid: scopedOwner(marker),
      marker,
    }], 1),
    assertState: (harness, marker) => {
      assert.equal(harness.module.jobs.value[0].id, scopedId(marker, 501, 502))
      assert.equal(harness.module.jobs.value[0].owner_uid, scopedOwner(marker))
      assert.equal(harness.module.jobs.value[0].marker, marker)
      assert.equal(harness.module.jobTotal.value, 1)
    },
    prepareState: harness => { harness.module.jobs.value = [{ id: 599, owner_uid: 'owner-a' }]; harness.module.jobTotal.value = 1 },
    snapshot: harness => ({
      jobs: normalize(harness.module.jobs.value),
      total: harness.module.jobTotal.value,
      loading: harness.module.jobsLoading.value,
    }),
    assertSnapshot: (harness, snapshot) => {
      assert.deepEqual(normalize(harness.module.jobs.value), snapshot.jobs)
      assert.equal(harness.module.jobTotal.value, snapshot.total)
      assert.equal(harness.module.jobsLoading.value, snapshot.loading)
    },
  },
  {
    name: 'migrations',
    method: 'migrations',
    requestName: 'memoryApi.migrations',
    loader: 'loadMigrations',
    activeTab: 'migrations',
    params: request => request.args[0],
    signal: request => request.args[1].signal,
    response: marker => pageResponse([{
      id: scopedId(marker, 601, 602),
      owner_uid: scopedOwner(marker),
      marker,
    }], 1),
    assertState: (harness, marker) => {
      assert.equal(harness.module.migrations.value[0].id, scopedId(marker, 601, 602))
      assert.equal(harness.module.migrations.value[0].owner_uid, scopedOwner(marker))
      assert.equal(harness.module.migrations.value[0].marker, marker)
      assert.equal(harness.module.migrationTotal.value, 1)
    },
    prepareState: harness => { harness.module.migrations.value = [{ id: 699, owner_uid: 'owner-a' }]; harness.module.migrationTotal.value = 1 },
    snapshot: harness => ({
      migrations: normalize(harness.module.migrations.value),
      total: harness.module.migrationTotal.value,
      loading: harness.module.migrationsLoading.value,
    }),
    assertSnapshot: (harness, snapshot) => {
      assert.deepEqual(normalize(harness.module.migrations.value), snapshot.migrations)
      assert.equal(harness.module.migrationTotal.value, snapshot.total)
      assert.equal(harness.module.migrationsLoading.value, snapshot.loading)
    },
  },
]

const createScopedLoaderHarness = (t, loaderCase) => {
  let listCallCount = 0
  const apiOverrides = {
    memoryApi: {
      list: (...args) => {
        listCallCount += 1
        const record = args[args.length - 1]
        if (loaderCase.method === 'list' && listCallCount > 1) return record.deferred.promise
        return pageResponse([], 0, { ...scopedLoaderMeta })
      },
    },
  }
  if (loaderCase.method !== 'list') {
    apiOverrides.memoryApi[loaderCase.method] = (...args) => args[args.length - 1].deferred.promise
  }
  const harness = createHarness(t, { superuser: true, apiOverrides })
  harness.module.activeTab.value = loaderCase.activeTab
  return harness
}

test('isolates each scoped read loader across A to B with aborted stale responses', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    for (const loaderCase of scopedLoaderCases) {
      const harness = createScopedLoaderHarness(t, loaderCase)
      await harness.mount()
      harness.module.ownerFilter.value = 'owner-a'
      const oldPending = harness.module[loaderCase.loader]()
      await harness.module[loaderCase.loader]()
      const requestsBeforeSwitch = harness.calls(loaderCase.requestName).slice()
      const oldRequest = lastRequest(requestsBeforeSwitch)
      assert.equal(loaderCase.params(oldRequest).uid, 'owner-a', loaderCase.name)
      assert.equal(requestsBeforeSwitch.length, loaderCase.method === 'list' ? 2 : 1, loaderCase.name)

      harness.module.ownerFilter.value = 'owner-b'
      harness.module.handleOwnerChange()
      await harness.flush()
      const requestsAfterSwitch = harness.calls(loaderCase.requestName)
      const newRequest = lastRequest(requestsAfterSwitch)
      assert.equal(requestsAfterSwitch.length, requestsBeforeSwitch.length + 1, loaderCase.name)
      assert.equal(loaderCase.params(newRequest).uid, 'owner-b', loaderCase.name)
      assert.equal(loaderCase.signal(oldRequest).aborted, true, loaderCase.name)

      newRequest.deferred.resolve(loaderCase.response('B'))
      await harness.flush()
      loaderCase.assertState(harness, 'B')
      const messagesAfterNewResponse = harness.messages.length

      if (staleOutcome === 'success') oldRequest.deferred.resolve(loaderCase.response('A'))
      else oldRequest.deferred.reject(new Error(`stale ${loaderCase.name} response`))
      await oldPending
      await harness.flush()
      loaderCase.assertState(harness, 'B')
      assert.equal(harness.messages.length, messagesAfterNewResponse, loaderCase.name)
    }
  }
})

test('does not replay the first A response after an A to B to A scope cycle', async t => {
  for (const loaderCase of scopedLoaderCases) {
    const harness = createScopedLoaderHarness(t, loaderCase)
    await harness.mount()
    harness.module.ownerFilter.value = 'owner-a'
    const firstAPending = harness.module[loaderCase.loader]()
    const firstARequest = lastRequest(harness.calls(loaderCase.requestName))

    harness.module.ownerFilter.value = 'owner-b'
    harness.module.handleOwnerChange()
    await harness.flush()
    const bRequest = lastRequest(harness.calls(loaderCase.requestName))

    harness.module.ownerFilter.value = 'owner-a'
    harness.module.handleOwnerChange()
    await harness.flush()
    const finalARequest = lastRequest(harness.calls(loaderCase.requestName))
    assert.equal(loaderCase.signal(firstARequest).aborted, true, loaderCase.name)
    assert.equal(loaderCase.signal(bRequest).aborted, true, loaderCase.name)
    assert.equal(loaderCase.params(finalARequest).uid, 'owner-a', loaderCase.name)

    finalARequest.deferred.resolve(loaderCase.response('A-final'))
    await harness.flush()
    loaderCase.assertState(harness, 'A-final')
    const messagesAfterFinalA = harness.messages.length

    firstARequest.deferred.resolve(loaderCase.response('A-first-stale'))
    await firstAPending
    await harness.flush()
    loaderCase.assertState(harness, 'A-final')
    assert.equal(harness.messages.length, messagesAfterFinalA, loaderCase.name)

    bRequest.deferred.resolve(loaderCase.response('B-stale'))
    await harness.flush()
    loaderCase.assertState(harness, 'A-final')
    assert.equal(harness.messages.length, messagesAfterFinalA, loaderCase.name)
  }
})

test('does not write delayed scoped reads after unmount', async t => {
  for (const loaderCase of scopedLoaderCases) {
    const harness = createScopedLoaderHarness(t, loaderCase)
    await harness.mount()
    harness.module.ownerFilter.value = 'owner-a'
    loaderCase.prepareState(harness)
    const pending = harness.module[loaderCase.loader]()
    const request = lastRequest(harness.calls(loaderCase.requestName))
    const snapshot = loaderCase.snapshot(harness)
    const identity = {
      isSuperuser: harness.module.isSuperuser.value,
      currentUid: harness.module.currentUid.value,
      currentUsername: harness.module.currentUsername.value,
      runtimeOwnerUid: harness.module.runtimeOwnerUid.value,
    }
    const messagesBeforeResponse = harness.messages.length

    harness.unmount()
    request.deferred.resolve(loaderCase.response('late'))
    await pending
    await harness.flush()

    assert.equal(harness.module.isSuperuser.value, identity.isSuperuser, loaderCase.name)
    assert.equal(harness.module.currentUid.value, identity.currentUid, loaderCase.name)
    assert.equal(harness.module.currentUsername.value, identity.currentUsername, loaderCase.name)
    assert.equal(harness.module.runtimeOwnerUid.value, identity.runtimeOwnerUid, loaderCase.name)
    loaderCase.assertSnapshot(harness, snapshot)
    assert.equal(harness.messages.length, messagesBeforeResponse, loaderCase.name)
  }
})

test('preserves a valid ordinary-user scope after a non-authorization list failure', async t => {
  let listCallCount = 0
  const currentMemory = { id: 'current-memory', owner_uid: 'user-a', marker: 'current' }
  const harness = createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    apiOverrides: {
      memoryApi: {
        list: () => {
          listCallCount += 1
          return listCallCount === 1
            ? pageResponse([currentMemory], 1, {
              is_superuser: false,
              current_uid: 'user-a',
              current_username: 'Alice',
            })
            : Promise.reject(new Error('temporary network failure'))
        },
      },
    },
  })
  await harness.mount()
  await harness.module.loadMemories()

  assert.equal(harness.module.isSuperuser.value, false)
  assert.equal(harness.module.currentUid.value, 'user-a')
  assert.equal(harness.module.currentUsername.value, 'Alice')
  assert.equal(harness.module.memoryScopeReady.value, true)
  assert.deepEqual(normalize(harness.module.memories.value), [currentMemory])
  assert.equal(harness.module.memoryTotal.value, 1)
})

test('ignores pending detail responses after a superuser is demoted', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    let listCallCount = 0
    const harness = createHarness(t, {
      superuser: true,
      currentUid: 'admin-user',
      apiOverrides: {
        memoryApi: {
          list: () => {
            listCallCount += 1
            return listCallCount === 1
              ? pageResponse([{ id: 'before-demotion', owner_uid: 'admin-user' }], 1, {
                is_superuser: true,
                current_uid: 'admin-user',
                current_username: 'Administrator',
              })
              : pageResponse([{ id: 'after-demotion', owner_uid: 'admin-user' }], 1, {
                is_superuser: false,
                current_uid: 'admin-user',
                current_username: 'Admin User',
              })
          },
          get: (...args) => args.at(-1).deferred.promise,
        },
      },
    })
    await harness.mount()
    const row = {
      id: 'pending-demotion-detail',
      version: 1,
      owner_uid: 'admin-user',
      memory_key: 'pending-key',
      memory_type: 'fact',
      content: 'pending-content',
      is_active: true,
    }
    const pending = harness.module.showDetails(row)
    const request = harness.calls('memoryApi.get')[0]
    await harness.module.loadMemories()
    await harness.flush()

    assert.equal(harness.module.selectedMemory.value, null, staleOutcome)
    assert.equal(harness.module.detailsVisible.value, false, staleOutcome)
    assert.equal(harness.module.memories.value[0].owner_uid, 'admin-user', staleOutcome)
    const messagesBeforeLateResponse = normalize(harness.messages)
    if (staleOutcome === 'success') request.deferred.resolve(detailResponse(row, 'late'))
    else request.deferred.reject(new Error('late demotion detail response'))
    await pending
    await harness.flush()

    assert.equal(harness.module.selectedMemory.value, null, staleOutcome)
    assert.equal(harness.module.detailsVisible.value, false, staleOutcome)
    assert.deepEqual(normalize(harness.messages), messagesBeforeLateResponse, staleOutcome)
  }
})
