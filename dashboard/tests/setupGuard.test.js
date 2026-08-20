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
const mainSource = source('main.js')
const routerSource = source('router/index.js')
const loginSource = source('views/LoginView.vue')
const appSource = source('App.vue')
const setupSource = source('views/SetupView.vue')
const profileGuideSource = source('components/SetupProfileGuide.vue')
const setupStyleSource = source('assets/css/setup.scss')
const publicIndexSource = readFileSync(fileURLToPath(new URL('../public/index.html', import.meta.url)), 'utf8')
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
  assert.match(apiSource, /status:\s*\(\)\s*=>\s*request\.get\('\/setup\/status',\s*withSetupCredentials\(\)\)/)
  assert.match(apiSource, /complete:\s*\(data\)\s*=>\s*request\.post\('\/setup\/complete',\s*data,\s*withSetupCredentials\(\)\)/)
  assert.match(apiSource, /models:\s*\(data\)\s*=>\s*request\.post\('\/setup\/models',\s*data,\s*withSetupCredentials\(\)\)/)
  assert.match(apiSource, /testChat:\s*\(data,\s*config\s*=\s*\{\}\)\s*=>\s*request\.post\('\/setup\/test-chat',\s*data,\s*withSetupCredentials\(config\)\)/)
  assert.match(apiSource, /const withSetupCredentials = \(config = \{\}\) => \(\{\s*\.\.\.config,\s*withCredentials:\s*true\s*\}\)/)
  assert.match(apiSource, /list:\s*\(params\)\s*=>\s*request\.get\('\/profiles\/list',\s*\{\s*params\s*\}\)/)
  assert.match(apiSource, /update:\s*\(id,\s*data\)\s*=>\s*request\.post\(`\/profiles\/update\?profile_id=\$\{id\}`,\s*data\)/)
  assert.match(apiSource, /error\.response\s*=\s*\{\s*data:\s*\{\s*code,\s*message:/)
  for (const text of [apiSource, loginSource, source('i18n/locales/zh/login.js'), source('i18n/locales/en/login.js')]) {
    assert.doesNotMatch(text, /reset_admin|resetAdmin|reset_token/)
  }
  assert.match(routerSource, /path:\s*'\/setup'/)
  assert.match(routerSource, /createSetupGuard\(/)
})

test('startup waits for router readiness and the public loading page stays self-contained', () => {
  const routerReadyStart = mainSource.indexOf('router.isReady()')
  const mountStart = mainSource.indexOf("app.mount('#app')")
  assert.ok(routerReadyStart >= 0)
  assert.ok(mountStart > routerReadyStart)
  assert.match(mainSource, /router\.isReady\(\)\.then\(\(\)\s*=>\s*\{\s*app\.mount\('#app'\)\s*\}\)/)

  assert.match(publicIndexSource, /id="app-loading"/)
  assert.match(publicIndexSource, /role="status"/)
  assert.match(publicIndexSource, /Loading\.\.\./)
  assert.match(
    publicIndexSource,
    /\.app-loading__spinner\s*\{[\s\S]*width:\s*24px\s*;[\s\S]*height:\s*24px\s*;[\s\S]*border-radius:\s*50%\s*;[\s\S]*border:\s*3px solid[\s\S]*border-top-color:[\s\S]*animation:\s*app-loading-spin/,
  )
  assert.match(publicIndexSource, /@keyframes\s+app-loading-spin[\s\S]*transform:\s*rotate\(360deg\)/)
  assert.match(publicIndexSource, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*\.app-loading__spinner[\s\S]*animation:\s*none\s*;/)
  assert.doesNotMatch(publicIndexSource, /<link\b/i)
  assert.doesNotMatch(publicIndexSource, /\burl\s*\(/i)
  assert.doesNotMatch(publicIndexSource, /<img\b/i)
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

  assert.match(setupSource, /import\s+\{\s*openRouterApi,\s*profileApi,\s*promptApi,\s*setupApi\s*\}\s+from\s+'@\/api'/)
  assert.match(setupSource, /import\s+SetupProfileGuide\s+from\s+'@\/components\/SetupProfileGuide\.vue'/)
  assert.match(setupSource, /readSetupProfileGuideData/)
  assert.match(setupSource, /const\s+profileGuideForm\s*=\s*reactive\(\{[\s\S]*?\bprompt:\s*null\b/)
  assert.match(setupSource, /const\s+profileGuideCommittedPromptContent\s*=\s*ref\(null\)/)
  assert.match(setupSource, /profileGuideForm\.prompt\s*=\s*[^;\n]+/)
  assert.match(setupSource, /const profileGuideActive = ref\(false\)/)
  assert.match(setupSource, /const profileGuideStarted = ref\(false\)/)
  assert.match(setupSource, /const profileGuideStep = ref\(0\)/)
  assert.match(setupSource, /profileGuideActive\.value\s*=\s*true/)
  assert.match(setupSource, /profileGuideActive\.value\s*=\s*false/)
  assert.match(
    setupSource,
    /<SetupProfileGuide[\s\S]*ref="profileGuideRef"[\s\S]*:active-section="profileGuideStep"[\s\S]*:transition-name="profileGuideTransitionName"[\s\S]*:show-steps="false"/,
  )

  const setupSteps = [...setupSource.matchAll(/<el-steps\b[\s\S]*?<\/el-steps>/g)].map(match => match[0])
  assert.equal(setupSteps.length, 2)
  assert.match(setupSteps[0], /(?:^|\s)align-center(?:\s|>)/)
  assert.match(setupSteps[1], /(?:^|\s)align-center(?:\s|>)/)
  assert.match(setupSteps[0], /<el-steps\b[^>]*\bv-if="!profileGuideStarted"[^>]*>/)
  assert.match(setupSteps[1], /<el-steps\b[^>]*\bv-else\b[^>]*>/)
  assert.match(setupSteps[1], /:active="profileGuideStep"/)
  for (const titleKey of ['base_settings', 'security_settings', 'tool_settings']) {
    assert.match(setupSteps[1], new RegExp(`profiles\\.${titleKey}`))
  }

  const entryStart = setupSource.indexOf('<div v-if="profileGuideActive && !profileGuideStarted" class="setup-profile-guide-entry">')
  const entryEnd = setupSource.indexOf('<template v-else>', entryStart)
  assert.ok(entryStart >= 0)
  assert.ok(entryEnd > entryStart)
  const entrySource = setupSource.slice(entryStart, entryEnd)
  assert.equal((entrySource.match(/<el-button\b/g) || []).length, 2)
  assert.match(entrySource, /<el-button\b[^>]*@click="finishProfileGuide"/)
  assert.match(entrySource, /<el-button\b[^>]*@click="startProfileGuide"/)
  assert.match(entrySource, /t\('setup\.skip_step'\)/)
  assert.match(entrySource, /t\('setup\.continue_configuration'\)/)
  assert.match(entrySource, /class="setup-profile-guide-entry-description"/)
  assert.match(entrySource, /t\('setup\.profile_guide_description'\)/)
  assert.doesNotMatch(entrySource, /<SetupProfileGuide\b/)
  assert.doesNotMatch(entrySource, /(?:loadProfileGuide|profileApi\.(?:list|update))\s*\(/)

  const completeSetupStart = setupSource.indexOf('async function completeSetup()')
  const startProfileGuideStart = setupSource.indexOf('function startProfileGuide()', completeSetupStart)
  const loadProfileGuideStart = setupSource.indexOf('async function loadProfileGuide()', startProfileGuideStart)
  assert.ok(completeSetupStart >= 0)
  assert.ok(startProfileGuideStart > completeSetupStart)
  assert.ok(loadProfileGuideStart > startProfileGuideStart)
  const completeSetupSource = setupSource.slice(completeSetupStart, startProfileGuideStart)
  const startProfileGuideSource = setupSource.slice(startProfileGuideStart, loadProfileGuideStart)
  const startedAssignment = startProfileGuideSource.indexOf('profileGuideStarted.value = true')
  const loadProfileGuideCall = startProfileGuideSource.indexOf('void loadProfileGuide()')
  assert.ok(startedAssignment >= 0)
  assert.ok(loadProfileGuideCall > startedAssignment)
  assert.doesNotMatch(completeSetupSource, /\bloadProfileGuide\s*\(/)

  assert.match(setupSource, /profileApi\.list\(\{\s*page:\s*1,\s*size:\s*1000\s*\}\)/)
  assert.match(setupSource, /promptApi\.list\(\{\s*page:\s*1,\s*size:\s*1000\s*\}\)/)
  assert.match(
    setupSource,
    /readSetupProfileGuideData\(\s*profileResponse,\s*promptResponse,\s*profileGuideResource\.profile_id\s*,?\s*\)/,
  )
  assert.match(setupSource, /profileApi\.update\(profileGuideResource\.profile_id,\s*\{\s*configs\s*\}\)/)
  assert.match(setupSource, /promptApi\.update\(prompt\.id,\s*\{\s*content:\s*prompt\.content\s*\}\)/)
  assert.match(setupSource, /profileGuideRef\.value\?\.commitPendingInputs\(\)/)
  assert.match(setupSource, /setupStatus\.required\s*\|\|\s*profileGuideActive/)
  assert.match(setupSource, /setupStatus\.required\s*===\s*false\s*&&\s*!profileGuideActive/)
  for (const forbidden of [/\bprofileFormRef\b/, /setup\.profile_name/, /profile_name_placeholder/]) {
    assert.doesNotMatch(setupSource, forbidden)
  }

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
  const sectionHeaderStart = setupSource.indexOf('class="setup-section-header"')
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

test('SetupProfileGuide keeps the three optional configuration groups and source contract', () => {
  const profileGuideStepsTag = profileGuideSource.match(/<el-steps\b[^>]*>/)?.[0]
  assert.ok(profileGuideStepsTag)
  assert.match(profileGuideStepsTag, /(?:^|\s)align-center(?:\s|>)/)
  assert.match(profileGuideSource, /<el-steps\b[^>]*\bv-if="showSteps"[^>]*>/)
  assert.match(profileGuideSource, /<el-steps[\s\S]*:active="activeSection"[\s\S]*class="setup-profile-guide__steps"/)
  assert.match(profileGuideSource, /showSteps:\s*\{\s*type:\s*Boolean,\s*default:\s*true\s*\}/)
  assert.equal((profileGuideSource.match(/<el-step\b/g) || []).length, 3)
  assert.match(profileGuideSource, /<section v-if="activeSection === 0" class="setup-profile-guide__section">/)
  assert.match(profileGuideSource, /<section v-else-if="activeSection === 1" class="setup-profile-guide__section">/)
  assert.match(profileGuideSource, /<section v-else class="setup-profile-guide__section">/)

  for (const field of [
    /form\.prompt\.content/,
    /form\.configs\.other\.context_summary_threshold_percent/,
    /security\.audit_channel_id/,
    /security\.audit_model_id/,
    /form\.configs\.security\.audit_report_language/,
    /form\.configs\.security\.audit_confirmation_timeout_seconds/,
    /form\.configs\.tool\.allowed_operation_dirs/,
    /form\.configs\.tool\.enabled_tools/,
    /form\.configs\.tool\.firecrawl_api_key/,
  ]) {
    assert.match(profileGuideSource, field)
  }
  assert.match(
    profileGuideSource,
    /<el-slider\b[^>]*v-model\s*=\s*"form\.configs\.security\.audit_threshold"[^>]*:min\s*=\s*"1"[^>]*:max\s*=\s*"7"[^>]*show-stops\b[^>]*show-input\b[^>]*class\s*=\s*"setup-profile-guide__slider"[^>]*>/,
  )
  assert.match(
    profileGuideSource,
    /\.setup-profile-guide\s*:deep\(\.el-slider__input\)\s*\{\s*flex\s*:\s*0\s+0\s+130px\s*;\s*width\s*:\s*130px\s*;\s*max-width\s*:\s*40%\s*;\s*\}/,
  )
  assert.match(profileGuideSource, /class="setup-profile-guide__section-description"/)
  assert.match(profileGuideSource, /t\('setup\.audit_guide_description'\)/)
  assert.match(
    profileGuideSource,
    /\.setup-profile-guide__section-description\s*\{[\s\S]*?color\s*:\s*var\(--setup-profile-guide-muted\)\s*;[\s\S]*?font-size\s*:\s*14px\s*;[\s\S]*?line-height\s*:\s*1\.6\s*;[\s\S]*?overflow-wrap\s*:\s*anywhere\s*;[\s\S]*?word-break\s*:\s*break-word\s*;[\s\S]*?white-space\s*:\s*normal\s*;/,
  )
  for (const key of ['default_prompt', 'default_prompt_placeholder', 'default_prompt_hint']) {
    assert.match(profileGuideSource, new RegExp(`setup\\.${key}`))
  }
  assert.ok(profileGuideSource.indexOf('form.prompt.content') < profileGuideSource.indexOf('form.configs.other.context_summary_threshold_percent'))

  assert.match(
    profileGuideSource,
    /<Transition\s+:name="transitionName"\s+mode="out-in">[\s\S]*<div\s+:key="activeSection"\s+class="setup-profile-guide__content">/,
  )
  assert.match(profileGuideSource, /const commitPendingInputs = \(\) => \{/)
  assert.match(profileGuideSource, /const discardPendingInputs = \(\) => \{/)
  assert.match(profileGuideSource, /commitPendingInputs[\s\S]*addAllowedOperationDir\(\)/)
  assert.doesNotMatch(profileGuideSource, /addFileSendBlockedExtension\(\)/)
  assert.match(profileGuideSource, /defineExpose\(\{\s*commitPendingInputs\s*,\s*discardPendingInputs\s*\}\)/)

  for (const forbidden of [
    /profiles\.scheduling_control/,
    /profiles\.file_send_config/,
    /\bmax_parallel_tools\b/,
    /\bexecutor_max_workers\b/,
    /\bbackground_task_max_concurrency\b/,
    /\bscheduled_task_max_concurrency\b/,
    /\bmax_turns\b/,
    /\btool_timeout\b/,
    /\bimage_generation_timeout\b/,
    /\bfile_send_max_count\b/,
    /\bfile_send_max_single_size_mb\b/,
    /\bfile_send_max_total_size_mb\b/,
    /\bfile_send_blocked_extensions\b/,
    /\bfileSendBlockedExtensionDraft\b/,
    /\baddFileSendBlockedExtension\b/,
    /\bremoveFileSendBlockedExtension\b/,
    /setup-profile-guide__file-grid/,
  ]) {
    assert.doesNotMatch(profileGuideSource, forbidden)
  }

  const reducedMotionStart = profileGuideSource.indexOf('@media (prefers-reduced-motion: reduce)')
  assert.ok(reducedMotionStart >= 0)
  const reducedMotionSource = profileGuideSource.slice(reducedMotionStart)
  assert.match(reducedMotionSource, /transition\s*:\s*none\s*;/)
  assert.match(reducedMotionSource, /transform\s*:\s*none\s*;/)

  assert.doesNotMatch(profileGuideSource, /\bmemory\b/i)
  assert.doesNotMatch(profileGuideSource, /profile[_-]?name/i)
})

test('Setup language control stays inside the shell with responsive card-relative positioning', () => {
  const shellStart = setupSource.indexOf('<main class="setup-shell">')
  const languageStart = setupSource.indexOf('<div class="setup-language">')
  const brandStart = setupSource.indexOf('<header class="setup-brand">')
  const shellEnd = setupSource.indexOf('</main>', shellStart)
  assert.ok(shellStart >= 0)
  assert.ok(languageStart > shellStart)
  assert.ok(brandStart > languageStart)
  assert.ok(brandStart < shellEnd)

  assert.match(setupStyleSource, /\.setup-shell\s*\{[^}]*position\s*:\s*relative\s*;/)
  assert.match(
    setupStyleSource,
    /\.setup-language\s*\{[^}]*position\s*:\s*absolute\s*;[^}]*top\s*:\s*20px\s*;[^}]*right\s*:\s*24px\s*;/,
  )

  const pageStyleStart = setupStyleSource.indexOf('.setup-page {')
  const pageStyleEnd = setupStyleSource.indexOf('}', pageStyleStart)
  assert.ok(pageStyleStart >= 0)
  assert.ok(pageStyleEnd > pageStyleStart)
  assert.doesNotMatch(setupStyleSource.slice(pageStyleStart, pageStyleEnd), /padding(?:-top)?\s*:\s*76px/)

  const mediumMediaStart = setupStyleSource.indexOf('@media (max-width: 720px)')
  const narrowMediaStart = setupStyleSource.indexOf('@media (max-width: 420px)')
  const reducedMotionStart = setupStyleSource.indexOf('@media (prefers-reduced-motion: reduce)')
  assert.ok(mediumMediaStart >= 0)
  assert.ok(narrowMediaStart > mediumMediaStart)
  assert.ok(reducedMotionStart > narrowMediaStart)
  assert.match(
    setupStyleSource.slice(mediumMediaStart, narrowMediaStart),
    /\.setup-language\s*\{[^}]*top\s*:\s*16px\s*;[^}]*right\s*:\s*16px\s*;/,
  )
  assert.match(
    setupStyleSource.slice(narrowMediaStart, reducedMotionStart),
    /\.setup-language\s*\{[^}]*top\s*:\s*12px\s*;[^}]*right\s*:\s*12px\s*;/,
  )
})

test('Chinese and English setup locales have identical complete key sets and non-empty critical copy', () => {
  assert.deepEqual(localeKeys(zhSetup), localeKeys(enSetup))
  const criticalKeys = [
    'required', 'username_length', 'username_format', 'password_length', 'password_bytes',
    'password_mismatch', 'url_format', 'max_length', 'status_checking', 'status_error_title',
    'status_error_description', 'status_retry', 'complete_failed', 'invalid_token_response',
    'skip_step', 'continue_configuration', 'skip_and_finish', 'save_and_continue', 'save_and_finish', 'guide_load_failed',
    'profile_guide_description', 'audit_guide_description',
  ]
  for (const key of criticalKeys) {
    assert.equal(typeof zhSetup[key], 'string')
    assert.ok(zhSetup[key].trim().length > 0)
    assert.equal(typeof enSetup[key], 'string')
    assert.ok(enSetup[key].trim().length > 0)
  }
  assert.deepEqual(localeKeys(zhLogin), localeKeys(enLogin))
})
