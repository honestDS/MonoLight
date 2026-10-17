import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope, reactive, ref, watch } from 'vue'
import { createI18n } from 'vue-i18n'

import { getKnowledgeBaseProfileLabel } from '../src/utils/knowledgeBaseManagement.js'
import * as knowledgeBaseManagement from '../src/utils/knowledgeBaseManagement.js'
import * as knowledgeBaseMigration from '../src/utils/knowledgeBaseMigration.js'
import * as requestTaskManager from '../src/utils/requestTaskManager.js'
import { createKnowledgeBaseScope } from '../src/utils/knowledgeBaseScope.js'
import { useUserPermissionsStore } from '../src/composables/useUserPermissions.js'
import zhCommon from '../src/i18n/locales/zh/common.js'
import zhKnowledgeBase from '../src/i18n/locales/zh/knowledgeBase.js'
import enCommon from '../src/i18n/locales/en/common.js'
import enKnowledgeBase from '../src/i18n/locales/en/knowledgeBase.js'

const useKnowledgeBaseViewSource = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeBaseView.js', import.meta.url),
  'utf8',
)
const useKnowledgeBaseDocumentsSource = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeBaseDocuments.js', import.meta.url),
  'utf8',
)
const useKnowledgeBaseQueryTestSource = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeBaseQueryTest.js', import.meta.url),
  'utf8',
)
const useManagedKnowledgeSource = readFileSync(
  new URL('../src/composables/knowledgeBase/useManagedKnowledge.js', import.meta.url),
  'utf8',
)
const useKnowledgeOrganizationSource = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeOrganization.js', import.meta.url),
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

const stripModuleSyntax = sourceText => stripImports(sourceText).replace(/\bexport\s+/g, '')
const useKnowledgeBaseViewScript = stripModuleSyntax(useKnowledgeBaseViewSource)
const useKnowledgeBaseDocumentsScript = stripModuleSyntax(useKnowledgeBaseDocumentsSource)
const useKnowledgeBaseQueryTestScript = stripModuleSyntax(useKnowledgeBaseQueryTestSource)
const useManagedKnowledgeScript = stripModuleSyntax(useManagedKnowledgeSource)
const useKnowledgeOrganizationScript = stripModuleSyntax(useKnowledgeOrganizationSource)
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
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

const createHarness = async (t, options = {}) => {
  const currentUid = options.currentUid ?? (options.superuser ? 'admin-user' : 'user-a')
  const currentUsername = options.currentUsername ?? 'Administrator'
  const knowledgeBases = options.knowledgeBases ?? []
  const users = options.users ?? [{ uid: currentUid, username: currentUsername }]
  const requests = []
  const messages = []
  const confirms = []
  const confirmationCloses = []
  const mountedCallbacks = []
  const unmountedCallbacks = []
  const timers = []
  let nextTimerId = 1
  const setTimeout = (callback, delay, ...args) => {
    const timer = { id: nextTimerId++, callback, delay, args, cancelled: false, ran: false }
    timers.push(timer)
    return timer.id
  }
  const clearTimeout = id => {
    const timer = timers.find(item => item.id === id)
    if (timer) timer.cancelled = true
  }
  const api = {
    requests,
    lists: [],
    creates: [],
    updates: [],
    deletes: [],
    userLists: [],
    repairs: [],
    gets: [],
    migrations: [],
    documents: [],
    document: [],
    importDocument: [],
    deleteDocument: [],
    managedItems: [],
    managedItem: [],
    managedHistory: [],
    createManagedItem: [],
    updateManagedItem: [],
    deleteManagedItem: [],
    retryManagedItem: [],
    queryTest: [],
    organizationJobs: [],
    organize: [],
    cancelOrganizationJob: [],
    retryOrganizationJob: [],
  }
  const createApiMethod = (method, getFields, defaultData) => (...args) => {
    const request = { method, ...getFields(...args) }
    requests.push(request)
    api[method].push(request)
    if (typeof options[method] === 'function') return options[method](...args, request)
    const data = typeof defaultData === 'function' ? defaultData(...args) : defaultData
    return Promise.resolve(dataResponse(data))
  }

  const knowledgeBaseApi = {
    list: params => {
      const request = { method: 'list', params: { ...params } }
      requests.push(request)
      api.lists.push(request)
      if (typeof options.list === 'function') return options.list(params, request)
      const items = knowledgeBases.map(item => ({ ...item }))
      return Promise.resolve(dataResponse({
        items,
        total: items.length,
        embedding_models: [],
        repair_required_count: 0,
      }))
    },
    create: payload => {
      const request = { method: 'create', payload }
      requests.push(request)
      api.creates.push(request)
      if (typeof options.create === 'function') return options.create(payload, request)
      return Promise.resolve(dataResponse({ id: 'created-kb', ...payload }))
    },
    update: (id, payload) => {
      const request = { method: 'update', id, payload }
      requests.push(request)
      api.updates.push(request)
      if (typeof options.update === 'function') return options.update(id, payload, request)
      return Promise.resolve(dataResponse({ id, ...payload }))
    },
    delete: id => {
      const request = { method: 'delete', id }
      requests.push(request)
      api.deletes.push(request)
      if (typeof options.delete === 'function') return options.delete(id, request)
      return Promise.resolve(dataResponse({ id }))
    },
    repair: (payload, params) => {
      const request = { method: 'repair', payload, params: params ? { ...params } : params }
      requests.push(request)
      api.repairs.push(request)
      if (typeof options.repair === 'function') return options.repair(payload, params, request)
      const items = knowledgeBases
        .filter(item => params?.uid ? item.uid === params.uid : item.id === params?.kb_id)
        .map(item => ({ ...item, migration_job_id: 1 }))
      return Promise.resolve(dataResponse({
        items,
        total: items.length,
      }))
    },
    get: id => {
      const request = { method: 'get', id }
      requests.push(request)
      api.gets.push(request)
      if (typeof options.get === 'function') return options.get(id, request)
      const knowledgeBase = knowledgeBases.find(item => item.id === id)
      return Promise.resolve(dataResponse(knowledgeBase ? { ...knowledgeBase } : { id }))
    },
    migrateEmbedding: (id, payload) => {
      const request = { method: 'migrateEmbedding', id, payload }
      requests.push(request)
      api.migrations.push(request)
      if (typeof options.migrateEmbedding === 'function') {
        return options.migrateEmbedding(id, payload, request)
      }
      return Promise.resolve(dataResponse({
        id,
        migration_status: 'preparing',
        target_embedding_channel_id: payload.embedding_channel_id,
        target_embedding_model_id: payload.embedding_model_id,
      }))
    },
    documents: createApiMethod('documents', (id, params) => ({ id, params: { ...params } }), {
      items: [],
      total: 0,
    }),
    document: createApiMethod('document', (id, docId) => ({ id, docId }), { content: '' }),
    importDocument: createApiMethod('importDocument', (id, payload) => ({ id, payload }), {
      status: 'accepted',
    }),
    deleteDocument: createApiMethod('deleteDocument', (id, docId) => ({ id, docId }), {
      status: 'accepted',
    }),
    managedItems: createApiMethod('managedItems', (id, params, config) => ({
      id,
      params: { ...params },
      config: config ? { ...config } : config,
    }), {
      items: [],
      total: 0,
    }),
    managedItem: createApiMethod('managedItem', (id, itemId) => ({ id, itemId }), (id, itemId) => ({
      id: itemId,
      knowledge_key: 'key',
      content: '',
      version: 1,
      llm_maintainable: false,
    })),
    managedHistory: createApiMethod('managedHistory', (id, itemId) => ({ id, itemId }), []),
    createManagedItem: createApiMethod('createManagedItem', (id, payload) => ({ id, payload }), {
      status: 'created',
    }),
    updateManagedItem: createApiMethod('updateManagedItem', (id, itemId, payload) => ({
      id,
      itemId,
      payload,
    }), { status: 'updated' }),
    deleteManagedItem: createApiMethod('deleteManagedItem', (id, itemId, payload) => ({
      id,
      itemId,
      payload,
    }), { status: 'deleted' }),
    retryManagedItem: createApiMethod('retryManagedItem', (id, itemId, payload) => ({
      id,
      itemId,
      payload,
    }), { status: 'retry_submitted' }),
    queryTest: createApiMethod('queryTest', (id, params) => ({ id, params: { ...params } }), {
      items: [],
      retrieval_mode: null,
      rerank_error: null,
    }),
    organizationJobs: createApiMethod('organizationJobs', (id, params) => ({
      id,
      params: { ...params },
    }), {
      items: [],
      total: 0,
    }),
    organize: createApiMethod('organize', (id, payload) => ({ id, payload }), {
      status: 'accepted',
    }),
    cancelOrganizationJob: createApiMethod('cancelOrganizationJob', (id, jobId) => ({ id, jobId }), {
      status: 'accepted',
    }),
    retryOrganizationJob: createApiMethod('retryOrganizationJob', (id, jobId) => ({ id, jobId }), {
      status: 'accepted',
    }),
  }

  const adminApi = {
    userList: params => {
      const request = { method: 'userList', params: { ...params } }
      requests.push(request)
      api.userLists.push(request)
      if (typeof options.userList === 'function') return options.userList(params, request)

      const page = Number(params?.page) || 1
      const size = Number(params?.size) || 100
      const start = (page - 1) * size
      const items = users.slice(start, start + size).map(item => ({ ...item }))
      return Promise.resolve(dataResponse({ items, total: users.length }))
    },
  }

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
        : options.confirm ?? true
      if (!result) throw 'cancel'
      return 'confirm'
    },
    close: () => confirmationCloses.push(true),
  }

  const pinia = createPinia()
  setActivePinia(pinia)
  const permissionsStore = useUserPermissionsStore(pinia)
  permissionsStore.currentUser = {
    uid: currentUid,
    username: currentUsername,
    is_superuser: options.superuser === true,
  }

  const i18n = createI18n({
    legacy: false,
    locale: options.locale ?? 'zh',
    fallbackLocale: 'en',
    messages: {
      zh: { common: zhCommon, knowledgeBase: zhKnowledgeBase },
      en: { common: enCommon, knowledgeBase: enKnowledgeBase },
    },
  })
  const scope = effectScope()
  const context = vm.createContext({
    AbortController,
    Blob,
    computed,
    ElMessage,
    ElMessageBox,
    FormData,
    formatTime: value => value,
    adminApi,
    knowledgeBaseApi,
    createKnowledgeBaseScope,
    setTimeout,
    clearTimeout,
    onBeforeUnmount: callback => unmountedCallbacks.push(callback),
    onMounted: callback => mountedCallbacks.push(callback),
    reactive,
    ref,
    ...requestTaskManager,
    watch,
    useI18n: () => ({
      t: (key, params) => i18n.global.t(
        key,
        params && typeof params === 'object' ? JSON.parse(JSON.stringify(params)) : params,
      ),
    }),
    useUserPermissionsStore,
    i18n,
    ...knowledgeBaseManagement,
    ...knowledgeBaseMigration,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  scope.run(() => {
    vm.runInContext(
      `${useKnowledgeBaseDocumentsScript}\n${useKnowledgeBaseQueryTestScript}\n` +
      `${useManagedKnowledgeScript}\n${useKnowledgeOrganizationScript}\n` +
      `${useKnowledgeBaseViewScript}\n` +
      'globalThis.__module = useKnowledgeBaseView()',
      context,
      { filename: 'useKnowledgeBaseView.js' },
    )
  })

  const harness = {
    api,
    confirms,
    confirmationCloses,
    get closedConfirmations() {
      return confirmationCloses.length
    },
    messages,
    timers,
    permissionsStore,
    mountedCallbacks,
    unmountedCallbacks,
    module: context.__module,
    async runNextTimer() {
      const timer = timers.find(item => !item.cancelled && !item.ran)
      if (!timer) return false
      timer.ran = true
      await timer.callback(...timer.args)
      return true
    },
    activeTimers() {
      return timers.filter(timer => !timer.cancelled && !timer.ran)
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

test('uses the list-wide repair count for the warning and clears it at zero', async t => {
  const responses = [
    dataResponse({ items: [], total: 0, embedding_models: [], repair_required_count: 4 }),
    dataResponse({ items: [], total: 0, embedding_models: [], repair_required_count: 0 }),
  ]
  const harness = await createHarness(t, { list: () => Promise.resolve(responses.shift()) })

  assert.equal(harness.module.repairRequiredCount.value, 0)
  await harness.module.fetchData()
  assert.equal(harness.module.tableData.value.length, 0)
  assert.equal(harness.module.repairRequiredCount.value, 4)

  await harness.module.fetchData()
  assert.equal(harness.module.repairRequiredCount.value, 0)
})

test('clears the repair count on identity changes and ignores an old list response', async t => {
  for (const changedField of ['uid', 'superuser']) {
    const stale = deferred()
    const fresh = deferred()
    let listCalls = 0
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      list: () => {
        listCalls += 1
        if (listCalls === 1) {
          return Promise.resolve(dataResponse({
            items: [], total: 0, embedding_models: [], repair_required_count: 6,
          }))
        }
        return listCalls === 2 ? stale.promise : fresh.promise
      },
    })

    await harness.module.fetchData()
    const staleLoad = harness.module.fetchData()

    harness.permissionsStore.currentUser = changedField === 'uid'
      ? { uid: 'user-b', username: 'Bob', is_superuser: false }
      : { uid: 'user-a', username: 'Alice', is_superuser: true }
    await flushPromises()
    assert.equal(harness.module.repairRequiredCount.value, 0)

    const freshLoad = harness.api.lists.length === 2 ? harness.module.fetchData() : null
    fresh.resolve(dataResponse({
      items: [], total: 0, embedding_models: [], repair_required_count: 3,
    }))
    if (freshLoad) await freshLoad
    else await flushPromises()

    stale.resolve(dataResponse({
      items: [], total: 0, embedding_models: [], repair_required_count: 99,
    }))
    await staleLoad
    await flushPromises()
    assert.equal(harness.module.repairRequiredCount.value, 3, changedField)
  }
})

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

test('submits an embedding migration and displays the returned state', async t => {
  const row = {
    id: 'kb-migrate',
    uid: 'user-a',
    name: 'Migration KB',
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
    embedding_channel_id: 1,
    embedding_model_id: 'old-model',
  }
  const updated = {
    ...row,
    migration_status: 'preparing',
    target_embedding_channel_id: 2,
    target_embedding_model_id: 'new-model',
  }
  const harness = await createHarness(t, {
    list: () => Promise.resolve(dataResponse({
      items: [row],
      total: 1,
      embedding_models: [{ channel_id: 2, model_id: 'new-model', channel_name: 'New channel' }],
    })),
    get: () => Promise.resolve(dataResponse(row)),
    migrateEmbedding: () => Promise.resolve(dataResponse(updated)),
  })

  await harness.module.fetchData()
  await harness.module.showMigrationDialog(row)
  harness.module.migrationTargetKey.value = '2::new-model'
  await harness.module.submitEmbeddingMigration()

  assert.deepEqual(normalize(harness.api.migrations.map(({ id, payload }) => ({ id, payload }))), [{
    id: row.id,
    payload: { embedding_channel_id: 2, embedding_model_id: 'new-model' },
  }])
  assert.equal(harness.module.selectedKb.value.migration_status, 'preparing')
  assert.equal(harness.module.tableData.value[0].migration_status, 'preparing')
  assert.equal(harness.module.tableData.value[0].target_embedding_model_id, 'new-model')
  assert.equal(harness.messages.filter(message => message.type === 'success').length, 1)
  assert.equal(harness.activeTimers().length, 1)
})

test('does not submit a migration confirmation after the identity and selected KB change', async t => {
  const confirmation = deferred()
  const oldKb = {
    id: 'kb-confirm-old',
    uid: 'user-a',
    name: 'Old confirmation KB',
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
    target_embedding_channel_id: 2,
    target_embedding_model_id: 'old-target',
  }
  const newKb = {
    id: 'kb-confirm-new',
    uid: 'user-b',
    name: 'New confirmation KB',
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
  }
  let listCalls = 0
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    list: () => {
      listCalls += 1
      return Promise.resolve(dataResponse({
        items: [listCalls === 1 ? oldKb : newKb],
        total: 1,
        embedding_models: listCalls === 1
          ? [{ channel_id: 2, model_id: 'old-target' }]
          : [{ channel_id: 3, model_id: 'new-target' }],
      }))
    },
    get: id => Promise.resolve(dataResponse(id === oldKb.id ? oldKb : newKb)),
    confirm: () => confirmation.promise,
  })

  await harness.module.fetchData()
  await harness.module.showMigrationDialog(oldKb)
  harness.module.migrationTargetKey.value = '2::old-target'
  const pendingConfirmation = harness.module.submitEmbeddingMigration()
  await flushPromises()
  assert.equal(harness.confirms.length, 1)
  assert.equal(harness.api.migrations.length, 0)

  harness.permissionsStore.currentUser = {
    uid: 'user-b',
    username: 'Bob',
    is_superuser: false,
  }
  await flushPromises()
  assert.deepEqual(harness.module.tableData.value.map(item => item.id), [newKb.id])
  await harness.module.showMigrationDialog(newKb)
  harness.module.migrationTargetKey.value = '3::new-target'

  confirmation.resolve(true)
  await pendingConfirmation

  assert.equal(harness.api.migrations.length, 0)
  assert.equal(harness.module.selectedKb.value.id, newKb.id)
  assert.equal(harness.module.migrationDialogVisible.value, true)
  assert.equal(harness.module.migrationTargetKey.value, '3::new-target')
  assert.equal(harness.messages.length, 0)
})

test('ignores late migration success or failure without disturbing the new identity dialog', async t => {
  for (const outcome of ['success', 'failure']) {
    const oldMigration = deferred()
    const newMigration = deferred()
    const oldKb = {
      id: 'kb-submit-old',
      uid: 'user-a',
      name: 'Old submitted KB',
      knowledge_base_type: 'user',
      migration_status: 'succeeded',
    }
    const newKb = {
      id: 'kb-submit-new',
      uid: 'user-b',
      name: 'New submitted KB',
      knowledge_base_type: 'user',
      migration_status: 'succeeded',
    }
    let listCalls = 0
    let migrationCalls = 0
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      list: () => {
        listCalls += 1
        const isOldScope = listCalls === 1
        return Promise.resolve(dataResponse({
          items: [isOldScope ? oldKb : newKb],
          total: 1,
          embedding_models: isOldScope
            ? [{ channel_id: 2, model_id: 'old-target' }]
            : [{ channel_id: 3, model_id: 'new-target' }],
        }))
      },
      get: id => Promise.resolve(dataResponse(id === oldKb.id ? oldKb : newKb)),
      migrateEmbedding: () => {
        migrationCalls += 1
        return migrationCalls === 1 ? oldMigration.promise : newMigration.promise
      },
    })

    await harness.module.fetchData()
    await harness.module.showMigrationDialog(oldKb)
    harness.module.migrationTargetKey.value = '2::old-target'
    const oldSubmission = harness.module.submitEmbeddingMigration()
    await flushPromises()
    assert.equal(harness.api.migrations.length, 1)
    assert.equal(harness.module.migrationSubmitting.value, true)

    harness.permissionsStore.currentUser = {
      uid: 'user-b',
      username: 'Bob',
      is_superuser: false,
    }
    await flushPromises()
    assert.equal(harness.module.migrationSubmitting.value, false)
    assert.deepEqual(harness.module.tableData.value.map(item => item.id), [newKb.id])
    await harness.module.showMigrationDialog(newKb)
    harness.module.migrationTargetKey.value = '3::new-target'
    const newSubmission = harness.module.submitEmbeddingMigration()
    await flushPromises()
    assert.equal(harness.api.migrations.length, 2)
    assert.deepEqual(normalize(harness.api.migrations[1]), {
      method: 'migrateEmbedding',
      id: newKb.id,
      payload: { embedding_channel_id: 3, embedding_model_id: 'new-target' },
    })
    assert.equal(harness.module.migrationSubmitting.value, true)
    const newTableBeforeLateResponse = normalize(harness.module.tableData.value)
    const newSelectionBeforeLateResponse = normalize(harness.module.selectedKb.value)
    const messageCountBeforeLateResponse = harness.messages.length

    if (outcome === 'success') {
      oldMigration.resolve(dataResponse({
        ...oldKb,
        migration_status: 'preparing',
        target_embedding_channel_id: 2,
        target_embedding_model_id: 'old-target',
      }))
    } else {
      oldMigration.reject(new Error('old migration failed'))
    }
    await oldSubmission
    await flushPromises()

    assert.deepEqual(normalize(harness.module.tableData.value), newTableBeforeLateResponse, outcome)
    assert.deepEqual(normalize(harness.module.selectedKb.value), newSelectionBeforeLateResponse, outcome)
    assert.equal(harness.module.migrationSubmitting.value, true, outcome)
    assert.equal(harness.module.migrationDialogVisible.value, true, outcome)
    assert.equal(harness.messages.length, messageCountBeforeLateResponse, outcome)
    assert.equal(harness.activeTimers().length, 0, outcome)

    newMigration.resolve(dataResponse({
      ...newKb,
      migration_status: 'preparing',
      target_embedding_channel_id: 3,
      target_embedding_model_id: 'new-target',
    }))
    await newSubmission
    assert.equal(harness.module.selectedKb.value.id, newKb.id)
    assert.equal(harness.module.selectedKb.value.migration_status, 'preparing')
    assert.equal(harness.module.tableData.value[0].migration_status, 'preparing')
    assert.equal(harness.module.migrationSubmitting.value, false)
    assert.equal(harness.messages.filter(message => message.type === 'success').length, 1)
  }
})

test('clears the repair count when the list request is unauthorized', async t => {
  for (const status of [401, 403]) {
    let listCalls = 0
    const harness = await createHarness(t, {
      list: () => {
        listCalls += 1
        if (listCalls === 1) {
          return Promise.resolve(dataResponse({
            items: [], total: 0, embedding_models: [], repair_required_count: 5,
          }))
        }
        return Promise.reject(Object.assign(new Error('unauthorized'), {
          response: { status },
        }))
      },
    })

    await harness.module.fetchData()
    assert.equal(harness.module.repairRequiredCount.value, 5)
    await harness.module.fetchData()
    assert.equal(harness.module.repairRequiredCount.value, 0, String(status))
  }
})

test('renders user profile names in source order', () => {
  const profileNames = Object.freeze(['中文配置', 'English Profile'])
  const knowledgeBase = Object.freeze({
    knowledge_base_type: 'user',
    profile_ids: ['profile-cn', 'profile-en'],
    profile_names: profileNames
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), '中文配置, English Profile')
  assert.deepEqual(profileNames, ['中文配置', 'English Profile'])
  assert.deepEqual(knowledgeBase, {
    knowledge_base_type: 'user',
    profile_ids: ['profile-cn', 'profile-en'],
    profile_names: profileNames
  })
})

test('renders the profile name for an llm-managed knowledge base', () => {
  const knowledgeBase = Object.freeze({
    knowledge_base_type: 'llm_managed',
    managed_profile_id: 'managed-profile',
    profile_names: Object.freeze(['托管配置'])
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), '托管配置')
})

test('keeps repeated names for different profiles', () => {
  const knowledgeBase = Object.freeze({
    profile_ids: ['profile-a', 'profile-b'],
    profile_names: Object.freeze(['Shared Profile', 'Shared Profile'])
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), 'Shared Profile, Shared Profile')
})

test('returns a placeholder when no profile name list is available', () => {
  const cases = [
    { profile_names: [], profile_ids: ['profile-id'] },
    { profile_names: undefined, profile_ids: ['profile-id'] },
    { profile_names: null, managed_profile_id: 'managed-profile-id' },
    { profile_names: 'not-an-array', profile_ids: ['profile-id'] },
    { profile_ids: ['profile-id'], managed_profile_id: 'managed-profile-id' },
    undefined,
    null
  ]

  for (const knowledgeBase of cases) {
    const label = getKnowledgeBaseProfileLabel(knowledgeBase)
    assert.equal(label, '-')
    assert.doesNotMatch(label, /#/)
  }
})

test('ignores non-string and whitespace-only names', () => {
  const knowledgeBase = Object.freeze({
    profile_names: Object.freeze(['中文配置', 42, '', '   ', null, 'English Profile', false])
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), '中文配置, English Profile')
})

test('keeps owner identity when displaying same-name knowledge bases and profiles', async t => {
  const knowledgeBases = [
    {
      id: 'kb-owner-a',
      uid: 'owner-a',
      username: 'Alice',
      name: 'Shared knowledge base',
      description: 'Owner A',
      embedding_channel_id: 1,
      embedding_model_id: 'embedding-a',
      knowledge_base_type: 'user',
      profile_ids: ['profile-owner-a'],
      profile_names: ['Shared profile'],
    },
    {
      id: 'kb-owner-b',
      uid: 'owner-b',
      username: 'Bob',
      name: 'Shared knowledge base',
      description: 'Owner B',
      embedding_channel_id: 2,
      embedding_model_id: 'embedding-b',
      knowledge_base_type: 'user',
      profile_ids: ['profile-owner-b'],
      profile_names: ['Shared profile'],
    },
  ]

  for (const scenario of [
    { superuser: false, currentUid: 'owner-a', currentUsername: 'Alice' },
    { superuser: true, currentUid: 'admin-user', currentUsername: 'Administrator' },
  ]) {
    const harness = await createHarness(t, { ...scenario, knowledgeBases })
    await harness.module.fetchData()

    assert.equal(harness.module.isSuperuser.value, scenario.superuser)
    assert.deepEqual(harness.module.tableData.value.map(row => ({
      id: row.id,
      uid: row.uid,
      username: row.username,
      name: row.name,
      profile_ids: row.profile_ids,
      profile_names: row.profile_names,
    })), [
      {
        id: 'kb-owner-a',
        uid: 'owner-a',
        username: 'Alice',
        name: 'Shared knowledge base',
        profile_ids: ['profile-owner-a'],
        profile_names: ['Shared profile'],
      },
      {
        id: 'kb-owner-b',
        uid: 'owner-b',
        username: 'Bob',
        name: 'Shared knowledge base',
        profile_ids: ['profile-owner-b'],
        profile_names: ['Shared profile'],
      },
    ])

    const [rowA, rowB] = harness.module.tableData.value
    harness.module.showEditDialog(rowA)
    assert.equal(harness.module.form.username, 'Alice')
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(rowA), 'Alice')
    assert.equal(harness.module.editingId.value, 'kb-owner-a')

    harness.module.showEditDialog(rowB)
    assert.equal(harness.module.form.username, 'Bob')
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(rowB), 'Bob')
    assert.equal(harness.module.editingId.value, 'kb-owner-b')

    await harness.module.showDialog()
    assert.equal(harness.module.form.username, scenario.currentUsername)
    assert.equal(harness.module.editingId.value, null)
    assert.equal(harness.module.isEditing.value, false)
  }
})

test('does not request owner users for ordinary users', async t => {
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    users: [{ uid: 'owner-b', username: 'Bob' }],
  })

  await harness.module.showDialog()

  assert.equal(harness.api.userLists.length, 0)
  assert.deepEqual(normalize(harness.module.ownerUsers.value), [])
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, null)
  assert.equal(harness.module.form.uid, null)
})

test('loads every owner-user page without replacing the current selection', async t => {
  const users = Array.from({ length: 1101 }, (_, index) => ({
    uid: `user-${index}`,
    username: `User ${index}`,
    is_active: true,
  }))
  users[500] = { uid: 'admin-user', username: 'Administrator', is_active: true }
  users[1000] = { uid: 'inactive-user', username: 'Inactive User', is_active: false }
  users[1100] = { uid: 'last-page-user', username: 'Last Page User', is_active: false }

  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users,
  })

  await harness.module.showDialog()

  assert.equal(harness.api.userLists.length, 12)
  assert.deepEqual(harness.api.userLists.map(request => request.params), Array.from({ length: 12 }, (_, index) => ({
    page: index + 1,
    size: 100,
  })))
  assert.deepEqual(normalize(harness.module.ownerUsers.value), users)
  assert.equal(harness.module.form.uid, 'admin-user')
  assert.equal(harness.module.getKnowledgeBaseOwnerLabel(users[1000]), 'Inactive User')
  assert.equal(harness.module.getKnowledgeBaseOwnerLabel(users[1100]), 'Last Page User')
  assert.ok(!harness.module.getKnowledgeBaseOwnerLabel(users[1000]).includes(users[1000].uid))
  assert.ok(!harness.module.getKnowledgeBaseOwnerLabel(users[1100]).includes(users[1100].uid))
})

test('reopening the owner dialog resets selection and options to the current user', async t => {
  const currentUser = { uid: 'admin-user', username: 'Administrator' }
  const users = [currentUser, { uid: 'owner-b', username: 'Bob' }]
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: currentUser.uid,
    currentUsername: currentUser.username,
    users,
  })

  await harness.module.showDialog()
  harness.module.form.uid = 'owner-b'
  harness.module.form.username = 'Bob'

  const reopening = harness.module.showDialog()
  assert.equal(harness.module.form.uid, currentUser.uid)
  assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
  await reopening

  assert.equal(harness.module.form.uid, currentUser.uid)
  assert.deepEqual(normalize(harness.module.ownerUsers.value), users)
  assert.equal(harness.module.ownerUsersError.value, null)
})

test('clears the owner selection when a successful list excludes the current user or is empty', async t => {
  for (const users of [
    [{ uid: 'owner-b', username: 'Bob' }],
    [],
  ]) {
    const harness = await createHarness(t, {
      superuser: true,
      currentUid: 'admin-user',
      currentUsername: 'Administrator',
      users,
    })

    await harness.module.showDialog()

    assert.deepEqual(normalize(harness.module.ownerUsers.value), users)
    assert.equal(harness.module.form.uid, null)
    assert.equal(harness.module.ownerUsersLoading.value, false)
    assert.equal(harness.module.ownerUsersError.value, null)
  }
})

test('keeps only the current owner visible while the owner list is loading', async t => {
  const request = deferred()
  const currentUser = { uid: 'admin-user', username: 'Administrator' }
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: currentUser.uid,
    currentUsername: currentUser.username,
    userList: () => request.promise,
  })

  const pending = harness.module.showDialog()

  assert.deepEqual(harness.api.userLists[0].params, { page: 1, size: 100 })
  assert.deepEqual(normalize(harness.module.ownerUsers.value.map(user => user.username)), [currentUser.username])
  assert.equal(harness.module.form.uid, currentUser.uid)
  assert.equal(harness.module.ownerUsersLoading.value, true)

  request.resolve(dataResponse({ items: [currentUser], total: 1 }))
  await pending
  assert.equal(harness.module.ownerUsersLoading.value, false)
})

test('keeps the old options on a failed page and restores them after a successful retry', async t => {
  for (const locale of ['zh', 'en']) {
    const currentUser = { uid: 'admin-user', username: 'Administrator' }
    const otherUser = { uid: 'owner-b', username: 'Bob' }
    const firstPage = deferred()
    const failedPage = deferred()
    let callCount = 0
    const harness = await createHarness(t, {
      locale,
      superuser: true,
      currentUid: currentUser.uid,
      currentUsername: currentUser.username,
      userList: params => {
        callCount += 1
        if (callCount === 1) return firstPage.promise
        if (callCount === 2) return failedPage.promise
        return Promise.resolve(dataResponse({
          items: params.page === 1 ? [currentUser] : [otherUser],
          total: 2,
        }))
      },
    })

    const pending = harness.module.showDialog()
    firstPage.resolve(dataResponse({ items: [currentUser], total: 2 }))
    await Promise.resolve()
    assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
    assert.equal(harness.module.ownerUsersLoading.value, true)

    failedPage.reject(new Error('network error'))
    await pending

    assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
    assert.equal(harness.module.form.uid, currentUser.uid)
    assert.equal(harness.module.ownerUsersLoading.value, false)
    assert.equal(
      harness.module.ownerUsersError.value,
      locale === 'zh' ? '获取用户列表失败：network error' : 'Failed to load users: network error',
    )

    await harness.module.fetchOwnerUsers()
    assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser, otherUser])
    assert.equal(harness.module.form.uid, currentUser.uid)
    assert.equal(harness.module.ownerUsersError.value, null)
    assert.equal(harness.module.ownerUsersLoading.value, false)
  }
})

test('submits owner uid only for superuser creates and never for ordinary users or edits', async t => {
  const owner = { uid: 'owner-b', username: 'Bob' }
  const installFormRef = harness => {
    const state = { validateCalls: 0 }
    harness.module.formRef.value = {
      resetFields() {},
      validate: async callback => {
        state.validateCalls += 1
        if (callback) await callback(true)
        return true
      },
    }
    return state
  }

  const superuserHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users: [
      { uid: 'admin-user', username: 'Administrator' },
      owner,
    ],
  })
  const superuserForm = installFormRef(superuserHarness)
  await superuserHarness.module.showDialog()
  superuserHarness.module.form.uid = owner.uid
  superuserHarness.module.form.name = 'Owned knowledge base'
  superuserHarness.module.form.description = 'Created for Bob'
  superuserHarness.module.form.embedding_model_key = '2::embed-v1'

  assert.equal(superuserHarness.module.isEditing.value, false)
  assert.equal(superuserHarness.module.rules.value.uid[0].required, true)
  await superuserHarness.module.submitForm()

  assert.equal(superuserForm.validateCalls, 1)
  assert.deepEqual(normalize(superuserHarness.api.creates[0].payload), {
    name: 'Owned knowledge base',
    description: 'Created for Bob',
    embedding_channel_id: 2,
    embedding_model_id: 'embed-v1',
    uid: owner.uid,
  })
  assert.equal(Object.prototype.hasOwnProperty.call(superuserHarness.api.creates[0].payload, 'username'), false)

  const ordinaryHarness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    users: [owner],
  })
  const ordinaryForm = installFormRef(ordinaryHarness)
  await ordinaryHarness.module.showDialog()
  ordinaryHarness.module.form.uid = 'other-user'
  ordinaryHarness.module.form.username = 'forged'
  ordinaryHarness.module.form.name = 'Ordinary knowledge base'
  ordinaryHarness.module.form.description = 'Created by Alice'
  ordinaryHarness.module.form.embedding_model_key = '2::embed-v1'

  assert.equal(ordinaryHarness.api.userLists.length, 0)
  assert.equal(ordinaryHarness.module.rules.value.uid[0].required, false)
  await ordinaryHarness.module.submitForm()

  assert.equal(ordinaryForm.validateCalls, 1)
  assert.deepEqual(normalize(ordinaryHarness.api.creates[0].payload), {
    name: 'Ordinary knowledge base',
    description: 'Created by Alice',
    embedding_channel_id: 2,
    embedding_model_id: 'embed-v1',
  })

  const row = {
    id: 'kb-owner-b',
    uid: owner.uid,
    username: owner.username,
    name: 'Existing knowledge base',
    description: 'Existing description',
    embedding_channel_id: 2,
    embedding_model_id: 'embed-v1',
  }
  for (const scenario of [
    { superuser: false, currentUid: 'user-a', currentUsername: 'Alice' },
    { superuser: true, currentUid: 'admin-user', currentUsername: 'Administrator' },
  ]) {
    const harness = await createHarness(t, scenario)
    const form = installFormRef(harness)
    harness.module.showEditDialog(row)
    harness.module.form.name = 'Updated knowledge base'
    harness.module.form.description = 'Updated description'

    assert.equal(harness.api.userLists.length, 0)
    assert.equal(harness.module.form.uid, owner.uid)
    assert.equal(harness.module.form.username, owner.username)
    assert.equal(harness.module.rules.value.uid[0].required, false)
    await harness.module.submitForm()

    assert.equal(form.validateCalls, 1)
    assert.deepEqual(normalize(harness.api.updates[0].payload), {
      name: 'Updated knowledge base',
      description: 'Updated description',
    })
  }
})

test('blocks owner create while users load or fail, then submits the selected retry owner', async t => {
  for (const locale of ['zh', 'en']) {
    const currentUser = { uid: 'admin-user', username: 'Administrator' }
    const owner = { uid: 'owner-b', username: 'Bob' }
    const firstRequest = deferred()
    const retryRequest = deferred()
    let userListCalls = 0
    const harness = await createHarness(t, {
      locale,
      superuser: true,
      currentUid: currentUser.uid,
      currentUsername: currentUser.username,
      userList: () => {
        userListCalls += 1
        return userListCalls === 1 ? firstRequest.promise : retryRequest.promise
      },
    })
    const form = { validateCalls: 0 }
    harness.module.formRef.value = {
      resetFields() {},
      validate: async callback => {
        form.validateCalls += 1
        if (callback) await callback(true)
        return true
      },
    }

    const pending = harness.module.showDialog()
    assert.equal(harness.module.ownerUsersLoading.value, true)
    harness.module.form.name = 'Retry-owned knowledge base'
    harness.module.form.description = 'Created after retry'
    harness.module.form.embedding_model_key = '2::embed-v1'

    await harness.module.submitForm()
    assert.equal(form.validateCalls, 0)
    assert.equal(harness.api.creates.length, 0)

    firstRequest.reject(new Error('network error'))
    await pending
    assert.equal(
      harness.module.ownerUsersError.value,
      locale === 'zh' ? '获取用户列表失败：network error' : 'Failed to load users: network error',
    )

    await harness.module.submitForm()
    assert.equal(form.validateCalls, 0)
    assert.equal(harness.api.creates.length, 0)

    const retry = harness.module.fetchOwnerUsers()
    assert.equal(harness.module.ownerUsersLoading.value, true)
    retryRequest.resolve(dataResponse({ items: [currentUser, owner], total: 2 }))
    await retry
    assert.equal(harness.module.ownerUsersError.value, null)

    harness.module.form.uid = owner.uid
    await harness.module.submitForm()

    assert.equal(form.validateCalls, 1)
    assert.deepEqual(normalize(harness.api.creates.map(request => request.payload)), [{
      name: 'Retry-owned knowledge base',
      description: 'Created after retry',
      embedding_channel_id: 2,
      embedding_model_id: 'embed-v1',
      uid: owner.uid,
    }])
    assert.notEqual(harness.api.creates[0].payload.uid, currentUser.uid)
  }
})

test('ignores an old response after retry, reopen, edit, unmount, role change, or login change', async t => {
  for (const action of ['retry', 'reopen', 'edit', 'unmount', 'role', 'uid']) {
    const currentUser = { uid: 'admin-user', username: 'Administrator' }
    const staleRequest = deferred()
    const freshRequest = deferred()
    let callCount = 0
    const harness = await createHarness(t, {
      superuser: true,
      currentUid: currentUser.uid,
      currentUsername: currentUser.username,
      userList: () => {
        callCount += 1
        return ['retry', 'reopen'].includes(action) && callCount === 2
          ? freshRequest.promise
          : staleRequest.promise
      },
    })
    const pending = harness.module.showDialog()

    if (action === 'retry') {
      const retry = harness.module.fetchOwnerUsers()
      freshRequest.resolve(dataResponse({ items: [currentUser], total: 1 }))
      await retry
    } else if (action === 'reopen') {
      const reopening = harness.module.showDialog()
      freshRequest.resolve(dataResponse({ items: [currentUser], total: 1 }))
      await reopening
    } else if (action === 'edit') {
      harness.module.showEditDialog({
        id: 'kb-edit',
        name: 'Edited knowledge base',
        description: '',
        embedding_channel_id: 1,
        embedding_model_id: 'embedding-model',
        uid: 'edited-owner',
        username: 'Edited owner',
      })
    } else if (action === 'unmount') {
      for (const callback of harness.unmountedCallbacks) callback()
    } else if (action === 'role') {
      harness.permissionsStore.currentUser = {
        uid: currentUser.uid,
        username: currentUser.username,
        is_superuser: false,
      }
    } else {
      harness.permissionsStore.currentUser = {
        uid: 'new-user',
        username: 'New user',
        is_superuser: true,
      }
    }

    staleRequest.resolve(dataResponse({ items: [{ uid: 'stale-owner', username: 'Stale owner' }], total: 1 }))
    await pending

    if (['unmount', 'role', 'uid'].includes(action)) {
      assert.deepEqual(normalize(harness.module.ownerUsers.value), [])
      assert.equal(harness.module.form.uid, null)
      assert.equal(harness.module.form.username, '')
      assert.equal(harness.module.dialogVisible.value, false)
    } else {
      assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
      assert.equal(harness.module.form.uid, action === 'edit' ? 'edited-owner' : currentUser.uid)
    }
    if (action === 'edit') assert.equal(harness.module.isEditing.value, true)
  }
})

test('uses the localized owner identity for each knowledge-base delete confirmation', async t => {
  const knowledgeBases = [
    {
      id: 'kb-owner-a',
      uid: 'opaque-account-a',
      username: 'Alice',
      name: 'Shared knowledge base',
      profile_names: ['Shared profile'],
    },
    {
      id: 'kb-owner-b',
      uid: 'opaque-account-b',
      username: 'Bob',
      name: 'Shared knowledge base',
      profile_names: ['Shared profile'],
    },
  ]

  for (const locale of ['zh', 'en']) {
    const harness = await createHarness(t, { locale, knowledgeBases })
    await harness.module.fetchData()
    const [first, second] = harness.module.tableData.value

    assert.deepEqual(first.profile_names, second.profile_names)
    await harness.module.handleDelete(first)
    await harness.module.handleDelete(second)

    assert.equal(harness.confirms.length, 2)
    assert.equal(harness.confirms[0][1], locale === 'zh' ? '提示' : 'Prompt')
    assert.notEqual(harness.confirms[0][0], harness.confirms[1][0])

    for (const [confirmation, row, other] of [
      [harness.confirms[0], first, second],
      [harness.confirms[1], second, first],
    ]) {
      const [message, , options] = confirmation
      assert.ok(message.includes(row.username))
      assert.ok(message.includes(row.id))
      assert.ok(message.includes(row.name))
      assert.ok(!message.includes(other.username))
      assert.ok(!message.includes(row.uid))
      assert.ok(!message.includes(other.uid))
      assert.equal(options.dangerouslyUseHTMLString, false)
    }

    assert.deepEqual(harness.api.deletes.map(request => request.id), [first.id, second.id])
  }
})

test('does not delete a knowledge base when confirmation is cancelled or closed', async t => {
  const row = {
    id: 'kb-cancelled',
    uid: 'owner-cancelled',
    name: 'Cancelled knowledge base',
  }

  for (const locale of ['zh', 'en']) {
    for (const action of ['cancel', 'close']) {
      const harness = await createHarness(t, {
        locale,
        confirm: async () => {
          if (action === 'close') throw 'close'
          return false
        },
      })

      await harness.module.handleDelete(row)
      assert.equal(harness.confirms.length, 1, `${locale}:${action}`)
      assert.equal(harness.api.deletes.length, 0, `${locale}:${action}`)
    }
  }
})

test('waits for confirmation and preserves literal names and unknown owner placeholders', async t => {
  for (const locale of ['zh', 'en']) {
    let resolveConfirmation
    const confirmation = new Promise(resolve => {
      resolveConfirmation = resolve
    })
    const harness = await createHarness(t, { locale, confirm: () => confirmation })
    const unknownOwnerLabel = locale === 'zh' ? '未知用户' : 'Unknown user'
    for (const username of [undefined, null, '']) {
      assert.equal(harness.module.getKnowledgeBaseOwnerLabel({ username }), unknownOwnerLabel)
    }
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(undefined), unknownOwnerLabel)
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(null), unknownOwnerLabel)
    const specialRow = {
      id: 'kb-special',
      uid: 'owner-special',
      name: 'KB <alpha> & "quotes"',
    }

    const pendingDelete = harness.module.handleDelete(specialRow)
    assert.equal(harness.api.deletes.length, 0)
    assert.equal(harness.confirms.length, 1)
    const [specialMessage, , specialOptions] = harness.confirms[0]
    assert.ok(specialMessage.includes(specialRow.name))
    assert.equal(specialOptions.dangerouslyUseHTMLString, false)

    resolveConfirmation(true)
    await pendingDelete

    const missingUsernameRow = {
      id: 'missingKbId',
      uid: 'opaque-user-id',
      name: 'Missing UID knowledge base',
    }
    await harness.module.handleDelete(missingUsernameRow)
    const [missingMessage, , missingOptions] = harness.confirms[1]
    assert.ok(missingMessage.includes(unknownOwnerLabel))
    assert.ok(!missingMessage.includes(missingUsernameRow.uid))
    assert.doesNotMatch(missingMessage, /undefined/)
    assert.equal(missingOptions.dangerouslyUseHTMLString, false)
    assert.deepEqual(harness.api.deletes.map(request => request.id), [specialRow.id, missingUsernameRow.id])
  }
})

test('repairs a selected owner from the complete admin user list without opening an arbitrary migration', async t => {
  const users = Array.from({ length: 205 }, (_, index) => ({
    uid: `owner-${index}`,
    username: `Owner ${index}`,
  }))
  users[100] = { uid: 'admin-user', username: 'Administrator' }
  const selectedOwner = users[204]
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users,
    knowledgeBases: [{
      id: 'kb-current-page',
      uid: 'owner-0',
      name: 'Current page KB',
      knowledge_base_type: 'user',
      embedding_channel_id: 1,
      embedding_model_id: 'embedding-model',
    }],
  })

  await harness.module.fetchData()
  await harness.module.showRepairDialog()

  assert.equal(harness.module.repairDialogVisible.value, true)
  assert.equal(harness.api.userLists.length, 3)
  assert.deepEqual(harness.api.userLists.map(request => request.params), [
    { page: 1, size: 100 },
    { page: 2, size: 100 },
    { page: 3, size: 100 },
  ])
  assert.deepEqual(normalize(harness.module.repairOwners.value), users)
  assert.equal(harness.module.repairOwnerUid.value, 'admin-user')
  assert.equal(harness.module.tableData.value.some(row => row.uid === selectedOwner.uid), false)

  harness.module.repairOwnerUid.value = selectedOwner.uid
  await harness.module.submitUserRepair()

  assert.equal(harness.api.repairs.length, 1)
  assert.deepEqual(harness.api.repairs[0].params, { uid: selectedOwner.uid })
  assert.equal(typeof harness.api.repairs[0].payload.dedupe_key, 'string')
  assert.ok(harness.api.repairs[0].payload.dedupe_key.length > 0)
  assert.equal(harness.module.repairDialogVisible.value, false)
  assert.equal(harness.module.migrationDialogVisible.value, false)
  assert.equal(harness.api.gets.length, 0)
  assert.ok(harness.api.lists.length >= 2)
})

test('repairs an ordinary user only for the logged-in uid without loading admin users', async t => {
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    users: [{ uid: 'other-user', username: 'Other user' }],
  })

  await harness.module.showRepairDialog()

  assert.equal(harness.module.repairDialogVisible.value, true)
  assert.equal(harness.module.repairOwnerUid.value, 'user-a')
  assert.equal(harness.api.userLists.length, 0)

  await harness.module.submitUserRepair()

  assert.equal(harness.api.repairs.length, 1)
  assert.deepEqual(harness.api.repairs[0].params, { uid: 'user-a' })
  assert.equal(harness.api.userLists.length, 0)
  assert.equal(harness.module.repairDialogVisible.value, false)
  assert.equal(harness.module.migrationDialogVisible.value, false)
})

test('blocks user repair while owners load, after owner-list failure, or without a valid selection', async t => {
  const pendingUsers = deferred()
  const loadingHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    userList: () => pendingUsers.promise,
  })
  const loading = loadingHarness.module.showRepairDialog()

  assert.equal(loadingHarness.module.repairOwnersLoading.value, true)
  await loadingHarness.module.submitUserRepair()
  assert.equal(loadingHarness.api.repairs.length, 0)

  pendingUsers.resolve(dataResponse({
    items: [
      { uid: 'admin-user', username: 'Administrator' },
      { uid: 'owner-b', username: 'Bob' },
    ],
    total: 2,
  }))
  await loading
  loadingHarness.module.repairOwnerUid.value = 'not-in-loaded-list'
  await loadingHarness.module.submitUserRepair()
  assert.equal(loadingHarness.api.repairs.length, 0)

  const failedHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    userList: () => Promise.reject(new Error('users unavailable')),
  })
  await failedHarness.module.showRepairDialog()
  assert.equal(failedHarness.module.repairOwnersLoading.value, false)
  assert.notEqual(failedHarness.module.repairOwnersError.value, null)
  await failedHarness.module.submitUserRepair()
  assert.equal(failedHarness.api.repairs.length, 0)

  const emptyHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users: [],
  })
  await emptyHarness.module.showRepairDialog()
  assert.deepEqual(normalize(emptyHarness.module.repairOwners.value), [])
  assert.equal(emptyHarness.module.repairOwnerUid.value, null)
  await emptyHarness.module.submitUserRepair()
  assert.equal(emptyHarness.api.repairs.length, 0)
})

test('keeps user repair open and retryable after an API failure, and blocks duplicate submissions', async t => {
  const request = deferred()
  const knowledgeBase = {
    id: 'kb-user-a',
    uid: 'user-a',
    username: 'Alice',
    name: 'Alice KB',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  let repairCalls = 0
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    knowledgeBases: [knowledgeBase],
    repair: (payload, params) => {
      repairCalls += 1
      if (repairCalls === 1) return Promise.reject(new Error('repair unavailable'))
      if (repairCalls === 2) return request.promise
      return Promise.resolve(dataResponse({
        items: [{ ...knowledgeBase, migration_job_id: 103 }],
        total: 1,
      }))
    },
  })

  await harness.module.showRepairDialog()
  await harness.module.submitUserRepair()
  assert.equal(harness.module.repairDialogVisible.value, true)
  assert.equal(harness.module.repairSubmitting.value, false)

  const retry = harness.module.submitUserRepair()
  await harness.module.submitUserRepair()
  assert.equal(harness.api.repairs.length, 2)
  assert.equal(harness.module.repairSubmitting.value, true)
  request.resolve(dataResponse({
    items: [{ ...knowledgeBase, migration_job_id: 103 }],
    total: 1,
  }))
  await retry

  assert.equal(harness.module.repairDialogVisible.value, false)
  assert.equal(harness.api.repairs.length, 2)
  assert.ok(harness.api.repairs.every(item => item.params.uid === 'user-a'))
})

test('confirms per-knowledge-base repair with literal owner identity and skips cancel or close', async t => {
  const row = {
    id: 'kb-repair-special',
    uid: 'opaque-owner-id',
    username: 'Alice <admin>',
    name: 'Knowledge base <alpha> & "quotes"',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }

  for (const action of ['cancel', 'close']) {
    const harness = await createHarness(t, {
      confirm: async () => {
        if (action === 'close') throw 'close'
        return false
      },
    })

    await harness.module.repairKnowledgeBase(row)

    assert.equal(harness.confirms.length, 1)
    const [message, , options] = harness.confirms[0]
    assert.ok(message.includes(row.name))
    assert.ok(message.includes(row.username))
    assert.ok(message.includes(row.id))
    assert.equal(options.dangerouslyUseHTMLString, false)
    assert.equal(harness.api.repairs.length, 0)
  }
})

test('does not confirm or submit a second knowledge-base repair while one is pending', async t => {
  const confirmation = deferred()
  const repair = deferred()
  const firstRow = {
    id: 'kb-repair-first',
    name: 'First KB',
    username: 'Alice',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const secondRow = {
    id: 'kb-repair-second',
    name: 'Second KB',
    username: 'Bob',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const harness = await createHarness(t, {
    confirm: () => confirmation.promise,
    repair: () => repair.promise,
    get: id => Promise.resolve(dataResponse({ id, migration_status: 'succeeded' })),
  })

  const first = harness.module.repairKnowledgeBase(firstRow)
  await harness.module.repairKnowledgeBase(secondRow)
  assert.equal(harness.confirms.length, 1)
  assert.equal(harness.api.repairs.length, 0)

  confirmation.resolve(true)
  await flushPromises()
  assert.equal(harness.api.repairs.length, 1)
  const duplicateDuringRequest = harness.module.repairKnowledgeBase(secondRow)
  await duplicateDuringRequest
  assert.equal(harness.confirms.length, 1)
  assert.equal(harness.api.repairs.length, 1)

  repair.resolve(dataResponse({ items: [{ id: firstRow.id, migration_job_id: 101 }], total: 1 }))
  await first
  assert.deepEqual(harness.api.repairs[0].params, { kb_id: firstRow.id })
  assert.equal(harness.module.selectedKb.value.id, firstRow.id)
  assert.equal(harness.module.migrationDialogVisible.value, true)
  assert.deepEqual(harness.api.gets.map(request => request.id), [firstRow.id])
})

test('releases a failed per-knowledge-base repair so it can be retried', async t => {
  let repairCalls = 0
  const row = {
    id: 'kb-retry',
    name: 'Retry KB',
    username: 'Alice',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const harness = await createHarness(t, {
    repair: () => {
      repairCalls += 1
      if (repairCalls === 1) return Promise.reject(new Error('repair unavailable'))
      return Promise.resolve(dataResponse({ items: [{ id: row.id, migration_job_id: 102 }], total: 1 }))
    },
    get: id => Promise.resolve(dataResponse({ id, migration_status: 'succeeded' })),
  })

  await harness.module.repairKnowledgeBase(row)
  assert.equal(harness.api.repairs.length, 1)
  await harness.module.repairKnowledgeBase(row)

  assert.equal(harness.api.repairs.length, 2)
  assert.ok(harness.api.repairs.every(request => request.params.kb_id === row.id))
  assert.equal(harness.module.migrationDialogVisible.value, true)
})

test('immediately refreshes migration state by id and polls a KB absent from the current page', async t => {
  const row = {
    id: 'kb-not-in-page',
    name: 'Finished in the table',
    migration_status: 'succeeded',
    migration_total_count: 5,
    migration_success_count: 5,
  }
  let getCalls = 0
  const harness = await createHarness(t, {
    get: id => {
      getCalls += 1
      return Promise.resolve(dataResponse(getCalls === 1
        ? {
            ...row,
            migration_status: 'building',
            migration_total_count: 5,
            migration_success_count: 2,
          }
        : {
            ...row,
            migration_status: 'succeeded',
            migration_total_count: 5,
            migration_success_count: 5,
          }))
    },
  })

  await harness.module.showMigrationDialog(row)

  assert.equal(harness.module.tableData.value.some(item => item.id === row.id), false)
  assert.equal(harness.module.migrationDialogVisible.value, true)
  assert.deepEqual(harness.api.gets.map(request => request.id), [row.id])
  assert.equal(harness.module.selectedKb.value.migration_status, 'building')
  assert.equal(harness.module.selectedKb.value.migration_total_count, 5)
  assert.equal(harness.module.selectedKb.value.migration_success_count, 2)
  assert.equal(harness.module.migrationProgress.value, 40)
  assert.equal(harness.activeTimers().length, 1)

  await harness.runNextTimer()
  assert.deepEqual(harness.api.gets.map(request => request.id), [row.id, row.id])
  assert.equal(harness.module.selectedKb.value.migration_status, 'succeeded')
  assert.equal(harness.module.selectedKb.value.migration_total_count, 5)
  assert.equal(harness.module.selectedKb.value.migration_success_count, 5)
  assert.equal(harness.module.migrationProgress.value, 100)
  assert.equal(harness.activeTimers().length, 0)
})

test('ignores stale migration responses after close, switching KB, unmount, or identity change', async t => {
  for (const action of ['close', 'switch', 'unmount', 'identity']) {
    const stale = deferred()
    const firstRow = {
      id: 'kb-stale',
      name: 'Stale KB',
      migration_status: 'succeeded',
      migration_total_count: 5,
      migration_success_count: 5,
    }
    const secondRow = {
      id: 'kb-current',
      name: 'Current KB',
      migration_status: 'succeeded',
      migration_total_count: 5,
      migration_success_count: 5,
    }
    const harness = await createHarness(t, {
      currentUid: 'user-a',
      currentUsername: 'Alice',
      get: id => id === firstRow.id
        ? stale.promise
        : Promise.resolve(dataResponse({ ...secondRow, id })),
    })
    const pending = harness.module.showMigrationDialog(firstRow)
    assert.deepEqual(harness.api.gets.map(request => request.id), [firstRow.id])

    if (action === 'close') {
      harness.module.closeMigrationDialog()
    } else if (action === 'switch') {
      await harness.module.showMigrationDialog(secondRow)
    } else if (action === 'unmount') {
      for (const callback of harness.unmountedCallbacks) callback()
    } else {
      harness.permissionsStore.currentUser = {
        uid: 'user-b',
        username: 'Bob',
        is_superuser: false,
      }
      await flushPromises()
    }

    const selectedBeforeStaleResponse = normalize(harness.module.selectedKb.value ?? null)
    stale.resolve(dataResponse({
      ...firstRow,
      migration_status: 'building',
      migration_total_count: 5,
      migration_success_count: 2,
    }))
    await pending
    await flushPromises()

    assert.deepEqual(normalize(harness.module.selectedKb.value ?? null), selectedBeforeStaleResponse, action)
    assert.equal(harness.activeTimers().length, 0, action)
    if (action === 'close') assert.equal(harness.module.migrationDialogVisible.value, false)
    if (action === 'switch') assert.equal(harness.module.selectedKb.value.id, secondRow.id)
  }
})

test('accepts the page-change event page number and updates the requested page normally', async t => {
  const harness = await createHarness(t, {
    knowledgeBases: [{
      id: 'kb-page-two',
      uid: 'user-a',
      username: 'Alice',
      name: 'Page two KB',
      knowledge_base_type: 'user',
      embedding_channel_id: 1,
      embedding_model_id: 'embedding-model',
    }],
  })
  harness.module.currentPage.value = 2

  await harness.module.fetchData(2)

  assert.equal(harness.api.lists.at(-1).params.page, 2)
  assert.deepEqual(harness.module.tableData.value.map(row => row.id), ['kb-page-two'])
  assert.equal(harness.messages.some(message => message.type === 'error'), false)
})

test('routes the more-menu repair action through confirmation and repairs only that knowledge base', async t => {
  const row = {
    id: 'kb-more-repair',
    uid: 'owner-a',
    username: 'Alice',
    name: 'More menu KB',
    knowledge_base_type: 'user',
    embedding_channel_id: 1,
    embedding_model_id: 'embedding-model',
  }
  const cancelled = await createHarness(t, { confirm: async () => false })

  await cancelled.module.handleKnowledgeBaseMoreAction('repair', row)

  assert.equal(cancelled.confirms.length, 1)
  assert.ok(cancelled.confirms[0][0].includes(row.name))
  assert.ok(cancelled.confirms[0][0].includes(row.username))
  assert.ok(cancelled.confirms[0][0].includes(row.id))
  assert.equal(cancelled.confirms[0][2].dangerouslyUseHTMLString, false)
  assert.equal(cancelled.api.repairs.length, 0)

  const repaired = await createHarness(t, {
    repair: () => Promise.resolve(dataResponse({
      items: [{ ...row, migration_job_id: 501 }],
      total: 1,
    })),
    get: id => Promise.resolve(dataResponse({ id, migration_status: 'succeeded' })),
  })

  await repaired.module.handleKnowledgeBaseMoreAction('repair', row)
  await flushPromises()

  assert.equal(repaired.confirms.length, 1)
  assert.ok(repaired.confirms[0][0].includes(row.name))
  assert.ok(repaired.confirms[0][0].includes(row.username))
  assert.ok(repaired.confirms[0][0].includes(row.id))
  assert.equal(repaired.api.repairs.length, 1)
  assert.deepEqual(repaired.api.repairs[0].params, { kb_id: row.id })
  assert.deepEqual(repaired.api.gets.map(request => request.id), [row.id])
  assert.equal(repaired.module.selectedKb.value.id, row.id)
})

test('clears loaded knowledge-base state on identity changes and unmount', async t => {
  const oldKb = {
    id: 'kb-old-owner-scope',
    uid: 'admin-user',
    username: 'Administrator',
    name: 'Old owner knowledge base',
    description: 'Old description',
    knowledge_base_type: 'llm_managed',
    embedding_channel_id: 2,
    embedding_model_id: 'embedding-v2',
    migration_status: 'building',
    target_embedding_channel_id: 3,
    target_embedding_model_id: 'embedding-v3',
  }
  const documentKb = {
    id: 'kb-old-document-scope',
    uid: oldKb.uid,
    username: oldKb.username,
    name: 'Old document knowledge base',
    knowledge_base_type: 'user',
    embedding_channel_id: 2,
    embedding_model_id: 'embedding-v2',
  }
  const document = { id: 'doc-secret', filename: 'secret-old-document.txt' }
  const managedItem = {
    id: 'managed-secret',
    knowledge_key: 'secret-managed-key',
    content: 'secret managed list content',
    version: 3,
    llm_maintainable: true,
    pending_job_id: 'publication-job-3',
  }
  const users = [
    { uid: 'admin-user', username: 'Administrator' },
    { uid: 'owner-b', username: 'Bob' },
  ]

  for (const action of ['uid', 'role', 'clear', 'unmount']) {
    let listCalls = 0
    const harness = await createHarness(t, {
      superuser: true,
      currentUid: 'admin-user',
      currentUsername: 'Administrator',
      users,
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
      documents: () => Promise.resolve(dataResponse({ items: [document], total: 1 })),
      document: () => Promise.resolve(dataResponse({ content: 'secret original document text' })),
      managedItems: () => Promise.resolve(dataResponse({ items: [managedItem], total: 1 })),
      managedItem: () => Promise.resolve(dataResponse({
        id: managedItem.id,
        knowledge_key: 'secret-managed-key',
        content: 'secret managed detail content',
        version: 4,
        llm_maintainable: true,
      })),
      managedHistory: () => Promise.resolve(dataResponse([{
        id: 'managed-history-1',
        knowledge_key: 'secret-managed-key',
        content: 'secret managed history content',
      }])),
      organizationJobs: () => Promise.resolve(dataResponse({
        items: [{
          job_id: 'organization-job-1',
          knowledge_base_id: oldKb.id,
          status: 'running',
          child_job_count: 1,
          child_terminal_count: 0,
        }],
        total: 1,
      })),
      queryTest: () => Promise.resolve(dataResponse({
        items: [{ id: 'query-result-1', content: 'secret query result' }],
        retrieval_mode: 'hybrid',
        rerank_error: 'secret rerank diagnostic',
      })),
    })
    const module = harness.module
    module.formRef.value = {
      resetFields() {},
      clearValidate() {},
      validate: async callback => {
        if (callback) await callback(true)
        return true
      },
    }
    module.importFormRef.value = {
      resetFields() {},
      clearValidate() {},
      validateField() {},
    }
    module.managedKnowledgeFormRef.value = { clearValidate() {} }
    module.queryTestFormRef.value = {
      clearValidate() {},
      validate: async callback => {
        if (callback) await callback(true)
        return true
      },
    }

    await module.fetchData()
    await module.showDialog()
    assert.deepEqual(normalize(module.ownerUsers.value), users)
    module.showEditDialog(oldKb)
    await module.showRepairDialog()
    await module.showMigrationDialog(oldKb)
    await module.showDocumentDialog(documentKb)
    await module.showContentDialog(document)
    module.showImportDialog(documentKb)
    const rawFile = new Blob(['secret selected upload contents'], { type: 'text/plain' })
    const uploadFile = { name: 'secret-upload.txt', raw: rawFile }
    module.handleFileChange(uploadFile, [uploadFile])
    await module.showManagedKnowledgeDialog(oldKb)
    await module.showManagedKnowledgeEditDialog(managedItem)
    await module.showManagedKnowledgeHistory(managedItem)
    await module.showOrganizationDialog()
    module.showQueryTestDialog(oldKb)
    module.queryTestForm.query = 'secret query text'
    module.queryTestForm.top_k = 7
    await module.submitQueryTest()

    const dialogs = [
      module.dialogVisible,
      module.repairDialogVisible,
      module.migrationDialogVisible,
      module.importDialogVisible,
      module.documentDialogVisible,
      module.contentDialogVisible,
      module.managedKnowledgeDialogVisible,
      module.managedKnowledgeEditDialogVisible,
      module.managedKnowledgeHistoryDialogVisible,
      module.organizationDialogVisible,
      module.queryTestDialogVisible,
    ]
    assert.deepEqual(dialogs.map(dialog => dialog.value), Array(11).fill(true), action)
    assert.equal(module.selectedKb.value.id, oldKb.id)
    assert.equal(module.total.value, 1)
    assert.equal(module.repairRequiredCount.value, 5)
    assert.equal(module.form.name, oldKb.name)
    assert.equal(module.editingId.value, oldKb.id)
    assert.deepEqual(normalize(module.documentList.value), [document])
    assert.equal(module.documentContent.value, 'secret original document text')
    assert.equal(module.uploadFileList.value[0].name, 'secret-upload.txt')
    assert.equal(module.importForm.file, rawFile)
    assert.equal(module.managedKnowledgeItems.value[0].content, managedItem.content)
    assert.equal(module.managedKnowledgeForm.content, 'secret managed detail content')
    assert.equal(module.managedKnowledgeHistory.value[0].content, 'secret managed history content')
    assert.equal(module.organizationJobs.value[0].status, 'running')
    assert.equal(module.queryTestResults.value[0].content, 'secret query result')
    assert.equal(module.retrievalMode.value, 'hybrid')
    assert.equal(module.rerankError.value, 'secret rerank diagnostic')
    assert.equal(module.queryTested.value, true)
    assert.ok(harness.activeTimers().length > 0, action)

    const assertCleared = () => {
      assert.deepEqual(dialogs.map(dialog => dialog.value), Array(11).fill(false), action)
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
      assert.deepEqual(normalize(module.documentList.value), [], action)
      assert.equal(module.documentTotal.value, 0, action)
      assert.equal(module.documentPage.value, 1, action)
      assert.equal(module.documentPageSize.value, 10, action)
      assert.equal(module.documentContent.value, '', action)
      assert.equal(module.contentTitle.value, '', action)
      assert.deepEqual(normalize(module.uploadFileList.value), [], action)
      assert.equal(module.importForm.file, null, action)
      assert.equal(module.importForm.chunk_size, 1000, action)
      assert.equal(module.importForm.chunk_overlap, 100, action)
      assert.equal(module.importForm.batch_size, 16, action)
      assert.equal(module.importing.value, false, action)
      assert.deepEqual(normalize(module.managedKnowledgeItems.value), [], action)
      assert.equal(module.managedKnowledgeTotal.value, 0, action)
      assert.equal(module.managedKnowledgePage.value, 1, action)
      assert.equal(module.managedKnowledgePageSize.value, 10, action)
      assert.equal(module.managedKnowledgeQuery.value, '', action)
      assert.equal(module.managedKnowledgeEditingId.value, null, action)
      assert.deepEqual(normalize(module.managedKnowledgeForm), {
        knowledge_key: '',
        content: '',
        expected_version: null,
        llm_maintainable: false,
      }, action)
      assert.equal(module.managedKnowledgeLoading.value, false, action)
      assert.equal(module.managedKnowledgeSubmitting.value, false, action)
      assert.deepEqual(normalize(module.managedKnowledgeHistory.value), [], action)
      assert.equal(module.managedKnowledgeHistoryKey.value, '', action)
      assert.equal(module.managedKnowledgeHistoryLoading.value, false, action)
      assert.deepEqual(normalize(module.organizationJobs.value), [], action)
      assert.equal(module.organizationTotal.value, 0, action)
      assert.equal(module.organizationActionJobId.value, null, action)
      assert.equal(module.organizationLoading.value, false, action)
      assert.equal(module.organizationSubmitting.value, false, action)
      assert.deepEqual(normalize(module.queryTestForm), { query: '', top_k: 5 }, action)
      assert.deepEqual(normalize(module.queryTestResults.value), [], action)
      assert.equal(module.retrievalMode.value, null, action)
      assert.equal(module.rerankError.value, null, action)
      assert.equal(module.queryTested.value, false, action)
      assert.equal(module.queryTesting.value, false, action)
      assert.equal(harness.activeTimers().length, 0, action)
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

test('ignores late read success or failure after role downgrade for every knowledge-base API', async t => {
  const userKb = { id: 'kb-user-read', name: 'User KB', knowledge_base_type: 'user' }
  const managedKb = { id: 'kb-managed-read', name: 'Managed KB', knowledge_base_type: 'llm_managed' }
  const oldDocument = { id: 'old-document', filename: 'old-secret.txt' }
  const freshDocument = { id: 'fresh-document', filename: 'fresh-document.txt' }
  const oldItem = {
    id: 'old-managed-item',
    knowledge_key: 'old-secret-key',
    content: 'old-secret managed content',
    version: 1,
    llm_maintainable: true,
    pending_job_id: 'old-pending-job',
  }
  const freshItem = {
    id: 'fresh-managed-item',
    knowledge_key: 'fresh-managed-key',
    content: 'fresh managed content',
    version: 9,
    llm_maintainable: true,
  }
  const cases = [
    {
      api: 'documents',
      kb: userKb,
      start: module => module.showDocumentDialog(userKb),
      oldData: { items: [oldDocument], total: 91 },
      freshData: { items: [freshDocument], total: 7 },
      state: module => ({
        dialog: module.documentDialogVisible.value,
        loading: module.documentLoading.value,
        items: module.documentList.value,
        total: module.documentTotal.value,
      }),
      cleared: { kb: null, dialog: false, loading: false, items: [], total: 0 },
      pending: { kb: userKb.id, dialog: true, loading: true, items: [], total: 0 },
      loaded: { kb: userKb.id, dialog: true, loading: false, items: [freshDocument], total: 7 },
    },
    {
      api: 'document',
      kb: userKb,
      oldInput: oldDocument,
      freshInput: freshDocument,
      start: (module, row) => {
        module.selectedKb.value = userKb
        return module.showContentDialog(row)
      },
      oldData: { content: 'old-secret original document text' },
      freshData: { content: 'fresh original document text' },
      state: module => ({
        dialog: module.contentDialogVisible.value,
        title: module.contentTitle.value,
        content: module.documentContent.value,
      }),
      cleared: { kb: null, dialog: false, title: '', content: '' },
      pending: { kb: userKb.id, dialog: false, title: '', content: '' },
      loaded: {
        kb: userKb.id,
        dialog: true,
        title: freshDocument.filename,
        content: 'fresh original document text',
      },
    },
    {
      api: 'managedItems',
      kb: managedKb,
      start: module => module.showManagedKnowledgeDialog(managedKb),
      oldData: { items: [oldItem], total: 91 },
      freshData: { items: [freshItem], total: 8 },
      state: module => ({
        dialog: module.managedKnowledgeDialogVisible.value,
        loading: module.managedKnowledgeLoading.value,
        items: module.managedKnowledgeItems.value,
        total: module.managedKnowledgeTotal.value,
      }),
      cleared: { kb: null, dialog: false, loading: false, items: [], total: 0 },
      pending: { kb: managedKb.id, dialog: true, loading: true, items: [], total: 0 },
      loaded: { kb: managedKb.id, dialog: true, loading: false, items: [freshItem], total: 8 },
    },
    {
      api: 'managedItem',
      kb: managedKb,
      oldInput: oldItem,
      freshInput: freshItem,
      start: (module, row) => {
        module.selectedKb.value = managedKb
        return module.showManagedKnowledgeEditDialog(row)
      },
      oldData: {
        id: oldItem.id,
        knowledge_key: oldItem.knowledge_key,
        content: oldItem.content,
        version: oldItem.version,
        llm_maintainable: true,
      },
      freshData: freshItem,
      state: module => ({
        dialog: module.managedKnowledgeEditDialogVisible.value,
        id: module.managedKnowledgeEditingId.value,
        key: module.managedKnowledgeForm.knowledge_key,
        content: module.managedKnowledgeForm.content,
        version: module.managedKnowledgeForm.expected_version,
      }),
      cleared: { kb: null, dialog: false, id: null, key: '', content: '', version: null },
      pending: { kb: managedKb.id, dialog: false, id: null, key: '', content: '', version: null },
      loaded: {
        kb: managedKb.id,
        dialog: true,
        id: freshItem.id,
        key: freshItem.knowledge_key,
        content: freshItem.content,
        version: freshItem.version,
      },
    },
    {
      api: 'managedHistory',
      kb: managedKb,
      oldInput: oldItem,
      freshInput: freshItem,
      start: (module, row) => {
        module.selectedKb.value = managedKb
        return module.showManagedKnowledgeHistory(row)
      },
      oldData: [{ id: 'old-history', knowledge_key: oldItem.knowledge_key, content: 'old-secret history' }],
      freshData: [{
        id: 'fresh-history',
        knowledge_key: freshItem.knowledge_key,
        content: 'fresh history content',
        version: freshItem.version,
      }],
      state: module => ({
        dialog: module.managedKnowledgeHistoryDialogVisible.value,
        loading: module.managedKnowledgeHistoryLoading.value,
        key: module.managedKnowledgeHistoryKey.value,
        history: module.managedKnowledgeHistory.value,
      }),
      cleared: { kb: null, dialog: false, loading: false, key: '', history: [] },
      pending: {
        kb: managedKb.id,
        dialog: true,
        loading: true,
        key: freshItem.knowledge_key,
        history: [],
      },
      loaded: {
        kb: managedKb.id,
        dialog: true,
        loading: false,
        key: freshItem.knowledge_key,
        history: [{
          id: 'fresh-history',
          knowledge_key: freshItem.knowledge_key,
          content: 'fresh history content',
          version: freshItem.version,
        }],
      },
    },
    {
      api: 'organizationJobs',
      kb: managedKb,
      start: module => {
        module.selectedKb.value = managedKb
        return module.showOrganizationDialog()
      },
      oldData: {
        items: [{
          job_id: 'old-organization-job',
          knowledge_base_id: managedKb.id,
          status: 'running',
          child_job_count: 1,
          child_terminal_count: 0,
        }],
        total: 91,
      },
      freshData: {
        items: [{
          job_id: 'fresh-organization-job',
          knowledge_base_id: managedKb.id,
          status: 'succeeded',
          child_job_count: 1,
          child_terminal_count: 1,
        }],
        total: 6,
      },
      state: module => ({
        dialog: module.organizationDialogVisible.value,
        loading: module.organizationLoading.value,
        jobs: module.organizationJobs.value,
        total: module.organizationTotal.value,
      }),
      cleared: { kb: null, dialog: false, loading: false, jobs: [], total: 0 },
      pending: { kb: managedKb.id, dialog: true, loading: true, jobs: [], total: 0 },
      loaded: {
        kb: managedKb.id,
        dialog: true,
        loading: false,
        jobs: [{
          job_id: 'fresh-organization-job',
          knowledge_base_id: managedKb.id,
          status: 'succeeded',
          child_job_count: 1,
          child_terminal_count: 1,
        }],
        total: 6,
      },
    },
  ]
  const snapshot = (scenario, module) => normalize({
    kb: module.selectedKb.value?.id ?? null,
    ...scenario.state(module),
  })

  for (const scenario of cases) {
    for (const outcome of ['success', 'failure']) {
      const oldRequest = deferred()
      const freshRequest = deferred()
      let requestCount = 0
      const harness = await createHarness(t, {
        superuser: true,
        currentUid: 'admin-user',
        [scenario.api]: () => {
          requestCount += 1
          return requestCount === 1 ? oldRequest.promise : freshRequest.promise
        },
      })
      const oldLoad = scenario.start(harness.module, scenario.oldInput)
      assert.equal(harness.api[scenario.api].length, 1, scenario.api)

      harness.permissionsStore.currentUser = {
        uid: 'admin-user',
        username: 'Administrator',
        is_superuser: false,
      }
      await flushPromises()
      assert.deepEqual(snapshot(scenario, harness.module), scenario.cleared, `${scenario.api}:${outcome}:reset`)
      assert.equal(harness.activeTimers().length, 0, `${scenario.api}:${outcome}:reset timers`)
      if (scenario.api === 'managedItems') {
        assert.equal(harness.api.managedItems[0].config.signal.aborted, true)
      }

      const freshLoad = scenario.start(harness.module, scenario.freshInput)
      assert.equal(harness.api[scenario.api].length, 2, scenario.api)
      assert.deepEqual(harness.api[scenario.api].map(request => request.id), [scenario.kb.id, scenario.kb.id])
      assert.deepEqual(snapshot(scenario, harness.module), scenario.pending, `${scenario.api}:${outcome}:pending`)
      const freshSnapshot = snapshot(scenario, harness.module)

      if (outcome === 'success') oldRequest.resolve(dataResponse(scenario.oldData))
      else oldRequest.reject(new Error(`old-secret ${scenario.api} failure`))
      await oldLoad
      await flushPromises()

      assert.deepEqual(snapshot(scenario, harness.module), freshSnapshot, `${scenario.api}:${outcome}:late`)
      assert.deepEqual(snapshot(scenario, harness.module), scenario.pending, `${scenario.api}:${outcome}:still pending`)
      assert.deepEqual(harness.messages, [], `${scenario.api}:${outcome}:messages`)
      assert.equal(harness.activeTimers().length, 0, `${scenario.api}:${outcome}:timers`)

      freshRequest.resolve(dataResponse(scenario.freshData))
      await freshLoad
      assert.deepEqual(snapshot(scenario, harness.module), scenario.loaded, `${scenario.api}:${outcome}:fresh`)
      assert.deepEqual(harness.messages, [], `${scenario.api}:${outcome}:fresh messages`)
      assert.equal(harness.activeTimers().length, 0, `${scenario.api}:${outcome}:fresh timers`)
      if (scenario.api === 'organizationJobs') {
        assert.deepEqual(
          harness.module.organizationJobs.value.map(job => job.knowledge_base_id),
          [scenario.kb.id],
        )
      }
    }
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
  const managedKb = {
    id: 'kb-confirm-managed',
    name: 'Managed KB',
    knowledge_base_type: 'llm_managed',
  }
  const document = { id: 'doc-confirm-delete', filename: 'document.txt' }
  const managedItem = {
    id: 'item-confirm-delete',
    knowledge_key: 'key-to-delete',
    content: 'managed content',
    version: 3,
  }
  const cases = [
    { api: 'deletes', kb: userKb, start: module => module.handleDelete(userKb) },
    {
      api: 'deleteDocument',
      kb: userKb,
      start: module => module.handleDeleteDocument(document),
    },
    {
      api: 'deleteManagedItem',
      kb: managedKb,
      start: module => module.handleDeleteManagedKnowledge(managedItem),
    },
    {
      api: 'cancelOrganizationJob',
      kb: managedKb,
      start: module => module.handleCancelOrganizationJob({
        job_id: 'job-to-cancel',
        knowledge_base_id: managedKb.id,
        status: 'running',
        child_job_count: 1,
        child_terminal_count: 0,
      }),
    },
    {
      api: 'retryOrganizationJob',
      kb: managedKb,
      start: module => module.handleRetryOrganizationJob({
        job_id: 'job-to-retry',
        knowledge_base_id: managedKb.id,
        status: 'failed',
        can_retry: true,
      }),
    },
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
  const managedKbA = { id: 'kb-managed-a-validation', uid: 'user-a', name: 'Alice managed KB', knowledge_base_type: 'llm_managed' }
  const managedKbB = { id: 'kb-managed-b-validation', uid: 'user-b', name: 'Bob managed KB', knowledge_base_type: 'llm_managed' }
  const managedItemA = { id: 'managed-item-a', knowledge_key: 'old-key', content: 'old managed content', version: 2 }
  const managedItemB = { id: 'managed-item-b', knowledge_key: 'fresh-key', content: 'existing managed content', version: 4 }
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
    {
      api: 'importDocument', method: 'importDocument', ref: 'importFormRef', oldTarget: userKbA, freshTarget: userKbB,
      open: (module, kb) => module.showImportDialog(kb),
      fill: (module, value) => {
        const uploadFile = { name: `${value}.txt`, raw: new Blob([`${value} imported document`], { type: 'text/plain' }) }
        module.handleFileChange(uploadFile, [uploadFile])
      },
      busy: module => module.importing.value,
      dialogs: module => [module.importDialogVisible.value], completedDialogs: [false],
      snapshot: module => ({ file: module.uploadFileList.value[0]?.name ?? '', size: module.importForm.file?.size ?? 0, dialog: module.importDialogVisible.value, busy: module.importing.value }),
      assertRequest: async request => {
        assert.equal(request.id, userKbB.id)
        assert.equal(await request.payload.get('file').text(), 'fresh imported document')
      },
      result: { status: 'accepted' },
    },
    {
      api: 'createManagedItem', method: 'createManagedItem', ref: 'managedKnowledgeFormRef', oldTarget: managedKbA, freshTarget: managedKbB,
      open: async (module, kb) => {
        await module.showManagedKnowledgeDialog(kb)
        module.showManagedKnowledgeCreateDialog()
      },
      fill: (module, value) => {
        module.managedKnowledgeForm.knowledge_key = `${value}-key`
        module.managedKnowledgeForm.content = `${value} managed content`
      },
      busy: module => module.managedKnowledgeSubmitting.value,
      dialogs: module => [module.managedKnowledgeDialogVisible.value, module.managedKnowledgeEditDialogVisible.value],
      completedDialogs: [true, false],
      snapshot: module => ({ key: module.managedKnowledgeForm.knowledge_key, content: module.managedKnowledgeForm.content, dialog: module.managedKnowledgeEditDialogVisible.value, busy: module.managedKnowledgeSubmitting.value }),
      assertRequest: request => {
        assert.equal(request.id, managedKbB.id)
        assert.equal(normalize(request.payload).knowledge_key, 'fresh-key')
        assert.equal(normalize(request.payload).content, 'fresh managed content')
      },
      result: { status: 'created' },
    },
    {
      api: 'updateManagedItem', method: 'updateManagedItem', ref: 'managedKnowledgeFormRef', oldTarget: managedKbA, freshTarget: managedKbB,
      oldItem: managedItemA, freshItem: managedItemB,
      open: async (module, kb, item) => {
        await module.showManagedKnowledgeDialog(kb)
        await module.showManagedKnowledgeEditDialog(item)
      },
      fill: (module, value) => {
        module.managedKnowledgeForm.knowledge_key = `${value}-key`
        module.managedKnowledgeForm.content = `${value} managed content`
      },
      busy: module => module.managedKnowledgeSubmitting.value,
      dialogs: module => [module.managedKnowledgeDialogVisible.value, module.managedKnowledgeEditDialogVisible.value],
      completedDialogs: [true, false],
      snapshot: module => ({ id: module.managedKnowledgeEditingId.value, key: module.managedKnowledgeForm.knowledge_key, content: module.managedKnowledgeForm.content, dialog: module.managedKnowledgeEditDialogVisible.value, busy: module.managedKnowledgeSubmitting.value }),
      assertRequest: request => {
        assert.equal(request.id, managedKbB.id)
        assert.equal(request.itemId, managedItemB.id)
        assert.equal(normalize(request.payload).knowledge_key, 'fresh-key')
        assert.equal(normalize(request.payload).content, 'fresh managed content')
      },
      result: { status: 'updated' },
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

    await scenario.open(harness.module, scenario.oldTarget, scenario.oldItem)
    scenario.fill(harness.module, 'old')
    const oldSubmit = scenario.api === 'importDocument'
      ? harness.module.submitImport()
      : scenario.api === 'createManagedItem' || scenario.api === 'updateManagedItem'
        ? harness.module.submitManagedKnowledge()
        : harness.module.submitForm()
    await flushPromises()
    assert.equal(validationCalls, 1, scenario.api)
    assert.equal(scenario.busy(harness.module), true, scenario.api)
    assert.equal(harness.api[scenario.api].length, 0, scenario.api)

    harness.permissionsStore.currentUser = { uid: 'user-b', username: 'Bob', is_superuser: false }
    await flushPromises()
    formRef.validate = () => Promise.resolve(true)
    await scenario.open(harness.module, scenario.freshTarget, scenario.freshItem)
    scenario.fill(harness.module, 'fresh')
    const freshSubmit = scenario.api === 'importDocument'
      ? harness.module.submitImport()
      : scenario.api === 'createManagedItem' || scenario.api === 'updateManagedItem'
        ? harness.module.submitManagedKnowledge()
        : harness.module.submitForm()
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
  const managedKbA = { id: 'kb-managed-a-mutation', uid: 'user-a', name: 'Alice managed KB', knowledge_base_type: 'llm_managed' }
  const managedKbB = { id: 'kb-managed-b-mutation', uid: 'user-b', name: 'Bob managed KB', knowledge_base_type: 'llm_managed' }
  const documentA = { id: 'doc-a-mutation', filename: 'alice.txt' }
  const documentB = { id: 'doc-b-mutation', filename: 'bob.txt' }
  const managedItemA = { id: 'item-a-mutation', knowledge_key: 'alice-key', content: 'Alice managed content', version: 2, llm_maintainable: true }
  const managedItemB = { id: 'item-b-mutation', knowledge_key: 'bob-key', content: 'Bob managed content', version: 5, llm_maintainable: true }
  const retryItemA = { ...managedItemA, publication_job_status: 'failed', publication_job_id: 'publication-a' }
  const retryItemB = { ...managedItemB, publication_job_status: 'failed', publication_job_id: 'publication-b' }
  const jobA = { job_id: 'organization-a', knowledge_base_id: managedKbA.id, status: 'running', child_job_count: 1, child_terminal_count: 0 }
  const jobB = { job_id: 'organization-b', knowledge_base_id: managedKbB.id, status: 'running', child_job_count: 1, child_terminal_count: 0 }
  const retryJobA = { ...jobA, job_id: 'organization-retry-a', status: 'failed', child_terminal_count: 1, can_retry: true }
  const retryJobB = { ...jobB, job_id: 'organization-retry-b', status: 'failed', child_terminal_count: 1, can_retry: true }
  const targets = {
    create: [{ label: 'old Alice' }, { label: 'fresh Bob' }],
    update: [{ kb: userKbA, label: 'old Alice' }, { kb: userKbB, label: 'fresh Bob' }],
    delete: [{ kb: userKbA }, { kb: userKbB }],
    import: [{ kb: userKbA, document: documentA, label: 'old Alice' }, { kb: userKbB, document: documentB, label: 'fresh Bob' }],
    deleteDocument: [{ kb: userKbA, document: documentA }, { kb: userKbB, document: documentB }],
    createManaged: [{ kb: managedKbA, label: 'old Alice' }, { kb: managedKbB, label: 'fresh Bob' }],
    updateManaged: [{ kb: managedKbA, item: managedItemA, label: 'old Alice' }, { kb: managedKbB, item: managedItemB, label: 'fresh Bob' }],
    deleteManaged: [{ kb: managedKbA, item: managedItemA }, { kb: managedKbB, item: managedItemB }],
    retryManaged: [{ kb: managedKbA, item: retryItemA }, { kb: managedKbB, item: retryItemB }],
    organize: [{ kb: managedKbA }, { kb: managedKbB }],
    cancelOrganization: [{ kb: managedKbA, job: jobA, settledJob: { ...jobA, status: 'cancelled', child_terminal_count: 1 } }, { kb: managedKbB, job: jobB, settledJob: { ...jobB, status: 'cancelled', child_terminal_count: 1 } }],
    retryOrganization: [{ kb: managedKbA, job: retryJobA, settledJob: { ...retryJobA, status: 'succeeded', can_retry: false } }, { kb: managedKbB, job: retryJobB, settledJob: { ...retryJobB, status: 'succeeded', can_retry: false } }],
  }
  const managedDialog = async (module, target, itemDialog) => {
    await module.showManagedKnowledgeDialog(target.kb)
    if (itemDialog === 'create') module.showManagedKnowledgeCreateDialog()
    if (itemDialog === 'edit') await module.showManagedKnowledgeEditDialog(target.item)
  }
  const rootForm = (module, target) => {
    module.form.name = `${target.label} knowledge`
    module.form.description = `${target.label} description`
    module.form.embedding_model_key = '1::embedding-model'
  }
  const managedForm = (module, target) => {
    module.managedKnowledgeForm.knowledge_key = `${target.label}-key`
    module.managedKnowledgeForm.content = `${target.label} managed content`
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
    {
      key: 'import', array: 'importDocument', method: 'importDocument', ref: 'importFormRef', busy: module => module.importing.value,
      open: (module, target) => module.showImportDialog(target.kb),
      fill: (module, target) => {
        const uploadFile = { name: `${target.label}.txt`, raw: new Blob([`${target.label} document contents`], { type: 'text/plain' }) }
        module.handleFileChange(uploadFile, [uploadFile])
      },
      submit: module => module.submitImport(),
      assertRequest: async (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(await request.payload.get('file').text(), `${target.label} document contents`)
      }, result: { status: 'accepted' },
    },
    {
      key: 'deleteDocument', array: 'deleteDocument', method: 'deleteDocument',
      open: (module, target) => module.showDocumentDialog(target.kb),
      submit: (module, target) => module.handleDeleteDocument(target.document), refresh: 'documents',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(request.docId, target.document.id)
      }, result: { status: 'accepted' },
    },
    {
      key: 'createManaged', array: 'createManagedItem', method: 'createManagedItem', ref: 'managedKnowledgeFormRef', busy: module => module.managedKnowledgeSubmitting.value,
      open: (module, target) => managedDialog(module, target, 'create'), fill: managedForm,
      submit: module => module.submitManagedKnowledge(), refresh: 'managedItems',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(normalize(request.payload).knowledge_key, `${target.label}-key`)
        assert.equal(normalize(request.payload).content, `${target.label} managed content`)
      }, result: { status: 'created' },
    },
    {
      key: 'updateManaged', array: 'updateManagedItem', method: 'updateManagedItem', ref: 'managedKnowledgeFormRef', busy: module => module.managedKnowledgeSubmitting.value,
      open: (module, target) => managedDialog(module, target, 'edit'), fill: managedForm,
      submit: module => module.submitManagedKnowledge(), refresh: 'managedItems',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(request.itemId, target.item.id)
        assert.equal(normalize(request.payload).knowledge_key, `${target.label}-key`)
        assert.equal(normalize(request.payload).content, `${target.label} managed content`)
      }, result: { status: 'updated' },
    },
    {
      key: 'deleteManaged', array: 'deleteManagedItem', method: 'deleteManagedItem',
      open: (module, target) => managedDialog(module, target),
      submit: (module, target) => module.handleDeleteManagedKnowledge(target.item), refresh: 'managedItems',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(request.itemId, target.item.id)
      }, result: { status: 'deleted' },
    },
    {
      key: 'retryManaged', array: 'retryManagedItem', method: 'retryManagedItem',
      open: (module, target) => managedDialog(module, target),
      submit: (module, target) => module.retryManagedKnowledgePublication(target.item), refresh: 'managedItems',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(request.itemId, target.item.id)
        assert.equal(normalize(request.payload).failed_job_id, target.item.publication_job_id)
      }, result: { status: 'retry_submitted' },
    },
    {
      key: 'organize', array: 'organize', method: 'organize', busy: module => module.organizationSubmitting.value,
      open: (module, target) => {
        module.selectedKb.value = target.kb
        return module.showOrganizationDialog()
      },
      submit: module => module.submitKnowledgeOrganization(), refresh: 'organizationJobs',
      assertRequest: (request, target) => assert.equal(request.id, target.kb.id), result: { status: 'accepted' },
    },
    {
      key: 'cancelOrganization', array: 'cancelOrganizationJob', method: 'cancelOrganizationJob',
      busy: (module, target) => module.organizationActionJobId.value === target.job.job_id,
      open: (module, target) => {
        module.selectedKb.value = target.kb
        return module.showOrganizationDialog()
      },
      submit: (module, target) => module.handleCancelOrganizationJob(target.job), refresh: 'organizationJobs',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(request.jobId, target.job.job_id)
      }, result: {},
    },
    {
      key: 'retryOrganization', array: 'retryOrganizationJob', method: 'retryOrganizationJob',
      busy: (module, target) => module.organizationActionJobId.value === target.job.job_id,
      open: (module, target) => {
        module.selectedKb.value = target.kb
        return module.showOrganizationDialog()
      },
      submit: (module, target) => module.handleRetryOrganizationJob(target.job), refresh: 'organizationJobs',
      assertRequest: (request, target) => {
        assert.equal(request.id, target.kb.id)
        assert.equal(request.jobId, target.job.job_id)
      }, result: {},
    },
  ]
  const snapshot = harness => {
    const module = harness.module
    return normalize({
      selectedKb: module.selectedKb.value,
      dialogs: [module.dialogVisible.value, module.importDialogVisible.value, module.documentDialogVisible.value, module.contentDialogVisible.value, module.managedKnowledgeDialogVisible.value, module.managedKnowledgeEditDialogVisible.value, module.managedKnowledgeHistoryDialogVisible.value, module.organizationDialogVisible.value],
      root: { loading: module.loading.value, table: module.tableData.value, total: module.total.value, submitting: module.submitting.value, editing: module.editingId.value, form: { ...module.form } },
      documents: { importing: module.importing.value, file: module.uploadFileList.value.map(item => item.name), fileSize: module.importForm.file?.size ?? 0, list: module.documentList.value, total: module.documentTotal.value },
      managed: { loading: module.managedKnowledgeLoading.value, submitting: module.managedKnowledgeSubmitting.value, items: module.managedKnowledgeItems.value, total: module.managedKnowledgeTotal.value, editing: module.managedKnowledgeEditingId.value, form: { ...module.managedKnowledgeForm } },
      organization: { loading: module.organizationLoading.value, submitting: module.organizationSubmitting.value, actionJob: module.organizationActionJobId.value, jobs: module.organizationJobs.value, total: module.organizationTotal.value },
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
      const targetForKb = id => [oldTarget, freshTarget].find(target => target.kb?.id === id)
      const organizationCalls = new Map()
      const harness = await createHarness(t, {
        currentUid: 'user-a',
        currentUsername: 'Alice',
        superuser: false,
        [scenario.method]: () => {
          mutationCalls += 1
          return mutationCalls === 1 ? oldMutation.promise : freshMutation.promise
        },
        managedItems: id => {
          const items = targetForKb(id)?.item ? [targetForKb(id).item] : []
          return Promise.resolve(dataResponse({ items, total: items.length }))
        },
        managedItem: (id, itemId) => Promise.resolve(dataResponse(
          targetForKb(id)?.item?.id === itemId ? targetForKb(id).item : { id: itemId, knowledge_key: 'key', content: '', version: 1 },
        )),
        organizationJobs: id => {
          const call = (organizationCalls.get(id) || 0) + 1
          organizationCalls.set(id, call)
          const target = targetForKb(id)
          const jobs = call === 1 ? (target?.job ? [target.job] : []) : (target?.settledJob ? [target.settledJob] : [])
          return Promise.resolve(dataResponse({ items: jobs, total: jobs.length }))
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
