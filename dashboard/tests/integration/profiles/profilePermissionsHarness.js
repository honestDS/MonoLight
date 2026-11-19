import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createPinia, setActivePinia } from 'pinia'
import {
  computed,
  effectScope,
  nextTick,
  reactive,
  ref,
  watch,
} from 'vue'
import { createI18n } from 'vue-i18n'
import { defaultProfileConfigs } from '../../../src/constants/index.js'
import { useUserPermissionsStore } from '../../../src/composables/useUserPermissions.js'
import enCommon from '../../../src/i18n/locales/en/common.js'
import enProfiles from '../../../src/i18n/locales/en/profiles.js'
import zhCommon from '../../../src/i18n/locales/zh/common.js'
import zhProfiles from '../../../src/i18n/locales/zh/profiles.js'
import * as memoryManagement from '../../../src/utils/memoryManagement.js'
import * as profileOptions from '../../../src/utils/profileOptions.js'
import { loadUseConfirmDialog } from '../../confirmDialogHarness.js'

const profileMemorySource = readFileSync(
  new URL('../../../src/composables/profiles/useProfileMemory.js', import.meta.url),
  'utf8',
)
const profileEditorSource = readFileSync(
  new URL('../../../src/composables/profiles/useProfileEditor.js', import.meta.url),
  'utf8',
)
const profilesViewSource = readFileSync(
  new URL('../../../src/composables/profiles/useProfilesView.js', import.meta.url),
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
export const dataResponse = data => ({ data: { data } })
export const normalize = value => JSON.parse(JSON.stringify(value))
export const pageResponse = (items, total, meta) => dataResponse({ items, total, meta })
export const IMAGE_COMPRESSION_DEFAULT_KB = 768
export const flush = async () => {
  for (let index = 0; index < 6; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const translate = key => String(key)

export const createProfilesTranslate = (locale = 'zh') => {
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: {
      zh: { common: zhCommon, profiles: zhProfiles },
      en: { common: enCommon, profiles: enProfiles },
    },
  })
  return (...args) => i18n.global.t(
    ...args.map(value => (value && typeof value === 'object' ? normalize(value) : value)),
  )
}

export const createHarness = async (t, options = {}) => {
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
    deletes: [],
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
        config_defaults: {
          tool: { image_compression_threshold_kb: IMAGE_COMPRESSION_DEFAULT_KB },
        },
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
    delete: (id, params) => {
      api.deletes.push({ id, params: params === undefined ? undefined : normalize(params) })
      if (options.deleteRequest) return options.deleteRequest(id, params)
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
    log_retention_days: 7,
    log_max_entries: 100000,
    temp_dir_max_size_mb: 2048,
    llm_request_max_size_mb: 64,
    audit_retention_days: 120,
    audit_report_email: 'audit@example.com',
    agent_work_max_concurrency: 8,
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

  const confirmations = []
  const ElMessage = (...args) => messages.push({ type: 'message', args })
  ElMessage.warning = (...args) => messages.push({ type: 'warning', args })
  ElMessage.error = (...args) => messages.push({ type: 'error', args })
  ElMessage.success = (...args) => messages.push({ type: 'success', args })
  const ElMessageBox = {
    confirm: async (message, title, confirmOptions) => {
      const confirmation = { message, title, options: confirmOptions }
      confirmations.push(confirmation)
      const result = typeof options.confirmDelete === 'function'
        ? await options.confirmDelete(confirmation)
        : options.confirmDelete ?? true
      if (result === false || result === 'cancel' || result === 'close') throw result
      return result
    },
  }
  const translateFn = options.translate ?? translate

  const scope = effectScope()
  const context = vm.createContext({
    computed,
    defaultProfileConfigs,
    ElMessage,
    ElMessageBox,
    knowledgeBaseApi,
    onMounted: callback => mountedCallbacks.push(callback),
    profileApi,
    reactive,
    ref,
    SUPPORT_LOCALES: [{ value: 'zh', label: 'Chinese' }],
    useI18n: () => ({ t: translateFn }),
    useConfirmDialog: loadUseConfirmDialog({ t: translateFn, ElMessageBox }),
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
    confirmations,
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

export const prepareForm = (module, { type, id, ownerUid, ids, name }) => {
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

export const assertFormScenario = (module, { type, id, name }, ownerUid) => {
  assert.equal(module.dialogType.value, type)
  assert.equal(module.form.id, id)
  assert.equal(module.form.name, name)
  assert.equal(module.form.uid, ownerUid)
  assert.equal(module.form.memory_organization.auto_organize_enabled, false)
}

export const assertOmitsKnowledgeBaseIds = payload => {
  assert.equal(Object.prototype.hasOwnProperty.call(payload, 'knowledge_base_ids'), false)
}
