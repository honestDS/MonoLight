import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import {
  computed,
  effectScope,
  nextTick,
  reactive,
  ref,
} from 'vue'
import * as memoryManagement from '../../../src/utils/memoryManagement.js'
import { createAbortableTaskManager } from '../../../src/utils/channelTestManager.js'
import enCommon from '../../../src/i18n/locales/en/common.js'
import enMemories from '../../../src/i18n/locales/en/memories.js'
import {
  MEMORY_JOB_OPERATIONS,
  MEMORY_JOB_STATUSES,
  MEMORY_TYPES,
} from '../../../src/constants/index.js'
import { loadUseConfirmDialog } from '../../confirmDialogHarness.js'

const memoryComposableSources = [
  readFileSync(new URL('../../../src/composables/memories/useMemoryRuntime.js', import.meta.url), 'utf8'),
  readFileSync(new URL('../../../src/composables/memories/useMemoryRecords.js', import.meta.url), 'utf8'),
  readFileSync(new URL('../../../src/composables/memories/useMemoriesView.js', import.meta.url), 'utf8'),
]

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

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const normalize = value => JSON.parse(JSON.stringify(value))

const dataResponse = data => ({ data: { data } })
const pageResponse = (items = [], total = items.length, meta) => dataResponse({
  items,
  total,
  ...(meta === undefined ? {} : { meta }),
})

const translate = (key, params = {}) => {
  if (key === 'memories.owner_unknown') return 'owner_unknown'
  if (key === 'memories.runtime_operation_submitted') {
    return `runtime_operation_submitted:${params.owner ?? ''}:${params.operation ?? ''}`
  }
  if (key === 'memories.blocked_with_reason') {
    return `memories.blocked_with_reason:${params.reason ?? ''}:${params.job ?? ''}`
  }
  return String(key).replace(/\{(\w+)\}/g, (_match, name) => String(params[name] ?? ''))
}

const confirmationTranslate = (key, params = {}) => {
  const [namespace, ...path] = key.split('.')
  const messages = namespace === 'common' ? enCommon : namespace === 'memories' ? enMemories : null
  const message = messages?.[path.join('.')]
  return typeof message === 'string'
    ? message.replace(/\{(\w+)\}/g, (_match, name) => String(params[name] ?? ''))
    : translate(key, params)
}

const flush = async () => {
  for (let index = 0; index < 8; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const createHarness = (t, options = {}) => {
  const messages = []
  const mountedCallbacks = []
  const beforeUnmountCallbacks = []
  const timers = new Map()
  const requestsByName = Object.create(null)
  const defaultMeta = {
    is_superuser: Boolean(options.superuser),
    current_uid: options.currentUid ?? (options.superuser ? 'admin-user' : 'user-a'),
    current_username: options.currentUsername ?? (options.superuser ? 'Administrator' : 'Alice'),
  }
  const apiState = {
    requests: [],
    requestsByName,
  }
  const overrides = options.apiOverrides ?? {}

  const defaultResponse = name => {
    if (name === 'memoryApi.list') return pageResponse([], 0, { ...defaultMeta })
    if (name === 'memoryApi.jobs' || name === 'memoryApi.migrations') return pageResponse([])
    if (name === 'memoryApi.history') return pageResponse([])
    if (name === 'memoryApi.settings') return dataResponse({ configured: true })
    if (name === 'profileApi.modelOptions') return pageResponse([])
    if (name === 'adminApi.userList' || name === 'channelApi.list') return pageResponse([])
    return dataResponse({})
  }

  const request = (name, args) => {
    const record = {
      name,
      args,
      deferred: createDeferred(),
    }
    apiState.requests.push(record)
    if (!requestsByName[name]) requestsByName[name] = []
    requestsByName[name].push(record)

    const [groupName, methodName] = name.split('.')
    const configured = overrides[groupName]?.[methodName]
    const response = typeof configured === 'function'
      ? configured(...args, record)
      : configured === undefined ? defaultResponse(name) : configured
    return Promise.resolve(response)
  }

  const createApi = (groupName, methodNames) => Object.fromEntries(
    methodNames.map(methodName => [methodName, (...args) => request(`${groupName}.${methodName}`, args)]),
  )

  const adminApi = createApi('adminApi', ['userList'])
  const channelApi = createApi('channelApi', ['list'])
  const profileApi = createApi('profileApi', ['modelOptions'])
  const memoryApi = createApi('memoryApi', [
    'list',
    'get',
    'create',
    'update',
    'delete',
    'jobs',
    'job',
    'retryJob',
    'cancelJob',
    'history',
    'resumeCurrent',
    'settings',
    'reindex',
    'migrations',
    'migration',
    'retryMigration',
    'cancelMigration',
    'retryCleanup',
    'updateSettings',
    'organize',
    'pin',
    'unpin',
  ])

  const ElMessage = value => messages.push({ type: 'message', value })
  ElMessage.info = value => messages.push({ type: 'info', value })
  ElMessage.warning = value => messages.push({ type: 'warning', value })
  ElMessage.error = value => messages.push({ type: 'error', value })
  ElMessage.success = value => messages.push({ type: 'success', value })
  const confirmCalls = []
  const ElMessageBox = {
    confirm: (...args) => {
      confirmCalls.push(args)
      const configured = typeof options.confirm === 'function'
        ? options.confirm(...args)
        : options.confirm ?? true
      if (configured === false) return Promise.reject('cancel')
      if (configured === 'cancel' || configured === 'close') return Promise.reject(configured)
      if (configured === undefined || configured === true) return Promise.resolve()
      return configured
    },
  }
  const actualUseConfirmDialog = loadUseConfirmDialog({
    t: options.translate ?? translate,
    ElMessageBox,
  })

  let timerId = 0
  const windowStub = {
    setTimeout(callback, delay) {
      const id = ++timerId
      timers.set(id, { callback, delay })
      return id
    },
    clearTimeout(id) {
      timers.delete(id)
    },
  }

  const context = vm.createContext({
    computed,
    reactive,
    ref,
    onMounted: callback => mountedCallbacks.push(callback),
    onBeforeUnmount: callback => beforeUnmountCallbacks.push(callback),
    useI18n: () => ({ t: options.translate ?? translate }),
    useConfirmDialog: actualUseConfirmDialog,
    ElMessage,
    ElMessageBox,
    adminApi,
    channelApi,
    profileApi,
    memoryApi,
    ...memoryManagement,
    ...{
      MEMORY_JOB_OPERATIONS,
      MEMORY_JOB_STATUSES,
      MEMORY_TYPES,
    },
    createAbortableTaskManager,
    AbortController,
    window: windowStub,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  const scope = effectScope()
  let module
  scope.run(() => {
    vm.runInContext(
      `${memoryComposableSources
        .map(source => stripImports(source).replace(/^(\s*)export\s+/gm, '$1'))
        .join('\n')}
  globalThis.__module = useMemoriesView()
`,
      context,
      { filename: 'MemoriesView.vue?script-setup' },
    )
    module = context.__module
  })

  const harness = {
    apiState,
    module,
    messages,
    confirmCalls,
    timers,
    calls: name => requestsByName[name] ?? [],
    async mount() {
      if (this.mounted) return
      this.mounted = true
      await Promise.all(mountedCallbacks.splice(0).map(callback => callback()))
      await flush()
    },
    async flush() {
      await flush()
    },
    mounted: false,
    unmounted: false,
    unmount() {
      if (this.unmounted) return
      this.unmounted = true
      for (const callback of beforeUnmountCallbacks.splice(0)) callback()
      scope.stop()
      timers.clear()
    },
  }

  t.after(() => harness.unmount())
  return harness
}

const detailCases = [
  {
    name: 'memory',
    method: 'get',
    requestName: 'memoryApi.get',
    show: 'showDetails',
    selected: 'selectedMemory',
    visible: 'detailsVisible',
  },
  {
    name: 'job',
    method: 'job',
    requestName: 'memoryApi.job',
    show: 'showJob',
    selected: 'selectedJob',
    visible: 'jobVisible',
  },
  {
    name: 'migration',
    method: 'migration',
    requestName: 'memoryApi.migration',
    show: 'showMigration',
    selected: 'selectedMigration',
    visible: 'migrationVisible',
  },
]

const detailRow = (detailCase, owner, marker) => ({
  id: `${detailCase.name}-${marker}`,
  owner_uid: owner,
})

const detailResponse = (row, marker) => dataResponse({
  id: row.id,
  owner_uid: row.owner_uid,
  marker,
})

const createDetailHarness = (t, detailCase, superuser = false) => createHarness(t, {
  superuser,
  ...(superuser ? {} : { currentUid: 'owner-a' }),
  apiOverrides: {
    memoryApi: {
      [detailCase.method]: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

const fillEditor = (module, values = {}) => Object.assign(module.form, {
  memory_key: 'editor-key',
  memory_type: 'fact',
  content: 'editor-content',
  change_evidence: 'editor-evidence',
  ...values,
})

const lastRequest = requests => requests[requests.length - 1]

export {
  confirmationTranslate,
  createDeferred,
  createDetailHarness,
  createHarness,
  dataResponse,
  detailCases,
  detailResponse,
  detailRow,
  fillEditor,
  lastRequest,
  normalize,
  pageResponse,
}
