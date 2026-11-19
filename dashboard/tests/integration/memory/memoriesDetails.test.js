import assert from 'node:assert/strict'
import test from 'node:test'
import { watch } from 'vue'

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

test('opens each detail type with the response owner and id', async t => {
  for (const detailCase of detailCases) {
    const harness = createDetailHarness(t, detailCase)
    await harness.mount()
    const row = detailRow(detailCase, 'owner-a', 'success')
    const pending = harness.module[detailCase.show](row)
    const request = harness.calls(detailCase.requestName)[0]

    assert.equal(request.args[0], row.id)
    request.deferred.resolve(detailResponse(row, 'selected'))
    await pending

    assert.equal(harness.module[detailCase.selected].value.id, row.id)
    assert.equal(harness.module[detailCase.selected].value.owner_uid, 'owner-a')
    assert.equal(harness.module[detailCase.selected].value.marker, 'selected')
    assert.equal(harness.module[detailCase.visible].value, true)
  }
})

const editSelectedMemoryCases = [
  {
    name: 'ordinary user edits their own record',
    superuser: false,
    currentUid: 'user-a',
    currentUsername: 'Alice',
    ownerUid: 'user-a',
  },
  {
    name: 'administrator edits another record in all-user scope',
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    ownerUid: 'user-b',
  },
]

test('transfers fresh details into edit mode and preserves a closed-details draft', async t => {
  for (const editCase of editSelectedMemoryCases) {
    const detail = {
      id: 701,
      version: 9,
      owner_uid: editCase.ownerUid,
      memory_key: `${editCase.name}-detail-key`,
      memory_type: 'preference',
      content: `${editCase.name}-detail-content`,
      change_evidence: `${editCase.name}-detail-evidence`,
    }
    const harness = createHarness(t, {
      superuser: editCase.superuser,
      currentUid: editCase.currentUid,
      currentUsername: editCase.currentUsername,
      apiOverrides: {
        memoryApi: {
          get: () => dataResponse(detail),
        },
      },
    })
    await harness.mount()

    const listRow = {
      id: detail.id,
      version: 8,
      owner_uid: editCase.ownerUid,
      memory_key: `${editCase.name}-list-key`,
      memory_type: 'fact',
      content: `${editCase.name}-list-content`,
      change_evidence: `${editCase.name}-list-evidence`,
    }
    await harness.module.showDetails(listRow)
    assert.equal(harness.module.detailsVisible.value, true, editCase.name)
    assert.equal(harness.module.editorVisible.value, false, editCase.name)

    const stopDetailsVisibilityWatch = watch(
      () => harness.module.editorVisible.value,
      (visible, previousVisible) => {
        if (previousVisible === false && visible === true) assert.equal(harness.module.detailsVisible.value, false, editCase.name)
      },
      { flush: 'sync' },
    )
    try {
      harness.module.editSelectedMemory()

      assert.equal(harness.module.editorMode.value, 'edit', editCase.name)
      assert.equal(harness.module.detailsVisible.value, false, editCase.name)
      assert.equal(harness.module.editorVisible.value, true, editCase.name)
      assert.deepEqual(normalize(harness.module.form), {
        id: detail.id,
        version: detail.version,
        owner_uid: detail.owner_uid,
        memory_key: detail.memory_key,
        memory_type: detail.memory_type,
        content: detail.content,
        change_evidence: detail.change_evidence,
        suppress_current: false,
      }, editCase.name)

      Object.assign(harness.module.form, {
        memory_key: `${editCase.name}-draft-key`,
        content: `${editCase.name}-draft-content`,
        change_evidence: `${editCase.name}-draft-evidence`,
      })
      const draft = normalize(harness.module.form)
      harness.module.editSelectedMemory()

      assert.equal(harness.module.detailsVisible.value, false, editCase.name)
      assert.equal(harness.module.editorVisible.value, true, editCase.name)
      assert.deepEqual(normalize(harness.module.form), draft, editCase.name)
    } finally {
      stopDetailsVisibilityWatch()
    }
  }
})

test('isolates pending memory details after entering edit mode', async t => {
  const detailCase = detailCases[0]
  const detailA = {
    id: 801,
    version: 12,
    owner_uid: 'owner-a',
    memory_key: 'record-a-key',
    memory_type: 'preference',
    content: 'record-a-content',
    change_evidence: 'record-a-evidence',
  }
  const detailB = {
    id: 802,
    version: 13,
    owner_uid: 'owner-b',
    memory_key: 'record-b-key',
    memory_type: 'fact',
    content: 'record-b-content',
    change_evidence: 'record-b-evidence',
  }

  for (const staleOutcome of ['success', 'failure']) {
    const harness = createDetailHarness(t, detailCase, true)
    await harness.mount()

    const firstPending = harness.module.showDetails(detailA)
    const firstRequest = harness.calls(detailCase.requestName)[0]
    firstRequest.deferred.resolve(dataResponse(detailA))
    await firstPending

    assert.equal(harness.module.selectedMemory.value.id, detailA.id, staleOutcome)
    assert.equal(harness.module.selectedMemory.value.version, detailA.version, staleOutcome)
    assert.equal(harness.module.selectedMemory.value.owner_uid, detailA.owner_uid, staleOutcome)
    assert.equal(harness.module.selectedMemory.value.content, detailA.content, staleOutcome)
    assert.equal(harness.module.detailsVisible.value, true, staleOutcome)

    const secondPending = harness.module.showDetails(detailB)
    const secondRequest = harness.calls(detailCase.requestName)[1]
    harness.module.editSelectedMemory()
    Object.assign(harness.module.form, {
      memory_key: 'draft-key',
      content: 'draft-content',
      change_evidence: 'draft-evidence',
    })
    const draft = normalize(harness.module.form)
    const messagesBeforeStaleResponse = normalize(harness.messages)

    if (staleOutcome === 'success') secondRequest.deferred.resolve(dataResponse(detailB))
    else secondRequest.deferred.reject(new Error('stale detail response'))
    await secondPending

    assert.equal(harness.module.detailsVisible.value, false, staleOutcome)
    assert.equal(harness.module.editorVisible.value, true, staleOutcome)
    assert.equal(harness.module.form.id, detailA.id, staleOutcome)
    assert.equal(harness.module.form.version, detailA.version, staleOutcome)
    assert.equal(harness.module.form.owner_uid, detailA.owner_uid, staleOutcome)
    assert.deepEqual(normalize(harness.module.form), draft, staleOutcome)
    assert.deepEqual(normalize(harness.messages), messagesBeforeStaleResponse, staleOutcome)
  }
})

test('keeps the editor closed and preserves state for blocked selected-memory edits', async t => {
  const row = {
    id: 702,
    version: 4,
    owner_uid: 'user-a',
    memory_key: 'selected-key',
    memory_type: 'fact',
    content: 'selected-content',
    change_evidence: 'selected-evidence',
  }
  const blockedCases = [
    { name: 'no selected record', selected: null, detailsVisible: true },
    { name: 'details closed', selected: row, detailsVisible: false },
    { name: 'record processing a mutation', selected: { ...row, pending_mutation_job_id: 901 }, detailsVisible: true },
    { name: 'deleted record', selected: { ...row, deleted_at: '2026-01-01T00:00:00Z' }, detailsVisible: true },
    { name: 'inactive record', selected: { ...row, is_active: false }, detailsVisible: true },
  ]

  for (const blockedCase of blockedCases) {
    const harness = createHarness(t)
    await harness.mount()
    harness.module.selectedMemory.value = blockedCase.selected
    harness.module.detailsVisible.value = blockedCase.detailsVisible
    harness.module.editorVisible.value = false
    Object.assign(harness.module.form, {
      id: 801,
      version: 6,
      owner_uid: 'user-a',
      memory_key: 'draft-key',
      memory_type: 'preference',
      content: 'draft-content',
      change_evidence: 'draft-evidence',
      suppress_current: true,
    })
    const stateBefore = {
      form: normalize(harness.module.form),
      detailsVisible: harness.module.detailsVisible.value,
    }
    const messagesBefore = normalize(harness.messages)

    harness.module.editSelectedMemory()

    assert.equal(harness.module.editorVisible.value, false, blockedCase.name)
    assert.equal(harness.module.detailsVisible.value, stateBefore.detailsVisible, blockedCase.name)
    assert.deepEqual(normalize(harness.module.form), stateBefore.form, blockedCase.name)
    assert.equal(harness.calls('memoryApi.create').length, 0, blockedCase.name)
    assert.equal(harness.calls('memoryApi.update').length, 0, blockedCase.name)
    assert.deepEqual(normalize(harness.messages), messagesBefore, blockedCase.name)
  }
})

test('keeps only the newest detail request for success and failure races', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    for (const detailCase of detailCases) {
      const harness = createDetailHarness(t, detailCase, true)
      await harness.mount()
      const firstRow = detailRow(detailCase, 'owner-a', 'first')
      const secondRow = detailRow(detailCase, 'owner-b', 'second')
      const firstPending = harness.module[detailCase.show](firstRow)
      const secondPending = harness.module[detailCase.show](secondRow)
      const requests = harness.calls(detailCase.requestName)
      assert.equal(requests.length, 2)

      requests[1].deferred.resolve(detailResponse(secondRow, 'second'))
      await secondPending
      const messagesBeforeStaleResponse = harness.messages.length

      if (staleOutcome === 'success') requests[0].deferred.resolve(detailResponse(firstRow, 'first'))
      else requests[0].deferred.reject(new Error('stale detail response'))
      await firstPending

      assert.equal(harness.module[detailCase.selected].value.id, secondRow.id)
      assert.equal(harness.module[detailCase.selected].value.owner_uid, 'owner-b')
      assert.equal(harness.module[detailCase.selected].value.marker, 'second')
      assert.equal(harness.module[detailCase.visible].value, true)
      assert.equal(harness.messages.length, messagesBeforeStaleResponse)
    }
  }
})

test('ignores detail responses after owner changes away and back', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    for (const detailCase of detailCases) {
      const harness = createDetailHarness(t, detailCase, true)
      await harness.mount()
      harness.module.ownerFilter.value = 'owner-a'
      const row = detailRow(detailCase, 'owner-a', 'stale-owner')
      const pending = harness.module[detailCase.show](row)
      const request = harness.calls(detailCase.requestName)[0]

      harness.module.ownerFilter.value = 'owner-b'
      harness.module.handleOwnerChange()
      harness.module.ownerFilter.value = 'owner-a'
      harness.module.handleOwnerChange()
      await harness.flush()
      const messagesBeforeStaleResponse = harness.messages.length

      if (staleOutcome === 'success') request.deferred.resolve(detailResponse(row, 'stale'))
      else request.deferred.reject(new Error('stale owner response'))
      await pending

      assert.equal(harness.module[detailCase.selected].value, null)
      assert.equal(harness.module[detailCase.visible].value, false)
      assert.equal(harness.messages.length, messagesBeforeStaleResponse)
    }
  }
})

test('ignores detail responses after unmount without messaging', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    for (const detailCase of detailCases) {
      const harness = createDetailHarness(t, detailCase)
      await harness.mount()
      const row = detailRow(detailCase, 'owner-a', 'unmounted')
      const pending = harness.module[detailCase.show](row)
      const request = harness.calls(detailCase.requestName)[0]
      harness.unmount()
      const messagesBeforeStaleResponse = harness.messages.length

      if (staleOutcome === 'success') request.deferred.resolve(detailResponse(row, 'stale'))
      else request.deferred.reject(new Error('unmounted detail response'))
      await pending

      assert.equal(harness.module[detailCase.selected].value, null)
      assert.equal(harness.module[detailCase.visible].value, false)
      assert.equal(harness.messages.length, messagesBeforeStaleResponse)
    }
  }
})

const createHistoryHarness = (t, superuser = false) => createHarness(t, {
  superuser,
  ...(superuser ? {} : { currentUid: 'owner-a' }),
  apiOverrides: {
    memoryApi: {
      history: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

test('loads history with the standard first page and selected memory', async t => {
  const harness = createHistoryHarness(t)
  await harness.mount()
  const row = { id: 'memory-history', owner_uid: 'owner-a', memory_key: 'alpha' }
  const pending = harness.module.showHistory(row)
  const request = harness.calls('memoryApi.history')[0]

  assert.equal(request.args[0], row.id)
  assert.deepEqual(normalize(request.args[1]), { page: 1, size: 100 })
  request.deferred.resolve(pageResponse([{ version: 2, content: 'history-entry' }], 1))
  await pending

  assert.equal(harness.module.selectedHistoryMemory.value.id, row.id)
  assert.equal(harness.module.selectedHistoryMemory.value.owner_uid, 'owner-a')
  assert.equal(harness.module.historyVisible.value, true)
  assert.equal(harness.module.historyLoading.value, false)
  assert.equal(harness.module.history.value.length, 1)
  assert.equal(harness.module.history.value[0].version, 2)
  assert.equal(harness.module.history.value[0].content, 'history-entry')
})

test('keeps new history loading while an owner-switched request settles', async t => {
  const harness = createHistoryHarness(t, true)
  await harness.mount()
  const oldRow = { id: 'history-old', owner_uid: 'owner-a', memory_key: 'old' }
  const oldPending = harness.module.showHistory(oldRow)
  const oldRequest = harness.calls('memoryApi.history')[0]

  harness.module.ownerFilter.value = 'owner-b'
  harness.module.handleOwnerChange()
  await harness.flush()

  const newRow = { id: 'history-new', owner_uid: 'owner-b', memory_key: 'new' }
  const newPending = harness.module.showHistory(newRow)
  const newRequest = harness.calls('memoryApi.history')[1]
  assert.equal(harness.module.historyLoading.value, true)

  oldRequest.deferred.reject(new Error('stale history response'))
  await oldPending
  assert.equal(harness.module.selectedHistoryMemory.value.id, newRow.id)
  assert.equal(harness.module.historyLoading.value, true)
  assert.equal(harness.module.history.value.length, 0)
  assert.equal(harness.messages.length, 0)

  newRequest.deferred.resolve(pageResponse([{ version: 3, content: 'new-history' }], 1))
  await newPending
  assert.equal(harness.module.historyLoading.value, false)
  assert.equal(harness.module.history.value.length, 1)
  assert.equal(harness.module.history.value[0].content, 'new-history')
})

const createHistoryDetailsHarness = t => createHarness(t, {
  superuser: true,
  apiOverrides: {
    memoryApi: {
      get: (...args) => args[args.length - 1].deferred.promise,
      history: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

test('keeps details and history ownership independent in the all-user scope', async t => {
  const harness = createHistoryDetailsHarness(t)
  await harness.mount()
  assert.equal(harness.module.runtimeOwnerUid.value, null)

  const memoryA = { id: 101, owner_uid: 'owner-a', memory_key: 'memory-a', content: 'details-a' }
  const memoryB = { id: 202, owner_uid: 'owner-b', memory_key: 'memory-b' }
  const detailsPending = harness.module.showDetails(memoryA)
  const detailsRequest = harness.calls('memoryApi.get')[0]
  const historyPending = harness.module.showHistory(memoryB)
  const historyRequest = harness.calls('memoryApi.history')[0]

  historyRequest.deferred.resolve(pageResponse([{ version: 1, content: 'history-b' }], 1))
  await historyPending
  assert.equal(harness.module.selectedMemory.value, null)
  assert.equal(harness.module.selectedHistoryMemory.value.id, memoryB.id)
  assert.equal(harness.module.selectedHistoryMemory.value.memory_key, 'memory-b')
  assert.equal(harness.module.selectedHistoryMemory.value.owner_uid, 'owner-b')
  assert.equal(harness.module.history.value[0].content, 'history-b')

  detailsRequest.deferred.resolve(dataResponse(memoryA))
  await detailsPending
  assert.equal(harness.module.selectedMemory.value.id, memoryA.id)
  assert.equal(harness.module.selectedMemory.value.owner_uid, 'owner-a')
  assert.equal(harness.module.selectedMemory.value.memory_key, 'memory-a')
  assert.equal(harness.module.selectedHistoryMemory.value.id, memoryB.id)
  assert.equal(harness.module.selectedHistoryMemory.value.memory_key, 'memory-b')
  assert.equal(harness.module.selectedHistoryMemory.value.owner_uid, 'owner-b')
  assert.equal(harness.module.history.value[0].content, 'history-b')

  const reverseHistoryPending = harness.module.showHistory(memoryB)
  const reverseHistoryRequest = harness.calls('memoryApi.history')[1]
  reverseHistoryRequest.deferred.resolve(pageResponse([{ version: 2, content: 'history-b-again' }], 1))
  await reverseHistoryPending
  assert.equal(harness.module.selectedMemory.value.id, memoryA.id)
  assert.equal(harness.module.selectedMemory.value.owner_uid, 'owner-a')
  assert.equal(harness.module.selectedHistoryMemory.value.id, memoryB.id)
  assert.equal(harness.module.selectedHistoryMemory.value.owner_uid, 'owner-b')
  assert.equal(harness.module.history.value[0].content, 'history-b-again')
})
