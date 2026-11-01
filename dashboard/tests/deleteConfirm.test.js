import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'
import enCommon from '../src/i18n/locales/en/common.js'
import zhCommon from '../src/i18n/locales/zh/common.js'
import { loadUseConfirmDialog } from './confirmDialogHarness.js'

const useDeleteConfirmSource = readFileSync(
  new URL('../src/composables/useDeleteConfirm.js', import.meta.url),
  'utf8'
)
  .replace(/^[ \t]*import[^\r\n]*(?:\r?\n|$)/gm, '')
  .replace(/^export\s+function\s+/m, 'function ')

const toHost = value => {
  if (value === undefined || value === null || typeof value !== 'object') return value
  return JSON.parse(JSON.stringify(value))
}

const createHarness = ({
  locale = 'zh',
  options = {},
  deleteImplementation
} = {}) => {
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: {
      zh: { common: zhCommon },
      en: { common: enCommon }
    }
  })
  const rawTranslate = i18n.global.t.bind(i18n.global)
  const t = (...args) => rawTranslate(...args.map(toHost))
  i18n.global.t = t

  const messages = []
  const confirmations = []
  const pendingConfirmations = []
  const apiCalls = []
  const refreshCalls = []
  const ElMessage = {
    success: message => messages.push({ type: 'success', message }),
    error: message => messages.push({ type: 'error', message })
  }
  const ElMessageBox = {
    confirm(message, title, confirmOptions) {
      confirmations.push({
        message,
        title,
        options: toHost(confirmOptions)
      })
      return new Promise((resolve, reject) => {
        pendingConfirmations.push({ resolve, reject })
      })
    }
  }

  const actualUseConfirmDialog = loadUseConfirmDialog({ t, ElMessageBox })
  const context = vm.createContext({
    ElMessage,
    i18n,
    useConfirmDialog: () => actualUseConfirmDialog()
  })
  vm.runInContext(
    `${useDeleteConfirmSource}\nglobalThis.__useDeleteConfirm = useDeleteConfirm`,
    context,
    { filename: 'useDeleteConfirm.js' }
  )

  const apiDelete = async id => {
    apiCalls.push(id)
    if (deleteImplementation) await deleteImplementation(id)
  }
  const onSuccess = () => refreshCalls.push(true)
  const handleDelete = context.__useDeleteConfirm(apiDelete, onSuccess, options).handleDelete

  return {
    confirmations,
    messages,
    apiCalls,
    refreshCalls,
    handleDelete,
    settleConfirmation: (action = 'confirm') => {
      const pending = pendingConfirmations.shift()
      assert.ok(pending, 'a confirmation must be pending')
      if (action === 'confirm') pending.resolve()
      else pending.reject(action)
    }
  }
}

test('uses localized defaults, escapes names, and deletes only after confirmation', async () => {
  const unsafeName = '<script>alert(1)</script><img src=x><span style="color:red">name</span>'

  for (const [locale, common] of [['zh', zhCommon], ['en', enCommon]]) {
    const harness = createHarness({ locale })
    const deletion = harness.handleDelete(`record-${locale}`, unsafeName)

    assert.equal(harness.confirmations.length, 1, locale)
    assert.deepEqual(harness.apiCalls, [], locale)
    assert.deepEqual(harness.messages, [], locale)
    assert.deepEqual(harness.refreshCalls, [], locale)

    const confirmation = harness.confirmations[0]
    assert.equal(confirmation.title, common.warning, locale)
    assert.deepEqual(confirmation.options, {
      type: 'warning',
      confirmButtonText: common.confirm,
      cancelButtonText: common.cancel,
      showCancelButton: true,
      dangerouslyUseHTMLString: false,
      customClass: 'app-confirm-dialog',
      closeOnClickModal: true
    }, locale)

    const html = await renderToString(confirmation.message)
    const expectedPrefix = common.delete_named_confirm.split('{name}')[0]
    const expectedSuffix = common.delete_named_confirm.split('{name}')[1]
    assert.ok(html.includes(expectedPrefix), locale)
    assert.ok(html.includes(expectedSuffix), locale)
    assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/, locale)
    assert.match(html, /&lt;img\b[^<]*&gt;/, locale)
    assert.match(html, /&lt;span\b[^<]*&gt;name&lt;\/span&gt;/, locale)
    assert.doesNotMatch(html, /<(?:script|img|span|style)\b/i, locale)

    harness.settleConfirmation()
    assert.equal(await deletion, true, locale)
    assert.deepEqual(harness.apiCalls, [`record-${locale}`], locale)
    assert.deepEqual(harness.messages, [
      { type: 'success', message: common.delete_success }
    ], locale)
    assert.deepEqual(harness.refreshCalls, [true], locale)
  }
})

test('returns false for cancelled or closed confirmations without side effects', async () => {
  for (const action of ['cancel', 'close']) {
    const harness = createHarness()
    const deletion = harness.handleDelete(`cancelled-${action}`, 'cancelled record')

    assert.deepEqual(harness.apiCalls, [])
    harness.settleConfirmation(action)

    assert.equal(await deletion, false, action)
    assert.deepEqual(harness.apiCalls, [], action)
    assert.deepEqual(harness.messages, [], action)
    assert.deepEqual(harness.refreshCalls, [], action)
  }
})

test('reports unexpected confirmation failures and API failures without refreshing', async () => {
  const dialogError = new Error('dialog failed')
  const dialogErrorHarness = createHarness({ locale: 'en' })
  const dialogErrorDeletion = dialogErrorHarness.handleDelete('dialog-error', 'dialog error record')
  dialogErrorHarness.settleConfirmation(dialogError)

  assert.equal(await dialogErrorDeletion, false)
  assert.deepEqual(dialogErrorHarness.apiCalls, [])
  assert.deepEqual(dialogErrorHarness.refreshCalls, [])
  assert.deepEqual(dialogErrorHarness.messages, [
    { type: 'error', message: dialogError.message }
  ])

  const unexpectedActionHarness = createHarness({ locale: 'en' })
  const unexpectedActionDeletion = unexpectedActionHarness.handleDelete(
    'unexpected-action',
    'unexpected action record'
  )
  unexpectedActionHarness.settleConfirmation('unexpected action')

  assert.equal(await unexpectedActionDeletion, false)
  assert.deepEqual(unexpectedActionHarness.apiCalls, [])
  assert.deepEqual(unexpectedActionHarness.refreshCalls, [])
  assert.deepEqual(unexpectedActionHarness.messages, [
    { type: 'error', message: enCommon.action_failed }
  ])

  for (const [options, expectedMessage] of [
    [{}, enCommon.delete_failed],
    [{ errorMessage: 'Custom delete failure' }, 'Custom delete failure']
  ]) {
    const apiErrorHarness = createHarness({
      locale: 'en',
      options,
      deleteImplementation: async () => { throw new Error() }
    })
    const apiErrorDeletion = apiErrorHarness.handleDelete('api-error', 'api error record')
    apiErrorHarness.settleConfirmation()

    assert.equal(await apiErrorDeletion, false, expectedMessage)
    assert.deepEqual(apiErrorHarness.apiCalls, ['api-error'], expectedMessage)
    assert.deepEqual(apiErrorHarness.refreshCalls, [], expectedMessage)
    assert.deepEqual(apiErrorHarness.messages, [
      { type: 'error', message: expectedMessage }
    ], expectedMessage)
  }
})

test('applies extra options over base options and isolates consecutive calls', async () => {
  const baseOptions = {
    message: 'Base confirmation',
    title: 'Base title',
    confirmButtonText: 'Base confirm',
    cancelButtonText: 'Base cancel',
    successMessage: 'Base success',
    errorMessage: 'Base failure'
  }
  const harness = createHarness({
    options: baseOptions,
    deleteImplementation: async id => {
      if (id === 'third-id') throw new Error()
    }
  })

  const firstDeletion = harness.handleDelete('first-id', 'first record')
  const firstConfirmation = harness.confirmations[0]
  const firstHtml = await renderToString(firstConfirmation.message)
  assert.match(firstHtml, /Base confirmation/)
  assert.doesNotMatch(firstHtml, /Extra confirmation|second record/)
  assert.equal(firstConfirmation.title, baseOptions.title)
  assert.equal(firstConfirmation.options.confirmButtonText, baseOptions.confirmButtonText)
  assert.equal(firstConfirmation.options.cancelButtonText, baseOptions.cancelButtonText)
  harness.settleConfirmation()
  assert.equal(await firstDeletion, true)

  const extraOptions = {
    message: 'Extra confirmation',
    title: 'Extra title',
    confirmButtonText: 'Extra confirm',
    cancelButtonText: 'Extra cancel',
    successMessage: 'Extra success'
  }
  const secondDeletion = harness.handleDelete('second-id', 'second record', extraOptions)
  const secondConfirmation = harness.confirmations[1]
  const secondHtml = await renderToString(secondConfirmation.message)
  assert.match(secondHtml, /Extra confirmation/)
  assert.doesNotMatch(secondHtml, /Base confirmation|first record/)
  assert.equal(secondConfirmation.title, extraOptions.title)
  assert.equal(secondConfirmation.options.confirmButtonText, extraOptions.confirmButtonText)
  assert.equal(secondConfirmation.options.cancelButtonText, extraOptions.cancelButtonText)
  harness.settleConfirmation()
  assert.equal(await secondDeletion, true)

  const thirdDeletion = harness.handleDelete(
    'third-id',
    'third record',
    { errorMessage: 'Extra failure' }
  )
  harness.settleConfirmation()
  assert.equal(await thirdDeletion, false)

  assert.deepEqual(harness.apiCalls, ['first-id', 'second-id', 'third-id'])
  assert.deepEqual(harness.messages, [
    { type: 'success', message: baseOptions.successMessage },
    { type: 'success', message: extraOptions.successMessage },
    { type: 'error', message: 'Extra failure' }
  ])
  assert.deepEqual(harness.refreshCalls, [true, true])
})
