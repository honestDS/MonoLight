import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope, reactive, ref, watch } from 'vue'
import { createI18n } from 'vue-i18n'

import * as knowledgeBaseManagement from '../../../src/utils/knowledgeBaseManagement.js'
import * as knowledgeBaseMigration from '../../../src/utils/knowledgeBaseMigration.js'
import { createKnowledgeBaseScope } from '../../../src/utils/knowledgeBaseScope.js'
import { useUserPermissionsStore } from '../../../src/composables/useUserPermissions.js'
import zhCommon from '../../../src/i18n/locales/zh/common.js'
import zhKnowledgeBase from '../../../src/i18n/locales/zh/knowledgeBase.js'
import enCommon from '../../../src/i18n/locales/en/common.js'
import enKnowledgeBase from '../../../src/i18n/locales/en/knowledgeBase.js'
import { loadUseConfirmDialog } from '../../confirmDialogHarness.js'

const useKnowledgeBaseViewSource = readFileSync(
  new URL('../../../src/composables/knowledgeBase/useKnowledgeBaseView.js', import.meta.url),
  'utf8',
)
const useKnowledgeBaseQueryTestSource = readFileSync(
  new URL('../../../src/composables/knowledgeBase/useKnowledgeBaseQueryTest.js', import.meta.url),
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
const useKnowledgeBaseQueryTestScript = stripModuleSyntax(useKnowledgeBaseQueryTestSource)
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
    queryTest: [],
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
    queryTest: createApiMethod('queryTest', (id, params) => ({ id, params: { ...params } }), {
      items: [],
      retrieval_mode: null,
      rerank_error: null,
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
    confirm: (...args) => {
      confirms.push(args)
      try {
        const result = typeof options.confirm === 'function'
          ? options.confirm(...args)
          : options.confirm ?? true
        return Promise.resolve(result).then(value => {
          if (!value) throw 'cancel'
          return 'confirm'
        })
      } catch (error) {
        return Promise.reject(error)
      }
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
  const translate = (key, params) => i18n.global.t(
    key,
    params && typeof params === 'object' ? JSON.parse(JSON.stringify(params)) : params,
  )
  const actualUseConfirmDialog = loadUseConfirmDialog({ t: translate, ElMessageBox })
  const scope = effectScope()
  const context = vm.createContext({
    computed,
    ElMessage,
    ElMessageBox,
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
    watch,
    useConfirmDialog: actualUseConfirmDialog,
    useI18n: () => ({ t: translate }),
    useUserPermissionsStore,
    i18n,
    ...knowledgeBaseManagement,
    ...knowledgeBaseMigration,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  scope.run(() => {
    vm.runInContext(
      `${useKnowledgeBaseQueryTestScript}\n` +
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

export { createHarness, dataResponse, normalize, escapeHtml, deferred, flushPromises }
