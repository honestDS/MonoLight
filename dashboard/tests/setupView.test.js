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
  watch,
} from 'vue'
import * as advancedSettings from '../src/utils/channelAdvancedSettings.js'
import * as modelMetadata from '../src/utils/channelModelMetadata.js'
import { createChannelTestManager } from '../src/utils/channelTestManager.js'
import { isValidHttpProxy, normalizeHttpProxy } from '../src/utils/channelHttpProxy.js'
import { truncateErrorMessage } from '../src/utils/errorMessage.js'
import * as setupForm from '../src/utils/setupForm.js'
import {
  HOME_PATH,
  LOGIN_PATH,
  normalizeSetupError,
  refreshSetupStatus,
  setupStatusState,
} from '../src/router/setupGuard.js'
import { defaultModelEntry } from '../src/constants/index.js'

const setupViewSource = readFileSync(new URL('../src/views/SetupView.vue', import.meta.url), 'utf8')
const setupViewScriptMatch = setupViewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!setupViewScriptMatch) throw new Error('SetupView.vue script setup was not found')

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

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const normalize = value => JSON.parse(JSON.stringify(value))

const translate = (key, params = {}) => String(key).replace(
  /\{(\w+)\}/g,
  (_match, name) => String(params[name] ?? ''),
)

const createStorage = () => {
  const values = new Map()
  return {
    values,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

const createMessage = messages => {
  const message = value => messages.push({ type: 'message', value })
  message.warning = value => messages.push({ type: 'warning', value })
  message.error = value => messages.push({ type: 'error', value })
  message.success = value => messages.push({ type: 'success', value })
  return message
}

const flush = async () => {
  for (let index = 0; index < 8; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const createHarness = t => {
  const messages = []
  const storage = createStorage()
  const mountedCallbacks = []
  const beforeUnmountCallbacks = []
  const apiState = {
    chatRequests: [],
    metadataRequests: [],
    completeRequests: [],
    completeStarted: createDeferred(),
  }

  const setupApi = {
    status: async () => ({ data: { data: { required: true } } }),
    models: () => {
      const deferred = createDeferred()
      const request = { deferred }
      apiState.metadataRequests.push(request)
      return deferred.promise
    },
    testChat: (payload, config = {}) => {
      const deferred = createDeferred()
      const request = { payload, config, deferred }
      apiState.chatRequests.push(request)
      return deferred.promise
    },
    complete: payload => {
      const deferred = createDeferred()
      const request = { payload, deferred }
      apiState.completeRequests.push(request)
      apiState.completeStarted.resolve(request)
      return deferred.promise
    },
  }

  const openRouterApi = {
    models: () => {
      const deferred = createDeferred()
      const request = { deferred }
      apiState.metadataRequests.push(request)
      return deferred.promise
    },
  }

  const router = {
    replacements: [],
    replace(location) {
      this.replacements.push(location)
      return Promise.resolve()
    },
  }

  const ElMessage = createMessage(messages)
  const profileApi = {
    list: async () => ({ data: { data: { items: [], meta: { tool_options: [] } } } }),
    update: async () => ({ data: { data: {} } }),
  }
  const promptApi = {
    list: async () => ({ data: { data: { items: [] } } }),
    update: async () => ({ data: { data: {} } }),
  }

  setupStatusState.setReady(true)
  const context = vm.createContext({
    computed,
    reactive,
    ref,
    watch,
    onMounted: callback => mountedCallbacks.push(callback),
    onBeforeUnmount: callback => beforeUnmountCallbacks.push(callback),
    useRouter: () => router,
    useI18n: () => ({ t: translate }),
    ElMessage,
    localStorage: storage,
    openRouterApi,
    profileApi,
    promptApi,
    setupApi,
    defaultModelEntry,
    truncateErrorMessage,
    createChannelTestManager,
    normalizeHttpProxy,
    isValidHttpProxy,
    ...advancedSettings,
    ...modelMetadata,
    ...setupForm,
    HOME_PATH,
    LOGIN_PATH,
    normalizeSetupError,
    refreshSetupStatus,
    setupStatusState,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })

  const scope = effectScope()
  let module
  scope.run(() => {
    vm.runInContext(
      `${stripImports(setupViewScriptMatch[1])}
globalThis.__module = {
  form,
  modelEntryStates,
  modelTestResults,
  profileGuideResource,
  profileGuideAuditModelOptions,
  profileGuideActive,
  channelFormRef,
  adminFormRef,
  detectingMetadataEntry,
  submitting,
  getModelEntryState,
  handleDetectedModelChange,
  addModelEntry,
  removeModelEntry,
  handleModelIdInput,
  handleAdvancedSettingsInput,
  validateStep,
  testChatModel,
  detectModelMetadata,
  completeSetup
}
`,
      context,
      { filename: 'SetupView.vue?script-setup' },
    )
    module = context.__module
  })

  for (const callback of mountedCallbacks) callback()

  const harness = {
    apiState,
    module,
    messages,
    router,
    storage,
    async flush() {
      await flush()
    },
    unmounted: false,
    unmount() {
      if (this.unmounted) return
      this.unmounted = true
      for (const callback of beforeUnmountCallbacks.splice(0)) callback()
      scope.stop()
    },
  }

  t.after(() => harness.unmount())
  return harness
}

const configureChannel = harness => {
  harness.module.form.channel.base_url = 'https://api.example.test'
  harness.module.form.channel.api_key = 'test-key'
  harness.module.channelFormRef.value = { validate: async () => true }
}

const configureEntry = (harness, entry, values) => {
  Object.assign(entry, values)
  harness.module.getModelEntryState(entry).advancedSettingsDraft = values.advancedDraft || ''
}

test('keeps selected model entries, configs, and result identities independent', async t => {
  const harness = createHarness(t)
  configureChannel(harness)
  await harness.flush()

  harness.module.handleDetectedModelChange(['model-a', 'model-c'])
  harness.module.addModelEntry()
  const entries = harness.module.form.channel.model_ids
  const addedEntry = entries.pop()
  entries.splice(1, 0, addedEntry)

  const first = entries[0]
  const middle = entries[1]
  const last = entries[2]
  configureEntry(harness, first, {
    model_id: 'model-a',
    protocol: 'OPENAI',
    temperature: 0.2,
    advancedDraft: '{"x-model":"a"}',
  })
  configureEntry(harness, middle, {
    model_id: 'model-middle',
    protocol: 'OPENAI',
    temperature: 0.4,
    advancedDraft: '{"x-model":"middle"}',
  })
  configureEntry(harness, last, {
    model_id: 'model-c',
    protocol: 'OPENAI_RESPONSES',
    temperature: 0.8,
    advancedDraft: '{"x-model":"c"}',
  })

  const firstRun = harness.module.testChatModel(first, 'non_stream', 'first prompt')
  const lastRun = harness.module.testChatModel(last, 'stream', 'last prompt')
  assert.equal(harness.apiState.chatRequests.length, 2)
  harness.apiState.chatRequests[0].deferred.resolve({ data: { data: { answer: 'a' } } })
  harness.apiState.chatRequests[1].deferred.resolve({ data: { data: { answer: 'c' } } })
  await Promise.all([firstRun, lastRun])

  const resultSnapshot = normalize(harness.module.modelTestResults.value)
    .map(result => ({ id: result.id, label: result.label }))
  const firstStateId = harness.module.getModelEntryState(first).id
  const lastStateId = harness.module.getModelEntryState(last).id

  middle.model_id = 'manual-middle'
  harness.module.handleModelIdInput(middle)
  harness.module.removeModelEntry(middle)

  assert.equal(harness.module.form.channel.model_ids.length, 2)
  assert.equal(harness.module.form.channel.model_ids[0], first)
  assert.equal(harness.module.form.channel.model_ids[1], last)
  assert.equal(first.temperature, 0.2)
  assert.equal(last.temperature, 0.8)
  assert.deepEqual(normalize(first.advanced_settings), { custom_headers: { 'x-model': 'a' } })
  assert.deepEqual(normalize(last.advanced_settings), { custom_headers: { 'x-model': 'c' } })
  assert.deepEqual(
    normalize(harness.module.modelTestResults.value).map(result => ({ id: result.id, label: result.label })),
    resultSnapshot,
  )
  assert.equal(harness.module.getModelEntryState(first).id, firstStateId)
  assert.equal(harness.module.getModelEntryState(last).id, lastStateId)

  harness.module.handleDetectedModelChange(['model-a', 'model-c', 'model-a', ' model-a ', 'model-new'])
  const ids = harness.module.form.channel.model_ids.map(entry => entry.model_id)
  assert.equal(ids.filter(id => id === 'model-a').length, 1)
  assert.equal(ids.filter(id => id === 'model-c').length, 1)
  assert.equal(ids.filter(id => id === 'model-new').length, 1)

  while (harness.module.form.channel.model_ids.length > 1) {
    harness.module.removeModelEntry(harness.module.form.channel.model_ids[1])
  }
  const onlyEntry = harness.module.form.channel.model_ids[0]
  harness.module.removeModelEntry(onlyEntry)
  assert.equal(harness.module.form.channel.model_ids.length, 1)
})

test('validates every model entry and allows each error to be corrected', async t => {
  const harness = createHarness(t)
  configureChannel(harness)
  const first = harness.module.form.channel.model_ids[0]
  configureEntry(harness, first, { model_id: 'model-a', protocol: 'OPENAI' })
  harness.module.addModelEntry()
  const second = harness.module.form.channel.model_ids[1]
  const secondState = harness.module.getModelEntryState(second)
  second.model_id = ''
  second.protocol = ''

  assert.equal(await harness.module.validateStep(1), false)
  assert.ok(secondState.modelIdError)
  assert.ok(secondState.protocolError)

  second.model_id = ' model-b '
  second.protocol = 'OPENAI_RESPONSES'
  assert.equal(await harness.module.validateStep(1), true)

  second.model_id = ' model-a '
  assert.equal(await harness.module.validateStep(1), false)
  assert.equal(harness.module.getModelEntryState(first).modelIdError, 'channels.model_id_duplicate')
  assert.equal(secondState.modelIdError, 'channels.model_id_duplicate')

  second.model_id = 'model-b'
  harness.module.handleModelIdInput(second)
  secondState.advancedSettingsDraft = '{"Authorization":"secret"}'
  assert.equal(await harness.module.validateStep(1), false)
  assert.ok(secondState.advancedSettingsError)

  secondState.advancedSettingsDraft = '{"x-request":"accepted"}'
  harness.module.handleAdvancedSettingsInput(second)
  assert.equal(await harness.module.validateStep(1), true)
  assert.deepEqual(normalize(second.advanced_settings), { custom_headers: { 'x-request': 'accepted' } })
})

test('cancels stale model tests without affecting the other model', async t => {
  const harness = createHarness(t)
  configureChannel(harness)
  await harness.flush()
  const first = harness.module.form.channel.model_ids[0]
  configureEntry(harness, first, {
    model_id: 'model-a',
    protocol: 'OPENAI',
    temperature: 0.15,
    advancedDraft: '{"x-model":"a"}',
  })
  harness.module.addModelEntry()
  const second = harness.module.form.channel.model_ids[1]
  configureEntry(harness, second, {
    model_id: 'model-b',
    protocol: 'OPENAI_RESPONSES',
    temperature: 0.85,
    advancedDraft: '{"x-model":"b"}',
  })

  const firstRun = harness.module.testChatModel(first, 'non_stream', 'prompt-a')
  const secondRun = harness.module.testChatModel(second, 'stream', 'prompt-b')
  assert.equal(harness.apiState.chatRequests.length, 2)
  assert.equal(harness.apiState.chatRequests[0].payload.model_id, 'model-a')
  assert.equal(harness.apiState.chatRequests[0].payload.protocol, 'OPENAI')
  assert.equal(harness.apiState.chatRequests[0].payload.temperature, 0.15)
  assert.deepEqual(normalize(harness.apiState.chatRequests[0].payload.advanced_settings), {
    custom_headers: { 'x-model': 'a' },
  })
  assert.equal(harness.apiState.chatRequests[1].payload.model_id, 'model-b')
  assert.equal(harness.apiState.chatRequests[1].payload.protocol, 'OPENAI_RESPONSES')
  assert.equal(harness.apiState.chatRequests[1].payload.temperature, 0.85)
  assert.deepEqual(normalize(harness.apiState.chatRequests[1].payload.advanced_settings), {
    custom_headers: { 'x-model': 'b' },
  })

  first.model_id = 'renamed-model-a'
  harness.module.handleModelIdInput(first)
  assert.equal(harness.apiState.chatRequests[0].config.signal.aborted, true)

  harness.apiState.chatRequests[0].deferred.resolve({ data: { data: { answer: 'stale' } } })
  harness.apiState.chatRequests[1].deferred.resolve({ data: { data: { answer: 'fresh' } } })
  await Promise.all([firstRun, secondRun])

  assert.equal(harness.module.getModelEntryState(first).testState, null)
  const results = normalize(harness.module.modelTestResults.value)
  assert.equal(results.length, 1)
  assert.equal(results[0].label, 'model-b')
  assert.equal(results[0].state.status, 'success')
})

test('keeps metadata tied to its entry across rename and deletion races', async t => {
  const harness = createHarness(t)
  const first = harness.module.form.channel.model_ids[0]
  first.model_id = 'old-model'
  harness.module.addModelEntry()
  const second = harness.module.form.channel.model_ids[1]
  second.model_id = 'new-model'

  const oldRun = harness.module.detectModelMetadata(first)
  assert.equal(harness.apiState.metadataRequests.length, 1)
  first.model_id = 'renamed-old-model'
  harness.module.handleModelIdInput(first)
  harness.module.removeModelEntry(first)

  const newRun = harness.module.detectModelMetadata(second)
  assert.equal(harness.apiState.metadataRequests.length, 2)
  assert.equal(harness.module.detectingMetadataEntry.value, second)

  harness.apiState.metadataRequests[0].deferred.resolve({
    data: {
      data: [{
        id: 'old-model',
        context_length: 99000,
        architecture: { input_modalities: ['image', 'audio'] },
        description: 'old capability',
      }],
    },
  })
  await oldRun
  assert.equal(harness.module.detectingMetadataEntry.value, second)
  assert.equal(second.context_window_k, 64)
  assert.equal(second.image_understanding, false)
  assert.equal(second.description, '')

  harness.apiState.metadataRequests[1].deferred.resolve({
    data: {
      data: [{
        id: 'new-model',
        context_length: 12345,
        architecture: { input_modalities: ['image'] },
        description: 'new capability',
        reasoning: { supported_efforts: ['low', 'high'] },
      }],
    },
  })
  await newRun

  assert.equal(second.context_window_k, 12)
  assert.equal(second.image_understanding, true)
  assert.equal(second.audio_understanding, false)
  assert.equal(second.description, 'new capability')
  assert.deepEqual(normalize(second.reasoning_efforts), ['low', 'high'])
  assert.equal(harness.module.detectingMetadataEntry.value, null)
})

test('builds audit candidates from the complete request snapshot', async t => {
  const harness = createHarness(t)
  configureChannel(harness)
  harness.module.form.admin.password = 'password-one'
  harness.module.form.admin.password_confirm = 'password-one'
  harness.module.form.channel.name = '  submitted channel  '
  harness.module.form.profile.name = '  submitted profile  '

  const first = harness.module.form.channel.model_ids[0]
  configureEntry(harness, first, { model_id: 'submitted-a', protocol: 'OPENAI' })
  harness.module.addModelEntry()
  const second = harness.module.form.channel.model_ids[1]
  configureEntry(harness, second, { model_id: 'submitted-b', protocol: 'OPENAI_RESPONSES' })
  harness.module.adminFormRef.value = { validate: async () => true }

  const completion = harness.module.completeSetup()
  const request = await harness.apiState.completeStarted.promise
  const submittedIds = normalize(request.payload.channel.model_ids).map(model => model.model_id)
  assert.deepEqual(submittedIds, ['submitted-a', 'submitted-b'])

  harness.module.form.channel.name = 'changed after request'
  second.model_id = 'changed-after-request'
  harness.module.handleModelIdInput(second)
  request.deferred.resolve({
    data: {
      data: {
        access_token: 'setup-token',
        token_type: 'bearer',
        profile_id: 7,
        channel_id: 22,
      },
    },
  })
  await completion

  assert.deepEqual(normalize(harness.module.profileGuideResource.model_ids), submittedIds)
  assert.deepEqual(normalize(harness.module.profileGuideAuditModelOptions.value), [
    {
      key: '22::submitted-a',
      label: 'submitted channel / submitted-a',
      channel_id: 22,
      model_id: 'submitted-a',
    },
    {
      key: '22::submitted-b',
      label: 'submitted channel / submitted-b',
      channel_id: 22,
      model_id: 'submitted-b',
    },
  ])
})
