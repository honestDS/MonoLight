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
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'
import { loadUseConfirmDialog } from './confirmDialogHarness.js'
import {
  defaultChannelForm,
  defaultModelEntry,
  normalizeModelEntry,
} from '../src/constants/index.js'
import * as channelAdvancedSettings from '../src/utils/channelAdvancedSettings.js'
import * as channelModelMetadata from '../src/utils/channelModelMetadata.js'
import { normalizeHttpProxy, isValidHttpProxy } from '../src/utils/channelHttpProxy.js'
import { createChannelTestManager } from '../src/utils/channelTestManager.js'
import { truncateErrorMessage } from '../src/utils/errorMessage.js'
import enChannels from '../src/i18n/locales/en/channels.js'
import enCommon from '../src/i18n/locales/en/common.js'
import zhChannels from '../src/i18n/locales/zh/channels.js'
import zhCommon from '../src/i18n/locales/zh/common.js'

const channelFormSource = readFileSync(
  new URL('../src/components/ChannelFormDialog.vue', import.meta.url),
  'utf8',
)
const channelFormScriptMatch = channelFormSource.match(
  /<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/,
)
if (!channelFormScriptMatch) throw new Error('ChannelFormDialog.vue script setup was not found')

const stripImports = sourceText => {
  const keptLines = []
  let inImport = false

  for (const line of sourceText.split(/\r?\n/)) {
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

const channelFormScript = stripImports(channelFormScriptMatch[1])
const localeMessages = {
  zh: { common: zhCommon, channels: zhChannels },
  en: { common: enCommon, channels: enChannels },
}

const toHost = value => {
  if (value === undefined) return undefined
  return JSON.parse(JSON.stringify(value))
}

const dataResponse = data => ({ data: { data } })

const createTranslate = locale => {
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: localeMessages,
  })

  return (...args) => {
    const normalizedArgs = args.map((arg, index) => (
      index === 1 && arg && typeof arg === 'object' ? toHost(arg) : arg
    ))
    return i18n.global.t(...normalizedArgs)
  }
}

const createEditChannel = () => ({
  id: 'channel-42',
  name: 'Existing channel',
  api_key: 'channel-api-key',
  base_url: 'https://api.example.test/v1',
  http_proxy: '',
  is_active: true,
  model_ids: [{
    ...defaultModelEntry(),
    model_id: 'chat-model',
    usage: 'CHAT',
    protocol: 'OPENAI',
  }],
})

const createHarness = async ({
  locale = 'zh',
  updateResponses = [],
  confirmationActions = [],
  alertAction = true,
} = {}) => {
  const t = createTranslate(locale)
  const messages = []
  const confirmations = []
  const alerts = []
  const updates = []
  const typesCalls = []
  const remainingUpdates = [...updateResponses]
  const remainingConfirmations = [...confirmationActions]
  const props = reactive({ visible: true, channel: createEditChannel() })
  const emitted = []
  const lifecycleCallbacks = []

  const pushMessage = (type, args) => {
    messages.push({ type, args: toHost(args) })
  }
  const ElMessage = (...args) => pushMessage('message', args)
  for (const type of ['success', 'warning', 'error']) {
    ElMessage[type] = (...args) => pushMessage(type, args)
  }

  const ElMessageBox = {
    confirm: async (message, title, options) => {
      const confirmation = {
        message,
        title,
        options: toHost(options),
      }
      confirmations.push(confirmation)
      const actionValue = remainingConfirmations.length > 0
        ? remainingConfirmations.shift()
        : true
      const action = typeof actionValue === 'function'
        ? await actionValue(confirmation)
        : actionValue
      if (action instanceof Error) throw action
      if (action === 'cancel' || action === 'close') throw action
      return action
    },
    alert: async (message, title, options) => {
      const alert = {
        message,
        title,
        options: toHost(options),
      }
      alerts.push(alert)
      const action = typeof alertAction === 'function' ? await alertAction(alert) : alertAction
      if (action instanceof Error) throw action
      return action
    },
  }

  const channelApi = {
    types: async () => {
      typesCalls.push({})
      return dataResponse({
        model_protocols: {
          CHAT: ['OPENAI'],
          EMBEDDING: ['OPENAI_EMBEDDING'],
          RERANK: ['COHERE_RERANK'],
          IMAGE_GENERATION: ['OPENAI_IMAGE'],
        },
        model_usages: ['CHAT', 'EMBEDDING', 'RERANK', 'IMAGE_GENERATION'],
      })
    },
    update: async (id, payload) => {
      updates.push({ id, payload: toHost(payload) })
      const response = remainingUpdates.length > 0 ? remainingUpdates.shift() : {}
      if (response instanceof Error) throw response
      if (typeof response === 'function') return response(id, payload)
      return dataResponse(response)
    },
    models: async () => dataResponse({ models: [] }),
    testChat: async () => dataResponse({}),
    testImageGeneration: async () => dataResponse({}),
    testEmbeddingDimension: async () => dataResponse({ dimension: 1 }),
  }
  const openRouterApi = {
    models: async () => dataResponse([]),
  }

  const emit = (...args) => emitted.push(toHost(args))
  const actualUseConfirmDialog = loadUseConfirmDialog({ t, ElMessageBox })
  const context = vm.createContext({
    computed,
    reactive,
    ref,
    watch,
    defaultChannelForm,
    defaultModelEntry,
    normalizeModelEntry,
    ElMessage,
    ElMessageBox,
    useI18n: () => ({ t }),
    useConfirmDialog: () => actualUseConfirmDialog(),
    channelApi,
    openRouterApi,
    truncateErrorMessage,
    createChannelTestManager,
    normalizeHttpProxy,
    isValidHttpProxy,
    ...channelAdvancedSettings,
    ...channelModelMetadata,
    defineProps: () => props,
    defineEmits: () => emit,
    onBeforeUnmount: callback => lifecycleCallbacks.push(callback),
    console,
  })

  const scope = effectScope()
  let module
  scope.run(() => {
    vm.runInContext(
      `${channelFormScript}
globalThis.__module = { form, submitting, submitForm }`,
      context,
      { filename: 'ChannelFormDialog.vue?script-setup' },
    )
    module = context.__module
  })

  const harness = {
    module,
    props,
    messages,
    confirmations,
    alerts,
    updates,
    emitted,
    typesCalls,
    t,
    async flush() {
      for (let index = 0; index < 8; index += 1) {
        await nextTick()
        await Promise.resolve()
      }
    },
    stop() {
      for (const callback of lifecycleCallbacks) callback()
      scope.stop()
    },
  }

  await harness.flush()
  return harness
}

const createTrackedHarness = async (t, options) => {
  const harness = await createHarness(options)
  t.after(() => harness.stop())
  return harness
}

const impactCases = [
  { field: 'synced_memory_organization_settings', messageKey: 'memory_organization_synced' },
  { field: 'retained_memory_organization_settings', messageKey: 'memory_organization_retained' },
  { field: 'disabled_memory_organization_settings', messageKey: 'memory_organization_disabled' },
  { field: 'pending_deletion_models', messageKey: 'pending_deletion_models_warning' },
  { field: 'deferred_memory_organization_settings', messageKey: 'memory_organization_deferred' },
  { field: 'synced_profile_rules', messageKey: 'profile_rules_synced' },
  { field: 'removed_profile_rules', messageKey: 'profile_rules_removed' },
  { field: 'synced_audit_refs', messageKey: 'audit_refs_synced' },
  { field: 'cleared_audit_refs', messageKey: 'audit_refs_cleared' },
]

test('channel update renders every localized impact count and confirms with the shared dialog', async t => {
  for (const locale of ['zh', 'en']) {
    for (const [index, impactCase] of impactCases.entries()) {
      const token = `${locale}-${impactCase.field}-token`
      const impact = {
        requires_confirmation: true,
        config_impact_token: token,
        [impactCase.field]: index + 1,
      }
      const harness = await createTrackedHarness(t, {
        locale,
        updateResponses: [impact, {}],
        confirmationActions: [true],
      })

      await harness.module.submitForm()

      assert.equal(harness.typesCalls.length, 1, `${locale}: types API should initialize once`)
      assert.equal(harness.confirmations.length, 1, `${locale}: ${impactCase.field}`)
      const confirmation = harness.confirmations[0]
      const html = await renderToString(confirmation.message)
      const expectedMessage = harness.t(`channels.${impactCase.messageKey}`, { count: index + 1 })
      assert.ok(html.includes(expectedMessage), `${locale}: ${expectedMessage}`)
      assert.equal((html.match(/<section\b/g) || []).length, 1, `${locale}: ${impactCase.field}`)
      assert.equal((html.match(/<ul\b/g) || []).length, 0, `${locale}: ${impactCase.field}`)
      assert.equal(confirmation.title, harness.t('channels.config_refs_changed_title'), locale)
      assert.equal(confirmation.options.confirmButtonText, harness.t('channels.confirm'), locale)
      assert.equal(confirmation.options.cancelButtonText, harness.t('channels.cancel'), locale)
      assert.equal(confirmation.options.customClass, 'app-confirm-dialog', locale)
      assert.equal(confirmation.options.type, 'warning', locale)
      assert.equal(confirmation.options.showCancelButton, true, locale)
      assert.equal(confirmation.options.confirmButtonType, undefined, locale)
      assert.equal(confirmation.options.confirmButtonClass, undefined, locale)
      assert.equal(confirmation.options.cancelButtonClass, undefined, locale)

      assert.equal(harness.updates.length, 2, `${locale}: ${impactCase.field}`)
      assert.equal(harness.updates[0].payload.confirm_config_impact, undefined, locale)
      assert.equal(harness.updates[0].payload.config_impact_token, undefined, locale)
      assert.equal(harness.updates[1].payload.confirm_config_impact, true, locale)
      assert.equal(harness.updates[1].payload.config_impact_token, token, locale)
      assert.deepEqual(
        harness.messages.filter(message => message.type === 'success').map(message => message.args[0]),
        [harness.t('channels.update_success')],
        locale,
      )
      assert.deepEqual(harness.emitted, [['update:visible', false], ['saved']], locale)
      assert.equal(harness.module.submitting.value, false, locale)
    }
  }
})

test('channel impact sections preserve the source count order', async t => {
  const impact = {
    requires_confirmation: true,
    config_impact_token: 'ordered-impact-token',
  }
  impactCases.forEach((impactCase, index) => {
    impact[impactCase.field] = index + 1
  })
  const harness = await createTrackedHarness(t, {
    locale: 'en',
    updateResponses: [impact, {}],
    confirmationActions: [true],
  })

  await harness.module.submitForm()

  const html = await renderToString(harness.confirmations[0].message)
  let previousPosition = -1
  for (const [index, impactCase] of impactCases.entries()) {
    const expectedMessage = harness.t(`channels.${impactCase.messageKey}`, { count: index + 1 })
    const position = html.indexOf(expectedMessage)
    assert.ok(position > previousPosition, impactCase.field)
    previousPosition = position
  }
  assert.equal((html.match(/<section\b/g) || []).length, impactCases.length)
})

test('cancel and close only preview a channel update and restore submitting', async t => {
  for (const action of ['cancel', 'close']) {
    const harness = await createTrackedHarness(t, {
      updateResponses: [{
        requires_confirmation: true,
        config_impact_token: `${action}-token`,
        pending_deletion_models: 1,
      }],
      confirmationActions: [action],
    })

    await harness.module.submitForm()

    assert.equal(harness.confirmations.length, 1, action)
    assert.equal(harness.updates.length, 1, action)
    assert.equal(harness.updates[0].payload.confirm_config_impact, undefined, action)
    assert.equal(harness.updates[0].payload.config_impact_token, undefined, action)
    assert.deepEqual(harness.messages.filter(message => message.type === 'success'), [], action)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], action)
    assert.deepEqual(harness.emitted, [], action)
    assert.equal(harness.props.visible, true, action)
    assert.equal(harness.module.form.name, 'Existing channel', action)
    assert.equal(harness.module.submitting.value, false, action)
  }
})

test('a fresh server confirmation displays fresh impact and uses the fresh token', async t => {
  const firstImpact = {
    requires_confirmation: true,
    config_impact_token: 'first-channel-impact-token',
    synced_profile_rules: 1,
  }
  const secondImpact = {
    requires_confirmation: true,
    config_impact_token: 'second-channel-impact-token',
    removed_profile_rules: 2,
  }
  const harness = await createTrackedHarness(t, {
    locale: 'en',
    updateResponses: [firstImpact, secondImpact, {}],
    confirmationActions: [true, true],
  })

  await harness.module.submitForm()

  assert.equal(harness.confirmations.length, 2)
  const firstHtml = await renderToString(harness.confirmations[0].message)
  const secondHtml = await renderToString(harness.confirmations[1].message)
  assert.ok(firstHtml.includes(harness.t('channels.profile_rules_synced', { count: 1 })))
  assert.doesNotMatch(firstHtml, /unavailable channel routing/)
  assert.ok(secondHtml.includes(harness.t('channels.profile_rules_removed', { count: 2 })))
  assert.doesNotMatch(secondHtml, /synchronize 1 channel routing/)
  assert.deepEqual(harness.updates.map(update => ({
    id: update.id,
    confirm: update.payload.confirm_config_impact,
    token: update.payload.config_impact_token,
  })), [
    { id: 'channel-42', confirm: undefined, token: undefined },
    { id: 'channel-42', confirm: true, token: 'first-channel-impact-token' },
    { id: 'channel-42', confirm: true, token: 'second-channel-impact-token' },
  ])
  assert.deepEqual(harness.emitted, [['update:visible', false], ['saved']])
  assert.equal(harness.module.submitting.value, false)
})

test('an update with no impact skips confirmation and completes normally', async t => {
  const harness = await createTrackedHarness(t, {
    updateResponses: [{ requires_confirmation: false }],
  })

  await harness.module.submitForm()

  assert.equal(harness.updates.length, 1)
  assert.equal(harness.confirmations.length, 0)
  assert.deepEqual(harness.messages.filter(message => message.type === 'success').map(message => message.args[0]), [
    harness.t('channels.update_success'),
  ])
  assert.deepEqual(harness.emitted, [['update:visible', false], ['saved']])
  assert.equal(harness.module.submitting.value, false)
})

test('submit failure and confirmation popup errors do not continue or close the form', async t => {
  const submitFailure = await createTrackedHarness(t, {
    updateResponses: [new Error('channel update failed')],
  })
  await submitFailure.module.submitForm()
  assert.equal(submitFailure.updates.length, 1)
  assert.equal(submitFailure.confirmations.length, 0)
  assert.deepEqual(submitFailure.emitted, [])
  assert.deepEqual(submitFailure.messages.filter(message => message.type === 'error').map(message => message.args[0]), [
    'channel update failed',
  ])
  assert.equal(submitFailure.props.visible, true)
  assert.equal(submitFailure.module.form.name, 'Existing channel')
  assert.equal(submitFailure.module.submitting.value, false)

  const popupError = await createTrackedHarness(t, {
    updateResponses: [{
      requires_confirmation: true,
      config_impact_token: 'popup-error-token',
      synced_audit_refs: 1,
    }],
    confirmationActions: [new Error('confirmation popup failed')],
  })
  await popupError.module.submitForm()
  assert.equal(popupError.updates.length, 1)
  assert.equal(popupError.confirmations.length, 1)
  assert.equal(popupError.updates[0].payload.confirm_config_impact, undefined)
  assert.deepEqual(popupError.emitted, [])
  assert.deepEqual(popupError.messages.filter(message => message.type === 'error').map(message => message.args[0]), [
    'confirmation popup failed',
  ])
  assert.equal(popupError.props.visible, true)
  assert.equal(popupError.module.form.name, 'Existing channel')
  assert.equal(popupError.module.submitting.value, false)
})

test('concurrent memory organization notice remains a separate alert after success', async t => {
  const harness = await createTrackedHarness(t, {
    locale: 'zh',
    updateResponses: [{ concurrently_disabled_memory_organization_settings: 3 }],
  })

  await harness.module.submitForm()

  assert.equal(harness.confirmations.length, 0)
  assert.equal(harness.alerts.length, 1)
  const alert = harness.alerts[0]
  assert.equal(
    alert.message,
    harness.t('channels.memory_organization_concurrently_disabled_notice', { count: 3 }),
  )
  assert.equal(alert.title, harness.t('channels.memory_organization_concurrently_disabled_title'))
  assert.equal(alert.options.type, 'warning')
  assert.equal(alert.options.confirmButtonText, harness.t('channels.confirm'))
  assert.deepEqual(harness.messages.filter(message => message.type === 'success').map(message => message.args[0]), [
    harness.t('channels.update_success'),
  ])
  assert.deepEqual(harness.emitted, [['update:visible', false], ['saved']])
})
