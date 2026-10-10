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

const memoryComposableSources = [
  readFileSync(new URL('../src/composables/memories/useMemoryRuntime.js', import.meta.url), 'utf8'),
  readFileSync(new URL('../src/composables/memories/useMemoryRecords.js', import.meta.url), 'utf8'),
  readFileSync(new URL('../src/composables/memories/useMemoriesView.js', import.meta.url), 'utf8'),
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
  assert.equal(harness.calls('memoryApi.settings').length, 0)

  harness.module.openRuntimeDialog('status')
  await harness.flush()
  assert.deepEqual(normalize(harness.calls('memoryApi.settings')[0].args[0].params), { uid: 'user-a' })

  harness.module.ownerFilter.value = 'user-b'
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  await harness.module.loadJobs()
  await harness.module.loadMigrations()

  assert.equal(harness.calls('adminApi.userList').length, 0)
  for (const name of ['memoryApi.list', 'memoryApi.jobs', 'memoryApi.migrations']) {
    for (const request of harness.calls(name)) assert.equal(request.args[0].uid, undefined)
  }
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

test('uses the selected superuser owner for list, jobs, and migrations while independently pre-filling the runtime dialog', async t => {
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
  assert.equal(harness.calls('memoryApi.settings').length, 0)
  const requestCountBeforeSelection = harness.apiState.requests.length

  harness.module.ownerFilter.value = 'user-b'
  harness.module.handleOwnerChange()
  await harness.flush()
  await harness.module.loadJobs()
  await harness.module.loadMigrations()

  const selectedRequests = harness.apiState.requests.slice(requestCountBeforeSelection)
  for (const name of ['memoryApi.list', 'memoryApi.jobs', 'memoryApi.migrations']) {
    const requests = harness.calls(name).filter(request => selectedRequests.includes(request))
    assert.ok(requests.length > 0)
    for (const request of requests) assert.equal(request.args[0].uid, 'user-b')
  }
  assert.equal(harness.calls('memoryApi.settings').length, 0)

  harness.module.openRuntimeDialog('status')
  await harness.flush()
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-b')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-b')
  assert.equal(harness.calls('memoryApi.settings').length, 1)
  assert.equal(harness.calls('memoryApi.settings')[0].args[0].params.uid, 'user-b')
})

test('blocks runtime actions without an owner in the all-user scope', async t => {
  const harness = createHarness(t, { superuser: true })
  await harness.mount()
  await harness.flush()

  for (const action of ['status', 'organize', 'reindex']) {
    harness.module.openRuntimeDialog(action)
    await harness.flush()

    assert.equal(harness.module.runtimeDialogVisible.value, true)
    assert.equal(harness.module.runtimeDialogAction.value, action)
    assert.equal(harness.module.runtimeOwnerUid.value, null)
    assert.equal(harness.module.organizeBlocked.value, true)
    assert.equal(harness.module.reindexBlocked.value, true)

    await harness.module.submitRuntimeOperation('organize')
    await harness.module.submitRuntimeOperation('reindex')
    assert.equal(harness.calls('memoryApi.settings').length, 0)
    assert.equal(harness.calls('memoryApi.organize').length, 0)
    assert.equal(harness.calls('memoryApi.reindex').length, 0)

    harness.module.closeRuntimeDialog()
    assert.equal(harness.module.runtimeDialogVisible.value, false)
  }
})

test('keeps runtime state independent while switching the list owner', async t => {
  const harness = createHarness(t, {
    superuser: true,
    apiOverrides: {
      memoryApi: {
        settings: () => dataResponse({
          configured: true,
          current_job: { id: 7, operation: 'organize', status: 'running' },
        }),
      },
    },
  })
  await harness.mount()
  await harness.flush()

  harness.module.ownerFilter.value = 'user-a'
  harness.module.openRuntimeDialog('status')
  await harness.flush()
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')

  harness.module.activeTab.value = 'jobs'
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

  harness.module.ownerFilter.value = 'user-b'
  const settingsBeforeSwitch = normalize(harness.module.settings)
  const currentTaskBeforeSwitch = normalize(harness.module.currentMemoryTask.value)
  const requestCountBeforeSwitch = harness.apiState.requests.length
  harness.module.handleOwnerChange()

  assert.deepEqual(normalize(harness.module.settings), settingsBeforeSwitch)
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
  assert.deepEqual(normalize(harness.module.currentMemoryTask.value), currentTaskBeforeSwitch)
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')

  await harness.flush()
  const refreshedRequests = harness.apiState.requests.slice(requestCountBeforeSwitch)
  const settingsRequests = refreshedRequests.filter(request => request.name === 'memoryApi.settings')
  const listRequests = refreshedRequests.filter(request => request.name === 'memoryApi.list')
  const jobRequests = refreshedRequests.filter(request => request.name === 'memoryApi.jobs')
  const migrationRequests = refreshedRequests.filter(request => request.name === 'memoryApi.migrations')

  assert.equal(settingsRequests.length, 1)
  assert.equal(settingsRequests[0].args[0].params.uid, 'user-a')
  assert.equal(listRequests.length, 1)
  assert.equal(listRequests[0].args[0].uid, 'user-b')
  assert.equal(jobRequests.length, 1)
  assert.equal(jobRequests[0].args[0].uid, 'user-b')
  assert.equal(migrationRequests.length, 0)
  assert.deepEqual(normalize(harness.module.settings), settingsBeforeSwitch)
  assert.deepEqual(normalize(harness.module.currentMemoryTask.value), currentTaskBeforeSwitch)
  assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a')
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
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
        list: () => pageResponse(memorySnapshot.map(row => ({ ...row })), memorySnapshot.length),
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

const scopedLoaderMeta = {
  is_superuser: true,
  current_uid: 'admin-user',
  current_username: 'Administrator',
}

const scopedOwner = marker => String(marker).startsWith('A') ? 'owner-a' : 'owner-b'
const scopedId = (marker, ownerAId, ownerBId) => String(marker).startsWith('A') ? ownerAId : ownerBId

const scopedLoaderCases = [
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

test('loads each admin runtime action for the selected owner without changing list state', async t => {
  for (const action of ['organize', 'reindex', 'status']) {
    const settingsByOwner = {
      'user-a': {
        configured: true,
        capacity: { active_record_count: 11, max_active_records: 50 },
        current_job: { id: 'job-a', operation: 'organize', status: 'running' },
      },
      'user-b': {
        configured: true,
        capacity: { active_record_count: 29, max_active_records: 60 },
        current_job: { id: 'job-b', operation: 'reindex', status: 'running' },
      },
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
          settings: ({ params }) => dataResponse(settingsByOwner[params.uid]),
        },
      },
    })
    await harness.mount()
    await harness.flush()

    harness.module.ownerFilter.value = 'user-a'
    harness.module.filters.keyword = 'keep-this-keyword'
    harness.module.filters.memory_type = 'preference'
    harness.module.jobFilters.status = 'running'
    harness.module.jobFilters.operation = 'organize'
    harness.module.jobFilters.memory_id = 'memory-7'
    harness.module.memoryPage.value = 4
    harness.module.memoryPageSize.value = 50
    harness.module.memoryTotal.value = 12
    harness.module.memories.value = [{ id: 'list-a', owner_uid: 'user-a' }]
    harness.module.jobPage.value = 3
    harness.module.jobPageSize.value = 50
    harness.module.jobTotal.value = 8
    harness.module.jobs.value = [{ id: 'job-list-a', owner_uid: 'user-a' }]
    harness.module.migrationPage.value = 2
    harness.module.migrationPageSize.value = 50
    harness.module.migrationTotal.value = 5
    harness.module.migrations.value = [{ id: 'migration-a', owner_uid: 'user-a' }]

    harness.module.openRuntimeDialog(action)
    await harness.flush()

    assert.equal(harness.module.runtimeDialogVisible.value, true, action)
    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a', action)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-a', action)
    assert.equal(harness.calls('memoryApi.settings').at(-1).args[0].params.uid, 'user-a', action)
    assert.equal(harness.module.settings.capacity.active_record_count, 11, action)
    assert.equal(harness.module.currentMemoryTask.value.id, 'job-a', action)
    assert.equal(harness.module.ownerLabel(harness.module.runtimeOwnerUid.value), 'Alice', action)

    const stateBeforeRuntimeSwitch = {
      ownerFilter: harness.module.ownerFilter.value,
      filters: normalize(harness.module.filters),
      jobFilters: normalize(harness.module.jobFilters),
      memoryPage: harness.module.memoryPage.value,
      memoryPageSize: harness.module.memoryPageSize.value,
      memoryTotal: harness.module.memoryTotal.value,
      memories: normalize(harness.module.memories.value),
      jobPage: harness.module.jobPage.value,
      jobPageSize: harness.module.jobPageSize.value,
      jobTotal: harness.module.jobTotal.value,
      jobs: normalize(harness.module.jobs.value),
      migrationPage: harness.module.migrationPage.value,
      migrationPageSize: harness.module.migrationPageSize.value,
      migrationTotal: harness.module.migrationTotal.value,
      migrations: normalize(harness.module.migrations.value),
    }
    const listRequestCount = harness.calls('memoryApi.list').length
    const jobsRequestCount = harness.calls('memoryApi.jobs').length
    const migrationsRequestCount = harness.calls('memoryApi.migrations').length
    const settingsRequestCount = harness.calls('memoryApi.settings').length

    harness.module.handleRuntimeOwnerChange('user-b')

    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-b', action)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-b', action)
    assert.equal(harness.module.settingsLoaded.value, false, action)
    assert.deepEqual(normalize(harness.module.settings), {}, action)
    assert.equal(harness.module.currentMemoryTask.value, null, action)

    await harness.flush()

    assert.equal(harness.calls('memoryApi.settings').length, settingsRequestCount + 1, action)
    assert.equal(harness.calls('memoryApi.settings').at(-1).args[0].params.uid, 'user-b', action)
    assert.equal(harness.module.settings.capacity.active_record_count, 29, action)
    assert.equal(harness.module.currentMemoryTask.value.id, 'job-b', action)
    assert.equal(harness.module.ownerLabel(harness.module.runtimeOwnerUid.value), 'Bob', action)
    assert.equal(harness.calls('memoryApi.list').length, listRequestCount, action)
    assert.equal(harness.calls('memoryApi.jobs').length, jobsRequestCount, action)
    assert.equal(harness.calls('memoryApi.migrations').length, migrationsRequestCount, action)
    assert.deepEqual({
      ownerFilter: harness.module.ownerFilter.value,
      filters: normalize(harness.module.filters),
      jobFilters: normalize(harness.module.jobFilters),
      memoryPage: harness.module.memoryPage.value,
      memoryPageSize: harness.module.memoryPageSize.value,
      memoryTotal: harness.module.memoryTotal.value,
      memories: normalize(harness.module.memories.value),
      jobPage: harness.module.jobPage.value,
      jobPageSize: harness.module.jobPageSize.value,
      jobTotal: harness.module.jobTotal.value,
      jobs: normalize(harness.module.jobs.value),
      migrationPage: harness.module.migrationPage.value,
      migrationPageSize: harness.module.migrationPageSize.value,
      migrationTotal: harness.module.migrationTotal.value,
      migrations: normalize(harness.module.migrations.value),
    }, stateBeforeRuntimeSwitch, action)

    harness.module.handleRuntimeOwnerChange('')
    assert.equal(harness.module.runtimeOwnerFilter.value, '', action)
    assert.equal(harness.module.runtimeOwnerUid.value, null, action)
    assert.equal(harness.module.settingsLoaded.value, false, action)
    assert.deepEqual(normalize(harness.module.settings), {}, action)
    assert.equal(harness.module.currentMemoryTask.value, null, action)
    assert.equal(harness.module.organizeBlocked.value, true, action)
    assert.equal(harness.module.reindexBlocked.value, true, action)

    await harness.flush()

    assert.equal(harness.calls('memoryApi.settings').length, settingsRequestCount + 1, action)
    assert.equal(harness.calls('memoryApi.organize').length, 0, action)
    assert.equal(harness.calls('memoryApi.reindex').length, 0, action)
  }
})

test('keeps ordinary-user runtime settings bound to the current user for every action', async t => {
  for (const action of ['organize', 'reindex', 'status']) {
    const requestedOwners = []
    const harness = createHarness(t, {
      currentUid: 'user-a',
      currentUsername: 'Alice',
      apiOverrides: {
        memoryApi: {
          settings: ({ params }) => {
            requestedOwners.push(params.uid)
            return dataResponse({
              configured: true,
              capacity: { active_record_count: 7, max_active_records: 40 },
            })
          },
        },
      },
    })
    await harness.mount()
    await harness.flush()

    assert.equal(harness.calls('adminApi.userList').length, 0, action)
    harness.module.ownerFilter.value = 'user-b'
    harness.module.openRuntimeDialog(action)
    await harness.flush()
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-a', action)
    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a', action)
    assert.equal(harness.module.ownerLabel(harness.module.runtimeOwnerUid.value), 'Alice', action)

    harness.module.runtimeOwnerFilter.value = 'user-b'
    await harness.module.loadSettings()
    harness.module.handleRuntimeOwnerChange('user-b')
    await harness.flush()

    assert.ok(requestedOwners.length >= 3, action)
    assert.equal(requestedOwners.every(uid => uid === 'user-a'), true, action)
    assert.equal(harness.module.runtimeOwnerFilter.value, 'user-a', action)
    assert.equal(harness.module.runtimeOwnerUid.value, 'user-a', action)
    assert.equal(harness.module.ownerLabel('user-a'), 'Alice', action)
    assert.equal(harness.module.ownerLabel('user-b'), 'user-b', action)
  }
})

test('does not open or query runtime status when entry is blocked or another dialog is open', async t => {
  const blockedEntryCases = [
    {
      name: 'invalid action',
      prepare: () => {},
      action: 'unknown',
    },
    {
      name: 'scope is not ready',
      prepare: harness => { harness.module.memoryScopeReady.value = false },
      action: 'status',
    },
    {
      name: 'another action is loading',
      prepare: harness => { harness.module.actionLoading.value = 'organize' },
      action: 'status',
    },
  ]

  for (const blockedCase of blockedEntryCases) {
    const harness = createHarness(t)
    await harness.mount()
    blockedCase.prepare(harness)
    harness.module.openRuntimeDialog(blockedCase.action)

    assert.equal(harness.module.runtimeDialogVisible.value, false, blockedCase.name)
    assert.equal(harness.calls('memoryApi.settings').length, 0, blockedCase.name)
  }

  const unmountedHarness = createHarness(t)
  await unmountedHarness.mount()
  unmountedHarness.unmount()
  unmountedHarness.module.openRuntimeDialog('status')
  assert.equal(unmountedHarness.module.runtimeDialogVisible.value, false)
  assert.equal(unmountedHarness.calls('memoryApi.settings').length, 0)

  for (const dialogKey of ['editorVisible', 'detailsVisible', 'historyVisible', 'jobVisible', 'migrationVisible']) {
    const harness = createHarness(t)
    await harness.mount()
    harness.module[dialogKey].value = true
    Object.assign(harness.module.form, {
      id: 901,
      version: 3,
      owner_uid: 'user-a',
      memory_key: 'draft-key',
      memory_type: 'preference',
      content: 'draft-content',
      change_evidence: 'draft-evidence',
      suppress_current: true,
    })
    const draftBeforeOpen = normalize(harness.module.form)

    harness.module.openRuntimeDialog('status')

    assert.equal(harness.module.runtimeDialogVisible.value, false, dialogKey)
    assert.equal(harness.module[dialogKey].value, true, dialogKey)
    assert.deepEqual(normalize(harness.module.form), draftBeforeOpen, dialogKey)
    assert.equal(harness.calls('memoryApi.settings').length, 0, dialogKey)
  }
})

test('invalidates pending memory, job, and migration details when runtime status opens first', async t => {
  for (const detailCase of detailCases) {
    for (const staleOutcome of ['success', 'failure']) {
      const harness = createDetailHarness(t, detailCase)
      await harness.mount()
      const row = detailRow(detailCase, 'owner-a', `runtime-${staleOutcome}`)
      const pending = harness.module[detailCase.show](row)
      const request = harness.calls(detailCase.requestName)[0]

      assert.equal(harness.module[detailCase.visible].value, false, detailCase.name)
      harness.module.openRuntimeDialog('status')
      await harness.flush()
      const messagesBeforeStaleResponse = normalize(harness.messages)

      if (staleOutcome === 'success') request.deferred.resolve(detailResponse(row, 'stale'))
      else request.deferred.reject(new Error(`stale ${detailCase.name} response`))
      await pending

      assert.equal(harness.module.runtimeDialogVisible.value, true, detailCase.name)
      assert.equal(harness.module[detailCase.selected].value, null, detailCase.name)
      assert.equal(harness.module[detailCase.visible].value, false, detailCase.name)
      assert.deepEqual(normalize(harness.messages), messagesBeforeStaleResponse, detailCase.name)
    }
  }
})

const runtimeSettingsData = (marker, activeRecordCount, maxActiveRecords = activeRecordCount + 40) => dataResponse({
  configured: true,
  capacity: { active_record_count: activeRecordCount, max_active_records: maxActiveRecords },
  current_job: { id: `runtime-${marker}`, operation: 'create', status: 'running' },
})

const createRuntimeSettingsHarness = t => createHarness(t, {
  superuser: true,
  apiOverrides: {
    memoryApi: {
      settings: (...args) => args[args.length - 1].deferred.promise,
    },
  },
})

test('keeps the newest runtime settings after an administrator owner switch race', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createRuntimeSettingsHarness(t)
    await harness.mount()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.openRuntimeDialog('status')
    const oldRequest = harness.calls('memoryApi.settings')[0]

    harness.module.handleRuntimeOwnerChange('user-b')
    const newRequest = harness.calls('memoryApi.settings')[1]
    assert.equal(oldRequest.args[0].params.uid, 'user-a')
    assert.equal(newRequest.args[0].params.uid, 'user-b')
    assert.equal(oldRequest.args[0].signal.aborted, true)

    newRequest.deferred.resolve(runtimeSettingsData('B', 29, 60))
    await harness.flush()
    const settingsAfterB = normalize(harness.module.settings)
    const taskAfterB = normalize(harness.module.currentMemoryTask.value)
    const messagesAfterB = normalize(harness.messages)
    assert.equal(harness.module.settings.capacity.active_record_count, 29)
    assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-B')
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')

    if (staleOutcome === 'success') oldRequest.deferred.resolve(runtimeSettingsData('A-stale', 11, 50))
    else oldRequest.deferred.reject(new Error('stale A settings response'))
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), settingsAfterB)
    assert.deepEqual(normalize(harness.module.currentMemoryTask.value), taskAfterB)
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.deepEqual(normalize(harness.messages), messagesAfterB)
  }
})

test('keeps the final A runtime read authoritative across a rapid A to B to A cycle', async t => {
  for (const firstAOutcome of ['success', 'failure']) {
    for (const bOutcome of ['success', 'failure']) {
      const harness = createRuntimeSettingsHarness(t)
      await harness.mount()
      harness.module.ownerFilter.value = 'user-a'
      harness.module.openRuntimeDialog('status')
      const firstARequest = harness.calls('memoryApi.settings')[0]

      harness.module.handleRuntimeOwnerChange('user-b')
      const bRequest = harness.calls('memoryApi.settings')[1]
      harness.module.handleRuntimeOwnerChange('user-a')
      const finalARequest = harness.calls('memoryApi.settings')[2]
      assert.equal(firstARequest.args[0].signal.aborted, true)
      assert.equal(bRequest.args[0].signal.aborted, true)
      assert.equal(finalARequest.args[0].params.uid, 'user-a')

      finalARequest.deferred.resolve(runtimeSettingsData('A-final', 37, 70))
      await harness.flush()
      const finalSnapshot = {
        settings: normalize(harness.module.settings),
        currentMemoryTask: normalize(harness.module.currentMemoryTask.value),
        settingsLoaded: harness.module.settingsLoaded.value,
        settingsLoading: harness.module.settingsLoading.value,
        settingsLoadError: harness.module.settingsLoadError.value,
        messages: normalize(harness.messages),
      }
      assert.equal(harness.module.settings.capacity.active_record_count, 37)
      assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-A-final')
      assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')

      if (firstAOutcome === 'success') firstARequest.deferred.resolve(runtimeSettingsData('A-first-stale', 11, 50))
      else firstARequest.deferred.reject(new Error('stale first A settings response'))
      await harness.flush()
      if (bOutcome === 'success') bRequest.deferred.resolve(runtimeSettingsData('B-stale', 29, 60))
      else bRequest.deferred.reject(new Error('stale B settings response'))
      await harness.flush()

      assert.deepEqual(normalize(harness.module.settings), finalSnapshot.settings)
      assert.deepEqual(normalize(harness.module.currentMemoryTask.value), finalSnapshot.currentMemoryTask)
      assert.equal(harness.module.settingsLoaded.value, finalSnapshot.settingsLoaded)
      assert.equal(harness.module.settingsLoading.value, finalSnapshot.settingsLoading)
      assert.equal(harness.module.settingsLoadError.value, finalSnapshot.settingsLoadError)
      assert.deepEqual(normalize(harness.messages), finalSnapshot.messages)
    }
  }
})

test('cancels and clears runtime settings on close before reopening the same owner', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createRuntimeSettingsHarness(t)
    await harness.mount()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.openRuntimeDialog('status')
    const oldRequest = harness.calls('memoryApi.settings')[0]

    harness.module.closeRuntimeDialog()
    assert.equal(oldRequest.args[0].signal.aborted, true)
    assert.equal(harness.module.runtimeDialogVisible.value, false)
    assert.deepEqual(normalize(harness.module.settings), {})
    assert.equal(harness.module.settingsLoaded.value, false)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')
    for (const name of [
      'memoryApi.cancelJob',
      'memoryApi.cancelMigration',
      'memoryApi.organize',
      'memoryApi.reindex',
      'memoryApi.retryJob',
      'memoryApi.retryMigration',
      'memoryApi.retryCleanup',
      'memoryApi.updateSettings',
      'memoryApi.create',
      'memoryApi.update',
      'memoryApi.delete',
      'memoryApi.pin',
      'memoryApi.unpin',
      'memoryApi.resumeCurrent',
    ]) assert.equal(harness.calls(name).length, 0, name)

    harness.module.openRuntimeDialog('status')
    const newRequest = harness.calls('memoryApi.settings')[1]
    assert.equal(newRequest.args[0].params.uid, 'user-a')
    if (staleOutcome === 'success') oldRequest.deferred.resolve(runtimeSettingsData('old-late', 7, 40))
    else oldRequest.deferred.reject(new Error('old closed settings response'))
    await harness.flush()
    assert.deepEqual(normalize(harness.module.settings), {})
    assert.equal(harness.module.settingsLoaded.value, false)
    assert.equal(harness.module.settingsLoading.value, true)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.equal(harness.messages.length, 0)

    newRequest.deferred.resolve(runtimeSettingsData('reopened', 43, 80))
    await harness.flush()
    assert.equal(harness.module.settings.capacity.active_record_count, 43)
    assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-reopened')
    assert.equal(harness.module.settingsLoaded.value, true)
    assert.equal(harness.module.settingsLoading.value, false)
    assert.equal(harness.module.settingsLoadError.value, '')
    assert.equal(harness.messages.length, 0)
  }
})

test('blocks runtime confirmation during an organize refresh failure and restores it after success', async t => {
  const harness = createRuntimeSettingsHarness(t)
  await harness.mount()
  harness.module.ownerFilter.value = 'user-a'
  harness.module.openRuntimeDialog('organize')
  const initialRequest = harness.calls('memoryApi.settings')[0]
  initialRequest.deferred.resolve(runtimeSettingsData('before-refresh', 13, 50))
  await harness.flush()
  assert.equal(harness.module.organizeBlocked.value, false)
  assert.equal(harness.module.reindexBlocked.value, false)
  assert.equal(harness.module.settings.capacity.active_record_count, 13)
  assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-before-refresh')
  const messagesBeforeFailure = normalize(harness.messages)

  const failedPending = harness.module.loadSettings()
  const failedRequest = harness.calls('memoryApi.settings')[1]
  assert.equal(harness.module.settingsLoading.value, true)
  assert.equal(harness.module.settingsLoaded.value, true)
  assert.equal(harness.module.organizeBlocked.value, true)
  assert.equal(harness.module.reindexBlocked.value, true)
  failedRequest.deferred.reject(new Error('runtime refresh failed'))
  await failedPending
  await harness.flush()

  assert.deepEqual(normalize(harness.module.settings), {})
  assert.equal(harness.module.currentMemoryTask.value, null)
  assert.equal(harness.module.settingsLoaded.value, false)
  assert.equal(harness.module.settingsLoading.value, false)
  assert.equal(harness.module.settingsLoadError.value, 'runtime refresh failed')
  assert.equal(harness.module.organizeBlocked.value, true)
  assert.equal(harness.module.reindexBlocked.value, true)
  assert.deepEqual(normalize(harness.messages), messagesBeforeFailure)

  const successfulPending = harness.module.loadSettings()
  const successfulRequest = harness.calls('memoryApi.settings')[2]
  successfulRequest.deferred.resolve(runtimeSettingsData('after-refresh', 31, 70))
  await successfulPending
  await harness.flush()
  assert.equal(harness.module.settings.capacity.active_record_count, 31)
  assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-after-refresh')
  assert.equal(harness.module.settingsLoaded.value, true)
  assert.equal(harness.module.settingsLoading.value, false)
  assert.equal(harness.module.settingsLoadError.value, '')
  assert.equal(harness.module.organizeBlocked.value, false)
  assert.equal(harness.module.reindexBlocked.value, false)
  assert.deepEqual(normalize(harness.messages), messagesBeforeFailure)
})

test('polls runtime settings only for the open owner and keeps silent reads out of loading state', async t => {
  const harness = createRuntimeSettingsHarness(t)
  await harness.mount()
  const closedPollBeforeOpen = [...harness.timers.values()].at(-1)
  const listCountBeforeClosedPoll = harness.calls('memoryApi.list').length
  await closedPollBeforeOpen.callback()
  await harness.flush()
  assert.equal(harness.calls('memoryApi.settings').length, 0)
  assert.ok(harness.calls('memoryApi.list').length > listCountBeforeClosedPoll)

  harness.module.ownerFilter.value = 'user-a'
  harness.module.openRuntimeDialog('status')
  const openRequest = harness.calls('memoryApi.settings')[0]
  openRequest.deferred.resolve(runtimeSettingsData('open', 17, 50))
  await harness.flush()
  assert.equal(harness.module.runtimeOwnerUid.value, 'user-a')
  assert.equal(harness.module.settingsLoaded.value, true)
  assert.equal(harness.module.settingsLoading.value, false)

  const pollTimer = [...harness.timers.values()].at(-1)
  const pollPending = pollTimer.callback()
  await harness.flush()
  const settingsRequestsDuringPoll = harness.calls('memoryApi.settings')
  assert.equal(settingsRequestsDuringPoll.length, 2)
  assert.deepEqual(settingsRequestsDuringPoll.map(request => request.args[0].params.uid), ['user-a', 'user-a'])
  assert.equal(harness.module.settingsLoading.value, false)
  assert.equal(harness.module.settingsLoaded.value, true)
  assert.equal(harness.module.settings.capacity.active_record_count, 17)

  settingsRequestsDuringPoll[1].deferred.resolve(runtimeSettingsData('polled', 23, 60))
  await pollPending
  await harness.flush()
  assert.equal(harness.module.settings.capacity.active_record_count, 23)
  assert.equal(harness.module.currentMemoryTask.value.id, 'runtime-polled')
  assert.equal(harness.module.settingsLoading.value, false)

  harness.module.closeRuntimeDialog()
  const settingsCountAfterClose = harness.calls('memoryApi.settings').length
  const listCountBeforeClosedPollAfterOpen = harness.calls('memoryApi.list').length
  const closedPollAfterOpen = [...harness.timers.values()].at(-1)
  await closedPollAfterOpen.callback()
  await harness.flush()
  assert.equal(harness.calls('memoryApi.settings').length, settingsCountAfterClose)
  assert.ok(harness.calls('memoryApi.list').length > listCountBeforeClosedPollAfterOpen)
})

test('preserves the runtime settings snapshot when a pending read settles after unmount', async t => {
  for (const staleOutcome of ['success', 'failure']) {
    const harness = createRuntimeSettingsHarness(t)
    await harness.mount()
    harness.module.ownerFilter.value = 'user-a'
    harness.module.openRuntimeDialog('status')
    const initialRequest = harness.calls('memoryApi.settings')[0]
    initialRequest.deferred.resolve(runtimeSettingsData('before-unmount', 19, 55))
    await harness.flush()
    harness.module.settingsLoadError.value = 'keep-before-unmount'

    const pending = harness.module.loadSettings()
    const request = harness.calls('memoryApi.settings')[1]
    const snapshot = {
      settings: normalize(harness.module.settings),
      currentMemoryTask: normalize(harness.module.currentMemoryTask.value),
      runtimeDialogVisible: harness.module.runtimeDialogVisible.value,
      runtimeOwnerFilter: harness.module.runtimeOwnerFilter.value,
      runtimeOwnerUid: harness.module.runtimeOwnerUid.value,
      settingsLoaded: harness.module.settingsLoaded.value,
      settingsLoading: harness.module.settingsLoading.value,
      settingsLoadError: harness.module.settingsLoadError.value,
      messages: normalize(harness.messages),
    }
    assert.equal(snapshot.settingsLoading, true)
    harness.unmount()
    assert.equal(request.args[0].signal.aborted, true)

    if (staleOutcome === 'success') request.deferred.resolve(runtimeSettingsData('late-unmount', 99, 120))
    else request.deferred.reject(new Error('late unmount settings failure'))
    await pending
    await harness.flush()

    assert.deepEqual(normalize(harness.module.settings), snapshot.settings)
    assert.deepEqual(normalize(harness.module.currentMemoryTask.value), snapshot.currentMemoryTask)
    assert.equal(harness.module.runtimeDialogVisible.value, snapshot.runtimeDialogVisible)
    assert.equal(harness.module.runtimeOwnerFilter.value, snapshot.runtimeOwnerFilter)
    assert.equal(harness.module.runtimeOwnerUid.value, snapshot.runtimeOwnerUid)
    assert.equal(harness.module.settingsLoaded.value, snapshot.settingsLoaded)
    assert.equal(harness.module.settingsLoading.value, snapshot.settingsLoading)
    assert.equal(harness.module.settingsLoadError.value, snapshot.settingsLoadError)
    assert.deepEqual(normalize(harness.messages), snapshot.messages)
  }
})

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
