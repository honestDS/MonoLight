import assert from 'node:assert/strict'
import test from 'node:test'
import { createI18n } from 'vue-i18n'
import enMemories from '../../../src/i18n/locales/en/memories.js'
import zhMemories from '../../../src/i18n/locales/zh/memories.js'

import {
  createHarness,
  dataResponse,
  lastRequest,
  normalize,
  pageResponse,
} from './memoriesViewHarness.js'

const runtimeOperationReadyResponse = () => dataResponse({
  configured: true,
  blocking: {
    organize: { blocked: false },
    maintenance: { blocked: false },
  },
})

const runtimeOperationBlockedResponse = operation => dataResponse({
  configured: true,
  blocking: {
    [operation === 'organize' ? 'organize' : 'maintenance']: {
      blocked: true,
      reason: operation === 'organize' ? 'organization_active' : 'reindex_active',
      job_id: `${operation}-blocking-job`,
    },
  },
})

const assertNoRuntimeOperationRequests = harness => {
  assert.equal(harness.calls('memoryApi.organize').length, 0)
  assert.equal(harness.calls('memoryApi.reindex').length, 0)
}

const attemptBlockedRuntimeOperation = async (harness, action) => {
  await harness.module.submitRuntimeOperation(action)
  await harness.module.submitRuntimeOperation(action === 'organize' ? 'reindex' : 'organize')
  await harness.module.submitRuntimeOperation('unknown-operation')
}

test('submits organize and reindex with a frozen runtime owner and independent list scope', async t => {
  const roleCases = [
    {
      name: 'administrator',
      superuser: true,
      currentUid: 'admin-user',
      currentUsername: 'Administrator',
      listUid: 'user-a',
      runtimeUid: 'user-b',
      expectedUid: 'user-b',
      expectedUsername: 'Bob',
    },
    {
      name: 'ordinary user',
      superuser: false,
      currentUid: 'user-a',
      currentUsername: 'Alice',
      listUid: 'foreign-list-user',
      runtimeUid: 'foreign-runtime-user',
      expectedUid: 'user-a',
      expectedUsername: 'Alice',
    },
  ]

  for (const operation of ['organize', 'reindex']) {
    for (const roleCase of roleCases) {
      const harness = createHarness(t, {
        superuser: roleCase.superuser,
        currentUid: roleCase.currentUid,
        currentUsername: roleCase.currentUsername,
        apiOverrides: {
          ...(roleCase.superuser ? {
            adminApi: {
              userList: () => pageResponse([
                { uid: 'user-a', username: 'Alice' },
                { uid: 'user-b', username: 'Bob' },
              ], 2),
            },
          } : {}),
          memoryApi: {
            settings: () => runtimeOperationReadyResponse(),
            [operation]: (...args) => args[args.length - 1].deferred.promise,
          },
        },
      })
      await harness.mount()
      await harness.flush()

      harness.module.ownerFilter.value = roleCase.listUid
      if (roleCase.superuser) {
        harness.module.handleOwnerChange()
        await harness.flush()
      }
      harness.module.openRuntimeDialog(operation)
      await harness.flush()
      assert.equal(harness.calls(`memoryApi.${operation}`).length, 0, roleCase.name)

      harness.module.handleRuntimeOwnerChange(roleCase.runtimeUid)
      await harness.flush()
      assert.equal(harness.module.runtimeOwnerUid.value, roleCase.expectedUid, roleCase.name)
      assert.equal(harness.calls(`memoryApi.${operation}`).length, 0, roleCase.name)

      harness.module.filters.keyword = 'list-keyword'
      harness.module.filters.memory_type = 'preference'
      harness.module.jobFilters.status = 'running'
      harness.module.jobFilters.operation = operation
      harness.module.jobFilters.memory_id = 'list-memory-id'

      const pending = harness.module.submitRuntimeOperation(operation)
      const duplicatePending = harness.module.submitRuntimeOperation(operation)
      const request = harness.calls(`memoryApi.${operation}`)[0]
      assert.equal(harness.calls(`memoryApi.${operation}`).length, 1, roleCase.name)
      assert.equal(harness.module.actionLoading.value, operation, roleCase.name)
      assert.deepEqual(Object.keys(request.args[0]).sort(), ['dedupe_key'], roleCase.name)
      assert.equal(typeof request.args[0].dedupe_key, 'string', roleCase.name)
      assert.ok(request.args[0].dedupe_key.length > 0, roleCase.name)
      for (const field of ['uid', 'owner_uid', 'keyword', 'memory_type', 'page', 'size']) {
        assert.equal(Object.prototype.hasOwnProperty.call(request.args[0], field), false, `${roleCase.name}:${field}`)
      }
      assert.deepEqual(normalize(request.args[1]), { params: { uid: roleCase.expectedUid } }, roleCase.name)

      harness.module.handleRuntimeOwnerChange(roleCase.superuser ? 'user-a' : 'foreign-runtime-user-2')
      harness.module.closeRuntimeDialog()
      harness.module.openRuntimeDialog(operation === 'organize' ? 'reindex' : 'organize')
      assert.equal(harness.module.runtimeOwnerUid.value, roleCase.expectedUid, roleCase.name)
      assert.equal(harness.module.runtimeDialogAction.value, operation, roleCase.name)
      assert.equal(harness.module.runtimeDialogVisible.value, true, roleCase.name)

      harness.module.ownerFilter.value = roleCase.superuser ? 'user-c' : 'foreign-list-user-2'
      harness.module.handleOwnerChange()
      harness.module.ownerFilter.value = roleCase.listUid
      harness.module.handleOwnerChange()
      await harness.flush()
      assert.equal(harness.module.runtimeOwnerUid.value, roleCase.expectedUid, roleCase.name)
      assert.equal(harness.calls(`memoryApi.${operation}`).length, 1, roleCase.name)

      request.deferred.resolve(dataResponse({ accepted: true }))
      await Promise.all([pending, duplicatePending])
      await harness.flush()

      assert.equal(harness.module.runtimeDialogVisible.value, false, roleCase.name)
      assert.equal(harness.module.actionLoading.value, '', roleCase.name)
      assert.equal(harness.confirmCalls.length, 0, roleCase.name)
      const successMessages = harness.messages.filter(message => message.type === 'info')
      assert.equal(successMessages.some(message => String(message.value).includes(roleCase.expectedUsername)), true, roleCase.name)
      if (roleCase.superuser) {
        assert.equal(successMessages.some(message => String(message.value).includes('Alice')), false, roleCase.name)
        assert.equal(lastRequest(harness.calls('memoryApi.list')).args[0].uid, roleCase.listUid, roleCase.name)
      } else {
        assert.equal(lastRequest(harness.calls('memoryApi.list')).args[0].uid, undefined, roleCase.name)
      }
      assert.equal(harness.calls(`memoryApi.${operation}`).length, 1, roleCase.name)
      assert.equal(harness.calls(`memoryApi.${operation === 'organize' ? 'reindex' : 'organize'}`).length, 0, roleCase.name)
    }
  }
})

test('blocks runtime operation submission for unavailable settings, owners, modes, and lifecycle states', async t => {
  const readyGuardCases = [
    {
      name: 'closed dialog',
      action: 'organize',
      prepare: async harness => {
        harness.module.openRuntimeDialog('organize')
        await harness.flush()
        harness.module.closeRuntimeDialog()
      },
    },
    {
      name: 'scope not ready',
      action: 'organize',
      prepare: async harness => {
        harness.module.openRuntimeDialog('organize')
        await harness.flush()
        harness.module.memoryScopeReady.value = false
      },
    },
    {
      name: 'mode mismatch',
      action: 'organize',
      submitAction: 'reindex',
      prepare: async harness => {
        harness.module.openRuntimeDialog('organize')
        await harness.flush()
      },
    },
    {
      name: 'unknown operation',
      action: 'reindex',
      submitAction: 'unknown-operation',
      prepare: async harness => {
        harness.module.openRuntimeDialog('reindex')
        await harness.flush()
      },
    },
    {
      name: 'unmounted',
      action: 'organize',
      prepare: async harness => {
        harness.module.openRuntimeDialog('organize')
        await harness.flush()
        harness.unmount()
      },
    },
  ]

  for (const blockedCase of readyGuardCases) {
    const harness = createHarness(t)
    await harness.mount()
    await blockedCase.prepare(harness)
    await harness.module.submitRuntimeOperation(blockedCase.submitAction ?? blockedCase.action)
    assertNoRuntimeOperationRequests(harness)
  }

  const missingOwnerHarness = createHarness(t, { superuser: true })
  await missingOwnerHarness.mount()
  missingOwnerHarness.module.openRuntimeDialog('organize')
  await missingOwnerHarness.flush()
  assert.equal(missingOwnerHarness.module.runtimeOwnerUid.value, null)
  assert.equal(missingOwnerHarness.calls('memoryApi.settings').length, 0)
  await attemptBlockedRuntimeOperation(missingOwnerHarness, 'organize')
  assertNoRuntimeOperationRequests(missingOwnerHarness)

  const loadingHarness = createHarness(t, {
    apiOverrides: {
      memoryApi: {
        settings: (...args) => args[args.length - 1].deferred.promise,
      },
    },
  })
  await loadingHarness.mount()
  loadingHarness.module.openRuntimeDialog('organize')
  await loadingHarness.flush()
  assert.equal(loadingHarness.module.settingsLoading.value, true)
  await attemptBlockedRuntimeOperation(loadingHarness, 'organize')
  assertNoRuntimeOperationRequests(loadingHarness)
  loadingHarness.calls('memoryApi.settings')[0].deferred.resolve(runtimeOperationReadyResponse())
  await loadingHarness.flush()

  const initialErrorHarness = createHarness(t, {
    apiOverrides: {
      memoryApi: {
        settings: (...args) => args[args.length - 1].deferred.promise,
      },
    },
  })
  await initialErrorHarness.mount()
  initialErrorHarness.module.openRuntimeDialog('organize')
  const initialErrorRequest = initialErrorHarness.calls('memoryApi.settings')[0]
  initialErrorRequest.deferred.reject(new Error('initial settings failed'))
  await initialErrorHarness.flush()
  assert.equal(initialErrorHarness.module.settingsLoaded.value, false)
  assert.equal(initialErrorHarness.module.settingsLoadError.value, 'initial settings failed')
  await attemptBlockedRuntimeOperation(initialErrorHarness, 'organize')
  assertNoRuntimeOperationRequests(initialErrorHarness)

  let currentReadCount = 0
  const currentErrorHarness = createHarness(t, {
    apiOverrides: {
      memoryApi: {
        settings: () => {
          currentReadCount += 1
          return currentReadCount === 1
            ? runtimeOperationReadyResponse()
            : Promise.reject(new Error('current settings read failed'))
        },
      },
    },
  })
  await currentErrorHarness.mount()
  currentErrorHarness.module.openRuntimeDialog('organize')
  await currentErrorHarness.flush()
  await currentErrorHarness.module.loadSettings()
  await currentErrorHarness.flush()
  assert.equal(currentErrorHarness.module.settingsLoaded.value, false)
  assert.equal(currentErrorHarness.module.settingsLoadError.value, 'current settings read failed')
  await attemptBlockedRuntimeOperation(currentErrorHarness, 'organize')
  assertNoRuntimeOperationRequests(currentErrorHarness)

  const recoveryCases = [
    {
      name: 'unconfigured organize',
      operation: 'organize',
      initial: () => dataResponse({ configured: false }),
      assertMessage: message => assert.equal(message, 'memories.no_config'),
    },
    {
      name: 'organize backend block',
      operation: 'organize',
      initial: () => runtimeOperationBlockedResponse('organize'),
      assertMessage: message => {
        assert.match(message, /memories\.blocked_with_reason/)
        assert.match(message, /memories\.blocking_organization_active/)
      },
    },
    {
      name: 'reindex backend block',
      operation: 'reindex',
      initial: () => runtimeOperationBlockedResponse('reindex'),
      assertMessage: message => {
        assert.match(message, /memories\.blocked_with_reason/)
        assert.match(message, /memories\.blocking_reindex_active/)
      },
    },
  ]

  for (const recoveryCase of recoveryCases) {
    let state = 'blocked'
    const harness = createHarness(t, {
      apiOverrides: {
        memoryApi: {
          settings: () => state === 'blocked' ? recoveryCase.initial() : runtimeOperationReadyResponse(),
          [recoveryCase.operation]: (...args) => args[args.length - 1].deferred.promise,
        },
      },
    })
    await harness.mount()
    harness.module.openRuntimeDialog(recoveryCase.operation)
    await harness.flush()
    recoveryCase.assertMessage(harness.module.runtimeBlockingMessage.value)
    await attemptBlockedRuntimeOperation(harness, recoveryCase.operation)
    assertNoRuntimeOperationRequests(harness)

    state = 'ready'
    await harness.module.loadSettings()
    await harness.flush()
    assert.equal(harness.module.runtimeBlockingMessage.value, '', recoveryCase.name)
    assert.equal(recoveryCase.operation === 'organize' ? harness.module.organizeBlocked.value : harness.module.reindexBlocked.value, false, recoveryCase.name)

    const pending = harness.module.submitRuntimeOperation(recoveryCase.operation)
    const request = harness.calls(`memoryApi.${recoveryCase.operation}`)[0]
    assert.equal(typeof request.args[0].dedupe_key, 'string', recoveryCase.name)
    assert.ok(request.args[0].dedupe_key.length > 0, recoveryCase.name)
    request.deferred.resolve(dataResponse({ accepted: true }))
    await pending
    await harness.flush()
    assert.equal(harness.module.runtimeDialogVisible.value, false, recoveryCase.name)
  }
})

test('shows localized unconfigured embedding guidance and restores reindex after settings recovery', async t => {
  const localeCases = [
    {
      locale: 'zh',
      memories: zhMemories,
      expectedMessage: '尚未配置嵌入模型，请先在 配置管理 中完成配置。',
    },
    {
      locale: 'en',
      memories: enMemories,
      expectedMessage: 'No embedding model is configured. Configure it in Profiles first.',
    },
  ]

  for (const localeCase of localeCases) {
    const i18n = createI18n({
      legacy: false,
      locale: localeCase.locale,
      messages: { [localeCase.locale]: { memories: localeCase.memories } },
    })
    let configured = false
    const harness = createHarness(t, {
      translate: (key, params) => i18n.global.t(key, params ? { ...params } : {}),
      apiOverrides: {
        memoryApi: {
          settings: () => dataResponse({ configured }),
        },
      },
    })
    await harness.mount()
    harness.module.openRuntimeDialog('reindex')
    await harness.flush()

    assert.equal(harness.module.runtimeBlockingMessage.value, localeCase.expectedMessage, localeCase.locale)
    assert.equal(harness.module.reindexBlocked.value, true, localeCase.locale)
    await harness.module.submitRuntimeOperation('reindex')
    assert.equal(harness.calls('memoryApi.reindex').length, 0, localeCase.locale)

    await harness.module.loadSettings()
    await harness.flush()
    assert.equal(harness.module.runtimeBlockingMessage.value, localeCase.expectedMessage, localeCase.locale)
    assert.equal(harness.module.reindexBlocked.value, true, localeCase.locale)
    assert.equal(harness.calls('memoryApi.reindex').length, 0, localeCase.locale)

    configured = true
    await harness.module.loadSettings()
    await harness.flush()
    assert.equal(harness.module.runtimeBlockingMessage.value, '', localeCase.locale)
    assert.equal(harness.module.reindexBlocked.value, false, localeCase.locale)
  }
})

test('retains the runtime user after a failed operation and retries only after a conflict clears', async t => {
  for (const operation of ['organize', 'reindex']) {
    let settingsCallCount = 0
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
          settings: () => {
            settingsCallCount += 1
            if (settingsCallCount <= 2) return runtimeOperationReadyResponse()
            if (settingsCallCount === 3) return runtimeOperationBlockedResponse(operation)
            return runtimeOperationReadyResponse()
          },
          [operation]: (...args) => args[args.length - 1].deferred.promise,
        },
      },
    })
    await harness.mount()
    await harness.flush()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.handleOwnerChange()
    await harness.flush()
    harness.module.filters.keyword = 'scope-keyword'
    harness.module.filters.memory_type = 'preference'
    harness.module.openRuntimeDialog(operation)
    await harness.flush()
    harness.module.handleRuntimeOwnerChange('user-b')
    await harness.flush()
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-b')

    const listCountBeforeFailure = harness.calls('memoryApi.list').length
    const failedPending = harness.module.submitRuntimeOperation(operation)
    const failedRequest = harness.calls(`memoryApi.${operation}`)[0]
    const failedDedupeKey = failedRequest.args[0].dedupe_key
    assert.deepEqual(normalize(failedRequest.args[1]), { params: { uid: 'user-b' } })
    failedRequest.deferred.reject(new Error('runtime submit failed'))
    await failedPending
    await harness.flush()

    assert.equal(harness.module.runtimeDialogVisible.value, true)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-b')
    assert.equal(harness.module.actionLoading.value, '')
    assert.equal(harness.messages.at(-1).type, 'error')
    assert.equal(harness.messages.at(-1).value, 'runtime submit failed')
    assert.equal(harness.calls('memoryApi.settings').at(-1).args[0].params.uid, 'user-b')
    assert.equal(harness.calls('memoryApi.list').length, listCountBeforeFailure)
    assert.equal(operation === 'organize' ? harness.module.organizeBlocked.value : harness.module.reindexBlocked.value, true)

    await harness.module.submitRuntimeOperation(operation)
    assert.equal(harness.calls(`memoryApi.${operation}`).length, 1)

    await harness.module.loadSettings()
    await harness.flush()
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-b')
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.equal(operation === 'organize' ? harness.module.organizeBlocked.value : harness.module.reindexBlocked.value, false)

    const retryPending = harness.module.submitRuntimeOperation(operation)
    const retryRequest = harness.calls(`memoryApi.${operation}`)[1]
    assert.ok(retryRequest.args[0].dedupe_key.length > 0)
    assert.notEqual(retryRequest.args[0].dedupe_key, failedDedupeKey)
    assert.deepEqual(normalize(retryRequest.args[1]), { params: { uid: 'user-b' } })
    retryRequest.deferred.resolve(dataResponse({ accepted: true }))
    await retryPending
    await harness.flush()

    assert.equal(harness.module.runtimeDialogVisible.value, false)
    assert.equal(harness.module.actionLoading.value, '')
    assert.equal(harness.module.ownerFilter.value, 'user-a')
    assert.equal(harness.calls('memoryApi.settings').length, 4)
    assert.equal(lastRequest(harness.calls('memoryApi.list')).args[0].uid, 'user-a')
    assert.equal(lastRequest(harness.calls('memoryApi.list')).args[0].keyword, 'scope-keyword')
    assert.equal(lastRequest(harness.calls('memoryApi.list')).args[0].memory_type, 'preference')
    assert.equal(harness.messages.at(-1).type, 'info')
    assert.ok(String(harness.messages.at(-1).value).includes('Bob'))
    assert.equal(harness.confirmCalls.length, 0)
  }
})

test('preserves runtime state and avoids cancellation APIs when an operation settles after unmount', async t => {
  for (const operation of ['organize', 'reindex']) {
    for (const staleOutcome of ['success', 'failure']) {
      const harness = createHarness(t, {
        apiOverrides: {
          memoryApi: {
            settings: () => runtimeOperationReadyResponse(),
            [operation]: (...args) => args[args.length - 1].deferred.promise,
          },
        },
      })
      await harness.mount()
      harness.module.openRuntimeDialog(operation)
      await harness.flush()
      const pending = harness.module.submitRuntimeOperation(operation)
      const request = harness.calls(`memoryApi.${operation}`)[0]
      const snapshot = {
        settings: normalize(harness.module.settings),
        currentMemoryTask: normalize(harness.module.currentMemoryTask.value),
        runtimeDialogVisible: harness.module.runtimeDialogVisible.value,
        runtimeDialogAction: harness.module.runtimeDialogAction.value,
        runtimeOwnerFilter: harness.module.runtimeOwnerFilter.value,
        runtimeOwnerUid: harness.module.runtimeOwnerUid.value,
        settingsLoaded: harness.module.settingsLoaded.value,
        settingsLoading: harness.module.settingsLoading.value,
        settingsLoadError: harness.module.settingsLoadError.value,
        actionLoading: harness.module.actionLoading.value,
        messages: normalize(harness.messages),
      }
      const listCount = harness.calls('memoryApi.list').length
      const settingsCount = harness.calls('memoryApi.settings').length

      harness.unmount()
      if (staleOutcome === 'success') request.deferred.resolve(dataResponse({ accepted: true }))
      else request.deferred.reject(new Error('late runtime operation failure'))
      await pending
      await harness.flush()

      assert.deepEqual(normalize(harness.module.settings), snapshot.settings, `${operation}:${staleOutcome}`)
      assert.deepEqual(normalize(harness.module.currentMemoryTask.value), snapshot.currentMemoryTask, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.runtimeDialogVisible.value, snapshot.runtimeDialogVisible, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.runtimeDialogAction.value, snapshot.runtimeDialogAction, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.runtimeOwnerFilter.value, snapshot.runtimeOwnerFilter, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.runtimeOwnerUid.value, snapshot.runtimeOwnerUid, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.settingsLoaded.value, snapshot.settingsLoaded, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.settingsLoading.value, snapshot.settingsLoading, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.settingsLoadError.value, snapshot.settingsLoadError, `${operation}:${staleOutcome}`)
      assert.equal(harness.module.actionLoading.value, snapshot.actionLoading, `${operation}:${staleOutcome}`)
      assert.deepEqual(normalize(harness.messages), snapshot.messages, `${operation}:${staleOutcome}`)
      assert.equal(harness.calls('memoryApi.list').length, listCount, `${operation}:${staleOutcome}`)
      assert.equal(harness.calls('memoryApi.settings').length, settingsCount, `${operation}:${staleOutcome}`)
      assert.equal(harness.calls('memoryApi.cancelJob').length, 0, `${operation}:${staleOutcome}`)
      assert.equal(harness.calls('memoryApi.cancelMigration').length, 0, `${operation}:${staleOutcome}`)
    }
  }
})

test('cancels a silent pending settings read before submitting and ignores its late result', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createHarness(t, {
      apiOverrides: {
        memoryApi: {
          settings: (...args) => args[args.length - 1].deferred.promise,
          organize: (...args) => args[args.length - 1].deferred.promise,
        },
      },
    })
    await harness.mount()
    harness.module.openRuntimeDialog('organize')
    const initialRequest = harness.calls('memoryApi.settings')[0]
    initialRequest.deferred.resolve(runtimeOperationReadyResponse())
    await harness.flush()
    harness.module.settingsLoadError.value = 'keep-current-settings-error'

    const silentPending = harness.module.loadSettings(true)
    const silentRequest = harness.calls('memoryApi.settings')[1]
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(silentRequest.args[0].signal.aborted, false)

    const operationPending = harness.module.submitRuntimeOperation('organize')
    const operationRequest = harness.calls('memoryApi.organize')[0]
    assert.equal(silentRequest.args[0].signal.aborted, true)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
    const snapshot = {
      settings: normalize(harness.module.settings),
      currentMemoryTask: normalize(harness.module.currentMemoryTask.value),
      runtimeOwnerUid: harness.module.runtimeOwnerUid.value,
      settingsLoaded: harness.module.settingsLoaded.value,
      settingsLoading: harness.module.settingsLoading.value,
      settingsLoadError: harness.module.settingsLoadError.value,
      messages: normalize(harness.messages),
    }

    if (staleOutcome === 'success') silentRequest.deferred.resolve(dataResponse({ configured: false }))
    else silentRequest.deferred.reject(new Error('late silent settings failure'))
    await silentPending
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), snapshot.settings, staleOutcome)
    assert.deepEqual(normalize(harness.module.currentMemoryTask.value), snapshot.currentMemoryTask, staleOutcome)
    assert.equal(harness.module.runtimeOwnerUid.value, snapshot.runtimeOwnerUid, staleOutcome)
    assert.equal(harness.module.settingsLoaded.value, snapshot.settingsLoaded, staleOutcome)
    assert.equal(harness.module.settingsLoading.value, snapshot.settingsLoading, staleOutcome)
    assert.equal(harness.module.settingsLoadError.value, snapshot.settingsLoadError, staleOutcome)
    assert.equal(harness.module.actionLoading.value, 'organize', staleOutcome)
    assert.equal(harness.module.runtimeDialogVisible.value, true, staleOutcome)
    assert.deepEqual(normalize(harness.messages), snapshot.messages, staleOutcome)

    await harness.module.submitRuntimeOperation('organize')
    assert.equal(harness.calls('memoryApi.organize').length, 1, staleOutcome)
    operationRequest.deferred.resolve(dataResponse({ accepted: true }))
    await operationPending
    await harness.flush()
  }
})

test('submits cleanup retry once while preserving runtime and list owner scopes', async t => {
  let cleanupSettings = {
    configured: true,
    old_collection_cleanup: { status: 'failed', job_id: 801 },
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
        settings: () => dataResponse(cleanupSettings),
        retryCleanup: (...args) => args[args.length - 1].deferred.promise,
      },
    },
  })
  await harness.mount()
  harness.module.ownerFilter.value = 'user-a'
  harness.module.openRuntimeDialog('status')
  await harness.flush()
  harness.module.handleRuntimeOwnerChange('user-b')
  await harness.flush()

  const settingsCountBeforeRetry = harness.calls('memoryApi.settings').length
  const retryPending = harness.module.retryCleanup(harness.module.cleanupRetryId.value)
  harness.module.retryCleanup(harness.module.cleanupRetryId.value)
  const retryRequests = harness.calls('memoryApi.retryCleanup')
  assert.equal(retryRequests.length, 1)
  assert.deepEqual(retryRequests[0].args, [801])
  assert.equal(harness.module.actionLoading.value, 'cleanup-801')

  harness.module.handleRuntimeOwnerChange('user-a')
  harness.module.closeRuntimeDialog()
  assert.equal(harness.module.runtimeDialogVisible.value, true)
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-b')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-b')

  harness.module.ownerFilter.value = 'user-c'
  harness.module.handleOwnerChange()
  harness.module.ownerFilter.value = 'user-a'
  harness.module.handleOwnerChange()
  await harness.flush()
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-b')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-b')

  cleanupSettings = {
    configured: true,
    old_collection_cleanup: { status: 'pending', job_id: 802 },
  }
  retryRequests[0].deferred.resolve(dataResponse({ accepted: true }))
  await retryPending
  await harness.flush()

  assert.equal(harness.module.actionLoading.value, '')
  assert.equal(harness.module.runtimeDialogVisible.value, true)
  assert.equal(harness.module.runtimeDialogAction.value, 'status')
  assert.equal(harness.module.cleanupRetryId.value, null)
  const settingsAfterRetry = harness.calls('memoryApi.settings').slice(settingsCountBeforeRetry)
  assert.ok(settingsAfterRetry.length > 0)
  assert.equal(settingsAfterRetry.every(request => request.args[0].params.uid === 'user-b'), true)
  assert.equal(harness.calls('memoryApi.list').at(-1).args[0].uid, 'user-a')
  assert.equal(harness.messages.some(message => message.type === 'info' && message.value === 'memories.retry_success'), true)
  assert.equal(harness.confirmCalls.length, 0)
  assert.equal(harness.calls('memoryApi.cancelJob').length, 0)
  assert.equal(harness.calls('memoryApi.cancelMigration').length, 0)
})

test('keeps cleanup retry available after a failed ordinary-user submission', async t => {
  let settings = {
    configured: true,
    old_collection_cleanup: { status: 'failed', job_id: 901 },
  }
  const harness = createHarness(t, {
    apiOverrides: {
      memoryApi: {
        settings: () => dataResponse(settings),
        retryCleanup: (...args) => args[args.length - 1].deferred.promise,
      },
    },
  })
  await harness.mount()
  harness.module.openRuntimeDialog('status')
  await harness.flush()

  const settingsCountBeforeRetry = harness.calls('memoryApi.settings').length
  const failedPending = harness.module.retryCleanup(harness.module.cleanupRetryId.value)
  const failedRequest = harness.calls('memoryApi.retryCleanup')[0]
  failedRequest.deferred.reject(new Error('cleanup retry failed'))
  await failedPending
  await harness.flush()

  assert.equal(harness.module.runtimeDialogVisible.value, true)
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  assert.equal(harness.module.actionLoading.value, '')
  assert.equal(harness.module.cleanupRetryId.value, 901)
  assert.equal(harness.messages.at(-1).type, 'error')
  assert.equal(harness.messages.at(-1).value, 'cleanup retry failed')
  const settingsAfterFailure = harness.calls('memoryApi.settings').slice(settingsCountBeforeRetry)
  assert.ok(settingsAfterFailure.length > 0)
  assert.equal(settingsAfterFailure.every(request => request.args[0].params.uid === 'user-a'), true)

  const retryPending = harness.module.retryCleanup(901)
  const retryRequest = harness.calls('memoryApi.retryCleanup')[1]
  assert.ok(retryRequest)
  assert.equal(harness.module.actionLoading.value, 'cleanup-901')
  settings = {
    configured: true,
    old_collection_cleanup: { status: 'pending', job_id: 902 },
  }
  retryRequest.deferred.resolve(dataResponse({ accepted: true }))
  await retryPending
  await harness.flush()

  assert.equal(harness.module.cleanupRetryId.value, null)
  assert.equal(harness.module.settings.old_collection_cleanup.status, 'pending')
  assert.equal(harness.module.actionLoading.value, '')
  assert.equal(harness.messages.at(-1).value, 'memories.retry_success')
  assert.equal(harness.module.runtimeDialogVisible.value, true)
})

test('keeps the latest cleanup status after a stale silent settings response', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createHarness(t, {
      apiOverrides: {
        memoryApi: {
          settings: (...args) => args[args.length - 1].deferred.promise,
          retryCleanup: (...args) => args[args.length - 1].deferred.promise,
        },
      },
    })
    await harness.mount()
    harness.module.openRuntimeDialog('status')
    const initialRequest = harness.calls('memoryApi.settings')[0]
    initialRequest.deferred.resolve(dataResponse({
      configured: true,
      old_collection_cleanup: { status: 'failed', job_id: 1001 },
    }))
    await harness.flush()

    const oldPending = harness.module.loadSettings(true)
    const oldRequest = harness.calls('memoryApi.settings')[1]
    assert.equal(harness.module.settingsLoading.value, false)

    const cleanupPending = harness.module.retryCleanup(harness.module.cleanupRetryId.value)
    const cleanupRequest = harness.calls('memoryApi.retryCleanup')[0]
    cleanupRequest.deferred.resolve(dataResponse({ accepted: true }))
    await cleanupPending
    await harness.flush()

    const settingsRequests = harness.calls('memoryApi.settings')
    assert.ok(settingsRequests.length > 2)
    const newRequest = settingsRequests.at(-1)
    assert.notEqual(newRequest, oldRequest)
    assert.equal(newRequest.args[0].params.uid, 'user-a')
    newRequest.deferred.resolve(dataResponse({
      configured: true,
      old_collection_cleanup: { status: 'pending', job_id: 1002 },
    }))
    await harness.flush()
    const settingsAfterRetry = normalize(harness.module.settings)
    const messagesAfterRetry = normalize(harness.messages)

    if (staleOutcome === 'success') {
      oldRequest.deferred.resolve(dataResponse({
        configured: true,
        old_collection_cleanup: { status: 'failed', job_id: 1001 },
      }))
    } else oldRequest.deferred.reject(new Error('stale cleanup status failure'))
    await oldPending
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), settingsAfterRetry, staleOutcome)
    assert.equal(harness.module.settings.old_collection_cleanup.status, 'pending', staleOutcome)
    assert.equal(harness.module.settings.old_collection_cleanup.job_id, 1002, staleOutcome)
    assert.equal(harness.module.cleanupRetryId.value, null, staleOutcome)
    assert.equal(harness.module.settingsLoaded.value, true, staleOutcome)
    assert.equal(harness.module.settingsLoadError.value, '', staleOutcome)
    assert.equal(harness.module.actionLoading.value, '', staleOutcome)
    assert.equal(harness.module.runtimeDialogVisible.value, true, staleOutcome)
    assert.deepEqual(normalize(harness.messages), messagesAfterRetry, staleOutcome)
  }
})

test('ignores cleanup retry responses after unmount', async t => {
  for (const outcome of ['success', 'failure']) {
    const harness = createHarness(t, {
      apiOverrides: {
        memoryApi: {
          settings: () => dataResponse({
            configured: true,
            old_collection_cleanup: { status: 'failed', job_id: 1101 },
          }),
          retryCleanup: (...args) => args[args.length - 1].deferred.promise,
        },
      },
    })
    await harness.mount()
    harness.module.openRuntimeDialog('status')
    await harness.flush()

    const pending = harness.module.retryCleanup(harness.module.cleanupRetryId.value)
    const request = harness.calls('memoryApi.retryCleanup')[0]
    const snapshot = {
      settings: normalize(harness.module.settings),
      runtimeOwnerUid: harness.module.runtimeOwnerUid.value,
      runtimeDialogVisible: harness.module.runtimeDialogVisible.value,
      actionLoading: harness.module.actionLoading.value,
      messages: normalize(harness.messages),
      requestCount: harness.apiState.requests.length,
    }

    harness.unmount()
    if (outcome === 'success') request.deferred.resolve(dataResponse({ accepted: true }))
    else request.deferred.reject(new Error('late cleanup failure'))
    await pending
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), snapshot.settings, outcome)
    assert.equal(harness.module.runtimeOwnerUid.value, snapshot.runtimeOwnerUid, outcome)
    assert.equal(harness.module.runtimeDialogVisible.value, snapshot.runtimeDialogVisible, outcome)
    assert.equal(harness.module.actionLoading.value, snapshot.actionLoading, outcome)
    assert.deepEqual(normalize(harness.messages), snapshot.messages, outcome)
    assert.equal(harness.apiState.requests.length, snapshot.requestCount, outcome)
    assert.equal(harness.calls('memoryApi.cancelJob').length, 0, outcome)
    assert.equal(harness.calls('memoryApi.cancelMigration').length, 0, outcome)
  }
})

test('retries migration-row cleanup after the runtime dialog closes for each authorized scope', async t => {
  const cases = [
    {
      name: 'ordinary owner',
      options: { currentUid: 'user-a', currentUsername: 'Alice' },
      ownerUid: 'user-a',
    },
    {
      name: 'superuser other owner',
      options: { superuser: true, currentUid: 'admin-user', currentUsername: 'Administrator' },
      ownerUid: 'user-b',
    },
  ]

  for (const retryCase of cases) {
    const harness = createHarness(t, {
      ...retryCase.options,
      apiOverrides: {
        memoryApi: {
          retryCleanup: () => dataResponse({ accepted: true }),
        },
      },
    })
    await harness.mount()
    harness.module.openRuntimeDialog('status')
    await harness.flush()
    harness.module.closeRuntimeDialog()

    const listCountBeforeRetry = harness.calls('memoryApi.list').length
    await harness.module.retryCleanup(801, {
      owner_uid: retryCase.ownerUid,
      old_collection_cleanup_job_id: 801,
      old_collection_cleanup_status: 'failed',
    })
    await harness.flush()

    assert.equal(harness.module.runtimeDialogVisible.value, false, retryCase.name)
    assert.deepEqual(harness.calls('memoryApi.retryCleanup').map(request => request.args), [[801]], retryCase.name)
    assert.equal(harness.confirmCalls.length, 0, retryCase.name)
    assert.equal(harness.calls('memoryApi.list').length, listCountBeforeRetry + 1, retryCase.name)
  }
})

test('blocks cleanup retry for unauthorized rows and invalid runtime contexts', async t => {
  const validRow = {
    owner_uid: 'user-a',
    old_collection_cleanup_job_id: 801,
    old_collection_cleanup_status: 'failed',
  }
  const cases = [
    { name: 'foreign owner', row: { ...validRow, owner_uid: 'user-b' } },
    { name: 'missing owner', row: { ...validRow, owner_uid: undefined } },
    { name: 'id mismatch', id: 802, row: validRow },
    { name: 'pending row', row: { ...validRow, old_collection_cleanup_status: 'pending' } },
    { name: 'running row', row: { ...validRow, old_collection_cleanup_status: 'running' } },
    {
      name: 'scope not ready',
      row: validRow,
      prepare: harness => { harness.module.memoryScopeReady.value = false },
    },
    {
      name: 'unmounted',
      row: validRow,
      prepare: harness => harness.unmount(),
    },
    { name: 'runtime not opened', id: 801 },
    {
      name: 'runtime not loaded',
      id: 801,
      options: {
        apiOverrides: {
          memoryApi: { settings: (...args) => args.at(-1).deferred.promise },
        },
      },
      prepare: async harness => {
        harness.module.openRuntimeDialog('status')
        await harness.flush()
      },
    },
    {
      name: 'runtime id mismatch',
      id: 802,
      options: {
        apiOverrides: {
          memoryApi: {
            settings: () => dataResponse({
              configured: true,
              old_collection_cleanup: { status: 'failed', job_id: 801 },
            }),
          },
        },
      },
      prepare: async harness => {
        harness.module.openRuntimeDialog('status')
        await harness.flush()
      },
    },
  ]

  for (const retryCase of cases) {
    const harness = createHarness(t, retryCase.options)
    await harness.mount()
    await retryCase.prepare?.(harness)
    await harness.module.retryCleanup(retryCase.id ?? 801, retryCase.row ?? null)
    assert.equal(harness.calls('memoryApi.retryCleanup').length, 0, retryCase.name)
  }
})

test('ignores in-flight superuser cleanup after same-uid demotion', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    let listCallCount = 0
    const harness = createHarness(t, {
      superuser: true,
      currentUid: 'admin-user',
      currentUsername: 'Administrator',
      apiOverrides: {
        memoryApi: {
          list: () => {
            listCallCount += 1
            return listCallCount === 1
              ? pageResponse([{ id: 'superuser-memory', owner_uid: 'user-b' }], 1, {
                is_superuser: true,
                current_uid: 'admin-user',
                current_username: 'Administrator',
              })
              : pageResponse([{ id: 'ordinary-memory', owner_uid: 'admin-user' }], 1, {
                is_superuser: false,
                current_uid: 'admin-user',
                current_username: 'Admin User',
              })
          },
          settings: () => dataResponse({
            configured: true,
            old_collection_cleanup: { status: 'failed', job_id: 1301 },
          }),
          retryCleanup: (...args) => args.at(-1).deferred.promise,
        },
      },
    })
    await harness.mount()
    harness.module.ownerFilter.value = 'user-b'
    harness.module.openRuntimeDialog('status')
    await harness.flush()

    const cleanupPending = harness.module.retryCleanup(1301)
    const cleanupRequest = harness.calls('memoryApi.retryCleanup')[0]
    assert.equal(harness.module.actionLoading.value, 'cleanup-1301', staleOutcome)

    await harness.module.loadMemories()
    await harness.flush()
    assert.equal(harness.module.isSuperuser.value, false, staleOutcome)
    assert.equal(harness.module.currentUid.value, 'admin-user', staleOutcome)
    assert.equal(harness.module.actionLoading.value, '', staleOutcome)
    const stateAfterDemotion = {
      memories: normalize(harness.module.memories.value),
      memoryTotal: harness.module.memoryTotal.value,
      settings: normalize(harness.module.settings),
    }
    const messagesAfterDemotion = normalize(harness.messages)
    const requestCountAfterDemotion = harness.apiState.requests.length

    if (staleOutcome === 'success') cleanupRequest.deferred.resolve(dataResponse({ accepted: true }))
    else cleanupRequest.deferred.reject(new Error('late cleanup retry failure'))
    await cleanupPending
    await harness.flush()

    assert.equal(harness.apiState.requests.length, requestCountAfterDemotion, staleOutcome)
    assert.deepEqual(normalize(harness.messages), messagesAfterDemotion, staleOutcome)
    assert.deepEqual({
      memories: normalize(harness.module.memories.value),
      memoryTotal: harness.module.memoryTotal.value,
      settings: normalize(harness.module.settings),
    }, stateAfterDemotion, staleOutcome)
    assert.equal(harness.module.actionLoading.value, '', staleOutcome)
  }
})
