import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { createPinia, setActivePinia } from 'pinia'
import { effectScope, nextTick, reactive, ref } from 'vue'
import { useUserPermissionsStore } from '../src/composables/useUserPermissions.js'
import { createMessagePlatformForm, loadMessagePlatformForm, buildMessagePlatformPayload, isValidMessagePlatformMaxTurns } from '../src/utils/messagePlatformForm.js'

const messagePlatformsViewSource = readFileSync(
  new URL('../src/views/MessagePlatformsView.vue', import.meta.url),
  'utf8',
)
const scriptMatch = messagePlatformsViewSource.match(/<script\s+setup>\r?\n([\s\S]*?)\r?\n<\/script>/)
assert.ok(scriptMatch)

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

const messagePlatformsScript = stripImports(scriptMatch[1])
const dataResponse = data => ({ data: { data } })
const pageResponse = (items, total = items.length, meta = {}) => dataResponse({ items, total, meta })
const normalize = value => JSON.parse(JSON.stringify(value))
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key)

const flush = async () => {
  for (let index = 0; index < 6; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const createHarness = async (t, options = {}) => {
  const currentUid = options.currentUid ?? (options.superuser ? 'admin-user' : 'user-a')
  const currentUser = options.currentUser ?? {
    uid: currentUid,
    username: options.username ?? (options.superuser ? 'Administrator' : 'Alice'),
    is_superuser: options.superuser === true,
  }
  const messages = []
  const mountedCallbacks = []
  const unmountedCallbacks = []
  const timerCalls = []
  const clearedTimers = []
  const api = {
    adminUserList: [],
    platformList: [],
    platformCreates: [],
    platformUpdates: [],
    profileList: [],
  }

  const pinia = createPinia()
  setActivePinia(pinia)
  const permissionsStore = useUserPermissionsStore()
  if (typeof options.superuser === 'boolean') {
    const token = `message-platform-permission-${options.superuser}`
    const refreshed = await permissionsStore.refresh({
      getToken: () => token,
      request: async () => dataResponse(currentUser),
    })
    assert.equal(refreshed, true)
  }

  const adminUsers = options.adminUsers ?? [
    { uid: 'user-a', username: 'Alice' },
    { uid: 'user-b', username: 'Bob' },
  ]
  const platformItems = options.platformItems ?? []
  const profileItems = options.profileItems ?? []
  const messagePlatformApi = {
    list: params => {
      api.platformList.push({ ...params })
      return Promise.resolve(pageResponse(platformItems.map(item => ({ ...item }))))
    },
    types: () => Promise.resolve(dataResponse({
      platform_types: ['WEIXIN_OPENCLAW'],
      languages: ['zh', 'en'],
    })),
    create: payload => {
      api.platformCreates.push(payload)
      return Promise.resolve(dataResponse(options.createResponse ?? {}))
    },
    update: (id, payload) => {
      api.platformUpdates.push({ id, payload })
      return Promise.resolve(dataResponse({}))
    },
    delete: () => Promise.resolve(dataResponse({})),
    recover: () => Promise.resolve(dataResponse({})),
    startWeixinLogin: () => Promise.resolve(dataResponse({ qrcode_img_content: 'stub' })),
    getWeixinLoginStatus: () => Promise.resolve(dataResponse({ qrcode_status: 'waiting' })),
  }
  const adminApi = {
    userList: params => {
      api.adminUserList.push({ ...params })
      return Promise.resolve(pageResponse(adminUsers.map(user => ({ ...user }))))
    },
  }
  const profileApi = {
    list: params => {
      api.profileList.push({ ...params })
      return Promise.resolve(pageResponse(profileItems.map(profile => ({ ...profile }))))
    },
  }

  const ElMessage = (...args) => messages.push({ type: 'message', args })
  ElMessage.warning = (...args) => messages.push({ type: 'warning', args })
  ElMessage.error = (...args) => messages.push({ type: 'error', args })
  ElMessage.success = (...args) => messages.push({ type: 'success', args })
  ElMessage.info = (...args) => messages.push({ type: 'info', args })

  const scope = effectScope()
  const context = vm.createContext({
    adminApi,
    clearInterval: timerId => clearedTimers.push(timerId),
    console: { log() {}, warn() {}, error() {}, info() {} },
    createMessagePlatformForm,
    ElMessage,
    loadMessagePlatformForm,
    onMounted: callback => mountedCallbacks.push(callback),
    onUnmounted: callback => unmountedCallbacks.push(callback),
    QRCode: { toDataURL: async () => 'stub-qrcode-data' },
    reactive,
    ref,
    buildMessagePlatformPayload,
    isValidMessagePlatformMaxTurns,
    messagePlatformApi,
    profileApi,
    setInterval: (...args) => {
      timerCalls.push(args)
      return timerCalls.length
    },
    useDeleteConfirm: () => ({ handleDelete: async () => {} }),
    useI18n: () => ({ t: key => String(key) }),
    useUserPermissionsStore,
    nextTick,
  })

  scope.run(() => {
    vm.runInContext(
      `${messagePlatformsScript}
globalThis.__module = {
  fetchUsers,
  defaultForm,
  openCreateDialog,
  handleEdit,
  buildPayload,
  submitForm,
  fetchPlatforms,
  form,
  users,
  isEdit,
  currentId,
  dialogVisible,
  userPermissions,
}
`,
      context,
      { filename: 'MessagePlatformsView.vue?script-setup' },
    )
  })

  const harness = {
    api,
    messages,
    mountedCallbacks,
    unmountedCallbacks,
    timerCalls,
    clearedTimers,
    permissionsStore,
    module: context.__module,
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

test('unknown and ordinary users fetch only their own user option without admin requests', async t => {
  const unknownHarness = await createHarness(t)
  await unknownHarness.module.fetchUsers()

  assert.deepEqual(unknownHarness.api.adminUserList, [])
  assert.deepEqual(normalize(unknownHarness.module.users.value), [])

  const ordinaryHarness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
    username: 'Alice',
  })
  await ordinaryHarness.module.fetchUsers()

  assert.deepEqual(ordinaryHarness.api.adminUserList, [])
  assert.deepEqual(normalize(ordinaryHarness.module.users.value.map(user => user.uid)), ['user-a'])
  assert.equal(ordinaryHarness.module.users.value[0].username, 'Alice')
})

test('ordinary create and update preserve business fields while omitting uid', async t => {
  const harness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
    username: 'Alice',
  })

  assert.equal(harness.module.defaultForm().uid, 'user-a')
  harness.module.openCreateDialog()
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.isEdit.value, false)

  harness.module.form.name = ' ordinary create '
  harness.module.form.platform_type = 'WEIXIN_OPENCLAW'
  harness.module.form.language = 'en'
  harness.module.form.use_stream_dispatch = true
  harness.module.form.is_enabled = true
  harness.module.form.uid = 'other-user'
  harness.module.form.profile_id = 17
  Object.assign(harness.module.form.config, {
    api_timeout_ms: 22000,
    long_poll_timeout_ms: 44000,
    poll_interval_ms: 1500,
    merge_single_poll_messages: false,
  })

  const ordinaryCreatePayload = harness.module.buildPayload()
  assert.equal(hasOwn(ordinaryCreatePayload, 'uid'), false)
  await harness.module.submitForm()
  await flush()

  assert.deepEqual(normalize(harness.api.platformCreates), [{
    name: 'ordinary create',
    platform_type: 'WEIXIN_OPENCLAW',
    language: 'en',
    use_stream_dispatch: true,
    is_enabled: true,
    profile_id: 17,
    config: {
      api_timeout_ms: 22000,
      long_poll_timeout_ms: 44000,
      poll_interval_ms: 1500,
      merge_single_poll_messages: false,
      stream_llm_request: false,
      session_settings: {
        enable_markdown: false,
        goal_mode: true,
        max_turns: 5,
        show_tool_calls: true,
        show_reasoning: true,
      },
    },
  }])
  assert.equal(hasOwn(harness.api.platformCreates[0], 'uid'), false)
  assert.deepEqual(harness.api.platformList, [{ page: 1, size: 10 }])

  harness.module.handleEdit({
    id: 41,
    name: 'ordinary edit',
    platform_type: 'WEIXIN_OPENCLAW',
    language: 'zh',
    use_stream_dispatch: true,
    is_enabled: true,
    uid: 'other-user',
    profile_id: 23,
    config: {
      api_timeout_ms: 23000,
      long_poll_timeout_ms: 45000,
      poll_interval_ms: 1600,
      merge_single_poll_messages: false,
    },
  })

  assert.equal(harness.module.isEdit.value, true)
  assert.equal(harness.module.currentId.value, 41)
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.form.name, 'ordinary edit')
  assert.equal(harness.module.form.language, 'zh')
  assert.equal(harness.module.form.use_stream_dispatch, true)
  assert.equal(harness.module.form.is_enabled, true)
  assert.equal(harness.module.form.profile_id, 23)
  assert.deepEqual(normalize(harness.module.form.config), {
    api_timeout_ms: 23000,
    long_poll_timeout_ms: 45000,
    poll_interval_ms: 1600,
    merge_single_poll_messages: false,
    stream_llm_request: false,
    session_settings: {
      enable_markdown: false,
      goal_mode: true,
      max_turns: 5,
      show_tool_calls: true,
      show_reasoning: true,
    },
  })

  harness.module.form.uid = null
  const ordinaryUpdatePayload = harness.module.buildPayload()
  assert.equal(hasOwn(ordinaryUpdatePayload, 'uid'), false)
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.platformCreates.length, 1)
  assert.deepEqual(normalize(harness.api.platformUpdates), [{
    id: 41,
    payload: {
      name: 'ordinary edit',
      platform_type: 'WEIXIN_OPENCLAW',
      language: 'zh',
      use_stream_dispatch: true,
      is_enabled: true,
      profile_id: 23,
      config: {
        api_timeout_ms: 23000,
        long_poll_timeout_ms: 45000,
        poll_interval_ms: 1600,
        merge_single_poll_messages: false,
        stream_llm_request: false,
        session_settings: {
          enable_markdown: false,
          goal_mode: true,
          max_turns: 5,
          show_tool_calls: true,
          show_reasoning: true,
        },
      },
    },
  }])
  assert.equal(hasOwn(harness.api.platformUpdates[0].payload, 'uid'), false)
  assert.deepEqual(harness.api.platformList, [
    { page: 1, size: 10 },
    { page: 1, size: 10 },
  ])
})

test('superusers load admin users, submit selected owners, and require uid for enabled platforms', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    adminUsers: [
      { uid: 'user-a', username: 'Alice' },
      { uid: 'user-b', username: 'Bob' },
      { uid: 'user-c', username: 'Carol' },
    ],
  })

  await harness.module.fetchUsers()
  assert.deepEqual(harness.api.adminUserList, [{ page: 1, size: 1000 }])
  assert.deepEqual(harness.module.users.value.map(user => user.uid), ['user-a', 'user-b', 'user-c'])

  harness.module.openCreateDialog()
  assert.equal(harness.module.defaultForm().uid, '')
  assert.equal(harness.module.form.uid, '')
  harness.module.form.name = 'superuser create'
  harness.module.form.uid = 'user-b'
  harness.module.form.profile_id = 31
  harness.module.form.config.api_timeout_ms = 24000

  const superuserCreatePayload = harness.module.buildPayload()
  assert.equal(superuserCreatePayload.uid, 'user-b')
  await harness.module.submitForm()
  await flush()

  assert.deepEqual(normalize(harness.api.platformCreates), [{
    name: 'superuser create',
    platform_type: 'WEIXIN_OPENCLAW',
    language: 'zh',
    use_stream_dispatch: false,
    is_enabled: true,
    uid: 'user-b',
    profile_id: 31,
    config: {
      api_timeout_ms: 24000,
      long_poll_timeout_ms: 30000,
      poll_interval_ms: 1000,
      merge_single_poll_messages: true,
      stream_llm_request: false,
      session_settings: {
        enable_markdown: false,
        goal_mode: true,
        max_turns: 5,
        show_tool_calls: true,
        show_reasoning: true,
      },
    },
  }])

  harness.module.handleEdit({
    id: 52,
    name: 'superuser edit',
    platform_type: 'WEIXIN_OPENCLAW',
    language: 'en',
    use_stream_dispatch: true,
    is_enabled: true,
    uid: 'user-c',
    profile_id: 32,
    config: { poll_interval_ms: 1700 },
  })
  assert.equal(harness.module.form.uid, 'user-c')
  assert.equal(harness.module.form.profile_id, 32)
  assert.equal(harness.module.form.language, 'en')
  assert.equal(harness.module.form.use_stream_dispatch, true)

  harness.module.form.uid = ''
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.platformUpdates.length, 0)
  assert.equal(harness.api.platformList.length, 1)
  assert.deepEqual(harness.messages.filter(message => message.type === 'warning').map(message => message.args[0]), [
    'messagePlatforms.uid_required',
  ])

  harness.module.form.uid = 'user-c'
  await harness.module.submitForm()
  await flush()

  assert.deepEqual(normalize(harness.api.platformUpdates), [{
    id: 52,
    payload: {
      name: 'superuser edit',
      platform_type: 'WEIXIN_OPENCLAW',
      language: 'en',
      use_stream_dispatch: true,
      is_enabled: true,
      uid: 'user-c',
      profile_id: 32,
      config: {
        api_timeout_ms: 15000,
        long_poll_timeout_ms: 30000,
        poll_interval_ms: 1700,
        merge_single_poll_messages: true,
        stream_llm_request: false,
        session_settings: {
          enable_markdown: false,
          goal_mode: true,
          max_turns: 5,
          show_tool_calls: true,
          show_reasoning: true,
        },
      },
    },
  }])
  assert.equal(harness.api.platformList.length, 2)
})

test('clearing permissions removes admin user loading and uid from payloads', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
  })
  await harness.module.fetchUsers()
  harness.module.openCreateDialog()
  harness.module.form.name = 'after clear'
  harness.module.form.uid = 'user-b'

  assert.equal(harness.module.buildPayload().uid, 'user-b')
  harness.permissionsStore.clear()
  await harness.module.fetchUsers()

  assert.equal(harness.api.adminUserList.length, 1)
  assert.deepEqual(normalize(harness.module.users.value), [])
  const payloadAfterClear = harness.module.buildPayload()
  assert.equal(hasOwn(payloadAfterClear, 'uid'), false)

  await harness.module.submitForm()
  await flush()
  assert.equal(harness.api.platformCreates.length, 1)
  assert.equal(hasOwn(harness.api.platformCreates[0], 'uid'), false)
})
