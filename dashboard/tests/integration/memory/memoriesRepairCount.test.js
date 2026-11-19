import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createHarness,
  normalize,
  pageResponse,
} from './memoriesViewHarness.js'

const repairRequiredMeta = (count, identity = {}) => ({
  is_superuser: false,
  current_uid: 'user-a',
  current_username: 'Alice',
  repair_required_count: count,
  ...identity,
})

test('uses the authorized list metadata count rather than the current page or pending records', async t => {
  let listCallCount = 0
  const harness = createHarness(t, {
    apiOverrides: {
      memoryApi: {
        list: () => {
          listCallCount += 1
          return listCallCount === 1
            ? pageResponse([], 0, repairRequiredMeta(4))
            : pageResponse([{ id: 'pending-record', pending_mutation_job_id: 91 }], 1, repairRequiredMeta(0))
        },
      },
    },
  })
  await harness.mount()

  assert.deepEqual(normalize(harness.module.memories.value), [])
  assert.equal(harness.module.repairRequiredCount.value, 4)

  await harness.module.loadMemories()

  assert.equal(harness.module.memories.value[0].pending_mutation_job_id, 91)
  assert.equal(harness.module.repairRequiredCount.value, 0)
})

test('ignores an old repair count after an administrator switches list owners', async t => {
  let listCallCount = 0
  const adminIdentity = {
    is_superuser: true,
    current_uid: 'admin-user',
    current_username: 'Administrator',
  }
  const harness = createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    apiOverrides: {
      memoryApi: {
        list: (...args) => {
          listCallCount += 1
          return listCallCount === 1
            ? pageResponse([], 0, repairRequiredMeta(2, adminIdentity))
            : args.at(-1).deferred.promise
        },
      },
    },
  })
  await harness.mount()
  harness.module.ownerFilter.value = 'owner-a'
  const oldPending = harness.module.loadMemories()
  const oldRequest = harness.calls('memoryApi.list')[1]

  harness.module.ownerFilter.value = 'owner-b'
  harness.module.handleOwnerChange()
  assert.equal(harness.module.repairRequiredCount.value, 0)
  await harness.flush()
  const newRequest = harness.calls('memoryApi.list')[2]
  assert.equal(newRequest.args[0].uid, 'owner-b')

  newRequest.deferred.resolve(pageResponse([], 0, repairRequiredMeta(8, adminIdentity)))
  await harness.flush()
  assert.equal(harness.module.repairRequiredCount.value, 8)

  oldRequest.deferred.resolve(pageResponse([], 0, repairRequiredMeta(3, adminIdentity)))
  await oldPending
  await harness.flush()
  assert.equal(harness.module.repairRequiredCount.value, 8)
})

test('clears the repair count after authorization failures or list metadata without authorization', async t => {
  const invalidCases = [
    { name: '401', errorResponse: { status: 401 } },
    { name: '403', errorResponse: { data: { code: 403 } } },
    { name: 'missing authorization metadata' },
  ]

  for (const invalidCase of invalidCases) {
    let listCallCount = 0
    const harness = createHarness(t, {
      apiOverrides: {
        memoryApi: {
          list: () => {
            listCallCount += 1
            if (listCallCount === 1) return pageResponse([], 0, repairRequiredMeta(6))
            if (invalidCase.errorResponse) {
              return Promise.reject(Object.assign(new Error('authorization expired'), {
                response: invalidCase.errorResponse,
              }))
            }
            return pageResponse([], 0, { repair_required_count: 99 })
          },
        },
      },
    })
    await harness.mount()
    assert.equal(harness.module.repairRequiredCount.value, 6, invalidCase.name)

    await harness.module.loadMemories()

    assert.equal(harness.module.repairRequiredCount.value, 0, invalidCase.name)
  }
})

test('clears the repair count on unmount and ignores a late list response', async t => {
  let listCallCount = 0
  const harness = createHarness(t, {
    apiOverrides: {
      memoryApi: {
        list: (...args) => {
          listCallCount += 1
          return listCallCount === 1
            ? pageResponse([], 0, repairRequiredMeta(5))
            : args.at(-1).deferred.promise
        },
      },
    },
  })
  await harness.mount()
  assert.equal(harness.module.repairRequiredCount.value, 5)

  const pending = harness.module.loadMemories()
  const request = harness.calls('memoryApi.list')[1]
  harness.unmount()
  assert.equal(harness.module.repairRequiredCount.value, 0)

  request.deferred.resolve(pageResponse([], 0, repairRequiredMeta(55)))
  await pending
  await harness.flush()
  assert.equal(harness.module.repairRequiredCount.value, 0)
})
