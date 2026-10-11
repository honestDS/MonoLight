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

const profilesViewSource = readFileSync(
  new URL('../src/views/ProfilesView.vue', import.meta.url),
  'utf8',
)
const scriptMatch = profilesViewSource.match(/<script\s+setup>\r?\n([\s\S]*?)\r?\n<\/script>/)
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

const profilesScript = stripImports(scriptMatch[1])
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
      return Promise.resolve(pageResponse([], 0, {
        show_owner: false,
        current_uid: currentUid,
        tool_options: [],
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
    channelApi: {},
    promptApi: {},
    systemApi: {},
    adminApi: {},
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  scope.run(() => {
    vm.runInContext(
      `${profilesScript}
globalThis.__module = {
  fetchKnowledgeBases,
  submitForm,
  form,
  dialogType,
  knowledgeBases,
  knowledgeBasesReady,
  knowledgeBasesLoading,
  knowledgeBasesUnavailable,
  knowledgeBaseOptions,
  userPermissions,
}
`,
      context,
      { filename: 'ProfilesView.vue?script-setup' },
    )
  })

  const harness = {
    api,
    messages,
    mountedCallbacks,
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

test('does not request knowledge bases for unknown or ordinary permissions', async t => {
  for (const [label, superuser] of [['unknown', undefined], ['ordinary', false]]) {
    const harness = await createHarness(t, { superuser })

    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.equal(harness.api.knowledgeBaseList.length, 0, label)
    assert.equal(harness.module.knowledgeBasesReady.value, false, label)
    assert.deepEqual(normalize(harness.module.knowledgeBases.value), [], label)
  }
})

test('ordinary create and edit submissions omit bindings and preserve cached ids', async t => {
  const cachedKnowledgeBases = [{
    id: 'kb-existing',
    uid: 'user-a',
    name: 'Existing knowledge base',
    description: '',
    knowledge_base_type: 'user',
  }]
  const cases = [
    { type: 'create', id: null, name: 'ordinary create' },
    { type: 'edit', id: 41, name: 'ordinary edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: false,
      knowledgeBases: cachedKnowledgeBases,
    })
    harness.module.knowledgeBases.value = cachedKnowledgeBases.map(item => ({ ...item }))
    harness.module.knowledgeBasesReady.value = true
    prepareForm(harness.module, {
      ...scenario,
      ownerUid: 'user-a',
      ids: ['kb-existing'],
    })
    await flush()
    assertFormScenario(harness.module, scenario, 'user-a')

    await harness.module.fetchKnowledgeBases()
    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.equal(harness.api.knowledgeBaseList.length, 0, scenario.type)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-existing'], scenario.type)

    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a')
    else assert.equal(harness.api.updates[0].id, scenario.id)
    assertOmitsKnowledgeBaseIds(payload)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-existing'], scenario.type)
  }
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

test('superuser fetch failure keeps ids but omits bindings from the save payload', async t => {
  const cachedKnowledgeBases = [{
    id: 'kb-preserved',
    uid: 'user-a',
    name: 'Preserved knowledge base',
    description: '',
    knowledge_base_type: 'user',
  }]
  const cases = [
    { type: 'create', id: null, name: 'failed create' },
    { type: 'edit', id: 63, name: 'failed edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: true,
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

    assert.equal(harness.api.knowledgeBaseList.length, 1, scenario.type)
    assert.equal(harness.module.knowledgeBasesReady.value, false, scenario.type)
    assert.equal(harness.module.knowledgeBasesUnavailable.value, true, scenario.type)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-preserved'], scenario.type)

    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a')
    else assert.equal(harness.api.updates[0].id, scenario.id)
    assertOmitsKnowledgeBaseIds(payload)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-preserved'], scenario.type)
  }
})
