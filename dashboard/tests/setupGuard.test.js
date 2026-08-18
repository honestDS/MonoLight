import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import enSetup from '../src/i18n/locales/en/setup.js'
import zhSetup from '../src/i18n/locales/zh/setup.js'
import enLogin from '../src/i18n/locales/en/login.js'
import zhLogin from '../src/i18n/locales/zh/login.js'
import {
  HOME_PATH,
  LOGIN_PATH,
  SETUP_PATH,
  createSetupGuard,
  createSetupStatusState,
  normalizeSetupError,
  readSetupRequired,
  refreshSetupStatus,
  resolveSetupNavigation,
} from '../src/router/setupGuard.js'

const source = relativePath => readFileSync(fileURLToPath(new URL(`../src/${relativePath}`, import.meta.url)), 'utf8')
const apiSource = source('api/index.js')
const routerSource = source('router/index.js')
const loginSource = source('views/LoginView.vue')
const appSource = source('App.vue')
const setupSource = source('views/SetupView.vue')
const setupStyleSource = source('assets/css/setup.scss')
const localeKeys = value => Object.keys(value).sort()

test('readSetupRequired accepts only boolean status values', () => {
  assert.equal(readSetupRequired({ data: { data: { required: true } } }), true)
  assert.equal(readSetupRequired({ data: { data: { required: false } } }), false)
  for (const response of [undefined, {}, { data: {} }, { data: { data: {} } }, { data: { data: { required: 0 } } }]) {
    assert.throws(() => readSetupRequired(response), { name: 'TypeError' })
  }
})

test('normalizeSetupError preserves business details and normalizes ordinary errors', () => {
  assert.deepEqual(normalizeSetupError({ response: { data: { code: 'SETUP_BUSY', message: 'Try later' } } }), {
    code: 'SETUP_BUSY',
    message: 'Try later',
  })
  assert.deepEqual(normalizeSetupError(new Error('network failed')), { code: null, message: 'network failed' })
  assert.deepEqual(normalizeSetupError({ response: { data: { code: 409 } }, message: 'fallback' }), {
    code: 409,
    message: 'fallback',
  })
})

test('createSetupStatusState publishes phases, supports subscriptions, and freezes snapshots and errors', () => {
  const state = createSetupStatusState()
  const snapshots = []
  const unsubscribe = state.subscribe(snapshot => snapshots.push(snapshot))

  assert.deepEqual(state.getSnapshot(), { phase: 'idle', required: null, error: null })
  state.setChecking()
  state.setReady(true)
  const error = { code: 'E_STATUS', message: 'failed', detail: { retryable: true } }
  state.setError(error)
  const failed = state.getSnapshot()
  assert.equal(failed.phase, 'error')
  assert.deepEqual(failed.error, error)
  assert.equal(Object.isFrozen(failed), true)
  assert.equal(Object.isFrozen(failed.error), true)
  assert.equal(snapshots.length, 3)

  unsubscribe()
  state.setReady(false)
  assert.equal(snapshots.length, 3)
  assert.throws(() => state.setReady('true'), { name: 'TypeError' })
})

test('refreshSetupStatus transitions to ready on success and rethrows errors with business code', async () => {
  const readyState = createSetupStatusState()
  const calls = []
  const required = await refreshSetupStatus({
    state: readyState,
    statusRequest: async () => {
      calls.push('status')
      return { data: { data: { required: false } } }
    },
  })
  assert.equal(required, false)
  assert.deepEqual(calls, ['status'])
  assert.deepEqual(readyState.getSnapshot(), { phase: 'ready', required: false, error: null })

  const failedState = createSetupStatusState()
  const failure = Object.assign(new Error('service rejected'), {
    response: { data: { code: 'SETUP_DOWN', message: 'Unavailable' } },
  })
  await assert.rejects(
    refreshSetupStatus({ state: failedState, statusRequest: async () => { throw failure } }),
    error => error === failure,
  )
  assert.deepEqual(failedState.getSnapshot(), {
    phase: 'error',
    required: null,
    error: { code: 'SETUP_DOWN', message: 'Unavailable' },
  })
})

test('resolveSetupNavigation covers setup state, token state, and every destination class', () => {
  const paths = [SETUP_PATH, LOGIN_PATH, '/', '/profiles']
  for (const toPath of paths) {
    assert.equal(resolveSetupNavigation({ required: true, toPath, hasToken: false }), toPath === SETUP_PATH ? undefined : SETUP_PATH)
    assert.equal(resolveSetupNavigation({ required: true, toPath, hasToken: true }), toPath === SETUP_PATH ? undefined : SETUP_PATH)
    assert.equal(resolveSetupNavigation({ required: false, toPath, hasToken: true }), toPath === SETUP_PATH ? HOME_PATH : undefined)
    assert.equal(resolveSetupNavigation({ required: false, toPath, hasToken: false }), toPath === SETUP_PATH ? LOGIN_PATH : toPath === LOGIN_PATH ? undefined : LOGIN_PATH)
  }
})

test('createSetupGuard sends pending users to setup and completed users according to token', async () => {
  const pendingState = createSetupStatusState()
  const pendingGuard = createSetupGuard({ state: pendingState, getToken: () => null, statusRequest: async () => ({ data: { data: { required: true } } }) })
  assert.equal(await pendingGuard('/profiles'), SETUP_PATH)
  assert.equal(await pendingGuard(SETUP_PATH), undefined)

  const completedState = createSetupStatusState()
  const completedWithToken = createSetupGuard({ state: completedState, getToken: () => 'token', statusRequest: async () => ({ data: { data: { required: false } } }) })
  assert.equal(await completedWithToken(SETUP_PATH), HOME_PATH)

  const completedWithoutToken = createSetupGuard({ state: createSetupStatusState(), getToken: () => '', statusRequest: async () => ({ data: { data: { required: false } } }) })
  assert.equal(await completedWithoutToken(SETUP_PATH), LOGIN_PATH)
  assert.equal(await completedWithoutToken('/profiles'), LOGIN_PATH)
  assert.equal(await completedWithoutToken(LOGIN_PATH), undefined)
})

test('createSetupGuard exposes a retryable setup error without repeating failed checks on setup', async () => {
  const state = createSetupStatusState()
  let calls = 0
  const failure = Object.assign(new Error('database unavailable'), { response: { data: { code: 'DB_DOWN', message: 'Database unavailable' } } })
  const guard = createSetupGuard({ state, getToken: () => null, statusRequest: async () => { calls += 1; throw failure } })

  assert.equal(await guard('/profiles'), SETUP_PATH)
  assert.equal(calls, 1)
  assert.deepEqual(state.getSnapshot(), { phase: 'error', required: null, error: { code: 'DB_DOWN', message: 'Database unavailable' } })
  assert.equal(await guard(SETUP_PATH), undefined)
  assert.equal(calls, 1)
})

test('a successful page retry restores ready state after a failed setup check', async () => {
  const state = createSetupStatusState()
  let calls = 0
  const guard = createSetupGuard({
    state,
    getToken: () => 'token',
    statusRequest: async () => {
      calls += 1
      if (calls === 1) throw Object.assign(new Error('temporary'), { response: { data: { code: 'TEMP', message: 'Temporary failure' } } })
      return { data: { data: { required: false } } }
    },
  })
  assert.equal(await guard('/'), SETUP_PATH)
  assert.equal(await refreshSetupStatus({ state, statusRequest: async () => ({ data: { data: { required: false } } }) }), false)
  assert.deepEqual(state.getSnapshot(), { phase: 'ready', required: false, error: null })
})

test('a failed page retry preserves the latest setup error and leaves setup renderable', async () => {
  const state = createSetupStatusState()
  let guardCalls = 0
  let retryCalls = 0
  const firstFailure = Object.assign(new Error('first failure'), { response: { data: { code: 'FIRST_FAILURE', message: 'First failure' } } })
  const retryFailure = Object.assign(new Error('retry failure'), { response: { data: { code: 'RETRY_FAILURE', message: 'Retry failure' } } })
  const guard = createSetupGuard({
    state,
    getToken: () => null,
    statusRequest: async () => { guardCalls += 1; throw firstFailure },
  })

  assert.equal(await guard('/profiles'), SETUP_PATH)
  assert.equal(guardCalls, 1)
  assert.deepEqual(state.getSnapshot(), {
    phase: 'error',
    required: null,
    error: { code: 'FIRST_FAILURE', message: 'First failure' },
  })
  await assert.rejects(
    refreshSetupStatus({ state, statusRequest: async () => { retryCalls += 1; throw retryFailure } }),
    error => error === retryFailure,
  )
  assert.equal(retryCalls, 1)
  assert.deepEqual(state.getSnapshot(), {
    phase: 'error',
    required: null,
    error: { code: 'RETRY_FAILURE', message: 'Retry failure' },
  })
  assert.equal(await guard(SETUP_PATH), undefined)
  assert.equal(guardCalls, 1)
  assert.equal(retryCalls, 1)
})

test('API and routing sources retain setup endpoints and setup error codes while removing reset flows', () => {
  assert.match(apiSource, /status:\s*\(\)\s*=>\s*request\.get\('\/setup\/status'\)/)
  assert.match(apiSource, /complete:\s*\(data\)\s*=>\s*request\.post\('\/setup\/complete',\s*data\)/)
  assert.match(apiSource, /models:\s*\(data\)\s*=>\s*request\.post\('\/setup\/models',\s*data\)/)
  assert.match(apiSource, /testChat:\s*\(data,\s*config\s*=\s*\{\}\)\s*=>\s*request\.post\('\/setup\/test-chat',\s*data,\s*config\)/)
  assert.match(apiSource, /error\.response\s*=\s*\{\s*data:\s*\{\s*code,\s*message:/)
  for (const text of [apiSource, loginSource, source('i18n/locales/zh/login.js'), source('i18n/locales/en/login.js')]) {
    assert.doesNotMatch(text, /reset_admin|resetAdmin|reset_token/)
  }
  assert.match(routerSource, /path:\s*'\/setup'/)
  assert.match(routerSource, /createSetupGuard\(/)
})

test('App keeps login and setup as independent standalone pages', () => {
  assert.match(appSource, /\['\/login',\s*'\/setup'\]\.includes\(this\.\$route\.path\)/)
  assert.match(routerSource, /path:\s*'\/login'/)
  assert.match(routerSource, /path:\s*'\/setup'/)
})

test('SetupView has only the approved token persistence and submission contract', () => {
  assert.equal((setupSource.match(/localStorage\.setItem\(/g) || []).length, 1)
  assert.match(setupSource, /localStorage\.setItem\('token',\s*access_token\)/)
  assert.doesNotMatch(setupSource, /sessionStorage\.setItem\(/)
  for (const forbidden of ['redirect', 'jwt_secret', 'encryption_key']) assert.doesNotMatch(setupSource, new RegExp(forbidden))

  assert.match(setupSource, /buildSetupRequest\(form\)/)
  assert.match(setupSource, /setupApi\.complete\(buildSetupRequest\(form\)\)/)
  assert.match(setupSource, /setupApi\.models\(/)
  assert.match(setupSource, /setupApi\.testChat\(/)
  assert.match(setupSource, /import\s+ChannelModelEntry\s+from\s+'@\/components\/ChannelModelEntry\.vue'/)
  assert.match(setupSource, /<ChannelModelEntry[\s\S]*:show-remove="false"[\s\S]*:show-enabled="false"/)
  assert.match(setupSource, /readSetupTokenData\(response\)/)
  const submitStart = setupSource.indexOf('setupApi.complete(')
  const submitEnd = setupSource.indexOf('\n', submitStart)
  assert.ok(submitStart >= 0)
  assert.doesNotMatch(setupSource.slice(submitStart, submitEnd), /password_confirm/)
})

test('SetupView keeps model test results in the shared dialog and preserves metadata autofill', () => {
  assert.match(setupSource, /import\s+ModelTestResultDialog\s+from\s+'@\/components\/ModelTestResultDialog\.vue'/)

  const dialogStart = setupSource.indexOf('<ModelTestResultDialog')
  const dialogEnd = setupSource.indexOf('/>', dialogStart)
  assert.ok(dialogStart >= 0)
  assert.ok(dialogEnd > dialogStart)
  const dialogSource = setupSource.slice(dialogStart, dialogEnd)
  assert.match(dialogSource, /v-model:visible="modelTestResultDialogVisible"/)
  assert.match(dialogSource, /:results="modelTestResults"/)
  assert.match(dialogSource, /@update:active-id="activeModelTestResultId = \$event"/)

  const channelEntryStart = setupSource.indexOf('<ChannelModelEntry')
  const channelEntryEnd = setupSource.indexOf('/>', channelEntryStart)
  assert.ok(channelEntryStart >= 0)
  assert.ok(channelEntryEnd > channelEntryStart)
  assert.match(
    setupSource.slice(channelEntryStart, channelEntryEnd),
    /@view-test-result="openModelTestResult"/,
  )
  assert.doesNotMatch(setupSource, /\btestResultExpanded\b/)
  assert.doesNotMatch(setupSource, /update:test-result-expanded/)

  assert.match(setupSource, /@detect-metadata="detectModelMetadata"/)
  assert.match(setupSource, /import\s+\{\s*getOpenRouterModelMatches,\s*applyOpenRouterModelMetadata\s*\}\s+from\s+'@\/utils\/channelModelMetadata\.js'/)
  const metadataStart = setupSource.indexOf('async function detectModelMetadata()')
  const metadataEnd = setupSource.indexOf('\nfunction openChatTestDialog', metadataStart)
  assert.ok(metadataStart >= 0)
  assert.ok(metadataEnd > metadataStart)
  const metadataSource = setupSource.slice(metadataStart, metadataEnd)
  assert.match(metadataSource, /openRouterApi\.models\(\)/)
  assert.match(metadataSource, /getOpenRouterModelMatches\(/)
  assert.match(metadataSource, /applyOpenRouterModelMetadata\(entry,\s*matches\[0\]\)/)
  assert.doesNotMatch(metadataSource, /modelTestResultDialogVisible/)
})

test('SetupView keeps step transition boundaries and reduced-motion styles in the source contract', () => {
  const stepsStart = setupSource.indexOf('<el-steps')
  const sectionHeaderStart = setupSource.indexOf('<div class="setup-section-header">')
  const viewportStart = setupSource.indexOf('<div class="setup-content-viewport">')
  const transitionStart = setupSource.indexOf('<Transition')
  const transitionEnd = setupSource.indexOf('</Transition>', transitionStart)
  const viewportEnd = setupSource.indexOf('</div>', transitionEnd)

  assert.ok(stepsStart >= 0)
  assert.ok(sectionHeaderStart >= 0)
  assert.ok(viewportStart > sectionHeaderStart)
  assert.ok(stepsStart < viewportStart)
  assert.ok(transitionStart > viewportStart)
  assert.ok(transitionEnd > transitionStart)
  assert.ok(viewportEnd > transitionEnd)

  const viewportSource = setupSource.slice(viewportStart, viewportEnd)
  const transitionSource = setupSource.slice(transitionStart, transitionEnd)
  assert.doesNotMatch(viewportSource, /<el-steps\b/)
  assert.match(transitionSource, /<Transition\b[^>]*\bmode="out-in"[^>]*>\s*<div\b[^>]*:key="activeStep"[^>]*class="setup-step-content"/)

  assert.match(setupSource, /const stepTransitionName = ref\('step-forward'\)/)
  const nextStepStart = setupSource.indexOf('async function nextStep()')
  const previousStepStart = setupSource.indexOf('function previousStep()', nextStepStart)
  const completeSetupStart = setupSource.indexOf('async function completeSetup()', previousStepStart)
  assert.ok(nextStepStart >= 0)
  assert.ok(previousStepStart > nextStepStart)
  assert.ok(completeSetupStart > previousStepStart)
  assert.match(setupSource.slice(nextStepStart, previousStepStart), /stepTransitionName\.value\s*=\s*'step-forward'/)
  assert.match(setupSource.slice(previousStepStart, completeSetupStart), /stepTransitionName\.value\s*=\s*'step-backward'/)

  assert.match(setupStyleSource, /\.setup-content-viewport\s*\{[\s\S]*?overflow-x\s*:\s*clip\s*;/)
  assert.match(setupStyleSource, /\.step-forward-enter-from\s*\{[\s\S]*?transform\s*:\s*translateX\(24px\)/)
  assert.match(setupStyleSource, /\.step-forward-leave-to\s*\{[\s\S]*?transform\s*:\s*translateX\(-24px\)/)
  assert.match(setupStyleSource, /\.step-backward-enter-from\s*\{[\s\S]*?transform\s*:\s*translateX\(-24px\)/)
  assert.match(setupStyleSource, /\.step-backward-leave-to\s*\{[\s\S]*?transform\s*:\s*translateX\(24px\)/)
  const reducedMotionStart = setupStyleSource.indexOf('@media (prefers-reduced-motion: reduce)')
  assert.ok(reducedMotionStart >= 0)
  const reducedMotionSource = setupStyleSource.slice(reducedMotionStart)
  assert.match(reducedMotionSource, /transition\s*:\s*none\s*;/)
  assert.match(reducedMotionSource, /transform\s*:\s*none\s*;/)
})

test('Chinese and English setup locales have identical complete key sets and non-empty critical copy', () => {
  assert.deepEqual(localeKeys(zhSetup), localeKeys(enSetup))
  const criticalKeys = [
    'required', 'username_length', 'username_format', 'password_length', 'password_bytes',
    'password_mismatch', 'url_format', 'max_length', 'status_checking', 'status_error_title',
    'status_error_description', 'status_retry', 'complete_failed', 'invalid_token_response',
  ]
  for (const key of criticalKeys) {
    assert.equal(typeof zhSetup[key], 'string')
    assert.ok(zhSetup[key].trim().length > 0)
    assert.equal(typeof enSetup[key], 'string')
    assert.ok(enSetup[key].trim().length > 0)
  }
  assert.deepEqual(localeKeys(zhLogin), localeKeys(enLogin))
})
