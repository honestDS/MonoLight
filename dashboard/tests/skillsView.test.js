import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import vm from 'node:vm'
import { reactive, ref, watch } from 'vue'
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'

import enCommon from '../src/i18n/locales/en/common.js'
import zhCommon from '../src/i18n/locales/zh/common.js'
import { loadUseConfirmDialog } from './confirmDialogHarness.js'

const root = dirname(fileURLToPath(import.meta.url))

const toHostObject = value => Object.fromEntries(Object.entries(value))

const escapeHtml = value => value
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#39;')

const stripImportLines = source => source
  .split('\n')
  .filter(line => !line.trimStart().startsWith('import '))
  .join('\n')

const scriptSetup = source => {
  const match = source.match(/<script setup>([\s\S]*?)<\/script>/)
  assert.ok(match, 'SkillsView must contain a script setup block')
  return match[1]
}

const apiResponse = (items, total, marketplaceInstalled = false) => ({
  data: { data: { items, total, meta: { marketplace_installed: marketplaceInstalled } } }
})

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
      confirmCalls.push({ message, title, options: toHostObject(options) })
      return new Promise((resolve, reject) => {
        pendingConfirmations.push({ resolve, reject })
      })
    }
  }
  const t = (...args) => i18n.global.t(
    ...args.map((value, index) => (
      index > 0 && value && typeof value === 'object' && !Array.isArray(value)
        ? toHostObject(value)
        : value
    ))
  )
  const actualUseConfirmDialog = loadUseConfirmDialog({ t, ElMessageBox })
  const apiCalls = { list: [], delete: [], restore: [] }
  let listHandler = async () => apiResponse([], 0)
  let deleteHandler = async () => {}
  let restoreHandler = async () => {}
  let currentToken = token
  const systemApi = {
    skills(params) {
      apiCalls.list.push({ ...params })
      return listHandler(params)
    },
    deleteSkill(id) {
      apiCalls.delete.push(id)
      return deleteHandler(id)
    },
    restoreSkillMarketplace() {
      apiCalls.restore.push(true)
      return restoreHandler()
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
    watch,
    onMounted: callback => lifecycle.mounted.push(callback),
    onUnmounted: callback => lifecycle.unmounted.push(callback),
    useI18n: () => ({ t }),
    ElMessage,
    ElMessageBox,
    useConfirmDialog: actualUseConfirmDialog,
    systemApi,
    useUserPermissionsStore: () => userPermissions,
    localStorage
  })
  const source = stripImportLines(scriptSetup(readFileSync(
    resolve(root, '../src/views/SkillsView.vue'),
    'utf8'
  )))
  vm.runInContext(
    `${source}\nglobalThis.__skillsView = { skills, loading, total, currentPage, pageSize, deletingId, marketplaceInstalled, restoring, handleRefresh, handlePageChange, handleSizeChange, handleRestoreMarketplace, handleDelete }`,
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
    setRestoreHandler: handler => { restoreHandler = handler },
    setToken: value => { currentToken = value },
    approveConfirmation: () => takeConfirmation().resolve(),
    cancelConfirmation: () => takeConfirmation().reject('cancel'),
    closeConfirmation: () => takeConfirmation().reject('close')
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

test('SkillsView derives marketplace state from list metadata and ignores stale metadata', async () => {
  const harness = createHarness({ permission: true })
  const firstPage = deferred()
  const stalePage = deferred()
  const latestPage = deferred()
  let requestCount = 0
  harness.setListHandler(params => {
    requestCount += 1
    if (requestCount === 1) return firstPage.promise
    return params.page === 2 ? stalePage.promise : latestPage.promise
  })
  harness.mount()

  firstPage.resolve(apiResponse([{ id: 'regular-first-page' }], 100, true))
  await flushPromises()
  assert.equal(harness.view.marketplaceInstalled.value, true)
  assert.deepEqual(harness.view.skills.value, [{ id: 'regular-first-page' }])

  harness.view.handlePageChange(2)
  harness.view.handlePageChange(3)
  latestPage.resolve(apiResponse([{ id: 'regular-latest-page' }], 100, false))
  await flushPromises()
  stalePage.resolve(apiResponse([{ id: 'stale-marketplace-row' }], 100, true))
  await flushPromises()

  assert.equal(harness.view.marketplaceInstalled.value, false)
  assert.deepEqual(harness.view.skills.value, [{ id: 'regular-latest-page' }])
  assert.equal(harness.view.total.value, 100)
})

test('SkillsView reloads the latest pagination choice when superuser permission returns', async () => {
  const scenarios = [
    {
      name: 'page change',
      apply: view => view.handlePageChange(4),
      expected: { page: 4, size: 10 }
    },
    {
      name: 'page size change',
      apply: view => view.handleSizeChange(25),
      expected: { page: 1, size: 25 }
    },
    {
      name: 'refresh',
      apply: view => {
        view.handlePageChange(4)
        view.handleRefresh()
      },
      expected: { page: 1, size: 10 }
    }
  ]

  for (const scenario of scenarios) {
    const harness = createHarness({ permission: true })
    harness.setListHandler(async ({ page, size }) => apiResponse([
      { id: `page-${page}-size-${size}` }
    ], 1000))
    harness.mount()
    await flushPromises()

    harness.permissions.isSuperuser = false
    await flushPromises()
    scenario.apply(harness.view)
    await flushPromises()

    assert.deepEqual(harness.apiCalls.list, [{ page: 1, size: 10 }], scenario.name)
    assert.equal(
      await harness.view.handleDelete({ id: `blocked-${scenario.name}`, name: 'blocked' }),
      false,
      scenario.name
    )
    assert.deepEqual(harness.apiCalls.delete, [], scenario.name)

    harness.permissions.isSuperuser = true
    await flushPromises()

    assert.deepEqual(harness.apiCalls.list, [
      { page: 1, size: 10 },
      scenario.expected
    ], scenario.name)
    assert.deepEqual(harness.view.skills.value, [
      { id: `page-${scenario.expected.page}-size-${scenario.expected.size}` }
    ], scenario.name)
    assert.equal(harness.view.total.value, 1000, scenario.name)
    assert.equal(harness.view.currentPage.value, scenario.expected.page, scenario.name)
    assert.equal(harness.view.pageSize.value, scenario.expected.size, scenario.name)
  }
})

test('SkillsView ignores an in-flight result during permission recheck and reloads the current choice', async () => {
  const harness = createHarness({ permission: true })
  const oldRequest = deferred()
  const resumedRequest = deferred()
  let requestCount = 0
  harness.setListHandler(() => {
    requestCount += 1
    return requestCount === 1 ? oldRequest.promise : resumedRequest.promise
  })
  harness.mount()
  await flushPromises()

  harness.permissions.isSuperuser = false
  await flushPromises()
  harness.view.handlePageChange(3)
  await flushPromises()
  assert.deepEqual(harness.apiCalls.list, [{ page: 1, size: 10 }])

  oldRequest.resolve(apiResponse([{ id: 'stale-result' }], 1000))
  await flushPromises()
  assert.equal(harness.view.skills.value.length, 0)
  assert.equal(harness.view.total.value, 0)

  harness.permissions.isSuperuser = true
  await flushPromises()
  assert.deepEqual(harness.apiCalls.list, [
    { page: 1, size: 10 },
    { page: 3, size: 10 }
  ])

  resumedRequest.resolve(apiResponse([{ id: 'fresh-result' }], 1000))
  await flushPromises()
  assert.deepEqual(harness.view.skills.value, [{ id: 'fresh-result' }])
  assert.equal(harness.view.total.value, 1000)
  assert.equal(harness.view.currentPage.value, 3)
})

test('SkillsView does not reload or replace data after unmount when permission returns', async () => {
  const harness = createHarness({ permission: true })
  const items = [{ id: 'existing-result' }]
  harness.setListHandler(async () => apiResponse(items, 1000))
  harness.mount()
  await flushPromises()

  const previousSkills = harness.view.skills.value
  const previousTotal = harness.view.total.value
  harness.unmount()
  harness.permissions.isSuperuser = false
  await flushPromises()
  harness.permissions.isSuperuser = true
  await flushPromises()

  assert.deepEqual(harness.apiCalls.list, [{ page: 1, size: 10 }])
  assert.strictEqual(harness.view.skills.value, previousSkills)
  assert.equal(harness.view.total.value, previousTotal)
})

test('SkillsView restores the marketplace from a non-home page and refreshes localized state', async () => {
  for (const locale of ['zh', 'en']) {
    const harness = createHarness({ permission: true, locale })
    const refreshedList = deferred()
    let pageOneLoads = 0
    harness.setListHandler(async ({ page }) => {
      if (page === 1) {
        pageOneLoads += 1
        return pageOneLoads === 1
          ? apiResponse([{ id: 'before-restore' }], 41, false)
          : refreshedList.promise
      }
      return apiResponse([{ id: `page-${page}` }], 41, false)
    })
    harness.mount()
    await flushPromises()
    harness.view.handlePageChange(4)
    await flushPromises()

    const restoreRequest = deferred()
    harness.setRestoreHandler(() => restoreRequest.promise)
    const restoration = harness.view.handleRestoreMarketplace()

    assert.equal(harness.view.marketplaceInstalled.value, false)
    assert.equal(harness.view.restoring.value, true)
    assert.deepEqual(harness.apiCalls.restore, [true])
    assert.equal(await harness.view.handleRestoreMarketplace(), false)
    assert.equal(await harness.view.handleDelete({ id: 'blocked-during-restore', name: 'blocked' }), false)
    assert.deepEqual(harness.apiCalls.restore, [true])
    assert.equal(harness.confirmCalls.length, 0)

    restoreRequest.resolve()
    await flushPromises()
    assert.equal(harness.view.marketplaceInstalled.value, true)
    assert.equal(harness.view.loading.value, true)
    assert.equal(harness.view.restoring.value, true)
    assert.equal(harness.view.currentPage.value, 1)
    assert.equal(
      harness.messageCalls.success.at(-1),
      locale === 'zh' ? zhCommon.skills.marketplace_restore_success : enCommon.skills.marketplace_restore_success
    )
    assert.equal(await harness.view.handleRestoreMarketplace(), false)
    assert.equal(await harness.view.handleDelete({ id: 'blocked-during-refresh', name: 'blocked' }), false)

    refreshedList.resolve(apiResponse([{ id: 'after-restore' }], 7, true))
    assert.equal(await restoration, true)
    await flushPromises()

    assert.deepEqual(harness.apiCalls.list, [
      { page: 1, size: 10 },
      { page: 4, size: 10 },
      { page: 1, size: 10 }
    ])
    assert.deepEqual(harness.view.skills.value, [{ id: 'after-restore' }])
    assert.equal(harness.view.total.value, 7)
    assert.equal(harness.view.marketplaceInstalled.value, true)
    assert.equal(harness.view.loading.value, false)
    assert.equal(harness.view.restoring.value, false)
    assert.deepEqual(harness.apiCalls.restore, [true])
  }
})

test('SkillsView blocks marketplace restore while unavailable, unknown, installed, or deleting', async () => {
  const loadingHarness = createHarness({ permission: true })
  const pendingList = deferred()
  loadingHarness.setListHandler(() => pendingList.promise)
  loadingHarness.mount()
  await flushPromises()
  assert.equal(loadingHarness.view.loading.value, true)
  assert.equal(loadingHarness.view.marketplaceInstalled.value, null)
  assert.equal(await loadingHarness.view.handleRestoreMarketplace(), false)
  assert.deepEqual(loadingHarness.apiCalls.restore, [])
  pendingList.resolve(apiResponse([], 0, false))
  await flushPromises()

  const noPermissionHarness = createHarness({ permission: false })
  noPermissionHarness.mount()
  await flushPromises()
  assert.equal(await noPermissionHarness.view.handleRestoreMarketplace(), false)
  assert.deepEqual(noPermissionHarness.apiCalls.restore, [])

  const notMountedHarness = createHarness({ permission: true })
  assert.equal(await notMountedHarness.view.handleRestoreMarketplace(), false)
  assert.deepEqual(notMountedHarness.apiCalls.restore, [])

  const unmountedHarness = createHarness({ permission: true })
  unmountedHarness.setListHandler(async () => apiResponse([], 0, false))
  unmountedHarness.mount()
  await flushPromises()
  unmountedHarness.unmount()
  assert.equal(await unmountedHarness.view.handleRestoreMarketplace(), false)
  assert.deepEqual(unmountedHarness.apiCalls.restore, [])

  const installedHarness = createHarness({ permission: true })
  installedHarness.setListHandler(async () => apiResponse([{ id: 'marketplace' }], 1, true))
  installedHarness.mount()
  await flushPromises()
  assert.equal(installedHarness.view.marketplaceInstalled.value, true)
  assert.equal(await installedHarness.view.handleRestoreMarketplace(), false)
  assert.deepEqual(installedHarness.apiCalls.restore, [])

  const deletingHarness = createHarness({ permission: true })
  deletingHarness.setListHandler(async () => apiResponse([{ id: 'ordinary-skill' }], 1, false))
  deletingHarness.mount()
  await flushPromises()
  const deletion = deletingHarness.view.handleDelete({ id: 'ordinary-skill', name: 'Ordinary skill' })
  assert.equal(deletingHarness.view.deletingId.value, 'ordinary-skill')
  assert.equal(await deletingHarness.view.handleRestoreMarketplace(), false)
  assert.deepEqual(deletingHarness.apiCalls.restore, [])
  deletingHarness.cancelConfirmation()
  assert.equal(await deletion, false)
  assert.equal(deletingHarness.view.deletingId.value, null)

  const restoreRequest = deferred()
  deletingHarness.setRestoreHandler(() => restoreRequest.promise)
  const restoration = deletingHarness.view.handleRestoreMarketplace()
  assert.equal(deletingHarness.view.restoring.value, true)
  assert.deepEqual(deletingHarness.apiCalls.restore, [true])
  restoreRequest.resolve()
  assert.equal(await restoration, true)
  await flushPromises()
})

test('SkillsView keeps marketplace state false after restore errors and permits retry', async () => {
  const scenarios = [
    { error: new Error('restore backend failure'), expected: 'restore backend failure' },
    { error: {}, expected: enCommon.skills.marketplace_restore_failed }
  ]

  for (const scenario of scenarios) {
    const harness = createHarness({ permission: true, locale: 'en' })
    let listLoads = 0
    harness.setListHandler(async () => {
      listLoads += 1
      return apiResponse([{ id: listLoads === 1 ? 'before-retry' : 'after-retry' }], 1, listLoads > 1)
    })
    harness.setRestoreHandler(async () => { throw scenario.error })
    harness.mount()
    await flushPromises()

    assert.equal(await harness.view.handleRestoreMarketplace(), false)
    assert.equal(harness.view.marketplaceInstalled.value, false)
    assert.equal(harness.view.restoring.value, false)
    assert.equal(harness.messageCalls.error.at(-1), scenario.expected)
    assert.equal(harness.messageCalls.success.length, 0)

    harness.setRestoreHandler(async () => {})
    assert.equal(await harness.view.handleRestoreMarketplace(), true)
    await flushPromises()
    assert.equal(harness.view.marketplaceInstalled.value, true)
    assert.equal(harness.view.restoring.value, false)
    assert.equal(harness.apiCalls.restore.length, 2)
    assert.equal(harness.apiCalls.list.length, 2)
  }
})

test('SkillsView ignores restore results after permission, token, or lifecycle changes', async () => {
  for (const outcome of ['success', 'failure']) {
    for (const guard of ['permission', 'token', 'unmount']) {
      const harness = createHarness({ permission: true, token: 'before-restore' })
      const restoreRequest = deferred()
      harness.setListHandler(async () => apiResponse([{ id: 'existing' }], 1, false))
      harness.setRestoreHandler(() => restoreRequest.promise)
      harness.mount()
      await flushPromises()

      const restoration = harness.view.handleRestoreMarketplace()
      assert.equal(harness.view.restoring.value, true)
      if (guard === 'permission') harness.permissions.isSuperuser = false
      if (guard === 'token') harness.setToken('after-restore')
      if (guard === 'unmount') harness.unmount()

      if (outcome === 'success') restoreRequest.resolve()
      else restoreRequest.reject(new Error('stale restore failure'))

      assert.equal(await restoration, false)
      await flushPromises()
      assert.equal(harness.view.marketplaceInstalled.value, false)
      assert.equal(harness.view.restoring.value, false)
      assert.equal(harness.messageCalls.success.length, 0)
      assert.equal(harness.messageCalls.error.length, 0)
      assert.equal(harness.apiCalls.list.length, 1)
      assert.deepEqual(harness.apiCalls.restore, [true])
    }
  }
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
    const common = locale === 'zh' ? zhCommon : enCommon

    assert.equal(harness.apiCalls.delete.length, 0)
    assert.equal(harness.confirmCalls.length, 1)
    const renderedMessage = await renderToString(harness.confirmCalls[0].message)
    assert.ok(renderedMessage.includes(escapeHtml(expectedMessage)))
    assert.doesNotMatch(renderedMessage, /<img\b/i)
    assert.equal(harness.confirmCalls[0].title, common.warning)
    assert.equal(harness.confirmCalls[0].options.confirmButtonText, common.confirm)
    assert.equal(harness.confirmCalls[0].options.cancelButtonText, common.cancel)
    assert.equal(harness.confirmCalls[0].options.customClass, 'app-confirm-dialog')
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

test('SkillsView uses a localized marketplace deletion warning and follows deletion metadata', async () => {
  for (const locale of ['zh', 'en']) {
    const harness = createHarness({ permission: true, locale })
    let listLoads = 0
    const marketplaceRow = {
      id: 'skillhub-manual-installer',
      name: '<img src=x onerror=alert(1)>'
    }
    harness.setListHandler(async () => {
      listLoads += 1
      return listLoads === 1
        ? apiResponse([marketplaceRow], 1, true)
        : apiResponse([], 0, false)
    })
    harness.mount()
    await flushPromises()

    const deletion = harness.view.handleDelete(marketplaceRow)
    const common = locale === 'zh' ? zhCommon : enCommon
    const template = common.skills.delete_marketplace_confirm
    const expectedMessage = template
      .replace('{name}', marketplaceRow.name)
      .replace('{id}', marketplaceRow.id)
    const renderedMessage = await renderToString(harness.confirmCalls[0].message)
    assert.ok(renderedMessage.includes(escapeHtml(expectedMessage)))
    assert.doesNotMatch(renderedMessage, /<img\b/i)
    assert.equal(harness.confirmCalls[0].title, common.warning)
    assert.equal(harness.confirmCalls[0].options.dangerouslyUseHTMLString, false)

    harness.approveConfirmation()
    assert.equal(await deletion, true)
    assert.equal(harness.view.marketplaceInstalled.value, false)
    assert.equal(harness.apiCalls.list.length, 2)
    assert.deepEqual(harness.apiCalls.restore, [])

    const ordinaryRow = { id: 'ordinary-skill', name: 'Ordinary skill' }
    const ordinaryDeletion = harness.view.handleDelete(ordinaryRow)
    const ordinaryTemplate = common.skills.delete_confirm
    const ordinaryExpectedMessage = ordinaryTemplate
      .replace('{name}', ordinaryRow.name)
      .replace('{id}', ordinaryRow.id)
    const ordinaryRenderedMessage = await renderToString(harness.confirmCalls[1].message)
    assert.ok(ordinaryRenderedMessage.includes(escapeHtml(ordinaryExpectedMessage)))
    assert.notEqual(ordinaryExpectedMessage, expectedMessage)
    harness.cancelConfirmation()
    assert.equal(await ordinaryDeletion, false)
    assert.deepEqual(harness.apiCalls.restore, [])
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
