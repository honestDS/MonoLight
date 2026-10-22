import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { createPinia, setActivePinia } from 'pinia'
import {
  computed,
  effectScope,
  h,
  nextTick,
  reactive,
  ref,
  watch,
} from 'vue'
import { defaultProfileConfigs } from '../src/constants/index.js'
import { useUserPermissionsStore } from '../src/composables/useUserPermissions.js'
import * as memoryManagement from '../src/utils/memoryManagement.js'
import * as profileOptions from '../src/utils/profileOptions.js'

const profileMemorySource = readFileSync(
  new URL('../src/composables/profiles/useProfileMemory.js', import.meta.url),
  'utf8',
)
const profileEditorSource = readFileSync(
  new URL('../src/composables/profiles/useProfileEditor.js', import.meta.url),
  'utf8',
)
const profilesViewSource = readFileSync(
  new URL('../src/composables/profiles/useProfilesView.js', import.meta.url),
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

const stripExports = sourceText => sourceText.replace(/^export /gm, '')
const profilesScript = [profileMemorySource, profileEditorSource, profilesViewSource]
  .map(sourceText => stripExports(stripImports(sourceText)))
  .join('\n')
const dataResponse = data => ({ data: { data } })
const normalize = value => JSON.parse(JSON.stringify(value))
const pageResponse = (items, total, meta) => dataResponse({ items, total, meta })
const flush = async () => {
  for (let index = 0; index < 6; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const translate = key => String(key)

const createHarness = async (t, options = {}) => {
  const ownerUid = options.ownerUid ?? 'user-a'
  const currentUid = options.currentUid ?? (options.superuser ? 'admin-user' : ownerUid)
  const knowledgeBaseItems = options.knowledgeBases ?? [
    {
      id: 'kb-user-a',
      uid: ownerUid,
      name: 'User knowledge base',
      description: '',
      knowledge_base_type: 'user',
    },
  ]
  const messages = []
  const mountedCallbacks = []
  const api = {
    profileList: [],
    knowledgeBaseList: [],
    memorySettings: [],
    modelOptions: [],
    channelList: [],
    promptList: [],
    adminUserList: [],
    systemSettings: [],
    systemSettingsUpdates: [],
    creates: [],
    updates: [],
  }

  const pinia = createPinia()
  const permissionsStore = useUserPermissionsStore(pinia)
  setActivePinia(pinia)
  if (typeof options.superuser === 'boolean') {
    const token = `profile-permission-${options.superuser}`
    const refreshed = await permissionsStore.refresh({
      getToken: () => token,
      request: async () => dataResponse({
        uid: currentUid,
        is_superuser: options.superuser,
      }),
    })
    assert.equal(refreshed, true)
  }

  const profileApi = {
    list: params => {
      api.profileList.push(params)
      if (options.profileListRequest) return options.profileListRequest(params)
      return Promise.resolve(pageResponse([], 0, {
        show_owner: false,
        current_uid: currentUid,
        tool_options: [],
        config_permissions: {
          security: options.superuser === true,
          ...(options.configPermissions || {}),
        },
      }))
    },
    create: payload => {
      api.creates.push(payload)
      return Promise.resolve(dataResponse({}))
    },
    update: (id, payload) => {
      api.updates.push({ id, payload })
      return Promise.resolve(dataResponse({}))
    },
    memorySettings: params => {
      api.memorySettings.push(params)
      return Promise.resolve(dataResponse({ configured: false }))
    },
    modelOptions: params => {
      api.modelOptions.push(params)
      const items = options.modelOptionsItems ?? []
      return Promise.resolve(pageResponse(items, items.length, {}))
    },
  }
  const knowledgeBaseApi = {
    list: params => {
      api.knowledgeBaseList.push(params)
      if (options.knowledgeBaseFailure) return Promise.reject(new Error('knowledge base request failed'))
      return Promise.resolve(pageResponse(
        knowledgeBaseItems.map(item => ({ ...item })),
        knowledgeBaseItems.length,
        {},
      ))
    },
  }
  const defaultSystemSettings = {
    log_locale: 'en',
    temp_dir_max_size_mb: 2048,
    llm_request_max_size_mb: 64,
    audit_retention_days: 120,
    audit_report_email: 'audit@example.com',
    session_reply_max_concurrency: 8,
    log_redaction_enabled: false,
  }
  const channelApi = {
    list: params => {
      api.channelList.push(params)
      return Promise.reject(new Error('403 channel management API forbidden'))
    },
  }
  const promptApi = {
    list: params => {
      api.promptList.push(params)
      return Promise.resolve(pageResponse([], 0, {}))
    },
  }
  const adminApi = {
    userList: params => {
      api.adminUserList.push(params)
      return Promise.resolve(pageResponse([], 0, {}))
    },
  }
  const systemApi = {
    settings: () => {
      api.systemSettings.push({})
      if (options.systemSettingsRequest) return options.systemSettingsRequest()
      return Promise.resolve(dataResponse(defaultSystemSettings))
    },
    updateSettings: payload => {
      api.systemSettingsUpdates.push({ ...payload })
      if (options.systemSettingsUpdate) return options.systemSettingsUpdate(payload)
      return Promise.resolve(dataResponse(payload))
    },
  }

  const ElMessage = (...args) => messages.push({ type: 'message', args })
  ElMessage.warning = (...args) => messages.push({ type: 'warning', args })
  ElMessage.error = (...args) => messages.push({ type: 'error', args })
  ElMessage.success = (...args) => messages.push({ type: 'success', args })

  const scope = effectScope()
  const context = vm.createContext({
    computed,
    defaultProfileConfigs,
    ElMessage,
    ElMessageBox: { confirm: async () => true },
    h,
    knowledgeBaseApi,
    onMounted: callback => mountedCallbacks.push(callback),
    profileApi,
    reactive,
    ref,
    SUPPORT_LOCALES: [{ value: 'zh', label: 'Chinese' }],
    useI18n: () => ({ t: translate }),
    useUserPermissionsStore,
    watch,
    ...memoryManagement,
    ...profileOptions,
    channelApi,
    promptApi,
    systemApi,
    adminApi,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  scope.run(() => {
    vm.runInContext(
      `${profilesScript}
  globalThis.__module = useProfilesView()
`,
      context,
      { filename: 'useProfilesView.js' },
    )
  })

  const harness = {
    api,
    messages,
    mountedCallbacks,
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
  await harness.module.loadProfiles()
  await flush()
  return harness
}

const prepareForm = (module, { type, id, ownerUid, ids, name }) => {
  module.dialogType.value = type
  module.form.id = id
  module.form.uid = ownerUid
  module.form.name = name
  module.form.prompt_id = null
  module.form.knowledge_base_ids = [...ids]
  module.form.memory_organization = {
    auto_organize_enabled: false,
    organization_channel_id: null,
    organization_model_id: null,
  }
  module.form.configs = defaultProfileConfigs()
}

const assertFormScenario = (module, { type, id, name }, ownerUid) => {
  assert.equal(module.dialogType.value, type)
  assert.equal(module.form.id, id)
  assert.equal(module.form.name, name)
  assert.equal(module.form.uid, ownerUid)
  assert.equal(module.form.memory_organization.auto_organize_enabled, false)
}

const assertOmitsKnowledgeBaseIds = payload => {
  assert.equal(Object.prototype.hasOwnProperty.call(payload, 'knowledge_base_ids'), false)
}

test('does not request knowledge bases when the current user is unknown', async t => {
  const harness = await createHarness(t)

  await harness.module.fetchKnowledgeBases()
  await flush()

  assert.equal(harness.api.knowledgeBaseList.length, 0)
  assert.equal(harness.module.knowledgeBasesReady.value, false)
  assert.deepEqual(normalize(harness.module.knowledgeBases.value), [])
})

test('ordinary create and edit submissions fetch and preserve own bindings', async t => {
  const knowledgeBases = [
    {
      id: 'kb-user-a',
      uid: 'user-a',
      name: 'Own user knowledge base',
      description: 'Own description',
      knowledge_base_type: 'user',
    },
    {
      id: 'kb-user-b',
      uid: 'user-b',
      name: 'Other user knowledge base',
      description: '',
      knowledge_base_type: 'user',
    },
    {
      id: 'kb-managed-a',
      uid: 'user-a',
      name: 'Managed knowledge base',
      description: '',
      knowledge_base_type: 'llm_managed',
    },
  ]
  const cases = [
    { type: 'create', id: null, name: 'ordinary create' },
    { type: 'edit', id: 41, name: 'ordinary edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: false,
      currentUid: 'user-a',
      knowledgeBases,
    })
    prepareForm(harness.module, {
      ...scenario,
      ownerUid: 'user-a',
      ids: ['kb-user-a'],
    })
    await flush()
    assertFormScenario(harness.module, scenario, 'user-a')

    await harness.module.fetchKnowledgeBases()
    await flush()
    const optionsAfterFirstFetch = normalize(harness.module.knowledgeBaseOptions.value)
    const idsAfterFirstFetch = normalize(harness.module.form.knowledge_base_ids)
    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.deepEqual(normalize(harness.api.knowledgeBaseList), [
      { page: 1, size: 1000 },
      { page: 1, size: 1000 },
    ], scenario.type)
    assert.equal(harness.module.knowledgeBasesReady.value, true, scenario.type)
    assert.equal(harness.module.knowledgeBasesUnavailable.value, false, scenario.type)
    assert.deepEqual(optionsAfterFirstFetch, [{
      value: 'kb-user-a',
      label: 'Own user knowledge base',
      description: 'Own description',
    }], scenario.type)
    assert.deepEqual(normalize(harness.module.knowledgeBaseOptions.value), optionsAfterFirstFetch, scenario.type)
    assert.deepEqual(normalize(harness.module.form.knowledge_base_ids), idsAfterFirstFetch, scenario.type)
    assert.deepEqual(idsAfterFirstFetch, ['kb-user-a'], scenario.type)

    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a')
    else assert.equal(harness.api.updates[0].id, scenario.id)
    assert.deepEqual(payload.knowledge_base_ids, ['kb-user-a'], scenario.type)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-user-a'], scenario.type)
  }
})

test('ordinary create dialog uses the current user and own knowledge base options', async t => {
  const harness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
    knowledgeBases: [
      {
        id: 'kb-user-a',
        uid: 'user-a',
        name: 'Own user knowledge base',
        description: 'Own description',
        knowledge_base_type: 'user',
      },
      {
        id: 'kb-user-b',
        uid: 'user-b',
        name: 'Other user knowledge base',
        description: '',
        knowledge_base_type: 'user',
      },
      {
        id: 'kb-managed-a',
        uid: 'user-a',
        name: 'Managed knowledge base',
        description: '',
        knowledge_base_type: 'llm_managed',
      },
    ],
  })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.uid, 'user-a')
  assert.deepEqual(normalize(harness.api.memorySettings), [{ uid: 'user-a' }])

  await harness.module.fetchKnowledgeBases()
  await flush()

  assert.deepEqual(normalize(harness.module.knowledgeBaseOptions.value), [{
    value: 'kb-user-a',
    label: 'Own user knowledge base',
    description: 'Own description',
  }])
})

test('superuser fetches owner knowledge bases and submits selected ids', async t => {
  const knowledgeBases = [
    {
      id: 'kb-user-a-1',
      uid: 'user-a',
      name: 'First knowledge base',
      description: '',
      knowledge_base_type: 'user',
    },
    {
      id: 'kb-user-a-2',
      uid: 'user-a',
      name: 'Second knowledge base',
      description: '',
      knowledge_base_type: 'user',
    },
  ]
  const cases = [
    { type: 'create', id: null, name: 'superuser create' },
    { type: 'edit', id: 52, name: 'superuser edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: true,
      knowledgeBases,
    })
    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.deepEqual(normalize(harness.api.knowledgeBaseList), [{ page: 1, size: 1000 }], scenario.type)
    assert.equal(harness.module.knowledgeBasesReady.value, true, scenario.type)
    assert.equal(harness.module.knowledgeBasesUnavailable.value, false, scenario.type)
    assert.deepEqual(harness.module.knowledgeBases.value.map(item => item.id), [
      'kb-user-a-1',
      'kb-user-a-2',
    ], scenario.type)

    prepareForm(harness.module, {
      ...scenario,
      ownerUid: 'user-a',
      ids: ['kb-user-a-1', 'kb-user-a-2'],
    })
    await flush()
    assertFormScenario(harness.module, scenario, 'user-a')
    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a')
    else assert.equal(harness.api.updates[0].id, scenario.id)
    assert.deepEqual(payload.knowledge_base_ids, ['kb-user-a-1', 'kb-user-a-2'], scenario.type)
  }
})

test('fetch failure keeps cached ids but omits bindings from the save payload', async t => {
  const cachedKnowledgeBases = [{
    id: 'kb-preserved',
    uid: 'user-a',
    name: 'Preserved knowledge base',
    description: '',
    knowledge_base_type: 'user',
  }]
  const cases = [
    { role: 'ordinary', superuser: false, type: 'create', id: null, name: 'ordinary failed create' },
    { role: 'ordinary', superuser: false, type: 'edit', id: 63, name: 'ordinary failed edit' },
    { role: 'superuser', superuser: true, type: 'create', id: null, name: 'superuser failed create' },
    { role: 'superuser', superuser: true, type: 'edit', id: 63, name: 'superuser failed edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: scenario.superuser,
      knowledgeBases: cachedKnowledgeBases,
      knowledgeBaseFailure: true,
    })
    harness.module.knowledgeBases.value = cachedKnowledgeBases.map(item => ({ ...item }))
    harness.module.knowledgeBasesReady.value = true
    prepareForm(harness.module, {
      ...scenario,
      ownerUid: 'user-a',
      ids: ['kb-preserved'],
    })
    await flush()
    assertFormScenario(harness.module, scenario, 'user-a')

    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.equal(harness.api.knowledgeBaseList.length, 1, scenario.role)
    assert.equal(harness.module.knowledgeBasesReady.value, false, scenario.role)
    assert.equal(harness.module.knowledgeBasesUnavailable.value, true, scenario.role)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-preserved'], scenario.role)

    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.role)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a', scenario.role)
    else assert.equal(harness.api.updates[0].id, scenario.id, scenario.role)
    assertOmitsKnowledgeBaseIds(payload)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-preserved'], scenario.role)
  }
})

test('unknown and ordinary users never request or mutate global settings', async t => {
  for (const scenario of [
    { name: 'unknown', options: {} },
    { name: 'ordinary', options: { superuser: false, currentUid: 'user-a' } },
  ]) {
    const harness = await createHarness(t, scenario.options)
    const settingsBefore = normalize(harness.module.systemSettings)
    const dialogBefore = harness.module.settingsDialogVisible.value
    const submittingBefore = harness.module.settingsSubmitting.value

    assert.equal(harness.module.systemSettings.log_redaction_enabled, false, scenario.name)
    for (const callback of harness.mountedCallbacks) callback()
    await flush()
    harness.module.handleRefresh()
    await flush()
    assert.equal(await harness.module.loadSystemSettings(), false, scenario.name)
    await harness.module.showSystemSettingsDialog()
    await harness.module.saveSystemSettings()
    await flush()

    assert.equal(harness.api.systemSettings.length, 0, scenario.name)
    assert.equal(harness.api.systemSettingsUpdates.length, 0, scenario.name)
    assert.deepEqual(normalize(harness.module.systemSettings), settingsBefore, scenario.name)
    assert.equal(harness.module.settingsDialogVisible.value, dialogBefore, scenario.name)
    assert.equal(harness.module.settingsSubmitting.value, submittingBefore, scenario.name)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], scenario.name)
  }
})

test('ordinary users and superusers load safe model options without the channel management API', async t => {
  const modelOptionsItems = [
    {
      id: 11,
      name: 'Safe channel',
      is_active: true,
      model_ids: [
        { model_id: 'CHAT', usage: 'CHAT', is_enabled: true },
        { model_id: 'EMBEDDING', usage: 'EMBEDDING', is_enabled: true },
      ],
    },
  ]

  for (const scenario of [
    { name: 'ordinary', superuser: false, currentUid: 'user-a' },
    { name: 'superuser', superuser: true, currentUid: 'admin-user' },
  ]) {
    const harness = await createHarness(t, {
      ...scenario,
      modelOptionsItems,
    })

    for (const callback of harness.mountedCallbacks) callback()
    await flush()

    assert.deepEqual(normalize(harness.api.modelOptions), [{ page: 1, size: 1000 }], scenario.name)
    assert.equal(harness.api.channelList.length, 0, scenario.name)
    assert.deepEqual(normalize(harness.module.channels.value), modelOptionsItems, scenario.name)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], scenario.name)

    const chatModel = modelOptionsItems[0].model_ids.find(model => model.usage === 'CHAT')
    harness.module.auditModelKey.value = `${modelOptionsItems[0].id}::${chatModel.model_id}`
    assert.equal(harness.module.form.configs.security.audit_channel_id, modelOptionsItems[0].id, scenario.name)
    assert.equal(harness.module.form.configs.security.audit_model_id, chatModel.model_id, scenario.name)

    harness.module.handleRefresh()
    await flush()

    assert.deepEqual(normalize(harness.api.modelOptions), [
      { page: 1, size: 1000 },
      { page: 1, size: 1000 },
    ], scenario.name)
    assert.equal(harness.api.channelList.length, 0, scenario.name)
    assert.deepEqual(normalize(harness.module.channels.value), modelOptionsItems, scenario.name)
    assert.equal(harness.module.form.configs.security.audit_channel_id, modelOptionsItems[0].id, scenario.name)
    assert.equal(harness.module.form.configs.security.audit_model_id, chatModel.model_id, scenario.name)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], scenario.name)
  }
})

test('superuser can load, open, and save global settings', async t => {
  const loadedSettings = {
    log_locale: 'en',
    temp_dir_max_size_mb: 4096,
    llm_request_max_size_mb: 128,
    audit_retention_days: 180,
    audit_report_email: 'ops@example.com',
    session_reply_max_concurrency: 12,
    log_redaction_enabled: true,
  }
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => Promise.resolve(dataResponse(loadedSettings)),
    systemSettingsUpdate: payload => dataResponse({
      ...payload,
      audit_report_email: 'saved@example.com',
    }),
  })

  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(await harness.module.loadSystemSettings(), true)
  assert.equal(harness.api.systemSettings.length, 1)
  assert.deepEqual(normalize(harness.module.systemSettings), loadedSettings)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')

  await harness.module.showSystemSettingsDialog()
  assert.equal(harness.api.systemSettings.length, 2)
  assert.equal(harness.module.settingsDialogVisible.value, true)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, true)

  harness.module.systemSettings.audit_retention_days = 365
  harness.module.systemSettings.llm_request_max_size_mb = 256
  harness.module.systemSettings.log_redaction_enabled = false
  const expectedPayload = normalize(harness.module.systemSettings)
  assert.equal(typeof expectedPayload.log_redaction_enabled, 'boolean')
  assert.equal(expectedPayload.log_redaction_enabled, false)
  await harness.module.saveSystemSettings()

  assert.deepEqual(harness.api.systemSettingsUpdates, [expectedPayload])
  assert.equal(harness.api.systemSettingsUpdates[0].llm_request_max_size_mb, 256)
  assert.equal(typeof harness.api.systemSettingsUpdates[0].log_redaction_enabled, 'boolean')
  assert.equal(harness.api.systemSettingsUpdates[0].log_redaction_enabled, false)
  assert.deepEqual(normalize(harness.module.systemSettings), {
    ...expectedPayload,
    audit_report_email: 'saved@example.com',
  })
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 256)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')
  assert.equal(harness.module.settingsDialogVisible.value, false)
  assert.equal(harness.module.settingsSubmitting.value, false)

  await harness.module.showSystemSettingsDialog()
  assert.equal(harness.api.systemSettings.length, 3)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, true)
  harness.module.systemSettings.log_redaction_enabled = false
  await harness.module.saveSystemSettings()

  assert.equal(harness.api.systemSettingsUpdates.length, 2)
  assert.equal(typeof harness.api.systemSettingsUpdates[1].log_redaction_enabled, 'boolean')
  assert.equal(harness.api.systemSettingsUpdates[1].log_redaction_enabled, false)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')
})

test('legacy global settings responses preserve the default request size across reloads and save', async t => {
  const legacySettings = {
    log_locale: 'en',
    temp_dir_max_size_mb: 4096,
    audit_retention_days: 180,
    audit_report_email: 'ops@example.com',
    session_reply_max_concurrency: 12,
  }
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => Promise.resolve(dataResponse(legacySettings)),
  })

  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(await harness.module.loadSystemSettings(), true)
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)

  assert.equal(await harness.module.loadSystemSettings(), true)
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)

  await harness.module.showSystemSettingsDialog()
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)

  await harness.module.saveSystemSettings()

  assert.equal(harness.api.systemSettingsUpdates[0].llm_request_max_size_mb, 32)
  assert.notEqual(harness.api.systemSettingsUpdates[0].llm_request_max_size_mb, undefined)
  assert.equal(typeof harness.api.systemSettingsUpdates[0].log_redaction_enabled, 'boolean')
  assert.equal(harness.api.systemSettingsUpdates[0].log_redaction_enabled, false)
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
})

test('failed global settings load does not open the dialog', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => Promise.reject(new Error('settings load failed')),
  })
  const settingsBefore = normalize(harness.module.systemSettings)

  await harness.module.showSystemSettingsDialog()

  assert.equal(harness.api.systemSettings.length, 1)
  assert.equal(harness.module.settingsDialogVisible.value, false)
  assert.deepEqual(normalize(harness.module.systemSettings), settingsBefore)
  assert.equal(harness.module.settingsSubmitting.value, false)
  assert.deepEqual(harness.messages.filter(message => message.type === 'error').map(message => message.args[0]), [
    'settings load failed',
  ])
})

test('failed global settings save preserves the request size and dialog state', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsUpdate: () => Promise.reject(new Error('settings save failed')),
  })

  await harness.module.showSystemSettingsDialog()
  harness.module.systemSettings.llm_request_max_size_mb = 256
  harness.module.systemSettings.log_redaction_enabled = true
  await harness.module.saveSystemSettings()

  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 256)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, true)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')
  assert.equal(harness.module.settingsDialogVisible.value, true)
  assert.equal(harness.module.settingsSubmitting.value, false)
  assert.deepEqual(harness.messages.filter(message => message.type === 'error').map(message => message.args[0]), [
    'settings save failed',
  ])
})

test('clearing permissions ignores late global settings responses', async t => {
  let resolveSettingsRequest
  const settingsRequest = new Promise(resolve => {
    resolveSettingsRequest = resolve
  })
  const loadHarness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => settingsRequest,
  })
  const settingsBeforeLoad = normalize(loadHarness.module.systemSettings)
  const opening = loadHarness.module.showSystemSettingsDialog()
  assert.equal(loadHarness.api.systemSettings.length, 1)

  loadHarness.permissionsStore.clear()
  resolveSettingsRequest(dataResponse({
    log_locale: 'late',
    temp_dir_max_size_mb: 8192,
    audit_retention_days: 365,
    audit_report_email: 'late@example.com',
    session_reply_max_concurrency: 32,
  }))
  await opening
  await flush()

  assert.deepEqual(normalize(loadHarness.module.systemSettings), settingsBeforeLoad)
  assert.equal(loadHarness.module.settingsDialogVisible.value, false)
  assert.equal(loadHarness.module.settingsSubmitting.value, false)
  assert.deepEqual(loadHarness.messages, [])

  let resolveSettingsUpdate
  const settingsUpdate = new Promise(resolve => {
    resolveSettingsUpdate = resolve
  })
  const saveHarness = await createHarness(t, {
    superuser: true,
    systemSettingsUpdate: () => settingsUpdate,
  })
  await saveHarness.module.showSystemSettingsDialog()
  saveHarness.module.systemSettings.log_locale = 'pending'
  const settingsBeforeSave = normalize(saveHarness.module.systemSettings)
  const saving = saveHarness.module.saveSystemSettings()
  assert.equal(saveHarness.module.settingsSubmitting.value, true)

  saveHarness.permissionsStore.clear()
  resolveSettingsUpdate(dataResponse({
    ...settingsBeforeSave,
    log_locale: 'late-save',
  }))
  await saving
  await flush()

  assert.deepEqual(saveHarness.api.systemSettingsUpdates, [settingsBeforeSave])
  assert.deepEqual(normalize(saveHarness.module.systemSettings), settingsBeforeSave)
  assert.equal(saveHarness.module.settingsDialogVisible.value, true)
  assert.equal(saveHarness.module.settingsSubmitting.value, false)
  assert.deepEqual(saveHarness.messages, [])
})

test('unknown users cannot set any profile as default', async t => {
  const harness = await createHarness(t)
  harness.module.showOwnerColumn.value = true
  harness.module.currentUid.value = 'user-a'

  for (const row of [
    { uid: 'user-a' },
    { uid: 'user-b' },
    { uid: null },
    {},
    undefined,
    null,
  ]) {
    assert.equal(harness.module.canSetDefaultProfile(row), false)
  }
})

test('ordinary users can set only their own profile as default', async t => {
  const harness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
  })

  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-a' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-b' }), false)
  assert.equal(harness.module.canSetDefaultProfile({ uid: null }), false)
  assert.equal(harness.module.canSetDefaultProfile({}), false)
  assert.equal(harness.module.canSetDefaultProfile(), false)

  harness.module.showOwnerColumn.value = true
  harness.module.currentUid.value = 'user-b'
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-a' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-b' }), false)
})

test('superusers can set profiles with any owner as default', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
  })

  assert.equal(harness.module.canSetDefaultProfile({ uid: 'admin-user' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-a' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: null }), true)
  assert.equal(harness.module.canSetDefaultProfile({}), true)
  assert.equal(harness.module.canSetDefaultProfile(), false)
})

test('clearing permissions removes default-profile permission', async t => {
  const ordinaryHarness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
  })
  const ownRow = { uid: 'user-a' }
  assert.equal(ordinaryHarness.module.canSetDefaultProfile(ownRow), true)
  ordinaryHarness.permissionsStore.clear()
  assert.equal(ordinaryHarness.module.canSetDefaultProfile(ownRow), false)

  const superuserHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
  })
  const otherRow = { uid: 'user-b' }
  assert.equal(superuserHarness.module.canSetDefaultProfile(otherRow), true)
  superuserHarness.permissionsStore.clear()
  assert.equal(superuserHarness.module.canSetDefaultProfile(otherRow), false)
})

test('superuser create form saves system-directory protection and audit depth without leaking between dialogs', async t => {
  const harness = await createHarness(t, { superuser: true })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.security.protect_system_directories, true)
  assert.equal(harness.module.form.configs.security.audit_depth, 3)

  harness.module.form.name = 'custom security settings'
  harness.module.form.configs.security.protect_system_directories = false
  harness.module.form.configs.security.audit_depth = 1
  await harness.module.submitForm()
  await flush()

  const customPayload = harness.api.creates[0]
  assert.ok(customPayload)
  assert.equal(customPayload.name, 'custom security settings')
  assert.equal(typeof customPayload.configs.security.protect_system_directories, 'boolean')
  assert.equal(customPayload.configs.security.protect_system_directories, false)
  assert.equal(customPayload.configs.security.audit_depth, 1)

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.security.protect_system_directories, true)
  assert.equal(harness.module.form.configs.security.audit_depth, 3)
  assert.equal(customPayload.configs.security.protect_system_directories, false)
  assert.equal(customPayload.configs.security.audit_depth, 1)

  harness.module.form.name = 'default security settings'
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.creates.length, 2)
  assert.equal(harness.api.creates[1].configs.security.protect_system_directories, true)
  assert.equal(harness.api.creates[1].configs.security.audit_depth, 3)
})

test('superuser editing legacy security configs supplies defaults and converts timeout without mutating the row', async t => {
  const harness = await createHarness(t, { superuser: true })
  const row = {
    id: 81,
    uid: 'user-a',
    name: 'legacy security profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      security: {
        audit_threshold: 0,
        audit_confirmation_timeout_minutes: 10,
      },
    },
  }
  const originalRow = normalize(row)

  for (let iteration = 0; iteration < 2; iteration += 1) {
    harness.module.showDialog('edit', row)
    await flush()

    assert.equal(harness.module.form.configs.security.protect_system_directories, true)
    assert.equal(harness.module.form.configs.security.audit_depth, 3)
    assert.equal(harness.module.form.configs.security.audit_threshold, 0)
    assert.equal(harness.module.form.configs.security.audit_confirmation_timeout_seconds, 600)

    await harness.module.submitForm()
    await flush()

    const update = harness.api.updates[iteration]
    assert.equal(update.id, row.id)
    assert.equal(typeof update.payload.configs.security.protect_system_directories, 'boolean')
    assert.equal(update.payload.configs.security.protect_system_directories, true)
    assert.equal(update.payload.configs.security.audit_depth, 3)
    assert.equal(update.payload.configs.security.audit_threshold, 0)
    assert.equal(update.payload.configs.security.audit_confirmation_timeout_seconds, 600)
    assert.equal(Object.prototype.hasOwnProperty.call(update.payload.configs.security, 'audit_confirmation_timeout_minutes'), false)
    assert.deepEqual(normalize(row), originalRow)
  }
})

test('editing and resaving configured audit and tool settings preserves boundary values', async t => {
  const modelOptionsItems = [{
    id: 71,
    name: 'Audit channel',
    is_active: true,
    model_ids: [{ model_id: 'audit-model', usage: 'CHAT', is_enabled: true }],
  }]
  const enabledTools = ['file_tool', 'read_multimodal_file']
  const allowedOperationDirs = ['D:/workspace/allowed']
  const scenarios = [
    { id: 91, protectSystemDirectories: false, auditDepth: 1, auditThreshold: 0 },
    { id: 92, protectSystemDirectories: true, auditDepth: 100, auditThreshold: 7 },
  ]

  for (const scenario of scenarios) {
    const harness = await createHarness(t, {
      superuser: false,
      currentUid: 'user-a',
      modelOptionsItems,
    })
    for (const callback of harness.mountedCallbacks) callback()
    await flush()

    const configs = defaultProfileConfigs()
    configs.security = {
      ...configs.security,
      audit_channel_id: modelOptionsItems[0].id,
      audit_model_id: 'audit-model',
      audit_threshold: scenario.auditThreshold,
      audit_confirmation_timeout_seconds: 420,
      audit_report_language: 'en',
      audit_depth: scenario.auditDepth,
      protect_system_directories: scenario.protectSystemDirectories,
    }
    configs.tool = {
      ...configs.tool,
      enabled_tools: [...enabledTools],
      allowed_operation_dirs: [...allowedOperationDirs],
    }
    let editableRow = {
      id: scenario.id,
      uid: 'user-a',
      name: `configured security profile ${scenario.id}`,
      prompt_id: null,
      knowledge_base_ids: [],
      configs,
    }
    const originalRow = normalize(editableRow)

    for (let iteration = 0; iteration < 2; iteration += 1) {
      harness.module.showDialog('edit', editableRow)
      await flush()

      assert.equal(harness.module.auditModelKey.value, '71::audit-model')
      assert.equal(typeof harness.module.form.configs.security.protect_system_directories, 'boolean')
      assert.equal(harness.module.form.configs.security.protect_system_directories, scenario.protectSystemDirectories)
      assert.equal(harness.module.form.configs.security.audit_depth, scenario.auditDepth)
      assert.deepEqual(normalize(harness.module.form.configs.tool.enabled_tools), enabledTools)
      assert.deepEqual(normalize(harness.module.form.configs.tool.allowed_operation_dirs), allowedOperationDirs)

      const expectedSecurity = normalize(editableRow.configs.security)
      const expectedTool = normalize(editableRow.configs.tool)
      const formSecurity = normalize(harness.module.form.configs.security)
      assert.deepEqual(formSecurity, expectedSecurity)
      await harness.module.submitForm()
      await flush()

      const update = harness.api.updates[iteration]
      assert.equal(update.id, scenario.id)
      assert.equal(Object.prototype.hasOwnProperty.call(update.payload.configs, 'security'), false)
      assert.deepEqual(normalize(harness.module.form.configs.security), formSecurity)
      assert.deepEqual(normalize(update.payload.configs.tool), expectedTool)
      assert.deepEqual(normalize(update.payload.configs.tool.enabled_tools), enabledTools)
      assert.deepEqual(normalize(update.payload.configs.tool.allowed_operation_dirs), allowedOperationDirs)
      assert.deepEqual(normalize(editableRow), originalRow)

      editableRow = {
        ...editableRow,
        configs: {
          ...normalize(update.payload.configs),
          security: expectedSecurity,
        },
      }
    }
  }
})

test('ordinary create dialog defaults image compression and keeps submitted values isolated', async t => {
  const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_enabled, true)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 500)

  harness.module.form.name = 'custom image compression profile'
  harness.module.form.configs.tool.image_compression_enabled = false
  harness.module.form.configs.tool.image_compression_threshold_kb = 1234
  await harness.module.submitForm()
  await flush()

  const firstPayload = harness.api.creates[0]
  assert.equal(firstPayload.configs.tool.image_compression_enabled, false)
  assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, 1234)

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_enabled, true)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 500)
  assert.equal(firstPayload.configs.tool.image_compression_enabled, false)
  assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, 1234)

  harness.module.form.name = 'default image compression profile'
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.creates.length, 2)
  assert.equal(harness.api.creates[1].configs.tool.image_compression_enabled, true)
  assert.equal(harness.api.creates[1].configs.tool.image_compression_threshold_kb, 500)
  assert.equal(firstPayload.configs.tool.image_compression_enabled, false)
  assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, 1234)
})

test('ordinary edit supplies image compression defaults for legacy tool configs', async t => {
  const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })
  const row = {
    id: 121,
    uid: 'user-a',
    name: 'legacy image compression profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: { tool: { tool_timeout: 45 } },
  }
  const originalRow = normalize(row)

  harness.module.showDialog('edit', row)
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_enabled, true)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 500)

  await harness.module.submitForm()
  await flush()

  const update = harness.api.updates[0]
  assert.equal(update.id, row.id)
  assert.equal(update.payload.configs.tool.image_compression_enabled, true)
  assert.equal(update.payload.configs.tool.image_compression_threshold_kb, 500)
  assert.deepEqual(normalize(row), originalRow)
})

test('ordinary edits preserve image compression values through save, reopen, and toggle changes', async t => {
  const scenarios = [
    { id: 122, enabled: false, threshold: 1 },
    { id: 123, enabled: true, threshold: 2048 },
  ]

  for (const scenario of scenarios) {
    const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })
    const row = {
      id: scenario.id,
      uid: 'user-a',
      name: `image compression profile ${scenario.id}`,
      prompt_id: null,
      knowledge_base_ids: [],
      configs: {
        tool: {
          image_compression_enabled: scenario.enabled,
          image_compression_threshold_kb: scenario.threshold,
        },
      },
    }
    const originalRow = normalize(row)

    harness.module.showDialog('edit', row)
    await flush()
    assert.equal(harness.module.form.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, scenario.threshold)
    await harness.module.submitForm()
    await flush()

    const firstPayload = harness.api.updates[0].payload
    assert.equal(firstPayload.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, scenario.threshold)

    const savedRow = { ...row, configs: normalize(firstPayload.configs) }
    harness.module.showDialog('edit', savedRow)
    await flush()
    assert.equal(harness.module.form.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, scenario.threshold)
    await harness.module.submitForm()
    await flush()

    const secondPayload = harness.api.updates[1].payload
    assert.equal(secondPayload.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(secondPayload.configs.tool.image_compression_threshold_kb, scenario.threshold)

    harness.module.form.configs.tool.image_compression_enabled = !scenario.enabled
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, scenario.threshold)
    await harness.module.submitForm()
    await flush()

    const toggledPayload = harness.api.updates[2].payload
    assert.equal(toggledPayload.configs.tool.image_compression_enabled, !scenario.enabled)
    assert.equal(toggledPayload.configs.tool.image_compression_threshold_kb, scenario.threshold)
    assert.deepEqual(normalize(row), originalRow)
  }
})

test('unknown and ordinary saves omit directly edited security without mutating the form', async t => {
  for (const identity of [
    { name: 'unknown', options: {} },
    { name: 'ordinary', options: { superuser: false, currentUid: 'user-a' } },
  ]) {
    for (const type of ['create', 'edit']) {
      const harness = await createHarness(t, identity.options)
      const configs = defaultProfileConfigs()
      configs.security.audit_depth = 2
      configs.tool.enabled_tools = ['file_tool']
      configs.other.retained_value = 'keep'
      const row = {
        id: 111,
        uid: 'user-a',
        name: `${identity.name} ${type}`,
        prompt_id: null,
        knowledge_base_ids: [],
        configs,
      }
      const originalRow = normalize(row)

      if (type === 'edit') harness.module.showDialog('edit', row)
      else harness.module.showDialog('create')
      await flush()
      harness.module.form.name = row.name
      harness.module.form.configs.security.protect_system_directories = false
      harness.module.form.configs.security.audit_depth = 11
      harness.module.form.configs.tool.enabled_tools = ['file_tool', 'read_multimodal_file']
      harness.module.form.configs.other.retained_value = 'keep'
      const formSecurity = normalize(harness.module.form.configs.security)

      await harness.module.submitForm()
      await flush()

      const payload = type === 'create'
        ? harness.api.creates[0]
        : harness.api.updates[0]?.payload
      assert.ok(payload, `${identity.name} ${type}`)
      assert.equal(Object.prototype.hasOwnProperty.call(payload.configs, 'security'), false)
      assert.deepEqual(normalize(payload.configs.tool.enabled_tools), ['file_tool', 'read_multimodal_file'])
      assert.equal(payload.configs.other.retained_value, 'keep')
      assert.deepEqual(normalize(harness.module.form.configs.security), formSecurity)
      if (type === 'edit') assert.deepEqual(normalize(row), originalRow)
    }
  }
})

test('profile config permissions default to false when unavailable', async t => {
  const unknownHarness = await createHarness(t)
  assert.deepEqual(normalize(unknownHarness.module.configPermissions.value), { security: false })

  const missingMetadataHarness = await createHarness(t, {
    superuser: true,
    profileListRequest: () => Promise.resolve(pageResponse([], 0, {
      show_owner: false,
      current_uid: 'admin-user',
      tool_options: [],
    })),
  })
  assert.deepEqual(normalize(missingMetadataHarness.module.configPermissions.value), { security: false })
})

test('superuser can save security configs granted by profile metadata', async t => {
  const harness = await createHarness(t, { superuser: true })
  assert.equal(harness.module.configPermissions.value.security, true)
  harness.module.showDialog('create')
  await flush()
  harness.module.form.name = 'permitted security profile'
  harness.module.form.configs.security.audit_depth = 9
  const formSecurity = normalize(harness.module.form.configs.security)

  await harness.module.submitForm()
  await flush()

  assert.deepEqual(normalize(harness.api.creates[0].configs.security), formSecurity)
})

test('point config permissions omit only the denied tool field', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    configPermissions: { 'tool.max_parallel_tools': false },
  })
  harness.module.showDialog('create')
  await flush()
  harness.module.form.name = 'point permission profile'
  harness.module.form.configs.tool.max_parallel_tools = 17
  harness.module.form.configs.tool.enabled_tools = ['file_tool']
  harness.module.form.configs.tool.allowed_operation_dirs = ['D:/workspace/allowed']
  harness.module.form.configs.tool.file_send_blocked_extensions = ['.zip']

  await harness.module.submitForm()
  await flush()

  const { tool } = harness.api.creates[0].configs
  assert.equal(Object.prototype.hasOwnProperty.call(tool, 'max_parallel_tools'), false)
  assert.deepEqual(normalize(tool.enabled_tools), ['file_tool'])
  assert.deepEqual(normalize(tool.allowed_operation_dirs), ['D:/workspace/allowed'])
  assert.deepEqual(normalize(tool.file_send_blocked_extensions), ['.zip'])
})

test('repeated profile submissions do not progressively mutate denied configs', async t => {
  const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })
  const row = {
    id: 112,
    uid: 'user-a',
    name: 'repeated denied security profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: defaultProfileConfigs(),
  }
  row.configs.security.audit_depth = 4
  row.configs.tool.enabled_tools = ['file_tool']
  const originalRow = normalize(row)
  harness.module.showDialog('edit', row)
  await flush()
  harness.module.form.configs.security.audit_depth = 12
  const formConfigs = normalize(harness.module.form.configs)

  await harness.module.submitForm()
  await flush()
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.updates.length, 2)
  assert.deepEqual(normalize(harness.api.updates[0].payload), normalize(harness.api.updates[1].payload))
  assert.equal(Object.prototype.hasOwnProperty.call(harness.api.updates[0].payload.configs, 'security'), false)
  assert.deepEqual(normalize(harness.module.form.configs), formConfigs)
  assert.deepEqual(normalize(row), originalRow)
})

test('new and legacy profiles keep missing auxiliary channel rules empty and independent', async t => {
  const chatConfig = {
    ...defaultProfileConfigs().channel.chat_channel,
    chat_timeout: 111,
    rules: [{ channel_id: 41, model_id: 'chat-model', reasoning_effort: 'low', priority: 1, weight: 2 }],
  }
  const summaryConfig = {
    ...defaultProfileConfigs().channel.context_summary_channel,
    chat_timeout: 222,
    rules: [{ channel_id: 42, model_id: 'summary-model', reasoning_effort: 'medium', priority: 1, weight: 2 }],
  }
  const expectedAuxiliary = normalize(defaultProfileConfigs().channel.auxiliary_channel)
  const legacyRow = {
    id: 113,
    uid: 'user-a',
    name: 'legacy profile without auxiliary channel',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      channel: {
        chat_channel: normalize(chatConfig),
        context_summary_channel: normalize(summaryConfig),
      },
    },
  }
  const originalLegacyRow = normalize(legacyRow)

  for (const scenario of [
    { type: 'create', name: 'new profile' },
    { type: 'edit', name: 'legacy profile', row: legacyRow },
  ]) {
    const harness = await createHarness(t)
    if (scenario.type === 'create') harness.module.showDialog('create')
    else harness.module.showDialog('edit', scenario.row)
    await flush()

    if (scenario.type === 'create') {
      harness.module.form.configs.channel.chat_channel = normalize(chatConfig)
      harness.module.form.configs.channel.context_summary_channel = normalize(summaryConfig)
    }

    const auxiliary = harness.module.form.configs.channel.auxiliary_channel
    assert.deepEqual(normalize(auxiliary), expectedAuxiliary, scenario.type)
    assert.deepEqual(normalize(auxiliary.rules), [], scenario.type)
    assert.notEqual(auxiliary, harness.module.form.configs.channel.chat_channel, scenario.type)
    assert.notEqual(auxiliary, harness.module.form.configs.channel.context_summary_channel, scenario.type)

    harness.module.form.name = scenario.name
    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    assert.deepEqual(normalize(payload.configs.channel.auxiliary_channel), expectedAuxiliary, scenario.type)
    assert.deepEqual(normalize(payload.configs.channel.chat_channel), chatConfig, scenario.type)
    assert.deepEqual(normalize(payload.configs.channel.context_summary_channel), summaryConfig, scenario.type)
    if (scenario.type === 'edit') assert.deepEqual(normalize(legacyRow), originalLegacyRow)
  }
})

test('configured auxiliary channel rules survive cleanup, save, reopen, and clearing', async t => {
  const chatConfig = {
    ...defaultProfileConfigs().channel.chat_channel,
    chat_timeout: 111,
    rules: [{ channel_id: 41, model_id: 'chat-model', reasoning_effort: 'low', priority: 1, weight: 2 }],
  }
  const summaryConfig = {
    ...defaultProfileConfigs().channel.context_summary_channel,
    chat_timeout: 222,
    rules: [{ channel_id: 42, model_id: 'summary-model', reasoning_effort: 'medium', priority: 1, weight: 2 }],
  }
  const expectedRules = [
    { channel_id: 12, model_id: 'aux-first', priority: 10, weight: 0, reasoning_effort: 'none' },
    { channel_id: 11, model_id: 'aux-later', priority: 20, weight: 2, reasoning_effort: 'high' },
  ]
  const expectedAuxiliary = {
    ...defaultProfileConfigs().channel.auxiliary_channel,
    chat_timeout: 73,
    rules: expectedRules,
  }
  const row = {
    id: 114,
    uid: 'user-a',
    name: 'configured auxiliary channel profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      channel: {
        chat_channel: normalize(chatConfig),
        context_summary_channel: normalize(summaryConfig),
        auxiliary_channel: {
          chat_timeout: 73,
          rules: [
            { channel_id: 11, model_id: 'aux-later', reasoning_effort: ' high ', priority: 20, weight: 2 },
            { channel_id: 13, model_id: '', reasoning_effort: 'low', priority: 5, weight: 1 },
            { channel_id: 12, model_id: 'aux-first', reasoning_effort: ' none ', priority: 10, weight: 0 },
            { channel_id: null, model_id: 'orphan-model', reasoning_effort: 'medium', priority: 3, weight: 1 },
          ],
        },
      },
    },
  }
  const originalRow = normalize(row)
  const harness = await createHarness(t)

  harness.module.showDialog('edit', row)
  await flush()
  assert.equal(harness.module.form.configs.channel.auxiliary_channel.chat_timeout, 73)
  assert.equal(harness.module.form.configs.channel.auxiliary_channel.rerank_timeout, expectedAuxiliary.rerank_timeout)
  assert.equal(harness.module.form.configs.channel.auxiliary_channel.rules.length, 4)

  await harness.module.submitForm()
  await flush()

  const firstPayload = harness.api.updates[0].payload
  assert.deepEqual(normalize(firstPayload.configs.channel.auxiliary_channel), expectedAuxiliary)
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), expectedAuxiliary)
  assert.deepEqual(normalize(firstPayload.configs.channel.chat_channel), chatConfig)
  assert.deepEqual(normalize(firstPayload.configs.channel.context_summary_channel), summaryConfig)
  assert.deepEqual(normalize(row), originalRow)

  let savedRow = { ...row, configs: normalize(firstPayload.configs) }
  harness.module.showDialog('edit', savedRow)
  await flush()
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), expectedAuxiliary)

  await harness.module.submitForm()
  await flush()

  const secondPayload = harness.api.updates[1].payload
  assert.deepEqual(normalize(secondPayload.configs.channel), normalize(firstPayload.configs.channel))
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), expectedAuxiliary)
  assert.deepEqual(normalize(row), originalRow)

  const clearedAuxiliary = { ...expectedAuxiliary, rules: [] }
  harness.module.form.configs.channel.auxiliary_channel.rules = []
  await harness.module.submitForm()
  await flush()

  const clearedPayload = harness.api.updates[2].payload
  assert.deepEqual(normalize(clearedPayload.configs.channel.auxiliary_channel), clearedAuxiliary)
  savedRow = { ...row, configs: normalize(clearedPayload.configs) }
  harness.module.showDialog('edit', savedRow)
  await flush()
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), clearedAuxiliary)
  assert.deepEqual(normalize(row), originalRow)
})

test('clearing permissions ignores late profile list permissions and rows', async t => {
  let resolveLateList
  let listRequestCount = 0
  const harness = await createHarness(t, {
    superuser: true,
    profileListRequest: () => {
      listRequestCount += 1
      if (listRequestCount === 1) {
        return Promise.resolve(pageResponse([], 0, {
          show_owner: false,
          current_uid: 'admin-user',
          tool_options: [],
          config_permissions: { security: true },
        }))
      }
      return new Promise(resolve => {
        resolveLateList = resolve
      })
    },
  })
  assert.equal(harness.module.configPermissions.value.security, true)

  const loading = harness.module.loadProfiles()
  assert.equal(harness.api.profileList.length, 2)
  harness.permissionsStore.clear()
  await flush()
  assert.deepEqual(normalize(harness.module.configPermissions.value), { security: false })

  resolveLateList(pageResponse([{ id: 999, name: 'stale admin profile' }], 1, {
    show_owner: true,
    current_uid: 'admin-user',
    tool_options: [],
    config_permissions: { security: true },
  }))
  await loading
  await flush()

  assert.deepEqual(normalize(harness.module.configPermissions.value), { security: false })
  assert.deepEqual(normalize(harness.module.profiles.value), [])
  assert.equal(harness.module.total.value, 0)
})
