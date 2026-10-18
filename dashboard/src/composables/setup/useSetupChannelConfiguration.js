import { computed, reactive, ref, watch } from 'vue'
import { defaultModelEntry } from '../../constants/index.js'
import { truncateErrorMessage } from '../../utils/errorMessage.js'
import { createChannelTestManager } from '../../utils/channelTestManager.js'
import { normalizeHttpProxy, isValidHttpProxy } from '../../utils/channelHttpProxy.js'
import {
  customHeadersTemplate,
  customHeadersPlaceholder,
  formatAdvancedSettings,
  parseAdvancedSettingsDraft,
  mergeCustomHeaders,
} from '../../utils/channelAdvancedSettings.js'
import {
  SETUP_PROTOCOLS,
  getOpenRouterModelMatches,
  getOpenRouterReasoningEfforts,
  applyOpenRouterModelMetadata,
} from '../../utils/channelModelMetadata.js'
import {
  validateSetupApiKey,
  validateSetupBaseUrl,
  validateSetupModelId,
  validateSetupName,
  validateSetupPassword,
  validateSetupPasswordConfirmation,
  validateSetupHttpProxy,
  validateSetupProtocol,
  validateSetupUsername,
  addSetupDetectedModels,
} from '../../utils/setupForm.js'

export function useSetupChannelConfiguration({ form, t, setupApi, openRouterApi, message }) {
  const adminFormRef = ref()
  const channelFormRef = ref()
  const proxyError = ref('')
  const modelEntryStates = reactive(new Map())
  const detectedModels = ref([])
  const selectedDetectedModels = ref([])
  const detectingModels = ref(false)
  const detectingMetadataEntry = ref(null)
  const modelTestResultDialogVisible = ref(false)
  const activeModelTestResultId = ref('')
  const chatTestDialogVisible = ref(false)
  const chatTestEntry = ref(null)
  const chatTestPromptError = ref('')
  const chatTestForm = reactive({
    testMode: 'non_stream',
    prompt: '',
  })
  const channelTestManager = createChannelTestManager()

  let openRouterModelsCache = null
  let nextModelEntryId = 0

  function getModelEntryState(entry) {
    let state = modelEntryStates.get(entry)
    if (!state) {
      state = {
        id: `setup-model-${++nextModelEntryId}`,
        modelIdError: '',
        protocolError: '',
        advancedSettingsDraft: formatAdvancedSettings(entry?.advanced_settings),
        advancedSettingsError: '',
        advancedSettingsExpanded: false,
        reasoningEffortOptions: [],
        testState: null,
      }
      modelEntryStates.set(entry, state)
    }
    return modelEntryStates.get(entry)
  }

  function isEntryActive(entry) {
    return form.channel.model_ids.includes(entry)
  }

  const createSetupValidator = validator => (_rule, value, callback) => {
    const error = validator(value)
    if (error) {
      callback(new Error(t('setup.' + error.key, error.params)))
      return
    }
    callback()
  }

  const validateChannelProxyRule = (_rule, value, callback) => {
    const error = validateSetupHttpProxy(value)
    proxyError.value = error ? t('channels.http_proxy_format_error') : ''
    if (error) {
      callback(new Error(proxyError.value))
      return
    }
    callback()
  }

  const adminRules = {
    username: [{ validator: createSetupValidator(validateSetupUsername), trigger: 'blur' }],
    password: [{ validator: createSetupValidator(validateSetupPassword), trigger: 'blur' }],
    password_confirm: [{
      validator: createSetupValidator(value => validateSetupPasswordConfirmation(value, form.admin.password)),
      trigger: 'blur',
    }],
  }

  const channelRules = {
    name: [{ validator: createSetupValidator(validateSetupName), trigger: 'blur' }],
    base_url: [{ validator: createSetupValidator(validateSetupBaseUrl), trigger: 'blur' }],
    api_key: [{ validator: createSetupValidator(validateSetupApiKey), trigger: 'blur' }],
    http_proxy: [{ validator: validateChannelProxyRule, trigger: 'blur' }],
  }

  const modelTestResults = computed(() => {
    return form.channel.model_ids
      .map((entry, idx) => {
        const state = getModelEntryState(entry)
        if (!state.testState) return null

        const modelId = typeof entry.model_id === 'string' ? entry.model_id.trim() : ''
        return {
          id: state.id,
          label: modelId || `${t('channels.model_entry')} #${idx + 1}`,
          state: state.testState,
        }
      })
      .filter(Boolean)
  })

  function syncModelTestResultDialog() {
    const results = modelTestResults.value
    if (results.length === 0) {
      modelTestResultDialogVisible.value = false
      activeModelTestResultId.value = ''
      return
    }

    if (!results.some(result => result.id === activeModelTestResultId.value)) {
      activeModelTestResultId.value = results[0].id
    }
  }

  function clearChannelTest(entry) {
    channelTestManager.cancel(entry)
    if (isEntryActive(entry)) {
      getModelEntryState(entry).testState = null
    }
    syncModelTestResultDialog()
  }

  function invalidateChannelTests() {
    channelTestManager.invalidate()
    for (const entry of form.channel.model_ids) {
      getModelEntryState(entry).testState = null
    }
    modelTestResultDialogVisible.value = false
    activeModelTestResultId.value = ''
  }

  function beginChannelTest(entry, testState) {
    if (!isEntryActive(entry)) return null
    const token = channelTestManager.begin(entry)
    if (!token) return null
    const state = getModelEntryState(entry)
    state.testState = testState
    activeModelTestResultId.value = state.id
    modelTestResultDialogVisible.value = true
    return token
  }

  function openModelTestResult(entry) {
    if (!isEntryActive(entry)) return
    const state = getModelEntryState(entry)
    if (!state.testState) return
    activeModelTestResultId.value = state.id
    modelTestResultDialogVisible.value = true
  }

  function resetDetectedModels() {
    selectedDetectedModels.value = []
    detectedModels.value = []
  }

  function syncDetectedSelection() {
    selectedDetectedModels.value = detectedModels.value
      .filter(model => form.channel.model_ids.some(entry => (
        typeof entry.model_id === 'string' && entry.model_id.trim() === model.id.trim()
      )))
      .map(model => model.id)
  }

  function validateChannelHttpProxy() {
    const valid = isValidHttpProxy(form.channel.http_proxy)
    proxyError.value = valid ? '' : t('channels.http_proxy_format_error')
    if (!valid) {
      message.warning(proxyError.value)
    }
    return valid
  }

  function validateModelAdvancedSettings(entry) {
    if (!isEntryActive(entry)) return null

    const state = getModelEntryState(entry)
    const result = parseAdvancedSettingsDraft(state.advancedSettingsDraft, t)
    state.advancedSettingsError = result.error
    if (result.error) {
      state.advancedSettingsExpanded = true
      message.warning(result.error)
      return null
    }
    return mergeCustomHeaders(entry, result.value)
  }

  function validateAllAdvancedSettings() {
    let firstError = ''
    for (const entry of form.channel.model_ids) {
      const state = getModelEntryState(entry)
      const result = parseAdvancedSettingsDraft(state.advancedSettingsDraft, t)
      state.advancedSettingsError = result.error
      if (result.error) {
        state.advancedSettingsExpanded = true
        if (!firstError) firstError = result.error
        continue
      }
      mergeCustomHeaders(entry, result.value)
    }

    if (firstError) {
      message.warning(firstError)
      return false
    }
    return true
  }

  function formatSetupValidationError(error) {
    return error ? t('setup.' + error.key, error.params) : ''
  }

  function validateModelEntries() {
    if (form.channel.model_ids.length === 0) {
      message.warning(t('channels.fill_required'))
      return false
    }

    let firstError = ''
    const seenModelIds = new Map()
    for (const entry of form.channel.model_ids) {
      const state = getModelEntryState(entry)
      state.modelIdError = formatSetupValidationError(validateSetupModelId(entry.model_id))
      state.protocolError = formatSetupValidationError(validateSetupProtocol(entry.protocol))

      if (!firstError && state.modelIdError) firstError = state.modelIdError
      if (!firstError && state.protocolError) firstError = state.protocolError

      const modelId = typeof entry.model_id === 'string' ? entry.model_id.trim() : ''
      if (!modelId) continue
      if (seenModelIds.has(modelId)) {
        const previousEntry = seenModelIds.get(modelId)
        const previousState = getModelEntryState(previousEntry)
        state.modelIdError = t('channels.model_id_duplicate')
        previousState.modelIdError = t('channels.model_id_duplicate')
        if (!firstError) firstError = state.modelIdError
      } else {
        seenModelIds.set(modelId, entry)
      }
    }

    if (firstError) {
      message.warning(firstError)
      return false
    }
    return true
  }

  function handleModelIdInput(entry) {
    if (!isEntryActive(entry)) return
    const state = getModelEntryState(entry)
    state.modelIdError = ''
    state.reasoningEffortOptions = []
    clearChannelTest(entry)
    syncDetectedSelection()
  }

  function handleProtocolChange(entry) {
    if (!isEntryActive(entry)) return
    getModelEntryState(entry).protocolError = ''
    clearChannelTest(entry)
  }

  function handleAdvancedSettingsInput(entry) {
    if (!isEntryActive(entry)) return
    getModelEntryState(entry).advancedSettingsError = ''
  }

  function fillCustomHeadersTemplate(entry) {
    if (!isEntryActive(entry)) return
    clearChannelTest(entry)
    const state = getModelEntryState(entry)
    state.advancedSettingsDraft = JSON.stringify(customHeadersTemplate, null, 2)
    state.advancedSettingsError = ''
  }

  function handleDetectedModelChange(values) {
    if (!Array.isArray(values)) return
    const touchedEntries = addSetupDetectedModels(form.channel.model_ids, values)
    for (const touchedEntry of touchedEntries) {
      const entry = form.channel.model_ids.find(candidate => candidate.model_id === touchedEntry.model_id)
      if (!entry) continue
      clearChannelTest(entry)
      const state = getModelEntryState(entry)
      state.modelIdError = ''
      state.reasoningEffortOptions = []
    }
    syncDetectedSelection()
  }

  function addModelEntry() {
    form.channel.model_ids.push(defaultModelEntry())
    const entry = form.channel.model_ids[form.channel.model_ids.length - 1]
    getModelEntryState(entry)
  }

  function removeModelEntry(entry) {
    if (form.channel.model_ids.length <= 1) return
    const index = form.channel.model_ids.indexOf(entry)
    if (index < 0) return

    clearChannelTest(entry)
    if (chatTestEntry.value === entry) {
      chatTestDialogVisible.value = false
      resetChatTestDialogState()
    }
    if (detectingMetadataEntry.value === entry) {
      detectingMetadataEntry.value = null
    }

    form.channel.model_ids.splice(index, 1)
    modelEntryStates.delete(entry)
    syncDetectedSelection()
    syncModelTestResultDialog()
  }

  async function detectModelList() {
    if (!form.channel.base_url || !form.channel.base_url.trim()) {
      return message.warning(t('channels.model_list_base_url_required'))
    }
    if (!form.channel.api_key || !form.channel.api_key.trim()) {
      return message.warning(t('channels.model_list_api_key_required'))
    }
    if (!validateChannelHttpProxy()) return

    detectingModels.value = true
    try {
      const res = await setupApi.models({
        api_key: form.channel.api_key || null,
        base_url: form.channel.base_url || null,
        http_proxy: normalizeHttpProxy(form.channel.http_proxy) || null,
      })
      const models = res.data?.data?.models
      detectedModels.value = Array.isArray(models)
        ? models.filter(model => (
          model &&
          typeof model === 'object' &&
          !Array.isArray(model) &&
          typeof model.id === 'string' &&
          model.id.trim()
        ))
        : []
      syncDetectedSelection()
      if (detectedModels.value.length === 0) {
        message.warning(t('channels.model_list_empty'))
        return
      }
      message.success(t('channels.model_list_success', { count: detectedModels.value.length }))
    } catch (error) {
      message.error(error.message || t('channels.model_list_failed'))
    } finally {
      detectingModels.value = false
    }
  }

  function formatErrorDetail(error) {
    const detail = error?.response?.data
    let errorMessage
    if (detail && typeof detail === 'object') {
      errorMessage = detail.message || detail.error || JSON.stringify(detail)
    } else {
      errorMessage = error?.message || t('channels.chat_test_failed')
    }
    return truncateErrorMessage(errorMessage)
  }

  async function detectModelMetadata(entry) {
    if (!isEntryActive(entry)) return
    if (detectingMetadataEntry.value) return
    if (entry.usage !== 'CHAT') {
      return message.warning(t('channels.model_metadata_chat_only'))
    }
    if (typeof entry.model_id !== 'string' || !entry.model_id.trim()) {
      getModelEntryState(entry).modelIdError = t('channels.model_id_required')
      return message.warning(t('channels.model_id_required'))
    }

    const requestedModelId = entry.model_id.trim()
    detectingMetadataEntry.value = entry
    const state = getModelEntryState(entry)
    state.reasoningEffortOptions = []
    try {
      if (!openRouterModelsCache) {
        const res = await openRouterApi.models()
        const models = res.data?.data
        if (!Array.isArray(models) || models.some(model => (
          !model || typeof model !== 'object' || Array.isArray(model)
        ))) {
          throw new Error(t('channels.model_metadata_invalid_response'))
        }
        openRouterModelsCache = models
      }

      const matches = getOpenRouterModelMatches(openRouterModelsCache, requestedModelId)
      if (matches.length === 0) {
        throw new Error(t('channels.model_metadata_not_found', { model: requestedModelId }))
      }
      if (matches.length > 1) {
        throw new Error(t('channels.model_metadata_ambiguous', { models: matches.map(model => model.id).join(', ') }))
      }

      if (!isEntryActive(entry) || typeof entry.model_id !== 'string' || entry.model_id.trim() !== requestedModelId) return
      const { fields: filledFields, model } = applyOpenRouterModelMetadata(entry, matches[0])
      const reasoningEfforts = getOpenRouterReasoningEfforts(model)
      if (filledFields.length === 0) {
        throw new Error(t('channels.model_metadata_no_mappable_fields'))
      }

      state.reasoningEffortOptions = reasoningEfforts
      clearChannelTest(entry)
      message.success(t('channels.model_metadata_detect_success', {
        model: typeof model.id === 'string' && model.id.trim() ? model.id : requestedModelId,
        fields: filledFields.map(field => t('channels.' + field)).join(', '),
      }))
      if (filledFields.includes('max_tokens')) {
        message.warning(t('channels.model_metadata_max_tokens_adjusted', {
          context_window_k: entry.context_window_k,
          max_tokens: entry.max_tokens,
        }))
      }
    } catch (error) {
      if (!isEntryActive(entry) || typeof entry.model_id !== 'string' || entry.model_id.trim() !== requestedModelId) return
      message.error(error.message || t('channels.model_metadata_detect_failed'))
    } finally {
      if (detectingMetadataEntry.value === entry) {
        detectingMetadataEntry.value = null
      }
    }
  }

  function openChatTestDialog(entry) {
    if (!isEntryActive(entry)) return
    chatTestEntry.value = entry
    chatTestForm.testMode = 'non_stream'
    chatTestForm.prompt = ''
    chatTestPromptError.value = ''
    chatTestDialogVisible.value = true
  }

  function resetChatTestDialogState() {
    chatTestEntry.value = null
    chatTestForm.testMode = 'non_stream'
    chatTestForm.prompt = ''
    chatTestPromptError.value = ''
  }

  function confirmChatTest() {
    const prompt = chatTestForm.prompt.trim()
    if (!prompt) {
      chatTestPromptError.value = t('channels.chat_test_prompt_required')
      return
    }

    const entry = chatTestEntry.value
    if (!entry) return
    const testMode = chatTestForm.testMode
    chatTestDialogVisible.value = false
    void testChatModel(entry, testMode, prompt)
  }

  async function testChatModel(entry, testMode, prompt) {
    if (!isEntryActive(entry)) return
    const state = getModelEntryState(entry)
    if (entry.usage !== 'CHAT') {
      return message.warning(t('channels.chat_test_chat_only'))
    }
    if (!entry.protocol) {
      state.protocolError = t('channels.model_protocol_required')
      return message.warning(t('channels.model_protocol_required'))
    }
    if (!form.channel.base_url || !form.channel.base_url.trim()) {
      return message.warning(t('channels.model_list_base_url_required'))
    }
    if (!form.channel.api_key || !form.channel.api_key.trim()) {
      return message.warning(t('channels.model_list_api_key_required'))
    }
    if (typeof entry.model_id !== 'string' || !entry.model_id.trim()) {
      state.modelIdError = t('channels.model_id_required')
      return message.warning(t('channels.model_id_required'))
    }
    if (!validateChannelHttpProxy()) return
    const advancedSettings = validateModelAdvancedSettings(entry)
    if (!advancedSettings) return

    const token = beginChannelTest(entry, {
      status: 'running',
      kind: 'CHAT',
      testMode,
      data: null,
      error: '',
    })
    if (!token) return

    try {
      const res = await setupApi.testChat({
        api_key: form.channel.api_key || null,
        base_url: form.channel.base_url || null,
        http_proxy: normalizeHttpProxy(form.channel.http_proxy) || null,
        model_id: entry.model_id.trim(),
        protocol: entry.protocol,
        temperature: entry.temperature,
        top_p: entry.top_p,
        reasoning_effort: null,
        max_tokens: entry.max_tokens || 0,
        test_mode: testMode,
        prompt,
        advanced_settings: advancedSettings,
      }, { signal: token.signal })
      if (!isEntryActive(entry) || !channelTestManager.isCurrent(token)) return
      getModelEntryState(entry).testState = {
        status: 'success',
        kind: 'CHAT',
        testMode,
        data: res.data?.data || {},
        error: '',
      }
    } catch (error) {
      if (isEntryActive(entry) && channelTestManager.isCurrent(token)) {
        getModelEntryState(entry).testState = {
          status: 'error',
          kind: 'CHAT',
          testMode,
          data: null,
          error: formatErrorDetail(error),
        }
      }
    } finally {
      channelTestManager.finish(token)
    }
  }

  watch(
    () => [form.channel.base_url, form.channel.api_key, form.channel.http_proxy],
    () => {
      resetDetectedModels()
      invalidateChannelTests()
      proxyError.value = ''
    }
  )

  async function validateForm(formRef) {
    if (!formRef.value) return false

    try {
      return await formRef.value.validate()
    } catch {
      return false
    }
  }

  async function validateStep(index) {
    if (index === 1) {
      const modelsValid = validateModelEntries()
      const advancedSettingsValid = validateAllAdvancedSettings()
      const proxyValid = validateChannelHttpProxy()
      const formValid = await validateForm(channelFormRef)
      return modelsValid && advancedSettingsValid && proxyValid && formValid
    }

    const formRefs = [adminFormRef, channelFormRef]
    return validateForm(formRefs[index])
  }

  return {
    adminFormRef,
    channelFormRef,
    adminRules,
    channelRules,
    SETUP_PROTOCOLS,
    customHeadersPlaceholder,
    proxyError,
    modelEntryStates,
    detectedModels,
    selectedDetectedModels,
    detectingModels,
    detectingMetadataEntry,
    modelTestResultDialogVisible,
    activeModelTestResultId,
    chatTestDialogVisible,
    chatTestPromptError,
    chatTestForm,
    modelTestResults,
    getModelEntryState,
    clearChannelTest,
    invalidateChannelTests,
    openModelTestResult,
    resetChatTestDialogState,
    handleDetectedModelChange,
    addModelEntry,
    removeModelEntry,
    detectModelList,
    detectModelMetadata,
    openChatTestDialog,
    confirmChatTest,
    handleModelIdInput,
    handleProtocolChange,
    handleAdvancedSettingsInput,
    fillCustomHeadersTemplate,
    validateStep,
    testChatModel,
  }
}
