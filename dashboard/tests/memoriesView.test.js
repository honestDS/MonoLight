import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import {
  computed,
  effectScope,
  nextTick,
  reactive,
  ref,
  watch,
} from 'vue'
import * as memoryManagement from '../src/utils/memoryManagement.js'
import { createAbortableTaskManager } from '../src/utils/channelTestManager.js'
import {
  MEMORY_JOB_OPERATIONS,
  MEMORY_JOB_STATUSES,
  MEMORY_TYPES,
} from '../src/constants/index.js'

const memoriesViewSource = readFileSync(new URL('../src/views/MemoriesView.vue', import.meta.url), 'utf8')
const memoriesViewScriptMatch = memoriesViewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!memoriesViewScriptMatch) throw new Error('MemoriesView.vue script setup was not found')

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
  return String(key).replace(/\{(\w+)\}/g, (_match, name) => String(params[name] ?? ''))
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
      if (typeof options.confirm === 'function') return options.confirm(...args)
      return options.confirm ?? true
    },
  }

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
    useI18n: () => ({ t: translate }),
    ElMessage,
    ElMessageBox,
    adminApi,
    channelApi,
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
      `${stripImports(memoriesViewScriptMatch[1])}
globalThis.__module = {
  activeTab,
  settings,
  isSuperuser,
  currentUid,
  currentUsername,
  owners,
  ownersLoading,
  ownersLoaded,
  memoryScopeReady,
  ownerFilter,
  settingsLoading,
  actionLoading,
  memories,
  memoriesLoading,
  memoryPage,
  memoryPageSize,
  memoryTotal,
  jobs,
  jobsLoading,
  jobPage,
  jobPageSize,
  jobTotal,
  migrations,
  migrationsLoading,
  migrationPage,
  migrationPageSize,
  migrationTotal,
  detailsVisible,
  selectedMemory,
  selectedHistoryMemory,
  historyVisible,
  historyLoading,
  history,
  jobVisible,
  selectedJob,
  migrationVisible,
  selectedMigration,
  currentMemoryTask,
  runtimeOwnerUid,
  organizeBlocked,
  reindexBlocked,
  loadSettings,
  loadOwners,
  loadMemories,
  loadJobs,
  loadMigrations,
  handleOwnerChange,
  ownerLabel,
  organize,
  reindex,
  showDetails,
  showJob,
  showMigration,
  showHistory,
  handleMemoryMoreAction,
  form,
  editorMode,
  editorVisible,
  submitting,
  contentTooLong,
  openEditor,
  editSelectedMemory,
  submitMemory
}
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

test('derives ordinary-user scope from list metadata without cross-user uid requests', async t => {
  const harness = createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
  })
  await harness.mount()

  assert.equal(harness.module.isSuperuser.value, false)
  assert.equal(harness.module.currentUid.value, 'user-a')
  assert.equal(harness.module.currentUsername.value, 'Alice')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  assert.equal(harness.module.ownerLabel('user-a'), 'Alice')

  harness.module.ownerFilter.value = 'user-b'
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  await harness.module.loadJobs()
  await harness.module.loadMigrations()

  assert.equal(harness.calls('adminApi.userList').length, 0)
  for (const name of ['memoryApi.list', 'memoryApi.jobs', 'memoryApi.migrations']) {
    for (const request of harness.calls(name)) assert.equal(request.args[0].uid, undefined)
  }
  assert.deepEqual(normalize(harness.calls('memoryApi.settings')[0].args[0].params), { uid: 'user-a' })
})

test('loads all superuser owner labels across paginated user-list responses', async t => {
  const firstPage = Array.from({ length: 100 }, (_value, index) => ({
    uid: `user-${index}`,
    username: `User ${index}`,
  }))
  const secondPage = [{ uid: 'user-100', username: 'User 100' }]
  const harness = createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    apiOverrides: {
      adminApi: {
        userList: params => pageResponse(params.page === 1 ? firstPage : secondPage, 101),
      },
    },
  })
  await harness.mount()
  await harness.flush()

  assert.deepEqual(normalize(harness.calls('adminApi.userList').map(request => request.args[0])), [
    { page: 1, size: 100 },
    { page: 2, size: 100 },
  ])
  assert.equal(harness.module.owners.value.length, 101)
  assert.equal(harness.module.ownerLabel('user-100'), 'User 100')
  assert.equal(harness.module.ownerLabel('unknown-owner'), 'unknown-owner')
  assert.equal(harness.module.ownerLabel(''), 'owner_unknown')
})

test('uses the selected superuser owner for list, jobs, migrations, and settings', async t => {
  const harness = createHarness(t, {
    superuser: true,
    apiOverrides: {
      adminApi: {
        userList: () => pageResponse([
          { uid: 'user-a', username: 'Alice' },
          { uid: 'user-b', username: 'Bob' },
        ], 2),
      },
    },
  })
  await harness.mount()
  await harness.flush()
  const requestCountBeforeSelection = harness.apiState.requests.length

  harness.module.ownerFilter.value = 'user-b'
  harness.module.handleOwnerChange()
  await harness.flush()
  await harness.module.loadJobs()
  await harness.module.loadMigrations()
  await harness.module.loadSettings()

  const selectedRequests = harness.apiState.requests.slice(requestCountBeforeSelection)
  assert.ok(selectedRequests.some(request => request.name === 'memoryApi.list'))
  assert.ok(selectedRequests.some(request => request.name === 'memoryApi.jobs'))
  assert.ok(selectedRequests.some(request => request.name === 'memoryApi.migrations'))
  assert.ok(selectedRequests.some(request => request.name === 'memoryApi.settings'))
  for (const name of ['memoryApi.list', 'memoryApi.jobs', 'memoryApi.migrations']) {
    const requests = harness.calls(name).filter(request => selectedRequests.includes(request))
    assert.ok(requests.length > 0)
    for (const request of requests) assert.equal(request.args[0].uid, 'user-b')
  }
  const settingsRequests = harness.calls('memoryApi.settings').filter(request => selectedRequests.includes(request))
  assert.ok(settingsRequests.length > 0)
  for (const request of settingsRequests) assert.equal(request.args[0].params.uid, 'user-b')
})

test('blocks settings and mutating actions while a superuser views all users', async t => {
  const harness = createHarness(t, { superuser: true })
  await harness.mount()
  await harness.flush()

  assert.equal(harness.module.runtimeOwnerUid.value, null)
  assert.equal(harness.module.organizeBlocked.value, true)
  assert.equal(harness.module.reindexBlocked.value, true)

  await harness.module.loadSettings()
  await harness.module.organize()
  await harness.module.reindex()

  assert.equal(harness.calls('memoryApi.settings').length, 0)
  assert.equal(harness.calls('memoryApi.organize').length, 0)
  assert.equal(harness.calls('memoryApi.reindex').length, 0)
})

test('clears scoped state synchronously and refreshes the active tab on owner switch', async t => {
  const harness = createHarness(t, { superuser: true })
  await harness.mount()
  await harness.flush()

  harness.module.activeTab.value = 'jobs'
  harness.module.ownerFilter.value = 'user-b'
  Object.assign(harness.module.settings, {
    configured: true,
    current_job: { id: 7, operation: 'organize', status: 'running' },
  })
  harness.module.memories.value = [{ id: 1 }]
  harness.module.memoryTotal.value = 1
  harness.module.memoryPage.value = 4
  harness.module.jobs.value = [{ id: 2 }]
  harness.module.jobTotal.value = 2
  harness.module.jobPage.value = 5
  harness.module.migrations.value = [{ id: 3 }]
  harness.module.migrationTotal.value = 3
  harness.module.migrationPage.value = 6
  harness.module.selectedMemory.value = { id: 1 }
  harness.module.selectedHistoryMemory.value = { id: 4 }
  harness.module.detailsVisible.value = true
  harness.module.history.value = [{ version: 1 }]
  harness.module.historyVisible.value = true
  harness.module.selectedJob.value = { id: 2 }
  harness.module.jobVisible.value = true
  harness.module.selectedMigration.value = { id: 3 }
  harness.module.migrationVisible.value = true
  assert.equal(harness.module.currentMemoryTask.value.status, 'running')

  const requestCountBeforeSwitch = harness.apiState.requests.length
  harness.module.handleOwnerChange()

  assert.deepEqual(normalize(harness.module.settings), {})
  assert.deepEqual(normalize(harness.module.memories.value), [])
  assert.equal(harness.module.memoryTotal.value, 0)
  assert.equal(harness.module.memoryPage.value, 1)
  assert.deepEqual(normalize(harness.module.jobs.value), [])
  assert.equal(harness.module.jobTotal.value, 0)
  assert.equal(harness.module.jobPage.value, 1)
  assert.deepEqual(normalize(harness.module.migrations.value), [])
  assert.equal(harness.module.migrationTotal.value, 0)
  assert.equal(harness.module.migrationPage.value, 1)
  assert.equal(harness.module.selectedMemory.value, null)
  assert.equal(harness.module.selectedHistoryMemory.value, null)
  assert.equal(harness.module.detailsVisible.value, false)
  assert.deepEqual(normalize(harness.module.history.value), [])
  assert.equal(harness.module.historyVisible.value, false)
  assert.equal(harness.module.selectedJob.value, null)
  assert.equal(harness.module.jobVisible.value, false)
  assert.equal(harness.module.selectedMigration.value, null)
  assert.equal(harness.module.migrationVisible.value, false)
  assert.equal(harness.module.currentMemoryTask.value, null)

  const refreshedRequests = harness.apiState.requests.slice(requestCountBeforeSwitch)
  assert.ok(refreshedRequests.some(request => request.name === 'memoryApi.settings'))
  assert.ok(refreshedRequests.some(request => request.name === 'memoryApi.list'))
  assert.ok(refreshedRequests.some(request => request.name === 'memoryApi.jobs'))
  assert.equal(refreshedRequests.some(request => request.name === 'memoryApi.migrations'), false)
  for (const request of refreshedRequests) {
    if (request.name === 'memoryApi.settings') assert.equal(request.args[0].params.uid, 'user-b')
    if (['memoryApi.list', 'memoryApi.jobs'].includes(request.name)) assert.equal(request.args[0].uid, 'user-b')
  }
})

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
  apiOverrides: {
    memoryApi: {
      [detailCase.method]: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

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
    owner_uid: 'owner-a',
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
      owner_uid: 'owner-a',
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
      const harness = createDetailHarness(t, detailCase)
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

const createEditorHarness = (t, superuser = false) => createHarness(t, {
  superuser,
  apiOverrides: {
    memoryApi: {
      create: (...args) => args[args.length - 1].deferred.promise,
      update: (...args) => args[args.length - 1].deferred.promise,
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
  assert.ok(harness.calls('memoryApi.settings').length > settingsCountBeforeSuccess)
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

test('waits for delete confirmation and sends the positive id, version, and dedupe key', async t => {
  const confirmation = createDeferred()
  const harness = createMenuHarness(t, { confirm: () => confirmation.promise })
  await harness.mount()
  const row = { id: 302, version: 8, owner_uid: 'owner-a' }

  harness.module.handleMemoryMoreAction('delete', row)
  assert.equal(harness.confirmCalls.length, 1)
  assert.deepEqual(normalize(harness.confirmCalls[0][2]), {
    type: 'warning',
    confirmButtonText: 'common.confirm',
    cancelButtonText: 'common.cancel',
  })
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

test('ignores unknown memory menu commands', async t => {
  const harness = createMenuHarness(t)
  await harness.mount()
  harness.module.handleMemoryMoreAction('unknown-command', { id: 307, version: 4, owner_uid: 'owner-a' })
  await harness.flush()

  assert.equal(harness.confirmCalls.length, 0)
  assert.equal(harness.calls('memoryApi.history').length, 0)
  assert.equal(harness.calls('memoryApi.delete').length, 0)
  assert.equal(harness.messages.length, 0)
})

const scopedLoaderMeta = {
  is_superuser: true,
  current_uid: 'admin-user',
  current_username: 'Administrator',
}

const scopedOwner = marker => String(marker).startsWith('A') ? 'owner-a' : 'owner-b'
const scopedId = (marker, ownerAId, ownerBId) => String(marker).startsWith('A') ? ownerAId : ownerBId

const scopedLoaderCases = [
  {
    name: 'settings',
    method: 'settings',
    requestName: 'memoryApi.settings',
    loader: 'loadSettings',
    activeTab: 'memories',
    params: request => request.args[0].params,
    signal: request => request.args[0].signal,
    response: marker => dataResponse({
      configured: true,
      marker,
      active_collection_name: `collection-${marker}`,
    }),
    assertState: (harness, marker) => {
      assert.equal(harness.module.settings.marker, marker)
      assert.equal(harness.module.settings.active_collection_name, `collection-${marker}`)
    },
    prepareState: harness => Object.assign(harness.module.settings, {
      marker: 'before',
      active_collection_name: 'collection-before',
      current_job: { id: 77, operation: 'organize', status: 'running' },
    }),
    snapshot: harness => ({
      settings: normalize(harness.module.settings),
      loading: harness.module.settingsLoading.value,
      currentTask: normalize(harness.module.currentMemoryTask.value),
    }),
    assertSnapshot: (harness, snapshot) => {
      assert.deepEqual(normalize(harness.module.settings), snapshot.settings)
      assert.equal(harness.module.settingsLoading.value, snapshot.loading)
      assert.deepEqual(normalize(harness.module.currentMemoryTask.value), snapshot.currentTask)
    },
  },
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

const lastRequest = requests => requests[requests.length - 1]

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
