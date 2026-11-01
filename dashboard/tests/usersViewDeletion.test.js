import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import vm from 'node:vm'
import { reactive, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'

import enCommon from '../src/i18n/locales/en/common.js'
import enUsers from '../src/i18n/locales/en/users.js'
import zhCommon from '../src/i18n/locales/zh/common.js'
import zhUsers from '../src/i18n/locales/zh/users.js'
import { loadUseConfirmDialog } from './confirmDialogHarness.js'

const root = dirname(fileURLToPath(import.meta.url))

const toHostObject = value => Object.fromEntries(Object.entries(value))

const escapeHtml = value => value
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')

const stripImportLines = (source) => source
  .split('\n')
  .filter(line => !line.trimStart().startsWith('import '))
  .join('\n')

const scriptSetup = (source) => {
  const match = source.match(/<script setup>([\s\S]*?)<\/script>/)
  assert.ok(match, 'UsersView must contain a script setup block')
  return match[1]
}

const createHarness = () => {
  const i18n = createI18n({
    legacy: false,
    locale: 'zh',
    fallbackLocale: 'en',
    messages: {
      zh: { users: zhUsers, common: zhCommon },
      en: { users: enUsers, common: enCommon }
    }
  })

  const messageCalls = {
    success: [],
    error: [],
    warning: []
  }
  const ElMessage = {
    success: message => messageCalls.success.push(message),
    error: message => messageCalls.error.push(message),
    warning: message => messageCalls.warning.push(message)
  }

  const confirmCalls = []
  let pendingConfirmation = null
  const ElMessageBox = {
    confirm(message, title, options) {
      confirmCalls.push({ message, title, options: toHostObject(options) })
      return new Promise((resolve, reject) => {
        pendingConfirmation = { resolve, reject }
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

  const apiCalls = {
    list: [],
    delete: []
  }
  let failDelete = false
  const adminApi = {
    userList: async params => {
      apiCalls.list.push(params)
      return { data: { data: { items: [], total: 0 } } }
    },
    userDelete: async uid => {
      apiCalls.delete.push(uid)
      if (failDelete) throw new Error('injected delete failure')
    },
    userAdd: async () => {},
    userUpdate: async () => {}
  }

  const helperSource = stripImportLines(readFileSync(
    resolve(root, '../src/composables/useDeleteConfirm.js'),
    'utf8'
  )).replace('export function useDeleteConfirm', 'function useDeleteConfirm')
  const helperContext = vm.createContext({
    ElMessageBox,
    ElMessage,
    i18n,
    useConfirmDialog: actualUseConfirmDialog
  })
  vm.runInContext(`${helperSource}\nglobalThis.__useDeleteConfirm = useDeleteConfirm`, helperContext, {
    filename: 'useDeleteConfirm.js'
  })

  const usersViewSource = stripImportLines(scriptSetup(readFileSync(
    resolve(root, '../src/views/UsersView.vue'),
    'utf8'
  )))
  const usersViewContext = vm.createContext({
    ref,
    reactive,
    onMounted: () => {},
    ElMessage,
    adminApi,
    useDeleteConfirm: helperContext.__useDeleteConfirm,
    useI18n: () => ({ t }),
    defaultUserForm: () => ({
      uid: null,
      username: '',
      password: '',
      is_active: true
    })
  })
  vm.runInContext(
    `${usersViewSource}\nglobalThis.__usersView = { handleDelete, loadUsers }`,
    usersViewContext,
    { filename: 'UsersView.vue' }
  )

  return {
    i18n,
    messageCalls,
    confirmCalls,
    apiCalls,
    view: usersViewContext.__usersView,
    setDeleteFailure: value => { failDelete = value },
    approveConfirmation: () => {
      assert.ok(pendingConfirmation, 'a confirmation must be pending')
      const confirmation = pendingConfirmation
      pendingConfirmation = null
      confirmation.resolve()
    },
    cancelConfirmation: () => {
      assert.ok(pendingConfirmation, 'a confirmation must be pending')
      const confirmation = pendingConfirmation
      pendingConfirmation = null
      confirmation.reject('cancel')
    },
    closeConfirmation: () => {
      assert.ok(pendingConfirmation, 'a confirmation must be pending')
      const confirmation = pendingConfirmation
      pendingConfirmation = null
      confirmation.reject('close')
    }
  }
}

const assertConfirmation = async (confirmation, userMessages, commonMessages, rawName) => {
  const html = await renderToString(confirmation.message)
  const expectedMessage = userMessages.delete_confirm.replace('{name}', rawName)
  assert.ok(html.includes(escapeHtml(expectedMessage)))
  assert.doesNotMatch(html, /<b>user<\/b>/)
  assert.equal(confirmation.title, commonMessages.warning)
  assert.equal(confirmation.options.confirmButtonText, commonMessages.confirm)
  assert.equal(confirmation.options.cancelButtonText, commonMessages.cancel)
  assert.equal(confirmation.options.type, 'warning')
  assert.equal(confirmation.options.showCancelButton, true)
  assert.equal(confirmation.options.customClass, 'app-confirm-dialog')
  assert.equal(confirmation.options.dangerouslyUseHTMLString, false)
}

test('UsersView deletion uses the real confirmation helper across locales', async () => {
  const harness = createHarness()
  const rawName = '<b>user</b>'

  const zhDelete = harness.view.handleDelete('uid-zh', rawName)
  assert.equal(harness.confirmCalls.length, 1)
  assert.equal(harness.apiCalls.delete.length, 0)
  assert.equal(harness.apiCalls.list.length, 0)
  await assertConfirmation(harness.confirmCalls[0], zhUsers, zhCommon, rawName)
  harness.approveConfirmation()
  assert.equal(await zhDelete, true)
  assert.deepEqual(harness.apiCalls.delete, ['uid-zh'])
  assert.equal(harness.apiCalls.list.length, 1)
  assert.equal(harness.messageCalls.success.length, 1)
  assert.equal(harness.messageCalls.success.at(-1), zhCommon.delete_success)

  harness.i18n.global.locale.value = 'en'
  const enDelete = harness.view.handleDelete('uid-en', rawName)
  assert.equal(harness.confirmCalls.length, 2)
  assert.equal(harness.apiCalls.delete.length, 1)
  await assertConfirmation(harness.confirmCalls[1], enUsers, enCommon, rawName)
  harness.approveConfirmation()
  assert.equal(await enDelete, true)
  assert.deepEqual(harness.apiCalls.delete, ['uid-zh', 'uid-en'])
  assert.equal(harness.apiCalls.list.length, 2)
  assert.equal(harness.messageCalls.success.length, 2)
  assert.equal(harness.messageCalls.success.at(-1), enCommon.delete_success)

  const deleteCountBeforeCancel = harness.apiCalls.delete.length
  const listCountBeforeCancel = harness.apiCalls.list.length
  const errorCountBeforeCancel = harness.messageCalls.error.length
  const cancelled = harness.view.handleDelete('uid-cancel', 'cancelled-user')
  assert.equal(harness.confirmCalls.length, 3)
  harness.cancelConfirmation()
  assert.equal(await cancelled, false)
  assert.equal(harness.apiCalls.delete.length, deleteCountBeforeCancel)
  assert.equal(harness.apiCalls.list.length, listCountBeforeCancel)
  assert.equal(harness.messageCalls.error.length, errorCountBeforeCancel)
  assert.equal(harness.messageCalls.success.length, 2)
  assert.equal(harness.confirmCalls.length, 3)

  harness.setDeleteFailure(true)
  const deleteCountBeforeFailure = harness.apiCalls.delete.length
  const listCountBeforeFailure = harness.apiCalls.list.length
  const errorCountBeforeFailure = harness.messageCalls.error.length
  const failed = harness.view.handleDelete('uid-failure', 'failure-user')
  assert.equal(harness.confirmCalls.length, 4)
  harness.approveConfirmation()
  assert.equal(await failed, false)
  assert.deepEqual(harness.apiCalls.delete.slice(deleteCountBeforeFailure), ['uid-failure'])
  assert.equal(harness.apiCalls.list.length, listCountBeforeFailure)
  assert.equal(harness.messageCalls.error.length, errorCountBeforeFailure + 1)
  assert.equal(harness.messageCalls.error.at(-1), 'injected delete failure')
  assert.equal(harness.messageCalls.success.length, 2)
  assert.equal(harness.confirmCalls.length, 4)

  const deleteCountBeforeClose = harness.apiCalls.delete.length
  const listCountBeforeClose = harness.apiCalls.list.length
  const errorCountBeforeClose = harness.messageCalls.error.length
  const closed = harness.view.handleDelete('uid-close', 'closed-user')
  assert.equal(harness.confirmCalls.length, 5)
  harness.closeConfirmation()
  assert.equal(await closed, false)
  assert.equal(harness.apiCalls.delete.length, deleteCountBeforeClose)
  assert.equal(harness.apiCalls.list.length, listCountBeforeClose)
  assert.equal(harness.messageCalls.error.length, errorCountBeforeClose)
})
