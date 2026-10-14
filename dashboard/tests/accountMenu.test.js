import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import vm from 'node:vm'
import { TextEncoder } from 'node:util'

import enCommon from '../src/i18n/locales/en/common.js'
import zhCommon from '../src/i18n/locales/zh/common.js'

const root = dirname(fileURLToPath(import.meta.url))
const appSource = readFileSync(resolve(root, '../src/App.vue'), 'utf8')
const appScriptMatch = appSource.match(/<script>([\s\S]*?)<\/script>/)
if (!appScriptMatch) throw new Error('App.vue script was not found')

const stripImportLines = source => source
  .split('\n')
  .filter(line => !line.trimStart().startsWith('import '))
  .join('\n')

const appScript = stripImportLines(appScriptMatch[1])
  .replace('export default', 'globalThis.__appOptions =')

const commonByLocale = { zh: zhCommon, en: enCommon }

const translate = (locale, key, params = {}) => {
  const path = key.startsWith('common.') ? key.slice('common.'.length) : key
  const value = path.split('.').reduce((current, part) => current?.[part], commonByLocale[locale])
  if (typeof value !== 'string') return key
  return value.replace(/\{(\w+)\}/g, (_match, name) => String(params[name] ?? ''))
}

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flushPromises = async () => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

const normalize = value => JSON.parse(JSON.stringify(value))

const createStorage = () => {
  const values = new Map()
  return {
    values,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

const createHarness = ({ locale = 'zh', token = 'initial-token' } = {}) => {
  let currentLocale = locale
  let validateImplementation = () => Promise.resolve(true)
  let apiImplementation = () => Promise.resolve()

  const storage = createStorage()
  if (token !== null) storage.setItem('token', token)

  const messages = []
  const ElMessage = {
    success: value => messages.push({ type: 'success', value }),
    error: value => messages.push({ type: 'error', value }),
    warning: value => messages.push({ type: 'warning', value }),
  }

  const router = {
    pushes: [],
    push: location => {
      router.pushes.push(location)
      return Promise.resolve()
    },
  }

  const permissions = {
    marker: 'permissions-present',
    clearCalls: 0,
    clear() {
      this.clearCalls += 1
      this.marker = null
    },
  }

  const apiCalls = []
  const authApi = {
    changePassword: payload => {
      apiCalls.push(payload)
      return apiImplementation(payload)
    },
  }

  const formRef = {
    validateCalls: 0,
    clearValidateCalls: 0,
    validate() {
      formRef.validateCalls += 1
      return validateImplementation()
    },
    clearValidate() {
      formRef.clearValidateCalls += 1
    },
  }

  const context = vm.createContext({
    TextEncoder,
    ElMessage,
    authApi,
    localStorage: storage,
    routeNameMap: {},
    useResizeObserver: () => {},
    useSessionTasks: () => ({
      tasks: [],
      error: null,
      loading: false,
      refresh: () => {},
      openSession: () => {},
    }),
    useUserPermissionsStore: () => permissions,
    LanguageSwitcher: {},
    SessionTaskCenter: {},
    githubIcon: 'github-icon-placeholder',
    logoImage: 'logo-placeholder',
    zhCn: 'zh-locale-placeholder',
    en: 'en-locale-placeholder',
  })
  vm.runInContext(appScript, context, { filename: 'App.vue?script' })

  const options = context.__appOptions
  const data = options.data()
  const instance = {
    ...data,
    $route: { path: '/chat' },
    $router: router,
    $i18n: { locale: currentLocale },
    $t: (key, params) => translate(currentLocale, key, params),
    $nextTick: callback => Promise.resolve().then(() => callback?.()),
    $refs: { passwordFormRef: formRef },
    userPermissions: permissions,
  }

  for (const [name, method] of Object.entries(options.methods)) {
    instance[name] = method.bind(instance)
  }

  return {
    options,
    instance,
    storage,
    router,
    permissions,
    messages,
    apiCalls,
    formRef,
    setLocale(value) {
      currentLocale = value
      instance.$i18n.locale = value
    },
    setValidateImplementation(value) {
      validateImplementation = value
    },
    setApiImplementation(value) {
      apiImplementation = value
    },
    passwordRules() {
      return options.computed.passwordRules.call(instance)
    },
    async flush() {
      await flushPromises()
    },
  }
}

const validatorResult = (rule, value) => {
  let result = null
  rule.validator({}, value, error => { result = error || null })
  return result
}

const assertValidatorAccepts = (rule, value) => {
  assert.equal(validatorResult(rule, value), null)
}

const assertValidatorRejects = (rule, value, message) => {
  const error = validatorResult(rule, value)
  assert.ok(error)
  assert.equal(error.message, message)
}

test('account commands reset password state and logout through the real methods', async () => {
  const harness = createHarness()
  const { instance } = harness
  instance.passwordForm.current_password = 'old current'
  instance.passwordForm.new_password = 'old new password'
  instance.passwordForm.confirm_password = 'old new password'
  instance.passwordRequestError = 'stale request error'

  instance.handleAccountCommand('change-password')
  await harness.flush()

  assert.equal(instance.passwordDialogVisible, true)
  assert.deepEqual(normalize(instance.passwordForm), {
    current_password: '',
    new_password: '',
    confirm_password: '',
  })
  assert.equal(instance.passwordRequestError, '')

  instance.handleAccountCommand('logout')
  assert.equal(harness.storage.getItem('token'), null)
  assert.equal(harness.permissions.clearCalls, 1)
  assert.equal(harness.permissions.marker, null)
  assert.deepEqual(harness.router.pushes, ['/login'])
})

test('passwordRules use the real validators for character, byte, and confirmation limits', () => {
  for (const locale of ['zh', 'en']) {
    const harness = createHarness({ locale })
    const rules = harness.passwordRules()
    const requiredFields = ['current_password', 'new_password', 'confirm_password']

    for (const field of requiredFields) {
      assert.equal(rules[field][0].required, true)
      assert.equal(rules[field][0].message, translate(locale, 'common.account.required'))
    }

    const currentRule = rules.current_password[1]
    const newRule = rules.new_password[1]
    const confirmRule = rules.confirm_password[1]
    assertValidatorAccepts(currentRule, 'x')
    assertValidatorRejects(
      currentRule,
      'a'.repeat(73),
      translate(locale, 'common.account.password_bytes'),
    )

    assertValidatorRejects(
      newRule,
      'a'.repeat(7),
      translate(locale, 'common.account.password_length'),
    )
    assertValidatorAccepts(newRule, 'a'.repeat(8))
    assertValidatorRejects(
      newRule,
      '\u{1F600}'.repeat(7),
      translate(locale, 'common.account.password_length'),
    )
    assertValidatorAccepts(newRule, '\u{1F600}'.repeat(8))

    assertValidatorAccepts(newRule, 'a'.repeat(72))
    assertValidatorRejects(
      newRule,
      'a'.repeat(73),
      translate(locale, 'common.account.password_bytes'),
    )
    assertValidatorAccepts(newRule, '中'.repeat(24))
    assertValidatorRejects(
      newRule,
      '中'.repeat(25),
      translate(locale, 'common.account.password_bytes'),
    )
    assertValidatorAccepts(newRule, '\u{1F600}'.repeat(18))
    assertValidatorRejects(
      newRule,
      '\u{1F600}'.repeat(19),
      translate(locale, 'common.account.password_bytes'),
    )

    harness.instance.passwordForm.new_password = 'new-password'
    assertValidatorAccepts(confirmRule, 'new-password')
    assertValidatorRejects(
      confirmRule,
      'different-password',
      translate(locale, 'common.account.password_mismatch'),
    )
  }
})

test('rejected validation stops submission and duplicate submits stay single-flight', async () => {
  const invalidHarness = createHarness()
  invalidHarness.instance.passwordDialogVisible = true
  invalidHarness.instance.passwordForm.current_password = 'current'
  invalidHarness.instance.passwordForm.new_password = 'new-password'
  invalidHarness.instance.passwordForm.confirm_password = 'new-password'
  invalidHarness.setValidateImplementation(() => Promise.reject(new Error('invalid form')))

  await invalidHarness.instance.submitPasswordChange()
  assert.equal(invalidHarness.apiCalls.length, 0)
  assert.equal(invalidHarness.instance.passwordSubmitting, false)
  assert.equal(invalidHarness.instance.passwordDialogVisible, true)
  assert.equal(invalidHarness.storage.getItem('token'), 'initial-token')
  assert.equal(invalidHarness.instance.passwordRequestError, '')

  const harness = createHarness()
  harness.instance.passwordDialogVisible = true
  harness.instance.passwordForm.current_password = 'current'
  harness.instance.passwordForm.new_password = 'new-password'
  harness.instance.passwordForm.confirm_password = 'new-password'
  const validateDeferred = createDeferred()
  const apiDeferred = createDeferred()
  harness.setValidateImplementation(() => validateDeferred.promise)
  harness.setApiImplementation(() => apiDeferred.promise)

  const firstSubmit = harness.instance.submitPasswordChange()
  assert.equal(harness.instance.passwordSubmitting, true)
  await harness.instance.submitPasswordChange()
  assert.equal(harness.apiCalls.length, 0)

  validateDeferred.resolve(true)
  await harness.flush()
  assert.equal(harness.apiCalls.length, 1)
  assert.equal(harness.instance.passwordSubmitting, true)

  await harness.instance.submitPasswordChange()
  assert.equal(harness.apiCalls.length, 1)

  apiDeferred.resolve()
  await firstSubmit
  assert.equal(harness.instance.passwordSubmitting, false)
  assert.equal(harness.storage.getItem('token'), null)
})

test('successful password changes send only preserved form values and use both locale messages', async () => {
  for (const locale of ['zh', 'en']) {
    const harness = createHarness({ locale, token: `token-${locale}` })
    harness.instance.passwordDialogVisible = true
    harness.instance.passwordForm.current_password = ' current value '
    harness.instance.passwordForm.new_password = ' new password '
    harness.instance.passwordForm.confirm_password = ' new password '
    harness.setApiImplementation(() => Promise.resolve({ data: { ok: true } }))

    await harness.instance.submitPasswordChange()

    assert.deepEqual(normalize(harness.apiCalls), [{
      current_password: ' current value ',
      new_password: ' new password ',
    }])
    assert.equal(harness.messages.at(-1).type, 'success')
    assert.equal(harness.messages.at(-1).value, translate(locale, 'common.account.success'))
    assert.equal(harness.instance.passwordDialogVisible, false)
    assert.deepEqual(normalize(harness.instance.passwordForm), {
      current_password: '',
      new_password: '',
      confirm_password: '',
    })
    assert.equal(harness.storage.getItem('token'), null)
    assert.equal(harness.permissions.marker, null)
    assert.deepEqual(harness.router.pushes, ['/login'])
  }
})

test('API and network errors preserve the dialog for a retry and clear the old error on retry', async () => {
  const failures = [
    new Error('HTTP 400: current password is invalid'),
    new Error('network disconnected'),
  ]

  for (const failure of failures) {
    const harness = createHarness({ locale: 'zh', token: 'retry-token' })
    harness.instance.passwordDialogVisible = true
    harness.instance.passwordForm.current_password = ' current '
    harness.instance.passwordForm.new_password = ' new password '
    harness.instance.passwordForm.confirm_password = ' new password '
    const retryDeferred = createDeferred()
    let attempts = 0
    harness.setApiImplementation(() => {
      attempts += 1
      return attempts === 1 ? Promise.reject(failure) : retryDeferred.promise
    })

    await harness.instance.submitPasswordChange()
    assert.equal(harness.apiCalls.length, 1)
    assert.equal(harness.instance.passwordRequestError, failure.message)
    assert.equal(harness.instance.passwordDialogVisible, true)
    assert.deepEqual(normalize(harness.instance.passwordForm), {
      current_password: ' current ',
      new_password: ' new password ',
      confirm_password: ' new password ',
    })
    assert.equal(harness.storage.getItem('token'), 'retry-token')
    assert.equal(harness.permissions.marker, 'permissions-present')
    assert.deepEqual(harness.router.pushes, [])
    assert.equal(harness.instance.passwordSubmitting, false)

    const retry = harness.instance.submitPasswordChange()
    assert.equal(harness.instance.passwordRequestError, '')
    await harness.flush()
    assert.equal(harness.apiCalls.length, 2)
    assert.equal(harness.instance.passwordSubmitting, true)

    retryDeferred.resolve()
    await retry
    assert.equal(harness.instance.passwordSubmitting, false)
    assert.equal(harness.instance.passwordDialogVisible, false)
    assert.equal(harness.storage.getItem('token'), null)
    assert.deepEqual(harness.router.pushes, ['/login'])
  }
})

test('stale validation and HTTP results cannot log out or report for a new token', async () => {
  for (const changeTiming of ['while-validation-pending', 'after-validation-settled']) {
    const harness = createHarness({ token: 'old-token' })
    harness.instance.passwordDialogVisible = true
    harness.instance.passwordForm.current_password = 'current'
    harness.instance.passwordForm.new_password = 'new-password'
    harness.instance.passwordForm.confirm_password = 'new-password'
    const validateDeferred = createDeferred()
    harness.setValidateImplementation(() => validateDeferred.promise)

    const submit = harness.instance.submitPasswordChange()
    if (changeTiming === 'while-validation-pending') {
      harness.storage.setItem('token', 'new-token')
      validateDeferred.resolve(true)
    } else {
      validateDeferred.resolve(true)
      harness.storage.setItem('token', 'new-token')
    }
    await submit

    assert.equal(harness.apiCalls.length, 0)
    assert.equal(harness.storage.getItem('token'), 'new-token')
    assert.equal(harness.permissions.clearCalls, 0)
    assert.deepEqual(harness.router.pushes, [])
    assert.deepEqual(harness.messages, [])
    assert.equal(harness.instance.passwordDialogVisible, true)
    assert.equal(harness.instance.passwordSubmitting, false)
  }

  for (const outcome of ['success', 'failure']) {
    const harness = createHarness({ token: 'old-token' })
    harness.instance.passwordDialogVisible = true
    harness.instance.passwordForm.current_password = 'current'
    harness.instance.passwordForm.new_password = 'new-password'
    harness.instance.passwordForm.confirm_password = 'new-password'
    const apiDeferred = createDeferred()
    harness.setApiImplementation(() => apiDeferred.promise)

    const submit = harness.instance.submitPasswordChange()
    await harness.flush()
    assert.equal(harness.apiCalls.length, 1)
    harness.storage.setItem('token', 'new-token')
    if (outcome === 'success') apiDeferred.resolve()
    else apiDeferred.reject(new Error('stale failure'))
    await submit

    assert.equal(harness.storage.getItem('token'), 'new-token')
    assert.equal(harness.permissions.clearCalls, 0)
    assert.deepEqual(harness.router.pushes, [])
    assert.deepEqual(harness.messages, [])
    assert.equal(harness.instance.passwordRequestError, '')
    assert.equal(harness.instance.passwordDialogVisible, true)
    assert.equal(harness.instance.passwordSubmitting, false)
  }
})

test('missing tokens only perform logout and the standalone watcher resets safely', async () => {
  const noTokenHarness = createHarness({ token: null })
  noTokenHarness.instance.passwordDialogVisible = true
  noTokenHarness.instance.passwordForm.current_password = 'current'
  await noTokenHarness.instance.submitPasswordChange()
  assert.equal(noTokenHarness.apiCalls.length, 0)
  assert.equal(noTokenHarness.permissions.clearCalls, 1)
  assert.deepEqual(noTokenHarness.router.pushes, ['/login'])
  assert.equal(noTokenHarness.instance.passwordSubmitting, false)

  const harness = createHarness()
  const { instance, options } = harness
  instance.passwordDialogVisible = true
  instance.passwordForm.current_password = 'current'
  instance.passwordForm.new_password = 'new-password'
  instance.passwordForm.confirm_password = 'new-password'
  instance.passwordRequestError = 'request error'
  assert.equal(options.computed.isStandalonePage.call(instance), false)
  options.watch.isStandalonePage.call(instance, false)
  assert.equal(instance.passwordDialogVisible, true)
  assert.equal(instance.passwordForm.current_password, 'current')

  instance.$route.path = '/login'
  assert.equal(options.computed.isStandalonePage.call(instance), true)
  options.watch.isStandalonePage.call(instance, true)
  assert.equal(instance.passwordDialogVisible, false)
  assert.deepEqual(normalize(instance.passwordForm), {
    current_password: '',
    new_password: '',
    confirm_password: '',
  })
  assert.equal(instance.passwordRequestError, '')

  instance.passwordSubmitting = true
  instance.passwordForm.current_password = 'pending current'
  instance.passwordRequestError = 'pending error'
  instance.resetPasswordForm()
  instance.resetPasswordForm()
  assert.equal(instance.passwordSubmitting, true)
  assert.deepEqual(normalize(instance.passwordForm), {
    current_password: '',
    new_password: '',
    confirm_password: '',
  })
  assert.equal(instance.passwordRequestError, '')
})
