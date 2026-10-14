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
} from 'vue'

const promptsViewSource = readFileSync(new URL('../src/views/PromptsView.vue', import.meta.url), 'utf8')
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
  const translate = key => {
    if (key === 'prompts.name') return locale.nameLabel
    if (key === 'prompts.owner_unassigned') return locale.ownerUnassigned
    return key
  }
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

test('gates ordinary prompt management by verified ownership and trusted list metadata', async t => {
  const harness = createHarness(t)
  await harness.flush()

  const ownPrompt = { id: 1, uid: 'user-a', name: 'own' }
  const foreignPrompt = { id: 2, uid: 'user-b', name: 'foreign' }
  const unassignedPrompt = { id: 3, uid: null, name: 'unassigned' }

  harness.permissions.currentUser = { uid: 'user-a' }
  assert.equal(harness.module.canManagePrompt(ownPrompt), true)
  assert.equal(harness.module.canManagePrompt(foreignPrompt), false)
  assert.equal(harness.module.canManagePrompt(unassignedPrompt), false)
  assert.equal(harness.module.canManagePrompt(null), false)

  harness.permissions.isSuperuser = 'true'
  harness.permissions.currentUser = { uid: 'user-c' }
  assert.equal(harness.module.canManagePrompt(ownPrompt), false)

  harness.permissions.isSuperuser = false
  harness.permissions.currentUser = null
  harness.module.currentUid.value = null
  assert.equal(harness.module.canManagePrompt(ownPrompt), false)

  harness.module.currentUid.value = 'user-a'
  assert.equal(harness.module.canManagePrompt(ownPrompt), true)
  assert.equal(harness.module.canManagePrompt(foreignPrompt), false)
})

test('allows a real superuser to manage every non-empty prompt record only', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.isSuperuser = true

  assert.equal(harness.module.canManagePrompt({ id: 1, uid: 'user-a' }), true)
  assert.equal(harness.module.canManagePrompt({ id: 2, uid: 'user-b' }), true)
  assert.equal(harness.module.canManagePrompt({ id: 3, uid: null }), true)
  assert.equal(harness.module.canManagePrompt(null), false)
  assert.equal(harness.module.canManagePrompt(undefined), false)
})

test('short-circuits unauthorized edit and delete entries without confirmation or requests', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  const foreignPrompt = { id: 9, uid: 'user-b', name: 'foreign' }

  harness.module.showDialog('edit', foreignPrompt)
  assert.equal(harness.module.dialogVisible.value, false)
  await harness.module.deletePrompt(foreignPrompt)

  assert.equal(harness.confirmations.length, 0)
  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.apiState.deleteRequests.length, 0)
  assert.deepEqual(
    harness.messages.filter(message => message.type === 'warning').map(message => message.value),
    ['prompts.permission_denied', 'prompts.permission_denied'],
  )
})

test('resolves owner labels from username to uid to the localized unassigned label', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.locale.ownerUnassigned = 'Localized owner'

  assert.equal(
    harness.module.getOwnerLabel({ username: 'alice', uid: 'user-a' }),
    'alice',
  )
  assert.equal(
    harness.module.getOwnerLabel({ username: '', uid: 'user-a' }),
    'user-a',
  )
  assert.equal(
    harness.module.getOwnerLabel({ username: null, uid: null }),
    'Localized owner',
  )
})

test('loads prompt items, total, and current uid from a successful list response', async t => {
  const items = [
    { id: 1, name: 'first', content: 'one', uid: 'user-a', username: 'alice' },
    { id: 2, name: 'second', content: 'two', uid: 'user-b', username: 'bob' },
  ]
  const harness = createHarness(t, {
    listResponse: response(items, 7, { current_uid: 'user-a' }),
  })
  await harness.flush()

  assert.deepEqual(snapshot(harness.module.prompts.value), items)
  assert.equal(harness.module.total.value, 7)
  assert.equal(harness.module.currentUid.value, 'user-a')
  assert.equal(harness.module.loading.value, false)
  assert.deepEqual(harness.apiState.listRequests[0].params, { page: 1, size: 10 })

  harness.permissions.currentUser = null
  harness.permissions.isSuperuser = false
  assert.equal(harness.module.canManagePrompt(items[0]), true)
  assert.equal(harness.module.canManagePrompt(items[1]), false)
})

test('clears stale list state and resets loading when loading prompts fails', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.module.prompts.value = [{ id: 1, name: 'stale', uid: 'user-a' }]
  harness.module.total.value = 4
  harness.module.currentUid.value = 'old-user'
  const error = new Error('list failed')
  harness.apiState.listHandler = async () => {
    throw error
  }

  const load = harness.module.loadPrompts()
  assert.equal(harness.module.loading.value, true)
  await load

  assert.deepEqual(snapshot(harness.module.prompts.value), [])
  assert.equal(harness.module.total.value, 0)
  assert.equal(harness.module.currentUid.value, null)
  assert.equal(harness.module.loading.value, false)
  assert.ok(harness.messages.some(message => message.type === 'error' && message.value === 'list failed'))
})

test('sizes the name column from current-page names and a reactive localized header', async t => {
  const harness = createHarness(t)
  await harness.flush()

  assert.equal(harness.module.nameColumnWidth.value, 120)

  const shortPrompt = { name: 'short' }
  harness.module.prompts.value = [shortPrompt]
  assert.equal(harness.module.nameColumnWidth.value, 120)

  const asciiPrompt = { name: 'a'.repeat(13) }
  harness.module.prompts.value = [asciiPrompt]
  assert.equal(harness.module.nameColumnWidth.value, 128)
  assert.equal(asciiPrompt.name, 'a'.repeat(13))

  const unicodePrompt = { name: '中'.repeat(7) }
  harness.module.prompts.value = [unicodePrompt]
  assert.equal(harness.module.nameColumnWidth.value, 129)
  assert.equal(unicodePrompt.name, '中'.repeat(7))

  harness.module.prompts.value = [{ name: 'a'.repeat(100) }]
  assert.equal(harness.module.nameColumnWidth.value, 480)

  harness.module.prompts.value = [shortPrompt]
  assert.equal(harness.module.nameColumnWidth.value, 120)

  harness.locale.nameLabel = 'abcdefghij'
  assert.equal(harness.module.nameColumnWidth.value, 126)
  harness.locale.nameLabel = 'Name'
  assert.equal(harness.module.nameColumnWidth.value, 120)
})

test('creates an ordinary prompt for its own identity without sending client ownership', async t => {
  for (const isSuperuser of [false, 'true']) {
    const harness = createHarness(t)
    await harness.flush()
    harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
    harness.permissions.isSuperuser = isSuperuser

    harness.module.showDialog('create')
    assert.equal(harness.module.form.uid, 'user-a')
    assert.equal(harness.module.form.username, 'Alice')
    assert.equal(harness.apiState.userListRequests.length, 0)

    harness.module.form.uid = 'foreign-user'
    harness.module.form.name = `ordinary-${String(isSuperuser)}`
    harness.module.form.content = 'content'
    await harness.module.submitForm()

    assert.equal(harness.apiState.userListRequests.length, 0)
    assert.deepEqual(snapshot(harness.apiState.createRequests.map(request => request.payload)), [
      {
        name: `ordinary-${String(isSuperuser)}`,
        content: 'content',
      },
    ])
  }
})

test('loads all owners before a superuser can submit a selected owner', async t => {
  const firstPage = createDeferred()
  const admin = { uid: 'admin', username: 'Admin' }
  const userB = { uid: 'user-b', username: 'Bob' }
  const harness = createHarness(t)
  await harness.flush()
  harness.apiState.userListHandler = params => {
    if (params.page === 1) return firstPage.promise
    return Promise.resolve(response([userB], 2))
  }
  harness.permissions.currentUser = admin
  harness.permissions.isSuperuser = true

  harness.module.showDialog('create')
  assert.equal(harness.module.form.uid, 'admin')
  assert.equal(harness.module.ownerUsersLoading.value, true)
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
  ])

  harness.module.form.name = 'admin prompt'
  harness.module.form.content = 'prompt content'
  await harness.module.submitForm()
  assert.equal(harness.apiState.createRequests.length, 0)

  firstPage.resolve(response([admin], 2))
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.deepEqual(snapshot(harness.module.ownerUsers.value), [admin, userB])
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
    { page: 2, size: 1000 },
  ])

  harness.module.form.uid = 'user-b'
  await harness.module.submitForm()
  assert.deepEqual(snapshot(harness.apiState.createRequests.map(request => request.payload)), [
    {
      name: 'admin prompt',
      content: 'prompt content',
      uid: 'user-b',
    },
  ])
})

test('blocks creation after owner loading fails until a retry succeeds', async t => {
  const admin = { uid: 'admin', username: 'Admin' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = admin
  harness.permissions.isSuperuser = true
  harness.apiState.userListHandler = async () => {
    throw new Error('owner list failed')
  }

  harness.module.showDialog('create')
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, 'prompts.load_users_failed')
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'prompts.load_users_failed'
  )))

  harness.module.form.name = 'retry prompt'
  harness.module.form.content = 'retry content'
  await harness.module.submitForm()
  assert.equal(harness.apiState.createRequests.length, 0)

  harness.apiState.userListHandler = async () => response([admin], 1)
  await harness.module.fetchOwnerUsers()
  assert.equal(harness.module.ownerUsersError.value, '')
  assert.deepEqual(snapshot(harness.module.ownerUsers.value), [admin])

  await harness.module.submitForm()
  assert.deepEqual(snapshot(harness.apiState.createRequests.map(request => request.payload)), [
    {
      name: 'retry prompt',
      content: 'retry content',
      uid: 'admin',
    },
  ])
})

test('blocks missing owner and required fields without creating a prompt', async t => {
  const missingOwner = createHarness(t)
  await missingOwner.flush()
  missingOwner.permissions.currentUser = { uid: null, username: 'Admin' }
  missingOwner.permissions.isSuperuser = true
  missingOwner.module.showDialog('create')
  await missingOwner.flush()
  missingOwner.module.form.name = 'missing owner'
  missingOwner.module.form.content = 'content'
  missingOwner.module.form.uid = null
  await missingOwner.module.submitForm()
  assert.equal(missingOwner.apiState.createRequests.length, 0)
  assert.ok(missingOwner.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_owner'
  )))

  const missingFields = createHarness(t)
  await missingFields.flush()
  missingFields.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  missingFields.permissions.isSuperuser = false
  missingFields.module.showDialog('create')
  missingFields.module.form.name = ''
  missingFields.module.form.content = 'content'
  await missingFields.module.submitForm()
  assert.equal(missingFields.apiState.createRequests.length, 0)
  assert.ok(missingFields.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.fill_required'
  )))
})

test('submits edits without changing ownership and rechecks the current identity', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  const prompt = {
    id: 17,
    name: 'old name',
    content: 'old content',
    uid: 'user-a',
    username: 'Alice',
  }

  harness.module.showDialog('edit', prompt)
  harness.module.form.name = 'new name'
  harness.module.form.content = 'new content'
  await harness.module.submitForm()

  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: 'new name',
    content: 'new content',
  })
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.form.username, 'Alice')

  harness.permissions.currentUser = { uid: 'user-b', username: 'Bob' }
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.form.username, 'Alice')
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.permission_denied'
  )))
})

test('prevents duplicate creation during a slow request and refreshes after success', async t => {
  const createRequest = createDeferred()
  const harness = createHarness(t, { createHandler: () => createRequest.promise })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  harness.module.showDialog('create')
  harness.module.form.name = 'slow prompt'
  harness.module.form.content = 'slow content'

  const firstSubmit = harness.module.submitForm()
  assert.equal(harness.module.submitting.value, true)
  await harness.module.submitForm()
  assert.equal(harness.apiState.createRequests.length, 1)

  createRequest.resolve(response())
  await firstSubmit
  await harness.flush()
  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.module.dialogVisible.value, false)
  assert.equal(harness.apiState.createRequests.length, 1)
  assert.equal(harness.apiState.listRequests.length, 2)
})

test('retains the form after a failed creation and supports retry', async t => {
  const failedRequest = createDeferred()
  let attempt = 0
  const harness = createHarness(t, {
    createHandler: () => {
      attempt += 1
      if (attempt === 1) return failedRequest.promise
      return Promise.resolve(response())
    },
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  harness.module.showDialog('create')
  harness.module.form.name = 'retryable prompt'
  harness.module.form.content = 'retryable content'

  const failedSubmit = harness.module.submitForm()
  assert.equal(harness.module.submitting.value, true)
  failedRequest.reject(new Error('save failed'))
  await failedSubmit
  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.name, 'retryable prompt')
  assert.equal(harness.module.form.content, 'retryable content')
  assert.ok(harness.messages.some(message => message.type === 'error' && message.value === 'save failed'))

  await harness.module.submitForm()
  await harness.flush()
  assert.equal(harness.apiState.createRequests.length, 2)
  assert.equal(harness.module.dialogVisible.value, false)
  assert.equal(harness.module.submitting.value, false)
})

test('reassigns an ordinary owner prompt only to a same-owner paginated replacement', async t => {
  const target = { id: 101, name: 'target', content: 'target content', uid: 'user-a' }
  const samePageReplacement = { id: 102, name: 'same-page', uid: 'user-a' }
  const foreignPrompt = { id: 103, name: 'foreign', uid: 'user-b' }
  const unassignedPrompt = { id: 104, name: 'unassigned', uid: null }
  const secondPageReplacement = { id: 105, name: 'second-page', uid: 'user-a' }
  const secondPageForeign = { id: 106, name: 'second-page-foreign', uid: 'user-b' }
  let deleteAttempt = 0
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  harness.apiState.listHandler = params => {
    if (params.size !== 1000) return Promise.resolve(response())
    if (params.page === 1) {
      return Promise.resolve(response([
        target,
        samePageReplacement,
        foreignPrompt,
        unassignedPrompt,
      ], 6))
    }
    return Promise.resolve(response([secondPageReplacement, secondPageForeign], 6))
  }
  harness.apiState.deleteHandler = () => {
    deleteAttempt += 1
    return deleteAttempt === 1
      ? Promise.reject(conflictError())
      : Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.equal(harness.module.pendingDeletePrompt.value.id, target.id)
  assert.equal(harness.module.reassignDialogVisible.value, true)
  assert.deepEqual(
    snapshot(harness.module.sameOwnerPromptOptions.value).map(prompt => prompt.id),
    [samePageReplacement.id, secondPageReplacement.id],
  )
  assert.equal(harness.module.replacementPromptId.value, samePageReplacement.id)
  assert.deepEqual(snapshot(harness.apiState.listRequests.map(request => request.params)), [
    { page: 1, size: 10 },
    { page: 1, size: 1000 },
    { page: 2, size: 1000 },
  ])

  harness.module.replacementPromptId.value = secondPageReplacement.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.equal(harness.apiState.deleteRequests[1].id, target.id)
  assert.deepEqual(snapshot(harness.apiState.deleteRequests[1].params), {
    replacement_prompt_id: secondPageReplacement.id,
    confirm_reassign: true,
  })
  await harness.flush()
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.deepEqual(snapshot(harness.apiState.listRequests.at(-1).params), { page: 1, size: 10 })
})

test('restricts superuser replacement choices to the deleted prompt owner', async t => {
  const target = { id: 201, name: 'user prompt', uid: 'user-a' }
  const ownerReplacement = { id: 202, name: 'owner replacement', uid: 'user-a' }
  const adminPrompt = { id: 203, name: 'admin prompt', uid: 'admin' }
  const foreignPrompt = { id: 204, name: 'foreign prompt', uid: 'user-b' }
  const unassignedPrompt = { id: 205, name: 'unassigned prompt', uid: null }
  let deleteAttempt = 0
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true
  harness.apiState.listHandler = params => {
    if (params.size !== 1000) return Promise.resolve(response())
    return Promise.resolve(response([
      target,
      ownerReplacement,
      adminPrompt,
      foreignPrompt,
      unassignedPrompt,
    ], 5))
  }
  harness.apiState.deleteHandler = () => {
    deleteAttempt += 1
    return deleteAttempt === 1
      ? Promise.reject(conflictError())
      : Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  assert.deepEqual(
    snapshot(harness.module.sameOwnerPromptOptions.value).map(prompt => prompt.id),
    [ownerReplacement.id],
  )

  harness.module.replacementPromptId.value = adminPrompt.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_replacement_prompt'
  )))

  harness.module.replacementPromptId.value = ownerReplacement.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.deepEqual(snapshot(harness.apiState.deleteRequests[1].params), {
    replacement_prompt_id: ownerReplacement.id,
    confirm_reassign: true,
  })
})

test('handles replacement candidate loading failures without opening the reassign dialog', async t => {
  const target = { id: 301, name: 'target', uid: 'user-a' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.deleteHandler = () => Promise.reject(conflictError())
  harness.apiState.listHandler = params => {
    if (params.size === 1000) return Promise.reject(new Error('candidate list failed'))
    return Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.deepEqual(snapshot(harness.module.replacementPromptOptions.value), [])
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'candidate list failed'
  )))
})

test('warns and stops when a conflicting prompt has no same-owner replacement', async t => {
  const target = { id: 401, name: 'target', uid: 'user-a' }
  const foreignPrompt = { id: 402, name: 'foreign', uid: 'user-b' }
  const unassignedPrompt = { id: 403, name: 'unassigned', uid: null }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.deleteHandler = () => Promise.reject(conflictError())
  harness.apiState.listHandler = params => params.size === 1000
    ? Promise.resolve(response([target, foreignPrompt, unassignedPrompt], 3))
    : Promise.resolve(response())

  await harness.module.deletePrompt(target)
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.deepEqual(snapshot(harness.module.sameOwnerPromptOptions.value), [])
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.no_replacement_prompt'
  )))
})

test('rejects foreign replacements and identity changes without a new delete request', async t => {
  const target = { id: 501, name: 'target', uid: 'user-a' }
  const sameOwnerReplacement = { id: 502, name: 'same owner', uid: 'user-a' }
  const foreignPrompt = { id: 503, name: 'foreign', uid: 'user-b' }
  const unassignedPrompt = { id: 504, name: 'unassigned', uid: null }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.deleteHandler = () => Promise.reject(conflictError())
  harness.apiState.listHandler = params => params.size === 1000
    ? Promise.resolve(response([target, sameOwnerReplacement, foreignPrompt, unassignedPrompt], 4))
    : Promise.resolve(response())

  await harness.module.deletePrompt(target)
  harness.module.replacementPromptId.value = foreignPrompt.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_replacement_prompt'
  )))

  harness.module.replacementPromptId.value = sameOwnerReplacement.id
  harness.permissions.currentUser = { uid: 'user-b' }
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.pendingDeletePrompt.value.id, target.id)
  assert.equal(harness.module.reassignDialogVisible.value, true)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.permission_denied'
  )))
})

test('does not delete after confirmation cancellation', async t => {
  const target = { id: 601, name: 'target', uid: 'user-a' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.confirmHandler = async () => {
    throw 'cancel'
  }

  await harness.module.deletePrompt(target)
  assert.equal(harness.confirmations.length, 1)
  assert.equal(harness.apiState.deleteRequests.length, 0)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.reassignDialogVisible.value, false)
})

test('ignores repeated delete operations during slow confirmation and deletion', async t => {
  const firstConfirmation = createDeferred()
  const slowDelete = createDeferred()
  const firstTarget = { id: 701, name: 'first target', uid: 'user-a' }
  const secondTarget = { id: 702, name: 'second target', uid: 'user-a' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.confirmHandler = () => firstConfirmation.promise

  const firstDelete = harness.module.deletePrompt(firstTarget)
  assert.equal(harness.module.deleteSubmitting.value, true)
  await harness.module.deletePrompt(firstTarget)
  assert.equal(harness.confirmations.length, 1)
  assert.equal(harness.apiState.deleteRequests.length, 0)

  firstConfirmation.resolve('confirm')
  await firstDelete
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.deleteSubmitting.value, false)

  harness.apiState.confirmHandler = async () => 'confirm'
  harness.apiState.deleteHandler = () => slowDelete.promise
  const secondDelete = harness.module.deletePrompt(secondTarget)
  await harness.flush()
  assert.equal(harness.module.deleteSubmitting.value, true)
  await harness.module.deletePrompt(secondTarget)
  assert.equal(harness.confirmations.length, 2)
  assert.equal(harness.apiState.deleteRequests.length, 2)

  slowDelete.resolve(response())
  await secondDelete
  await harness.flush()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.equal(harness.module.deleteSubmitting.value, false)
})

test('keeps the pending replacement state after a failed confirmed delete for retry', async t => {
  const target = { id: 801, name: 'target', uid: 'user-a' }
  const replacement = { id: 802, name: 'replacement', uid: 'user-a' }
  let deleteAttempt = 0
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.listHandler = params => params.size === 1000
    ? Promise.resolve(response([target, replacement], 2))
    : Promise.resolve(response())
  harness.apiState.deleteHandler = () => {
    deleteAttempt += 1
    if (deleteAttempt === 1) return Promise.reject(conflictError())
    if (deleteAttempt === 2) return Promise.reject(new Error('confirmed delete failed'))
    return Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  harness.module.replacementPromptId.value = replacement.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.equal(harness.module.pendingDeletePrompt.value.id, target.id)
  assert.equal(harness.module.replacementPromptId.value, replacement.id)
  assert.equal(harness.module.reassignDialogVisible.value, true)
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'confirmed delete failed'
  )))

  await harness.module.confirmReassignAndDelete()
  await harness.flush()
  assert.equal(harness.apiState.deleteRequests.length, 3)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.equal(harness.module.reassignDialogVisible.value, false)
})

test('updates a superuser prompt after paginated owner loading and refreshes the saved owner', async t => {
  const ownerPage = createDeferred()
  const prompt = {
    id: 901,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const updatedPrompt = {
    id: prompt.id,
    name: 'updated name',
    content: 'updated content',
    uid: 'user-b',
    username: 'Bob',
  }
  const originalPrompt = snapshot(prompt)
  let listCall = 0
  const harness = createHarness(t, {
    listHandler: async () => {
      listCall += 1
      return listCall === 1 ? response([prompt], 1, { current_uid: 'admin' }) : response([updatedPrompt], 1, { current_uid: 'admin' })
    },
    userListHandler: params => {
      if (params.page === 1) return ownerPage.promise
      return Promise.resolve(response([{ uid: 'user-b', username: 'Bob' }], 2))
    },
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.ownerUsersLoading.value, true)
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
  ])

  harness.module.form.name = updatedPrompt.name
  harness.module.form.content = updatedPrompt.content
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 0)

  ownerPage.resolve(response([{ uid: 'user-a', username: 'Alice' }], 2))
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.form.uid, 'user-a')
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
    { page: 2, size: 1000 },
  ])

  harness.module.form.uid = updatedPrompt.uid
  await harness.module.submitForm()
  await harness.flush()

  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: updatedPrompt.name,
    content: updatedPrompt.content,
    uid: updatedPrompt.uid,
  })
  assert.equal(harness.module.dialogVisible.value, false)
  assert.deepEqual(snapshot(harness.module.prompts.value), [updatedPrompt])
  assert.equal(harness.module.prompts.value[0].username, 'Bob')
  assert.equal(harness.module.getOwnerLabel(harness.module.prompts.value[0]), 'Bob')
  assert.deepEqual(snapshot(harness.apiState.listRequests.map(request => request.params)), [
    { page: 1, size: 10 },
    { page: 1, size: 10 },
  ])

  harness.module.showDialog('edit', harness.module.prompts.value[0])
  assert.equal(harness.module.form.id, prompt.id)
  assert.equal(harness.module.form.uid, updatedPrompt.uid)
  assert.deepEqual(snapshot(prompt), originalPrompt)
})

test('blocks a superuser edit after owner loading fails, then retries with a new owner', async t => {
  const prompt = {
    id: 1001,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const newOwner = { uid: 'user-b', username: 'Bob' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true
  harness.apiState.userListHandler = async () => {
    throw new Error('owner list failed')
  }

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.uid, prompt.uid)
  assert.equal(harness.module.form.username, prompt.username)
  assert.equal(harness.module.ownerUsersError.value, 'prompts.load_users_failed')
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'prompts.load_users_failed'
  )))

  harness.module.form.name = 'updated name'
  harness.module.form.content = 'updated content'
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.uid, prompt.uid)
  assert.equal(harness.module.form.username, prompt.username)

  harness.apiState.userListHandler = async () => response([prompt, newOwner], 2)
  await harness.module.fetchOwnerUsers()
  assert.equal(harness.module.ownerUsersError.value, '')
  assert.deepEqual(snapshot(harness.module.ownerUsers.value), [prompt, newOwner])

  harness.module.form.uid = newOwner.uid
  await harness.module.submitForm()
  await harness.flush()
  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: 'updated name',
    content: 'updated content',
    uid: newOwner.uid,
  })
  assert.equal(harness.module.dialogVisible.value, false)
})

test('blocks a superuser edit when the owner is cleared', async t => {
  const prompt = {
    id: 1101,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const harness = createHarness(t, {
    listResponse: response([prompt], 1, { current_uid: 'admin' }),
    userListHandler: async () => response([{ uid: 'user-a', username: 'Alice' }], 1),
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  harness.module.form.uid = null
  harness.module.form.name = 'updated name'
  harness.module.form.content = 'updated content'
  await harness.module.submitForm()

  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_owner'
  )))
})

test('requires a superuser to assign an owner before updating an unassigned prompt', async t => {
  const prompt = {
    id: 1151,
    name: 'original name',
    content: 'original content',
    uid: null,
    username: null,
  }
  const owner = { uid: 'user-b', username: 'Bob' }
  const updatedPrompt = {
    ...prompt,
    ...owner,
    name: 'updated name',
    content: 'updated content',
  }
  let listCall = 0
  const harness = createHarness(t, {
    listHandler: async () => {
      listCall += 1
      return response([listCall === 1 ? prompt : updatedPrompt], 1, { current_uid: 'admin' })
    },
    userListHandler: async () => response([owner], 1),
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  assert.equal(harness.module.form.uid, null)
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, '')

  harness.module.form.name = updatedPrompt.name
  harness.module.form.content = updatedPrompt.content
  await harness.module.submitForm()

  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_owner'
  )))

  harness.module.form.uid = owner.uid
  await harness.module.submitForm()
  await harness.flush()

  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: updatedPrompt.name,
    content: updatedPrompt.content,
    uid: updatedPrompt.uid,
  })
  assert.equal(harness.module.dialogVisible.value, false)
  assert.deepEqual(snapshot(harness.module.prompts.value), [updatedPrompt])
  assert.deepEqual(snapshot(harness.apiState.listRequests.map(request => request.params)), [
    { page: 1, size: 10 },
    { page: 1, size: 10 },
  ])
})

test('retains a superuser edit after a prompt-in-use conflict and retries with the same payload', async t => {
  const prompt = {
    id: 1201,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const newOwner = { uid: 'user-b', username: 'Bob' }
  const firstUpdate = createDeferred()
  let updateAttempt = 0
  let listAttempt = 0
  const harness = createHarness(t, {
    listHandler: async () => {
      listAttempt += 1
      return listAttempt === 1
        ? response([prompt], 1, { current_uid: 'admin' })
        : response([{ ...prompt, name: 'updated name', content: 'updated content', ...newOwner }], 1, { current_uid: 'admin' })
    },
    userListHandler: async () => response([{ uid: prompt.uid, username: prompt.username }, newOwner], 2),
    updateHandler: () => {
      updateAttempt += 1
      return updateAttempt === 1 ? firstUpdate.promise : Promise.resolve(response())
    },
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, '')

  harness.module.form.name = 'updated name'
  harness.module.form.content = 'updated content'
  harness.module.form.uid = newOwner.uid
  const firstSubmit = harness.module.submitForm()
  assert.equal(harness.module.submitting.value, true)
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 1)

  firstUpdate.reject(conflictError())
  await firstSubmit
  await harness.flush()

  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.name, 'updated name')
  assert.equal(harness.module.form.content, 'updated content')
  assert.equal(harness.module.form.uid, newOwner.uid)
  assert.equal(harness.apiState.listRequests.length, 1)
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'prompt is in use'
  )))

  await harness.module.submitForm()
  await harness.flush()

  assert.equal(harness.apiState.updateRequests.length, 2)
  assert.deepEqual(snapshot(harness.apiState.updateRequests.map(request => request.payload)), [
    {
      name: 'updated name',
      content: 'updated content',
      uid: newOwner.uid,
    },
    {
      name: 'updated name',
      content: 'updated content',
      uid: newOwner.uid,
    },
  ])
  assert.equal(harness.module.dialogVisible.value, false)
  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.apiState.listRequests.length, 2)
})
