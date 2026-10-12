import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import vm from 'node:vm'
import { reactive, ref } from 'vue'
import { createI18n } from 'vue-i18n'

import enCommon from '../src/i18n/locales/en/common.js'
import enUsers from '../src/i18n/locales/en/users.js'
import zhCommon from '../src/i18n/locales/zh/common.js'
import zhUsers from '../src/i18n/locales/zh/users.js'

const root = dirname(fileURLToPath(import.meta.url))

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
      confirmCalls.push({ message, title, options })
      return new Promise((resolve, reject) => {
        pendingConfirmation = { resolve, reject }
      })
    }
  }

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
    i18n
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
    useI18n: () => ({ t: (key, params) => params === undefined ? i18n.global.t(key) : i18n.global.t(key, { ...params }) }),
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
    }
  }
}

test('UsersView deletion uses the real confirmation helper across locales', async () => {
  const harness = createHarness()
  const rawName = '<b>user</b>'

  const zhDelete = harness.view.handleDelete('uid-zh', rawName)
  assert.equal(harness.confirmCalls.length, 1)
  assert.equal(harness.apiCalls.delete.length, 0)
  assert.equal(harness.apiCalls.list.length, 0)
  assert.equal(harness.confirmCalls[0].message, zhUsers.delete_confirm.replace('{name}', rawName))
  assert.ok(harness.confirmCalls[0].message.includes(rawName))
  assert.equal(harness.confirmCalls[0].options.dangerouslyUseHTMLString, false)
  harness.approveConfirmation()
  assert.equal(await zhDelete, true)
  assert.deepEqual(harness.apiCalls.delete, ['uid-zh'])
  assert.equal(harness.apiCalls.list.length, 1)

  harness.i18n.global.locale.value = 'en'
  const enDelete = harness.view.handleDelete('uid-en', rawName)
  assert.equal(harness.confirmCalls.length, 2)
  assert.equal(harness.apiCalls.delete.length, 1)
  assert.equal(harness.confirmCalls[1].message, enUsers.delete_confirm.replace('{name}', rawName))
  assert.ok(harness.confirmCalls[1].message.includes(rawName))
  assert.equal(harness.confirmCalls[1].options.dangerouslyUseHTMLString, false)
  harness.approveConfirmation()
  assert.equal(await enDelete, true)
  assert.deepEqual(harness.apiCalls.delete, ['uid-zh', 'uid-en'])
  assert.equal(harness.apiCalls.list.length, 2)

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
  assert.equal(harness.confirmCalls.length, 4)
})
