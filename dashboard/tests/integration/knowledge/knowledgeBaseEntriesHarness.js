import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope, ref, watch } from 'vue'

import * as knowledgeBaseManagement from '../../../src/utils/knowledgeBaseManagement.js'
import { createKnowledgeBaseScope } from '../../../src/utils/knowledgeBaseScope.js'
import * as requestTaskManager from '../../../src/utils/requestTaskManager.js'
import { useUserPermissionsStore } from '../../../src/composables/useUserPermissions.js'
import { loadUseConfirmDialog } from '../../confirmDialogHarness.js'

const source = readFileSync(
  new URL('../../../src/composables/knowledgeBase/useKnowledgeBaseEntries.js', import.meta.url),
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
const escapeHtml = value => String(value)
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#39;')
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
        : options.confirm ?? true
      if (result === false || result === 'cancel') throw 'cancel'
      if (result === 'close') throw 'close'
      return 'confirm'
    },
    close: () => confirmationCloses.push(true),
  }

  const translate = options.translate ?? (key => key)

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
    useConfirmDialog: loadUseConfirmDialog({ t: translate, ElMessageBox }),
    useI18n: () => ({ t: translate }),
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

export {
  assertPrivateStateCleared,
  createHarness,
  dataResponse,
  deferred,
  escapeHtml,
  flushPromises,
  normalize,
}
