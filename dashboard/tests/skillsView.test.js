import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import vm from 'node:vm'
import { reactive, ref } from 'vue'
import { createI18n } from 'vue-i18n'

import enCommon from '../src/i18n/locales/en/common.js'
import zhCommon from '../src/i18n/locales/zh/common.js'

const root = dirname(fileURLToPath(import.meta.url))

const stripImportLines = source => source
  .split('\n')
  .filter(line => !line.trimStart().startsWith('import '))
  .join('\n')

const scriptSetup = source => {
  const match = source.match(/<script setup>([\s\S]*?)<\/script>/)
  assert.ok(match, 'SkillsView must contain a script setup block')
  return match[1]
}

const apiResponse = (items, total) => ({ data: { data: { items, total } } })

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flushPromises = async (turns = 12) => {
  for (let index = 0; index < turns; index += 1) await Promise.resolve()
}

const createHarness = ({ permission, locale = 'zh', token = 'initial-token' } = {}) => {
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: {
      zh: { common: zhCommon },
      en: { common: enCommon }
    }
  })
  const userPermissions = reactive({ isSuperuser: permission })
  const lifecycle = { mounted: [], unmounted: [] }
  const messageCalls = { success: [], error: [] }
  const ElMessage = {
    success: message => messageCalls.success.push(message),
    error: message => messageCalls.error.push(message)
  }
  const confirmCalls = []
  const pendingConfirmations = []
  const ElMessageBox = {
    confirm(message, title, options) {
      confirmCalls.push({ message, title, options })
      return new Promise((resolve, reject) => {
        pendingConfirmations.push({ resolve, reject })
      })
    }
  }
  const apiCalls = { list: [], delete: [] }
  let listHandler = async () => apiResponse([], 0)
  let deleteHandler = async () => {}
  let currentToken = token
  const systemApi = {
    skills(params) {
      apiCalls.list.push({ ...params })
      return listHandler(params)
    },
    deleteSkill(id) {
      apiCalls.delete.push(id)
      return deleteHandler(id)
    }
  }
  const localStorage = {
    getItem(key) {
      assert.equal(key, 'token')
      return currentToken
    }
  }
  const context = vm.createContext({
    ref,
    onMounted: callback => lifecycle.mounted.push(callback),
    onUnmounted: callback => lifecycle.unmounted.push(callback),
    useI18n: () => ({ t: (key, params) => params === undefined
      ? i18n.global.t(key)
      : i18n.global.t(key, { ...params }) }),
    ElMessage,
    ElMessageBox,
    systemApi,
    useUserPermissionsStore: () => userPermissions,
    localStorage
  })
  const source = stripImportLines(scriptSetup(readFileSync(
    resolve(root, '../src/views/SkillsView.vue'),
    'utf8'
  )))
  vm.runInContext(
    `${source}\nglobalThis.__skillsView = { skills, loading, total, currentPage, pageSize, deletingId, handleRefresh, handlePageChange, handleSizeChange, handleDelete }`,
    context,
    { filename: 'SkillsView.vue' }
  )

  const takeConfirmation = () => {
    const confirmation = pendingConfirmations.shift()
    assert.ok(confirmation, 'a confirmation must be pending')
    return confirmation
  }

  return {
    apiCalls,
    confirmCalls,
    i18n,
    messageCalls,
    permissions: userPermissions,
    view: context.__skillsView,
    mount: () => lifecycle.mounted.forEach(callback => callback()),
    unmount: () => lifecycle.unmounted.forEach(callback => callback()),
    setListHandler: handler => { listHandler = handler },
    setDeleteHandler: handler => { deleteHandler = handler },
    setToken: value => { currentToken = value },
    approveConfirmation: () => takeConfirmation().resolve(),
    cancelConfirmation: () => takeConfirmation().reject(new Error('cancelled')),
    closeConfirmation: () => takeConfirmation().reject(new Error('closed'))
  }
}

test('SkillsView gates access and preserves the server list result', async () => {
  for (const permission of [undefined, false]) {
    const harness = createHarness({ permission })
    harness.mount()
    await flushPromises()

    assert.equal(await harness.view.handleDelete({ id: 'blocked-id', name: 'blocked' }), false)
    assert.deepEqual(harness.apiCalls.list, [])
    assert.deepEqual(harness.apiCalls.delete, [])
    assert.deepEqual(harness.confirmCalls, [])
  }

  const harness = createHarness({ permission: true })
  const items = [
    { id: 'skill-id', name: 'Visible skill', description: '' },
    { id: 'folder-only', name: '', description: '' }
  ]
  harness.setListHandler(async () => apiResponse(items, 23))
  harness.mount()
  await flushPromises()

  assert.deepEqual(harness.apiCalls.list, [{ page: 1, size: 10 }])
  assert.equal(harness.view.total.value, 23)
  assert.deepEqual(harness.view.skills.value, items)
  assert.equal(harness.view.skills.value[1].name, '')
  assert.equal(harness.view.skills.value[1].description, '')
})

test('SkillsView paginates, ignores stale results, reports errors, and ignores results after unmount', async () => {
  const harness = createHarness({ permission: true })
  harness.setListHandler(async params => apiResponse([{ id: `page-${params.page}` }], 250))
  harness.mount()
  await flushPromises()

  harness.view.handlePageChange(4)
  await flushPromises()
  harness.view.handleRefresh()
  await flushPromises()
  harness.view.handlePageChange(7)
  await flushPromises()
  harness.view.handleSizeChange(25)
  await flushPromises()
  harness.view.handlePageChange(3)
  await flushPromises()

  assert.deepEqual(harness.apiCalls.list.slice(0, 6), [
    { page: 1, size: 10 },
    { page: 4, size: 10 },
    { page: 1, size: 10 },
    { page: 7, size: 10 },
    { page: 1, size: 25 },
    { page: 3, size: 25 }
  ])

  const stale = deferred()
  const latest = deferred()
  harness.setListHandler(params => params.page === 4 ? stale.promise : latest.promise)
  harness.view.handlePageChange(4)
  harness.view.handlePageChange(5)
  latest.resolve(apiResponse([{ id: 'latest-result' }], 250))
  await flushPromises()
  stale.resolve(apiResponse([{ id: 'stale-result' }], 240))
  await flushPromises()

  assert.deepEqual(harness.view.skills.value, [{ id: 'latest-result' }])
  assert.equal(harness.view.total.value, 250)
  assert.equal(harness.view.loading.value, false)

  harness.setListHandler(async () => { throw new Error('list backend failure') })
  harness.view.handleRefresh()
  await flushPromises()
  assert.equal(harness.messageCalls.error.at(-1), 'list backend failure')
  assert.equal(harness.view.loading.value, false)

  const previousSkills = harness.view.skills.value
  const previousTotal = harness.view.total.value
  const pendingList = deferred()
  harness.setListHandler(() => pendingList.promise)
  harness.view.handlePageChange(2)
  harness.unmount()
  pendingList.resolve(apiResponse([{ id: 'after-unmount' }], 1))
  await flushPromises()

  assert.equal(harness.view.skills.value, previousSkills)
  assert.equal(harness.view.total.value, previousTotal)
})

test('SkillsView confirms localized safe-text names and deduplicates pending deletion', async () => {
  for (const locale of ['zh', 'en']) {
    const harness = createHarness({ permission: true, locale })
    harness.setListHandler(async () => apiResponse([], 1))
    harness.mount()
    await flushPromises()

    const row = { id: `real-id-${locale}`, name: '<img src=x onerror=alert(1)>' }
    const deleteRequest = deferred()
    harness.setDeleteHandler(() => deleteRequest.promise)
    const deletion = harness.view.handleDelete(row)
    const template = locale === 'zh' ? zhCommon.skills.delete_confirm : enCommon.skills.delete_confirm
    const expectedMessage = template.replace('{name}', row.name).replace('{id}', row.id)

    assert.equal(harness.apiCalls.delete.length, 0)
    assert.equal(harness.confirmCalls.length, 1)
    assert.equal(harness.confirmCalls[0].message, expectedMessage)
    assert.equal(harness.confirmCalls[0].options.dangerouslyUseHTMLString, false)
    assert.equal(await harness.view.handleDelete({ id: 'duplicate-confirm', name: 'duplicate' }), false)
    assert.equal(harness.confirmCalls.length, 1)
    assert.equal(harness.apiCalls.delete.length, 0)

    harness.approveConfirmation()
    await flushPromises()
    assert.deepEqual(harness.apiCalls.delete, [row.id])
    assert.equal(await harness.view.handleDelete({ id: 'duplicate-delete', name: 'duplicate' }), false)
    assert.equal(harness.confirmCalls.length, 1)
    assert.deepEqual(harness.apiCalls.delete, [row.id])

    deleteRequest.resolve()
    assert.equal(await deletion, true)
    assert.equal(harness.apiCalls.list.length, 2)
    assert.equal(harness.messageCalls.success.length, 1)
  }
})

test('SkillsView handles cancel and close without errors and remains retryable', async () => {
  const harness = createHarness({ permission: true })
  harness.setListHandler(async () => apiResponse([{ id: 'retryable' }], 1))
  harness.mount()
  await flushPromises()

  const row = { id: 'retryable', name: 'Retryable' }
  const cancel = harness.view.handleDelete(row)
  harness.cancelConfirmation()
  assert.equal(await cancel, false)
  assert.equal(harness.view.deletingId.value, null)

  const close = harness.view.handleDelete(row)
  harness.closeConfirmation()
  assert.equal(await close, false)
  assert.equal(harness.view.deletingId.value, null)
  assert.deepEqual(harness.apiCalls.delete, [])
  assert.equal(harness.messageCalls.error.length, 0)

  const retry = harness.view.handleDelete(row)
  harness.approveConfirmation()
  assert.equal(await retry, true)
  assert.deepEqual(harness.apiCalls.delete, [row.id])
  assert.equal(harness.messageCalls.error.length, 0)
})

test('SkillsView reports delete failures and returns to the last valid page after deleting its final row', async () => {
  const failureHarness = createHarness({ permission: true })
  const originalItems = [{ id: 'keep-row', name: 'Keep row' }]
  failureHarness.setListHandler(async () => apiResponse(originalItems, 1))
  failureHarness.setDeleteHandler(async () => { throw new Error('delete backend failure') })
  failureHarness.mount()
  await flushPromises()

  const failure = failureHarness.view.handleDelete(originalItems[0])
  failureHarness.approveConfirmation()
  assert.equal(await failure, false)
  assert.equal(failureHarness.messageCalls.error.at(-1), 'delete backend failure')
  assert.equal(failureHarness.messageCalls.success.length, 0)
  assert.equal(failureHarness.apiCalls.list.length, 1)
  assert.deepEqual(failureHarness.view.skills.value, originalItems)

  const rollbackHarness = createHarness({ permission: true })
  const lastRow = { id: 'last-row', name: 'Last row' }
  const previousPage = [{ id: 'page-two-a' }, { id: 'page-two-b' }]
  let pageThreeResponses = 0
  rollbackHarness.setListHandler(async ({ page }) => {
    if (page === 3) {
      pageThreeResponses += 1
      return pageThreeResponses === 1
        ? apiResponse([lastRow], 21)
        : apiResponse([], 20)
    }
    if (page === 2) return apiResponse(previousPage, 20)
    return apiResponse([{ id: 'page-one' }], 21)
  })
  rollbackHarness.mount()
  await flushPromises()
  rollbackHarness.view.handlePageChange(3)
  await flushPromises()
  assert.deepEqual(rollbackHarness.view.skills.value, [lastRow])

  const deletion = rollbackHarness.view.handleDelete(lastRow)
  rollbackHarness.approveConfirmation()
  assert.equal(await deletion, true)

  assert.deepEqual(rollbackHarness.apiCalls.list.slice(-2), [
    { page: 3, size: 10 },
    { page: 2, size: 10 }
  ])
  assert.equal(rollbackHarness.view.currentPage.value, 2)
  assert.equal(rollbackHarness.view.total.value, 20)
  assert.deepEqual(rollbackHarness.view.skills.value, previousPage)
})

test('SkillsView rechecks permission, token, and lifecycle after confirmation', async () => {
  for (const guard of ['permission', 'token', 'unmount']) {
    const harness = createHarness({ permission: true, token: 'before-confirm' })
    harness.mount()
    await flushPromises()

    const deletion = harness.view.handleDelete({ id: `guard-${guard}`, name: guard })
    if (guard === 'permission') harness.permissions.isSuperuser = false
    if (guard === 'token') harness.setToken('after-confirm')
    if (guard === 'unmount') harness.unmount()
    harness.approveConfirmation()

    assert.equal(await deletion, false)
    assert.equal(harness.apiCalls.delete.length, 0)
    assert.equal(harness.apiCalls.list.length, 1)
  }
})
