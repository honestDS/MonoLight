import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import {
  computed,
  effectScope,
  nextTick,
  reactive,
  ref,
} from 'vue'
import { loadUseConfirmDialog } from '../../confirmDialogHarness.js'

const promptsViewSource = readFileSync(new URL('../../../src/views/PromptsView.vue', import.meta.url), 'utf8')
const promptsViewScriptMatch = promptsViewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!promptsViewScriptMatch) throw new Error('PromptsView.vue script setup was not found')

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

const response = (items = [], total = items.length, meta = {}) => ({
  data: {
    data: {
      items,
      total,
      meta,
    },
  },
})

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const conflictError = () => Object.assign(new Error('prompt is in use'), {
  response: { status: 409 },
})

const snapshot = value => JSON.parse(JSON.stringify(value))

const flush = async () => {
  for (let index = 0; index < 8; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const createMessage = messages => {
  const message = value => messages.push({ type: 'message', value })
  message.warning = value => messages.push({ type: 'warning', value })
  message.error = value => messages.push({ type: 'error', value })
  message.success = value => messages.push({ type: 'success', value })
  return message
}

const createHarness = (t, options = {}) => {
  const messages = []
  const confirmations = []
  const mountedCallbacks = []
  const locale = reactive({
    nameLabel: 'Name',
    ownerUnassigned: 'Unassigned',
  })
  const permissions = reactive({
    currentUser: null,
    isSuperuser: false,
  })
  const apiState = {
    listRequests: [],
    userListRequests: [],
    createRequests: [],
    updateRequests: [],
    deleteRequests: [],
    confirmHandler: options.confirmHandler || (async () => 'confirm'),
    listHandler: options.listHandler || (async () => options.listResponse || response()),
    userListHandler: options.userListHandler || (async () => ({ data: { data: { items: [], total: 0 } } })),
    createHandler: options.createHandler || (async () => ({ data: { data: {} } })),
    updateHandler: options.updateHandler || (async () => ({ data: { data: {} } })),
    deleteHandler: options.deleteHandler || (async () => ({ data: { data: {} } })),
  }

  const promptApi = {
    list: async params => {
      const request = { params: { ...params } }
      apiState.listRequests.push(request)
      return apiState.listHandler(request.params)
    },
    create: async payload => {
      const request = { payload }
      apiState.createRequests.push(request)
      return apiState.createHandler(request)
    },
    update: async (id, payload) => {
      const request = { id, payload }
      apiState.updateRequests.push(request)
      return apiState.updateHandler(request)
    },
    delete: async (id, params) => {
      const request = { id, params }
      apiState.deleteRequests.push(request)
      return apiState.deleteHandler(request)
    },
  }
  const adminApi = {
    userList: async params => {
      const request = { params: { ...params } }
      apiState.userListRequests.push(request)
      return apiState.userListHandler(request.params)
    },
  }
  const ElMessage = createMessage(messages)
  const ElMessageBox = {
    confirm: async (...args) => {
      confirmations.push(args)
      return apiState.confirmHandler(args)
    },
  }
  const translate = options.translate || (key => {
    if (key === 'prompts.name') return locale.nameLabel
    if (key === 'prompts.owner_unassigned') return locale.ownerUnassigned
    return key
  })
  const actualUseConfirmDialog = loadUseConfirmDialog({ t: translate, ElMessageBox })
  const context = vm.createContext({
    computed,
    reactive,
    ref,
    onMounted: callback => mountedCallbacks.push(callback),
    ElMessage,
    ElMessageBox,
    adminApi,
    promptApi,
    useI18n: () => ({ t: translate }),
    useConfirmDialog: actualUseConfirmDialog,
    useUserPermissionsStore: () => permissions,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  const scope = effectScope()
  let moduleBindings
  scope.run(() => {
    vm.runInContext(
      `${stripImports(promptsViewScriptMatch[1])}
globalThis.__module = {
  prompts,
  loading,
  total,
  currentPage,
  pageSize,
  currentUid,
  dialogVisible,
  deleteSubmitting,
  pendingDeletePrompt,
  replacementPromptId,
  replacementPromptOptions,
  sameOwnerPromptOptions,
  reassignDialogVisible,
  nameColumnWidth,
  form,
  submitForm,
  submitting,
  ownerUsers,
  ownerUsersLoading,
  ownerUsersError,
  fetchOwnerUsers,
  getOwnerLabel,
  canManagePrompt,
  showDialog,
  deletePrompt,
  confirmReassignAndDelete,
  loadPrompts
}
`,
      context,
      { filename: 'PromptsView.vue?script-setup' },
    )
    moduleBindings = context.__module
  })

  for (const callback of mountedCallbacks) callback()

  const harness = {
    apiState,
    confirmations,
    locale,
    messages,
    module: moduleBindings,
    permissions,
    async flush() {
      await flush()
    },
    unmounted: false,
    unmount() {
      if (this.unmounted) return
      this.unmounted = true
      scope.stop()
    },
  }

  t.after(() => harness.unmount())
  return harness
}

export {
  conflictError,
  createDeferred,
  createHarness,
  response,
  snapshot,
}
