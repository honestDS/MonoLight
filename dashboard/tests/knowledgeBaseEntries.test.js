import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope, ref, watch } from 'vue'

import * as knowledgeBaseManagement from '../src/utils/knowledgeBaseManagement.js'
import { createKnowledgeBaseScope } from '../src/utils/knowledgeBaseScope.js'
import * as requestTaskManager from '../src/utils/requestTaskManager.js'
import { useUserPermissionsStore } from '../src/composables/useUserPermissions.js'

const source = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeBaseEntries.js', import.meta.url),
  'utf8',
)

const stripImports = sourceText => {
  const keptLines = []
  let inImport = false

  for (const line of sourceText.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const composableScript = stripImports(source).replace(/\bexport\s+/g, '')
const dataResponse = data => ({ data: { data } })
const normalize = value => JSON.parse(JSON.stringify(value))
const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}
const flushPromises = async () => {
  for (let index = 0; index < 12; index += 1) await Promise.resolve()
}

const createHarness = (t, options = {}) => {
  const methods = [
    'get',
    'documents',
    'managedItems',
    'document',
    'managedItem',
    'managedHistory',
    'deleteDocument',
    'deleteManagedItem',
    'retryManagedItem',
  ]
  const apiCalls = Object.fromEntries(methods.map(method => [method, []]))
  const messages = []
  const confirms = []
  const confirmationCloses = []
  const unmountedCallbacks = []
  const timers = []
  let nextTimerId = 1
  const routeId = ref(options.routeId ?? 1)
  const setTimeout = (callback, delay, ...args) => {
    const timer = { id: nextTimerId++, callback, delay, args, cancelled: false, ran: false }
    timers.push(timer)
    return timer.id
  }
  const clearTimeout = id => {
    const timer = timers.find(item => item.id === id)
    if (timer) timer.cancelled = true
  }
  const defaultData = {
    get: id => ({ id, knowledge_base_type: 'user', index_status: 'ready' }),
    documents: () => ({ items: [], total: 0 }),
    managedItems: () => ({ items: [], total: 0 }),
    document: () => ({ content: '' }),
    managedItem: () => ({ content: '' }),
    managedHistory: () => [],
    deleteDocument: (_id, entryId) => ({ id: entryId, status: 'accepted' }),
    deleteManagedItem: (_id, entryId) => ({ id: entryId, status: 'deleted' }),
    retryManagedItem: (_id, entryId) => ({ id: entryId, status: 'retry_submitted' }),
  }
  const describeCall = {
    get: ([id]) => ({ id }),
    documents: ([id, params]) => ({ id, params: { ...params } }),
    managedItems: ([id, params, config]) => ({ id, params: { ...params }, config }),
    document: ([id, entryId]) => ({ id, entryId }),
    managedItem: ([id, entryId]) => ({ id, entryId }),
    managedHistory: ([id, entryId]) => ({ id, entryId }),
    deleteDocument: ([id, entryId]) => ({ id, entryId }),
    deleteManagedItem: ([id, entryId, payload]) => ({ id, entryId, payload }),
    retryManagedItem: ([id, entryId, payload]) => ({ id, entryId, payload }),
  }
  const knowledgeBaseApi = Object.fromEntries(methods.map(method => [method, (...args) => {
    const call = { method, ...describeCall[method](args) }
    apiCalls[method].push(call)
    const handler = options[method]
    if (typeof handler === 'function') return handler(...args, call)
    const result = defaultData[method](...args)
    return Promise.resolve(dataResponse(result))
  }]))

  const ElMessage = (...args) => messages.push({ type: 'message', args })
  ElMessage.success = (...args) => messages.push({ type: 'success', args })
  ElMessage.error = (...args) => messages.push({ type: 'error', args })
  ElMessage.warning = (...args) => messages.push({ type: 'warning', args })
  ElMessage.info = (...args) => messages.push({ type: 'info', args })
  const ElMessageBox = {
    confirm: async (...args) => {
      confirms.push(args)
      const result = typeof options.confirm === 'function'
        ? await options.confirm(...args)
        : options.confirm
      if (result === false) throw 'cancel'
      return result === undefined || result === true ? 'confirm' : result
    },
    close: () => confirmationCloses.push(true),
  }

  const pinia = createPinia()
  setActivePinia(pinia)
  const permissionsStore = useUserPermissionsStore(pinia)
  permissionsStore.currentUser = {
    uid: options.uid ?? 'user-a',
    username: options.username ?? 'User A',
    is_superuser: options.superuser === true,
  }

  const scope = effectScope()
  const context = vm.createContext({
    AbortController,
    computed,
    ElMessage,
    ElMessageBox,
    knowledgeBaseApi,
    createKnowledgeBaseScope,
    setTimeout,
    clearTimeout,
    onBeforeUnmount: callback => unmountedCallbacks.push(callback),
    ref,
    ...requestTaskManager,
    watch,
    useI18n: () => ({ t: key => key }),
    useUserPermissionsStore,
    ...knowledgeBaseManagement,
    __routeId: routeId,
  })
  scope.run(() => {
    vm.runInContext(
      `${composableScript}\nglobalThis.__module = useKnowledgeBaseEntries(__routeId)`,
      context,
      { filename: 'useKnowledgeBaseEntries.js' },
    )
  })

  const harness = {
    api: apiCalls,
    confirms,
    confirmationCloses,
    messages,
    timers,
    routeId,
    permissionsStore,
    module: context.__module,
    activeTimers() {
      return timers.filter(timer => !timer.cancelled && !timer.ran)
    },
    async runNextTimer() {
      const timer = timers.find(item => !item.cancelled && !item.ran)
      if (!timer) return false
      timer.ran = true
      await timer.callback(...timer.args)
      return true
    },
    dispose() {
      if (this.disposed) return
      this.disposed = true
      for (const callback of unmountedCallbacks) callback()
      scope.stop()
    },
    disposed: false,
  }
  t.after(() => harness.dispose())
  return harness
}

const assertPrivateStateCleared = (module, { loading = false } = {}) => {
  assert.equal(module.selectedKb.value, null)
  assert.equal(module.isManaged.value, false)
  assert.equal(module.loading.value, loading)
  assert.equal(module.error.value, null)
  assert.deepEqual(normalize(module.items.value), [])
  assert.equal(module.total.value, 0)
  assert.equal(module.page.value, 1)
  assert.equal(module.pageSize.value, 10)
  assert.equal(module.query.value, '')
  assert.equal(module.entryActionId.value, null)
  assert.equal(module.detailVisible.value, false)
  assert.equal(module.detailLoading.value, false)
  assert.equal(module.detailTitle.value, '')
  assert.equal(module.detailContent.value, '')
  assert.equal(module.detailError.value, null)
  assert.equal(module.historyVisible.value, false)
  assert.equal(module.historyLoading.value, false)
  assert.equal(module.historyTitle.value, '')
  assert.deepEqual(normalize(module.historyItems.value), [])
  assert.equal(module.historyError.value, null)
}

test('loads numeric routes immediately and uses the USER or MANAGED API and labels', async t => {
  const harness = createHarness(t, {
    routeId: 11,
    get: id => Promise.resolve(dataResponse(id === 11
      ? { id, knowledge_base_type: 'user', index_status: 'ready' }
      : { id, knowledge_base_type: 'llm_managed', index_status: 'ready' })),
  })

  assert.deepEqual(harness.api.get.map(call => call.id), [11])
  assert.equal(harness.api.documents.length, 0)
  await flushPromises()
  assert.deepEqual(harness.api.documents[0].params, { page: 1, size: 10 })
  assert.equal(harness.module.getEntryName({ filename: 'handbook.md' }), 'handbook.md')
  assert.equal(harness.module.getEntrySourceLabel({}), 'knowledgeBase.entries_source_manual')

  harness.module.page.value = 3
  harness.module.pageSize.value = 25
  await harness.module.handlePageChange()
  assert.deepEqual(harness.api.documents.at(-1).params, { page: 3, size: 25 })

  harness.routeId.value = 22
  assert.deepEqual(harness.api.get.map(call => call.id), [11, 22])
  await flushPromises()
  assert.deepEqual(harness.api.managedItems.at(-1).params, { page: 1, size: 10, query: '' })
  assert.equal(harness.module.getEntryName({ knowledge_key: 'policy.retention' }), 'policy.retention')
  assert.equal(
    harness.module.getEntrySourceLabel({ source_type: 'user_api' }),
    'knowledgeBase.managed_source_user_api',
  )

  harness.module.page.value = 4
  harness.module.pageSize.value = 30
  harness.module.query.value = 'retention'
  await harness.module.handleSearch()
  assert.deepEqual(harness.api.managedItems.at(-1).params, {
    page: 1,
    size: 30,
    query: 'retention',
  })
})

test('keeps selectedKb empty after get failure and lets refresh retry', async t => {
  let getCount = 0
  const harness = createHarness(t, {
    get: id => {
      getCount += 1
      return getCount === 1
        ? Promise.reject(new Error('offline'))
        : Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' }))
    },
  })

  await flushPromises()
  assert.equal(harness.module.selectedKb.value, null)
  assert.equal(harness.module.loading.value, false)
  assert.equal(harness.module.error.value, 'knowledgeBase.entries_load_failed')
  assert.equal(harness.api.documents.length, 0)

  await harness.module.refresh()
  assert.equal(harness.module.selectedKb.value.id, 1)
  assert.equal(harness.api.get.length, 2)
  assert.equal(harness.api.documents.length, 1)
})

test('latest list request wins and a failed page request releases loading', async t => {
  const stale = deferred()
  const latest = deferred()
  let listCount = 0
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: (_id, _params, _config) => {
      listCount += 1
      if (listCount === 2) return stale.promise
      if (listCount === 3) return latest.promise
      if (listCount === 4) return Promise.reject(new Error('page unavailable'))
      return Promise.resolve(dataResponse({ items: [], total: 0 }))
    },
  })
  await flushPromises()

  harness.module.query.value = 'older term'
  const olderLoad = harness.module.handleSearch()
  harness.module.query.value = 'latest term'
  const latestLoad = harness.module.handleSearch()
  assert.equal(harness.api.managedItems[1].config.signal.aborted, true)
  assert.deepEqual(harness.api.managedItems[1].params, {
    page: 1,
    size: 10,
    query: 'older term',
  })
  assert.deepEqual(harness.api.managedItems[2].params, {
    page: 1,
    size: 10,
    query: 'latest term',
  })

  latest.resolve(dataResponse({ items: [{ id: 2, knowledge_key: 'current' }], total: 1 }))
  await latestLoad
  stale.resolve(dataResponse({ items: [{ id: 1, knowledge_key: 'stale' }], total: 1 }))
  await olderLoad
  assert.deepEqual(harness.module.items.value.map(item => item.knowledge_key), ['current'])
  assert.equal(harness.module.loading.value, false)

  await harness.module.handlePageChange()
  assert.equal(harness.module.loading.value, false)
  assert.equal(harness.module.error.value, 'knowledgeBase.entries_load_failed')
  assert.equal(harness.messages.at(-1).type, 'error')
})

test('prioritizes index and publication statuses and requires an indexed version match', async t => {
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
  })
  await flushPromises()
  const status = row => harness.module.getEntryStatus(row)

  assert.equal(status({
    publication_job_status: 'pending',
    version: 4,
    is_recallable: false,
    indexed_version: 4,
  }),
    'knowledgeBase.entries_status_processing')
  assert.equal(status({
    publication_job_status: 'succeeded',
    version: 4,
    is_recallable: true,
    indexed_version: 4,
  }),
    'knowledgeBase.entries_status_ready')
  assert.equal(status({
    publication_job_status: 'succeeded',
    version: 5,
    is_recallable: true,
    indexed_version: 4,
  }),
    'knowledgeBase.entries_status_pending')

  harness.module.selectedKb.value.index_status = 'pending'
  assert.equal(status({ publication_job_status: 'failed' }), 'knowledgeBase.entries_status_failed')
  assert.equal(status({ pending_job_id: 10 }), 'knowledgeBase.entries_status_processing')
  assert.equal(status({ publication_job_status: 'cancelled' }), 'knowledgeBase.entries_status_cancelled')
  assert.equal(status({}), 'knowledgeBase.entries_status_indexing')

  harness.module.selectedKb.value.index_status = 'failed'
  assert.equal(status({ publication_job_status: 'failed' }),
    'knowledgeBase.entries_status_index_unavailable')
  assert.equal(status({ pending_job_id: 10 }), 'knowledgeBase.entries_status_index_unavailable')

  harness.module.selectedKb.value = { id: 1, knowledge_base_type: 'user', index_status: 'ready' }
  assert.equal(status({ version: 1, is_recallable: false }), 'knowledgeBase.entries_status_ready')
  harness.module.selectedKb.value.index_status = 'pending'
  assert.equal(status({}), 'knowledgeBase.entries_status_indexing')
})

test('polls only pending managed entries on the current page and stops at a terminal result', async t => {
  let managedListCount = 0
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => {
      managedListCount += 1
      return Promise.resolve(dataResponse(managedListCount === 1
        ? { items: [{ id: 5, pending_job_id: 8 }], total: 1 }
        : { items: [{ id: 5, publication_job_status: 'succeeded' }], total: 1 }))
    },
  })
  await flushPromises()
  assert.equal(harness.activeTimers().length, 1)
  assert.equal(harness.activeTimers()[0].delay, 2000)

  assert.equal(await harness.runNextTimer(), true)
  assert.equal(managedListCount, 2)
  assert.equal(harness.activeTimers().length, 0)

  harness.module.items.value = [{ id: 6, publication_job_status: 'pending' }]
  harness.module.page.value = 2
  await harness.module.handlePageChange()
  assert.deepEqual(harness.api.managedItems.at(-1).params, { page: 2, size: 10, query: '' })
  assert.equal(harness.activeTimers().length, 0)

  const userHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'user',
      index_status: 'ready',
    })),
    documents: () => Promise.resolve(dataResponse({
      items: [{ id: 6, pending_job_id: 8, publication_job_status: 'pending' }],
      total: 1,
    })),
  })
  await flushPromises()
  assert.equal(userHarness.activeTimers().length, 0)

  const scopedHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => Promise.resolve(dataResponse({
      items: [{ id: 8, pending_job_id: 9 }],
      total: 1,
    })),
  })
  await flushPromises()
  assert.equal(scopedHarness.activeTimers().length, 1)
  scopedHarness.routeId.value = 2
  assert.equal(scopedHarness.activeTimers().length, 0)
  await flushPromises()
  assert.equal(scopedHarness.activeTimers().length, 1)
  scopedHarness.permissionsStore.clear()
  assert.equal(scopedHarness.activeTimers().length, 0)
  scopedHarness.permissionsStore.currentUser = {
    uid: 'user-a',
    username: 'User A',
    is_superuser: false,
  }
  await flushPromises()
  assert.equal(scopedHarness.activeTimers().length, 1)
  scopedHarness.dispose()
  assert.equal(scopedHarness.activeTimers().length, 0)
})

test('poll refreshes managed metadata and moves the pending entry to ready', async t => {
  let getCount = 0
  let listCount = 0
  const harness = createHarness(t, {
    get: id => {
      getCount += 1
      return Promise.resolve(dataResponse({
        id,
        knowledge_base_type: 'llm_managed',
        index_status: getCount === 1 ? 'pending' : 'ready',
      }))
    },
    managedItems: () => {
      listCount += 1
      return Promise.resolve(dataResponse({
        items: [listCount === 1
          ? { id: 15, version: 4, pending_job_id: 27 }
          : {
            id: 15,
            version: 4,
            publication_job_status: 'succeeded',
            is_recallable: true,
            indexed_version: 4,
          }],
        total: 1,
      }))
    },
  })
  await flushPromises()

  assert.equal(harness.module.selectedKb.value.index_status, 'pending')
  assert.equal(harness.module.getEntryStatus(harness.module.items.value[0]),
    'knowledgeBase.entries_status_processing')
  assert.equal(harness.activeTimers().length, 1)

  assert.equal(await harness.runNextTimer(), true)
  assert.equal(getCount, 2)
  assert.equal(listCount, 2)
  assert.equal(harness.module.selectedKb.value.index_status, 'ready')
  assert.equal(harness.module.getEntryStatus(harness.module.items.value[0]),
    'knowledgeBase.entries_status_ready')
  assert.equal(harness.activeTimers().length, 0)
})

test('preserves raw detail content and closing invalidates pending detail and history reads', async t => {
  const lateDocument = deferred()
  let documentCount = 0
  const userHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
    document: () => {
      documentCount += 1
      return documentCount === 1
        ? Promise.resolve(dataResponse({ content: '  <article>\nraw & exact\n</article>  ' }))
        : lateDocument.promise
    },
  })
  await flushPromises()
  const userRow = { id: 7, filename: 'raw.md' }
  await userHarness.module.viewEntry(userRow)
  assert.equal(userHarness.module.detailContent.value, '  <article>\nraw & exact\n</article>  ')
  assert.equal(userHarness.module.detailTitle.value, 'raw.md')
  assert.equal(userHarness.api.document.length, 1)
  assert.equal(userHarness.api.managedHistory.length, 0)

  userHarness.module.closeDetail()
  const lateView = userHarness.module.viewEntry(userRow)
  assert.equal(userHarness.module.detailVisible.value, true)
  userHarness.module.closeDetail()
  lateDocument.resolve(dataResponse({ content: 'late private content' }))
  await lateView
  assert.equal(userHarness.module.detailVisible.value, false)
  assert.equal(userHarness.module.detailContent.value, '')
  assert.equal(userHarness.module.detailLoading.value, false)

  const lateHistory = deferred()
  let historyCount = 0
  const managedHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItem: () => Promise.resolve(dataResponse({ content: '\n  managed raw\t' })),
    managedHistory: () => {
      historyCount += 1
      return historyCount === 1
        ? lateHistory.promise
        : Promise.resolve(dataResponse([{ version: 3, content: 'current history' }]))
    },
  })
  await flushPromises()
  const managedRow = { id: 9, knowledge_key: 'policy.access' }
  await managedHarness.module.viewEntry(managedRow)
  assert.equal(managedHarness.module.detailContent.value, '\n  managed raw\t')
  assert.equal(managedHarness.module.detailTitle.value, 'policy.access')

  const historyLoad = managedHarness.module.showHistory(managedRow)
  assert.equal(managedHarness.module.historyVisible.value, true)
  assert.equal(managedHarness.module.historyLoading.value, true)
  assert.equal(managedHarness.module.historyTitle.value, 'knowledgeBase.managed_history_title')
  managedHarness.module.closeHistory()
  lateHistory.resolve(dataResponse([{ version: 2, content: 'stale history' }]))
  await historyLoad
  assert.equal(managedHarness.module.historyVisible.value, false)
  assert.equal(managedHarness.module.historyLoading.value, false)
  assert.deepEqual(normalize(managedHarness.module.historyItems.value), [])
  assert.equal(managedHarness.api.managedHistory.length, 1)

  await managedHarness.module.showHistory(managedRow)
  assert.deepEqual(normalize(managedHarness.module.historyItems.value), [
    { version: 3, content: 'current history' },
  ])
  managedHarness.module.closeHistory()
  assert.equal(managedHarness.module.historyVisible.value, false)
  assert.deepEqual(normalize(managedHarness.module.historyItems.value), [])
})

test('USER entries do not request managed history or retry operations', async t => {
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
  })
  await flushPromises()

  await harness.module.showHistory({ id: 4, knowledge_key: 'not-managed' })
  await harness.module.retryEntry({
    id: 4,
    publication_job_status: 'failed',
    publication_job_id: 12,
    version: 3,
  })
  assert.equal(harness.module.historyVisible.value, false)
  assert.equal(harness.api.managedHistory.length, 0)
  assert.equal(harness.api.retryManagedItem.length, 0)
})

test('delete confirmation is single-flight, cancellation writes nothing, and mutations use the right API payloads', async t => {
  const confirmation = deferred()
  const cancelHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
    confirm: () => confirmation.promise,
  })
  await flushPromises()
  const userRow = { id: 12, filename: 'manual.md' }
  const firstDelete = cancelHarness.module.deleteEntry(userRow)
  const duplicateDelete = cancelHarness.module.deleteEntry(userRow)
  await duplicateDelete
  assert.equal(cancelHarness.confirms.length, 1)
  assert.equal(cancelHarness.module.entryActionId.value, 12)
  confirmation.resolve(false)
  await firstDelete
  assert.equal(cancelHarness.api.deleteDocument.length, 0)
  assert.equal(cancelHarness.api.deleteManagedItem.length, 0)
  assert.equal(cancelHarness.module.entryActionId.value, null)

  const userHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
  })
  await flushPromises()
  await userHarness.module.deleteEntry(userRow)
  assert.deepEqual(userHarness.api.deleteDocument.map(call => [call.id, call.entryId]), [[1, 12]])
  assert.equal(userHarness.api.deleteManagedItem.length, 0)

  const managedHarness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
  })
  await flushPromises()
  const managedRow = { id: 18, knowledge_key: 'policy.retention', version: 7 }
  await managedHarness.module.deleteEntry(managedRow)
  assert.equal(managedHarness.api.deleteDocument.length, 0)
  assert.deepEqual(managedHarness.api.deleteManagedItem[0].payload.expected_version, 7)
  assert.match(managedHarness.api.deleteManagedItem[0].payload.dedupe_key, /^managed-ui-delete:/)

  await managedHarness.module.retryEntry({
    id: 19,
    version: 8,
    publication_job_status: 'failed',
    publication_job_id: 402,
  })
  assert.equal(managedHarness.api.retryManagedItem.length, 1)
  const retryPayload = normalize(managedHarness.api.retryManagedItem[0].payload)
  assert.deepEqual(retryPayload, {
    expected_version: 8,
    failed_job_id: 402,
    dedupe_key: retryPayload.dedupe_key,
  })
  assert.match(retryPayload.dedupe_key, /^managed-ui-retry:/)
})

test('identity changes during a delete confirmation prevent the write', async t => {
  const confirmation = deferred()
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' })),
    confirm: () => confirmation.promise,
  })
  await flushPromises()

  const deletion = harness.module.deleteEntry({ id: 21, filename: 'private.md' })
  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: false,
  }
  await deletion
  confirmation.resolve(true)
  await flushPromises()
  assert.equal(harness.api.deleteDocument.length, 0)
  assert.equal(harness.module.entryActionId.value, null)
  assert.equal(harness.confirmationCloses.length, 1)
})

test('stale delete and retry responses cannot clear a new action, loading state, or trigger refresh', async t => {
  for (const operation of ['deleteManagedItem', 'retryManagedItem']) {
    const oldResponse = deferred()
    const newResponse = deferred()
    const pendingList = deferred()
    let actionCount = 0
    let listCount = 0
    const harness = createHarness(t, {
      get: id => Promise.resolve(dataResponse({
        id,
        knowledge_base_type: 'llm_managed',
        index_status: 'ready',
      })),
      managedItems: () => {
        listCount += 1
        if (listCount === 3) return pendingList.promise
        return Promise.resolve(dataResponse({
          items: [{ id: listCount === 1 ? 10 : 20, knowledge_key: 'private.item', version: 2 }],
          total: 1,
        }))
      },
      [operation]: () => {
        actionCount += 1
        return actionCount === 1 ? oldResponse.promise : newResponse.promise
      },
    })
    await flushPromises()

    const oldRow = operation === 'retryManagedItem'
      ? { id: 10, version: 2, publication_job_status: 'failed', publication_job_id: 501 }
      : { id: 10, version: 2, knowledge_key: 'private.item' }
    const oldAction = operation === 'retryManagedItem'
      ? harness.module.retryEntry(oldRow)
      : harness.module.deleteEntry(oldRow)
    await flushPromises()
    assert.equal(actionCount, 1)

    harness.permissionsStore.currentUser = {
      uid: 'user-b',
      username: 'User B',
      is_superuser: false,
    }
    await flushPromises()
    const pendingRefresh = harness.module.fetchEntries()
    await flushPromises()
    assert.equal(harness.module.loading.value, true)

    const newRow = operation === 'retryManagedItem'
      ? { id: 20, version: 3, publication_job_status: 'failed', publication_job_id: 601 }
      : { id: 20, version: 3, knowledge_key: 'private.item' }
    const newAction = operation === 'retryManagedItem'
      ? harness.module.retryEntry(newRow)
      : harness.module.deleteEntry(newRow)
    await flushPromises()
    assert.equal(harness.module.entryActionId.value, 20)
    assert.equal(harness.module.loading.value, true)
    const callCountBeforeOldResponse = listCount
    const messageCountBeforeOldResponse = harness.messages.length

    if (operation === 'retryManagedItem') oldResponse.reject(new Error('old retry failed'))
    else oldResponse.resolve(dataResponse({ status: 'deleted' }))
    await oldAction
    assert.equal(harness.module.entryActionId.value, 20)
    assert.equal(harness.module.loading.value, true)
    assert.equal(listCount, callCountBeforeOldResponse)
    assert.equal(harness.messages.length, messageCountBeforeOldResponse)

    newResponse.resolve(dataResponse({
      status: operation === 'retryManagedItem' ? 'retry_submitted' : 'deleted',
    }))
    await newAction
    pendingList.resolve(dataResponse({ items: [], total: 0 }))
    await pendingRefresh
    await flushPromises()
    assert.equal(harness.module.entryActionId.value, null)
    assert.equal(harness.module.loading.value, false)
    assert.equal(listCount, callCountBeforeOldResponse + 1)
  }
})

test('route A-B-A and identity changes isolate old GET and list results', async t => {
  const oldAGet = deferred()
  const oldBGet = deferred()
  const oldAList = deferred()
  const staleIdentityList = deferred()
  const currentIdentityList = deferred()
  let getCount = 0
  let listCount = 0
  const harness = createHarness(t, {
    routeId: 1,
    get: id => {
      getCount += 1
      if (getCount === 1) return oldAGet.promise
      if (getCount === 2) return oldBGet.promise
      if (getCount === 3) {
        return Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' }))
      }
      return Promise.resolve(dataResponse({ id, knowledge_base_type: 'user', index_status: 'ready' }))
    },
    documents: () => {
      listCount += 1
      if (listCount === 1) return oldAList.promise
      if (listCount === 2) {
        return Promise.resolve(dataResponse({ items: [{ id: 2, filename: 'current-b.md' }], total: 1 }))
      }
      if (listCount === 3) return staleIdentityList.promise
      if (listCount === 4) return currentIdentityList.promise
      return Promise.resolve(dataResponse({ items: [], total: 0 }))
    },
  })
  assert.equal(harness.api.get.length, 1)

  harness.routeId.value = 2
  harness.routeId.value = 1
  assert.deepEqual(harness.api.get.map(call => call.id), [1, 2, 1])
  await flushPromises()
  assert.equal(harness.module.selectedKb.value.id, 1)
  assert.equal(harness.api.documents.length, 1)

  harness.routeId.value = 2
  await flushPromises()
  assert.equal(harness.module.selectedKb.value.id, 2)
  assert.deepEqual(harness.module.items.value.map(item => item.filename), ['current-b.md'])

  oldAGet.reject(new Error('stale A get'))
  oldBGet.resolve(dataResponse({ id: 2, knowledge_base_type: 'user', name: 'stale B' }))
  oldAList.resolve(dataResponse({ items: [{ id: 1, filename: 'stale-a.md' }], total: 1 }))
  await flushPromises()
  assert.equal(harness.module.selectedKb.value.id, 2)
  assert.deepEqual(harness.module.items.value.map(item => item.filename), ['current-b.md'])
  assert.equal(harness.messages.length, 0)

  const staleIdentityLoad = harness.module.fetchEntries()
  assert.equal(harness.api.documents.length, 3)
  harness.permissionsStore.currentUser = {
    uid: 'user-c',
    username: 'User C',
    is_superuser: false,
  }
  assert.deepEqual(normalize(harness.module.items.value), [])
  assert.equal(harness.module.selectedKb.value, null)
  await flushPromises()
  assert.equal(harness.api.get.at(-1).id, 2)
  currentIdentityList.resolve(dataResponse({ items: [{ id: 92, filename: 'new-identity.md' }], total: 1 }))
  await flushPromises()
  staleIdentityList.reject(new Error('stale list failure'))
  await staleIdentityLoad
  assert.deepEqual(harness.module.items.value.map(item => item.filename), ['new-identity.md'])
  assert.equal(harness.messages.length, 0)
})

test('identity reset clears every private field and timer, while display-name changes preserve state', async t => {
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => Promise.resolve(dataResponse({
      items: [{ id: 31, knowledge_key: 'private.policy', pending_job_id: 81 }],
      total: 1,
    })),
    managedItem: () => Promise.resolve(dataResponse({ content: 'private detail' })),
    managedHistory: () => Promise.resolve(dataResponse([{ version: 1 }])),
  })
  await flushPromises()
  await harness.module.viewEntry({ id: 31, knowledge_key: 'private.policy' })
  await harness.module.showHistory({ id: 31, knowledge_key: 'private.policy' })
  harness.module.page.value = 4
  harness.module.pageSize.value = 50
  harness.module.query.value = 'private query'
  assert.equal(harness.activeTimers().length, 1)
  const getCount = harness.api.get.length
  const itemSnapshot = normalize(harness.module.items.value)

  harness.permissionsStore.currentUser = {
    uid: 'user-a',
    username: 'Renamed user',
    is_superuser: false,
  }
  await flushPromises()
  assert.equal(harness.api.get.length, getCount)
  assert.equal(harness.module.page.value, 4)
  assert.deepEqual(normalize(harness.module.items.value), itemSnapshot)
  assert.equal(harness.module.detailVisible.value, true)
  assert.equal(harness.module.historyVisible.value, true)
  assert.equal(harness.activeTimers().length, 1)

  harness.permissionsStore.currentUser = {
    uid: 'user-a',
    username: 'Renamed admin',
    is_superuser: true,
  }
  assertPrivateStateCleared(harness.module, { loading: true })
  assert.equal(harness.activeTimers().length, 0)
  await flushPromises()
  assert.equal(harness.api.get.length, getCount + 1)

  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: true,
  }
  assertPrivateStateCleared(harness.module, { loading: true })
  await flushPromises()
  assert.equal(harness.api.get.at(-1).id, 1)

  harness.permissionsStore.clear()
  assertPrivateStateCleared(harness.module)
  assert.equal(harness.activeTimers().length, 0)
  const getsAtLogout = harness.api.get.length
  await flushPromises()
  assert.equal(harness.api.get.length, getsAtLogout)

  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: true,
  }
  await flushPromises()
  await harness.module.viewEntry({ id: 31, knowledge_key: 'private.policy' })
  await harness.module.showHistory({ id: 31, knowledge_key: 'private.policy' })
  assert.equal(harness.activeTimers().length, 1)
  harness.dispose()
  assertPrivateStateCleared(harness.module)
  assert.equal(harness.activeTimers().length, 0)
})

test('old detail failures and history successes cannot populate a new identity scope', async t => {
  const oldDetail = deferred()
  const oldHistory = deferred()
  let detailCount = 0
  let historyCount = 0
  const harness = createHarness(t, {
    get: id => Promise.resolve(dataResponse({
      id,
      knowledge_base_type: 'llm_managed',
      index_status: 'ready',
    })),
    managedItems: () => Promise.resolve(dataResponse({
      items: [{ id: 40, knowledge_key: 'scope.private', pending_job_id: 90 }],
      total: 1,
    })),
    managedItem: () => {
      detailCount += 1
      return detailCount === 1
        ? oldDetail.promise
        : Promise.resolve(dataResponse({ content: 'new identity detail' }))
    },
    managedHistory: () => {
      historyCount += 1
      return historyCount === 1
        ? oldHistory.promise
        : Promise.resolve(dataResponse([{ version: 2, content: 'new identity history' }]))
    },
  })
  await flushPromises()

  const row = { id: 40, knowledge_key: 'scope.private' }
  const oldDetailLoad = harness.module.viewEntry(row)
  const oldHistoryLoad = harness.module.showHistory(row)
  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'User B',
    is_superuser: false,
  }
  await flushPromises()
  assert.equal(harness.module.detailVisible.value, false)
  assert.equal(harness.module.historyVisible.value, false)
  assert.equal(harness.activeTimers().length, 1)

  await harness.module.viewEntry(row)
  await harness.module.showHistory(row)
  assert.equal(harness.module.detailContent.value, 'new identity detail')
  assert.deepEqual(normalize(harness.module.historyItems.value), [
    { version: 2, content: 'new identity history' },
  ])
  const messagesBeforeStaleResults = harness.messages.length

  oldDetail.reject(new Error('old detail failed'))
  oldHistory.resolve(dataResponse([{ version: 1, content: 'old identity history' }]))
  await Promise.all([oldDetailLoad, oldHistoryLoad])
  assert.equal(harness.module.detailVisible.value, true)
  assert.equal(harness.module.detailContent.value, 'new identity detail')
  assert.equal(harness.module.historyVisible.value, true)
  assert.deepEqual(normalize(harness.module.historyItems.value), [
    { version: 2, content: 'new identity history' },
  ])
  assert.equal(harness.messages.length, messagesBeforeStaleResults)
})
