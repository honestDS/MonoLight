import assert from 'node:assert/strict'
import test from 'node:test'
import { renderToString } from 'vue/server-renderer'
import enCommon from '../../../src/i18n/locales/en/common.js'
import enMemories from '../../../src/i18n/locales/en/memories.js'

import {
  confirmationTranslate,
  createDeferred,
  createHarness,
  dataResponse,
  fillEditor,
  normalize,
  pageResponse,
} from './memoriesViewHarness.js'

const createEditorHarness = (t, superuser = false) => createHarness(t, {
  superuser,
  apiOverrides: {
    memoryApi: {
      create: (...args) => args[args.length - 1].deferred.promise,
      update: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

test('scopes create requests to the selected or current owner without body ownership fields', async t => {
  const selectedHarness = createEditorHarness(t, true)
  await selectedHarness.mount()
  selectedHarness.module.ownerFilter.value = 'user-b'
  selectedHarness.module.openEditor()
  assert.equal(selectedHarness.module.form.owner_uid, 'user-b')
  fillEditor(selectedHarness.module)
  const selectedPending = selectedHarness.module.submitMemory()
  const selectedRequest = selectedHarness.calls('memoryApi.create')[0]

  assert.deepEqual(normalize(selectedRequest.args[1]), { params: { uid: 'user-b' } })
  assert.equal(Object.prototype.hasOwnProperty.call(selectedRequest.args[0], 'owner_uid'), false)
  assert.equal(Object.prototype.hasOwnProperty.call(selectedRequest.args[0], 'uid'), false)
  selectedRequest.deferred.resolve(dataResponse({ accepted: true }))
  await selectedPending
  assert.equal(selectedHarness.module.editorVisible.value, false)

  const defaultHarness = createEditorHarness(t, true)
  await defaultHarness.mount()
  defaultHarness.module.openEditor()
  assert.equal(defaultHarness.module.form.owner_uid, defaultHarness.module.currentUid.value)

  const ordinaryHarness = createEditorHarness(t)
  await ordinaryHarness.mount()
  ordinaryHarness.module.openEditor()
  ordinaryHarness.module.form.owner_uid = 'other-user'
  fillEditor(ordinaryHarness.module)
  const ordinaryPending = ordinaryHarness.module.submitMemory()
  const ordinaryRequest = ordinaryHarness.calls('memoryApi.create')[0]

  assert.deepEqual(normalize(ordinaryRequest.args[1]), { params: { uid: 'user-a' } })
  assert.equal(Object.prototype.hasOwnProperty.call(ordinaryRequest.args[0], 'owner_uid'), false)
  assert.equal(Object.prototype.hasOwnProperty.call(ordinaryRequest.args[0], 'uid'), false)
  ordinaryRequest.deferred.resolve(dataResponse({ accepted: true }))
  await ordinaryPending
})

test('updates an external-owner record by positive id and version without changing its owner', async t => {
  const harness = createEditorHarness(t, true)
  await harness.mount()
  const row = {
    id: 42,
    version: 7,
    owner_uid: 'external-owner',
    memory_key: 'existing-key',
    memory_type: 'fact',
    content: 'existing-content',
  }
  harness.module.openEditor(row)
  assert.equal(harness.module.editorMode.value, 'edit')
  assert.equal(harness.module.form.owner_uid, 'external-owner')
  fillEditor(harness.module, { content: 'updated-content' })
  const pending = harness.module.submitMemory()
  const request = harness.calls('memoryApi.update')[0]
  const payload = request.args[0]

  assert.equal(request.args.length, 1)
  assert.equal(payload.memory_id, 42)
  assert.equal(payload.expected_version, 7)
  assert.equal(payload.content, 'updated-content')
  assert.equal(Object.prototype.hasOwnProperty.call(payload, 'owner_uid'), false)
  assert.equal(Object.prototype.hasOwnProperty.call(payload, 'uid'), false)
  assert.equal(harness.module.form.owner_uid, 'external-owner')
  request.deferred.resolve(dataResponse({ accepted: true }))
  await pending
  assert.equal(harness.module.form.owner_uid, 'external-owner')
})

test('blocks editor saves for missing owner, invalid fields, unavailable scope, and closed windows', async t => {
  const blockedCases = [
    {
      name: 'missing owner',
      setup: module => { module.form.owner_uid = '' },
      message: true,
    },
    {
      name: 'blank key',
      setup: module => { module.form.memory_key = '  ' },
      message: true,
    },
    {
      name: 'blank content',
      setup: module => { module.form.content = '\n\t' },
      message: true,
    },
    {
      name: 'overlong content',
      setup: module => { module.form.content = 'x'.repeat(1000) },
      message: true,
      assertTooLong: true,
    },
    {
      name: 'scope not ready',
      setup: module => { module.memoryScopeReady.value = false },
      message: false,
    },
    {
      name: 'editor closed',
      setup: module => { module.editorVisible.value = false },
      message: false,
    },
  ]

  for (const blockedCase of blockedCases) {
    const harness = createEditorHarness(t, true)
    await harness.mount()
    harness.module.openEditor()
    fillEditor(harness.module)
    blockedCase.setup(harness.module)
    if (blockedCase.assertTooLong) assert.equal(harness.module.contentTooLong.value, true)
    const messagesBefore = harness.messages.length

    await harness.module.submitMemory()

    assert.equal(harness.calls('memoryApi.create').length, 0, blockedCase.name)
    assert.equal(harness.calls('memoryApi.update').length, 0, blockedCase.name)
    assert.equal(harness.messages.length, messagesBefore + (blockedCase.message ? 1 : 0), blockedCase.name)
    assert.equal(harness.module.submitting.value, false, blockedCase.name)
  }
})

test('captures the synchronous payload before later form edits', async t => {
  const harness = createEditorHarness(t)
  await harness.mount()
  harness.module.openEditor()
  fillEditor(harness.module, {
    memory_key: 'before-key',
    memory_type: 'preference',
    content: 'before-content',
    change_evidence: 'before-evidence',
  })
  const pending = harness.module.submitMemory()
  const request = harness.calls('memoryApi.create')[0]
  const sentPayload = normalize(request.args[0])

  fillEditor(harness.module, {
    memory_key: 'after-key',
    memory_type: 'todo',
    content: 'after-content',
    change_evidence: 'after-evidence',
  })
  assert.deepEqual(normalize(request.args[0]), sentPayload)
  request.deferred.resolve(dataResponse({ accepted: true }))
  await pending
})

test('allows one save request at a time and retries failures before refreshing on success', async t => {
  const harness = createEditorHarness(t)
  await harness.mount()
  harness.module.openEditor()
  fillEditor(harness.module)
  const listCountBeforeSuccess = harness.calls('memoryApi.list').length
  const settingsCountBeforeSuccess = harness.calls('memoryApi.settings').length

  const failedPending = harness.module.submitMemory()
  const failedRequest = harness.calls('memoryApi.create')[0]
  const duplicatePending = harness.module.submitMemory()
  assert.equal(harness.calls('memoryApi.create').length, 1)
  assert.equal(harness.module.submitting.value, true)
  failedRequest.deferred.reject(new Error('save failed'))
  await Promise.all([failedPending, duplicatePending])
  assert.equal(harness.module.editorVisible.value, true)
  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.messages.at(-1).type, 'error')

  fillEditor(harness.module, { content: 'retry-content' })
  const retryPending = harness.module.submitMemory()
  const retryRequest = harness.calls('memoryApi.create')[1]
  assert.equal(retryRequest.args[0].content, 'retry-content')
  retryRequest.deferred.resolve(dataResponse({ accepted: true }))
  await retryPending

  assert.equal(harness.module.editorVisible.value, false)
  assert.equal(harness.module.submitting.value, false)
  assert.ok(harness.calls('memoryApi.list').length > listCountBeforeSuccess)
  assert.equal(harness.calls('memoryApi.settings').length, settingsCountBeforeSuccess)
})

test('ignores stale saves after owner switch or same-owner close and reopen', async t => {
  const invalidationCases = [
    {
      name: 'owner switch',
      prepare: async harness => {
        harness.module.ownerFilter.value = 'owner-b'
        harness.module.handleOwnerChange()
        await harness.flush()
      },
    },
    {
      name: 'same-owner close',
      prepare: async harness => {
        harness.module.editorVisible.value = false
      },
    },
  ]

  for (const staleOutcome of ['success', 'failure']) {
    for (const invalidationCase of invalidationCases) {
      const harness = createEditorHarness(t, true)
      await harness.mount()
      harness.module.ownerFilter.value = 'owner-a'
      harness.module.openEditor()
      fillEditor(harness.module, { content: 'old-content' })
      const oldPending = harness.module.submitMemory()
      const oldRequest = harness.calls('memoryApi.create')[0]

      await invalidationCase.prepare(harness)
      harness.module.openEditor()
      fillEditor(harness.module, { content: 'new-content' })
      const newPending = harness.module.submitMemory()
      const newRequest = harness.calls('memoryApi.create')[1]
      const messagesBeforeOldResponse = harness.messages.length
      assert.equal(harness.module.submitting.value, true, invalidationCase.name)

      if (staleOutcome === 'success') oldRequest.deferred.resolve(dataResponse({ accepted: true }))
      else oldRequest.deferred.reject(new Error('stale save response'))
      await oldPending

      assert.equal(harness.module.editorVisible.value, true, invalidationCase.name)
      assert.equal(harness.module.submitting.value, true, invalidationCase.name)
      assert.equal(harness.messages.length, messagesBeforeOldResponse, invalidationCase.name)

      newRequest.deferred.resolve(dataResponse({ accepted: true }))
      await newPending
      assert.equal(harness.module.editorVisible.value, false, invalidationCase.name)
      assert.equal(harness.module.submitting.value, false, invalidationCase.name)
    }
  }
})

test('ignores save responses after unmount without clearing submission state or messaging', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createEditorHarness(t)
    await harness.mount()
    harness.module.openEditor()
    fillEditor(harness.module)
    const pending = harness.module.submitMemory()
    const request = harness.calls('memoryApi.create')[0]
    harness.unmount()
    const messagesBeforeResponse = harness.messages.length

    if (staleOutcome === 'success') request.deferred.resolve(dataResponse({ accepted: true }))
    else request.deferred.reject(new Error('unmounted save response'))
    await pending

    assert.equal(harness.module.editorVisible.value, true)
    assert.equal(harness.module.submitting.value, true)
    assert.equal(harness.messages.length, messagesBeforeResponse)
  }
})

const createMenuHarness = (t, options = {}) => createHarness(t, {
  currentUid: 'owner-a',
  ...options,
  apiOverrides: {
    ...options.apiOverrides,
    memoryApi: {
      ...options.apiOverrides?.memoryApi,
      history: (...args) => args[args.length - 1].deferred.promise,
      delete: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

test('routes the history command to the selected row without deleting', async t => {
  const harness = createMenuHarness(t)
  await harness.mount()
  const row = { id: 301, owner_uid: 'owner-a', memory_key: 'history-row' }

  harness.module.handleMemoryMoreAction('history', row)
  const request = harness.calls('memoryApi.history')[0]
  assert.equal(request.args[0], row.id)
  assert.equal(harness.calls('memoryApi.delete').length, 0)
  assert.equal(harness.confirmCalls.length, 0)

  request.deferred.resolve(pageResponse([{ version: 4, content: 'history-content' }], 1))
  await harness.flush()
  assert.equal(harness.module.selectedHistoryMemory.value.id, row.id)
  assert.equal(harness.module.selectedHistoryMemory.value.owner_uid, row.owner_uid)
  assert.equal(harness.module.historyVisible.value, true)
  assert.equal(harness.module.history.value.length, 1)
  assert.equal(harness.module.history.value[0].content, 'history-content')
})

test('toggles pin state through the memory menu and refreshes the list', async t => {
  const memorySnapshot = [{ id: 308, owner_uid: 'owner-a', memory_key: 'toggle-pin-row', pinned: false }]
  const harness = createMenuHarness(t, {
    apiOverrides: {
      memoryApi: {
        list: () => pageResponse(memorySnapshot.map(row => ({ ...row })), memorySnapshot.length, {
          is_superuser: false,
          current_uid: 'owner-a',
          current_username: 'Alice',
        }),
        pin: id => {
          memorySnapshot.find(row => row.id === id).pinned = true
          return dataResponse({})
        },
        unpin: id => {
          memorySnapshot.find(row => row.id === id).pinned = false
          return dataResponse({})
        },
      },
    },
  })
  await harness.mount()
  const firstRow = harness.module.memories.value[0]
  const listCountAfterMount = harness.calls('memoryApi.list').length

  harness.module.handleMemoryMoreAction('toggle-pin', firstRow)
  await harness.flush()

  assert.equal(harness.calls('memoryApi.pin').length, 1)
  assert.equal(harness.calls('memoryApi.pin')[0].args[0], firstRow.id)
  assert.equal(harness.calls('memoryApi.unpin').length, 0)
  assert.equal(harness.module.memories.value[0].pinned, true)
  assert.equal(harness.calls('memoryApi.list').length, listCountAfterMount + 1)

  const refreshedRow = harness.module.memories.value[0]
  harness.module.handleMemoryMoreAction('toggle-pin', refreshedRow)
  await harness.flush()

  assert.equal(harness.calls('memoryApi.pin').length, 1)
  assert.equal(harness.calls('memoryApi.unpin').length, 1)
  assert.equal(harness.calls('memoryApi.unpin')[0].args[0], refreshedRow.id)
  assert.equal(harness.module.memories.value[0].pinned, false)
  assert.equal(harness.calls('memoryApi.list').length, listCountAfterMount + 2)
  assert.equal(harness.messages.filter(message => message.type === 'info').length, 2)
  assert.equal(harness.calls('memoryApi.history').length, 0)
  assert.equal(harness.calls('memoryApi.delete').length, 0)
  assert.equal(harness.confirmCalls.length, 0)
})

test('waits for delete confirmation and sends the positive id, version, and dedupe key', async t => {
  const confirmation = createDeferred()
  const harness = createMenuHarness(t, {
    confirm: () => confirmation.promise,
    translate: confirmationTranslate,
  })
  await harness.mount()
  const row = { id: 302, version: 8, owner_uid: 'owner-a' }

  harness.module.handleMemoryMoreAction('delete', row)
  assert.equal(harness.confirmCalls.length, 1)
  const [message, title, options] = harness.confirmCalls[0]
  const html = await renderToString(message)
  assert.ok(html.includes(enMemories.delete_confirm))
  assert.equal(title, enCommon.warning)
  assert.equal(options.confirmButtonText, enCommon.confirm)
  assert.equal(options.cancelButtonText, enCommon.cancel)
  assert.equal(options.type, 'warning')
  assert.equal(options.showCancelButton, true)
  assert.equal(options.dangerouslyUseHTMLString, false)
  assert.equal(options.customClass, 'app-confirm-dialog')
  assert.equal(harness.calls('memoryApi.delete').length, 0)

  confirmation.resolve(true)
  await harness.flush()
  const request = harness.calls('memoryApi.delete')[0]
  assert.equal(request.args[0].memory_id, 302)
  assert.equal(request.args[0].expected_version, 8)
  assert.equal(typeof request.args[0].dedupe_key, 'string')
  assert.ok(request.args[0].dedupe_key.length > 0)
  request.deferred.resolve(dataResponse({ accepted: true }))
  await harness.flush()
  assert.equal(harness.messages.some(message => message.type === 'info'), true)
})

test('does not delete or report errors when delete confirmation is cancelled or closed', async t => {
  for (const outcome of ['cancel', 'close']) {
    const harness = createMenuHarness(t, { confirm: () => Promise.reject(outcome) })
    await harness.mount()
    harness.module.handleMemoryMoreAction('delete', { id: 303, version: 9, owner_uid: 'owner-a' })
    await harness.flush()

    assert.equal(harness.confirmCalls.length, 1)
    assert.equal(harness.calls('memoryApi.delete').length, 0)
    assert.equal(harness.messages.some(message => message.type === 'error'), false)
    assert.equal(harness.messages.length, 0)
  }
})

test('blocks delete confirmation for non-mutable rows', async t => {
  const rows = [
    { id: 304, version: 1, owner_uid: 'owner-a', pending_mutation_job_id: 901 },
    { id: 305, version: 2, owner_uid: 'owner-a', deleted_at: '2026-01-01T00:00:00Z' },
    { id: 306, version: 3, owner_uid: 'owner-a', is_active: false },
  ]

  for (const row of rows) {
    const harness = createMenuHarness(t)
    await harness.mount()
    harness.module.handleMemoryMoreAction('delete', row)
    await harness.flush()

    assert.equal(harness.confirmCalls.length, 0)
    assert.equal(harness.calls('memoryApi.delete').length, 0)
    assert.equal(harness.messages.length, 0)
  }
})

test('ignores toggle-pin for non-mutable rows', async t => {
  const rows = [
    { id: 309, owner_uid: 'owner-a', pending_mutation_job_id: 901 },
    { id: 310, owner_uid: 'owner-a', deleted_at: '2026-01-01T00:00:00Z' },
    { id: 311, owner_uid: 'owner-a', is_active: false },
  ]

  for (const row of rows) {
    for (const pinned of [false, true]) {
      const harness = createMenuHarness(t)
      await harness.mount()
      const listCountBefore = harness.calls('memoryApi.list').length
      const messagesBefore = harness.messages.length
      const testRow = { ...row, pinned }

      harness.module.handleMemoryMoreAction('toggle-pin', testRow)
      harness.module.handleMemoryMoreAction('toggle-pin', testRow)
      await harness.flush()

      assert.equal(harness.calls('memoryApi.pin').length, 0)
      assert.equal(harness.calls('memoryApi.unpin').length, 0)
      assert.equal(harness.calls('memoryApi.list').length, listCountBefore)
      assert.equal(harness.messages.length, messagesBefore)
      assert.equal(harness.confirmCalls.length, 0)
    }
  }
})

test('reports toggle-pin failures without refreshing the list', async t => {
  for (const pinned of [false, true]) {
    const harness = createMenuHarness(t, {
      apiOverrides: {
        memoryApi: {
          pin: () => Promise.reject(new Error('pin operation failed')),
          unpin: () => Promise.reject(new Error('pin operation failed')),
        },
      },
    })
    await harness.mount()
    const row = { id: pinned ? 313 : 312, owner_uid: 'owner-a', pinned }
    const listCountBefore = harness.calls('memoryApi.list').length

    harness.module.handleMemoryMoreAction('toggle-pin', row)
    await harness.flush()

    const expectedMethod = pinned ? 'memoryApi.unpin' : 'memoryApi.pin'
    const otherMethod = pinned ? 'memoryApi.pin' : 'memoryApi.unpin'
    assert.equal(harness.calls(expectedMethod).length, 1)
    assert.equal(harness.calls(expectedMethod)[0].args[0], row.id)
    assert.equal(harness.calls(otherMethod).length, 0)
    assert.equal(harness.calls('memoryApi.list').length, listCountBefore)
    assert.equal(harness.messages.filter(message => message.type === 'error').length, 1)
    assert.equal(harness.messages[0].value, 'pin operation failed')
    assert.equal(harness.messages.some(message => message.type === 'info'), false)
    assert.equal(harness.confirmCalls.length, 0)
  }
})

test('ignores unknown memory menu commands', async t => {
  const harness = createMenuHarness(t)
  await harness.mount()
  harness.module.handleMemoryMoreAction('unknown-command', { id: 307, version: 4, owner_uid: 'owner-a' })
  await harness.flush()

  assert.equal(harness.confirmCalls.length, 0)
  assert.equal(harness.calls('memoryApi.history').length, 0)
  assert.equal(harness.calls('memoryApi.delete').length, 0)
  assert.equal(harness.calls('memoryApi.pin').length, 0)
  assert.equal(harness.calls('memoryApi.unpin').length, 0)
  assert.equal(harness.messages.length, 0)
})
