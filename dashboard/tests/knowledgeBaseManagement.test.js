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
import { useUserPermissionsStore } from '../src/composables/useUserPermissions.js'
import zhCommon from '../src/i18n/locales/zh/common.js'
import zhKnowledgeBase from '../src/i18n/locales/zh/knowledgeBase.js'
import enCommon from '../src/i18n/locales/en/common.js'
import enKnowledgeBase from '../src/i18n/locales/en/knowledgeBase.js'

const useKnowledgeBaseViewSource = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeBaseView.js', import.meta.url),
  'utf8',
)
const useDeleteConfirmSource = readFileSync(
  new URL('../src/composables/useDeleteConfirm.js', import.meta.url),
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
const useDeleteConfirmScript = stripModuleSyntax(useDeleteConfirmSource)
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
      return Promise.resolve(dataResponse({ id: 'created-kb', ...payload }))
    },
    update: (id, payload) => {
      const request = { method: 'update', id, payload }
      requests.push(request)
      api.updates.push(request)
      return Promise.resolve(dataResponse({ id, ...payload }))
    },
    delete: id => {
      const request = { method: 'delete', id }
      requests.push(request)
      api.deletes.push(request)
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
    computed,
    ElMessage,
    ElMessageBox,
    formatTime: value => value,
    adminApi,
    knowledgeBaseApi,
    setTimeout,
    clearTimeout,
    onBeforeUnmount: callback => unmountedCallbacks.push(callback),
    onMounted: callback => mountedCallbacks.push(callback),
    reactive,
    ref,
    watch,
    useI18n: () => ({
      t: (key, params) => i18n.global.t(
        key,
        params && typeof params === 'object' ? JSON.parse(JSON.stringify(params)) : params,
      ),
    }),
    useKnowledgeBaseDocuments: () => ({}),
    useManagedKnowledge: () => ({ stopManagedKnowledgePolling() {} }),
    useKnowledgeOrganization: () => ({ stopOrganizationPolling() {} }),
    useUserPermissionsStore,
    i18n,
    ...knowledgeBaseManagement,
    ...knowledgeBaseMigration,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  scope.run(() => {
    vm.runInContext(
      `${useDeleteConfirmScript}\n${useKnowledgeBaseViewScript}\n` +
      'globalThis.__module = useKnowledgeBaseView()',
      context,
      { filename: 'useKnowledgeBaseView.js' },
    )
  })

  const harness = {
    api,
    confirms,
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
        await callback(true)
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
        await callback(true)
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

    assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
    assert.equal(harness.module.form.uid, action === 'edit' ? 'edited-owner' : currentUser.uid)
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
