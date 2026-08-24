<template>
  <el-dialog
    :title="isEdit ? $t('channels.edit_channel') : $t('channels.create_channel')"
    v-model="dialogVisible"
    width="65%"
    class="standard-dialog dialog-with-scroll-body"
    center
    align-center
    :close-on-click-modal="true">
    <div class="channel-settings-shell">
      <div class="channel-settings-title">{{ $t('channels.channel_settings') }}</div>
      <div class="channel-settings-body">
        <div class="channel-settings-top">
          <el-form :model="form" class="channel-settings-form">
            <div class="channel-settings-row channel-settings-row--fields">
              <el-form-item :label="$t('channels.channel_name')">
                <el-input v-model="form.name" :placeholder="$t('channels.channel_name_placeholder')" />
              </el-form-item>
              <el-form-item :label="$t('channels.api_key')">
                <el-input v-model="form.api_key" type="password" show-password :placeholder="$t('channels.api_key_placeholder')" />
              </el-form-item>
              <el-form-item :label="$t('channels.base_url')">
                <el-input v-model="form.base_url" :placeholder="$t('channels.base_url_placeholder')" />
              </el-form-item>
              <el-form-item :label="$t('channels.http_proxy')" :error="proxyError" class="http-proxy-form-item">
                <div class="http-proxy-input-wrapper">
                  <el-input
                    v-model="form.http_proxy"
                    :placeholder="$t('channels.http_proxy_placeholder')"
                    @input="proxyError = ''" />
                </div>
              </el-form-item>
              <el-form-item class="channel-model-detect-form-item">
                <div class="channel-model-detect-row">
                  <el-button type="primary" plain :loading="detectingModels" @click="detectModelList">
                    {{ $t('channels.detect_model_list') }}
                  </el-button>
                  <el-select
                    v-model="selectedDetectedModels"
                    multiple
                    collapse-tags
                    collapse-tags-tooltip
                    class="channel-model-detect-select"
                    popper-class="channel-model-detect-popper"
                    filterable
                    clearable
                    fit-input-width
                    :placeholder="$t('channels.select_detected_model')"
                    :disabled="detectedModels.length === 0"
                    @change="handleDetectedModelChange">
                    <el-option v-for="model in detectedModels" :key="model.id" :label="model.id" :value="model.id">
                      <div class="detected-model-option">
                        <span class="detected-model-name" :title="model.id">{{ model.id }}</span>
                        <span class="detected-model-meta">
                          <span v-if="model.owned_by" class="detected-model-owner" :title="model.owned_by">{{ model.owned_by }}</span>
                        </span>
                      </div>
                    </el-option>
                  </el-select>
                </div>
              </el-form-item>
            </div>
          </el-form>
        </div>

        <ChannelModelEntry
          v-for="(entry, idx) in form.model_ids"
          :key="idx"
          :entry="entry"
          :locked="entry.lifecycle_status === 'pending_delete'"
          :index="idx"
          :model-usages="modelUsages"
          :model-protocols="getModelProtocols(entry.usage)"
          :model-id-error="modelIdErrors[idx]"
          :protocol-error="protocolErrors[idx]"
          :advanced-settings-draft="advancedSettingsDrafts[idx]"
          :advanced-settings-error="advancedSettingsErrors[idx]"
          :advanced-settings-expanded="advancedSettingsExpanded[idx]"
          :custom-headers-placeholder="customHeadersPlaceholder"
          :testing="modelTestStates.get(entry)?.status === 'running'"
          :test-state="modelTestStates.get(entry) || null"
          :detecting-metadata="detectingMetadataIndex === idx"
          :detecting-dimension="detectingDimensionIndex === idx"
          :metadata-detection-disabled="detectingMetadataIndex !== null && detectingMetadataIndex !== idx"
          @test-chat="openChatTestDialog(entry, idx)"
          @test-image-generation="testImageGenerationModel(entry, idx)"
          @detect-metadata="detectModelMetadata(entry, idx)"
          @remove="removeModelEntry(idx)"
          @model-id-input="handleModelIdInput(idx)"
          @usage-change="handleModelUsageChange(entry, idx)"
          @protocol-change="handleProtocolChange(idx)"
          @detect-dimension="detectEmbeddingDimension(entry, idx)"
          @fill-headers-template="fillCustomHeadersTemplate(idx)"
          @advanced-settings-input="handleAdvancedSettingsInput(idx)"
          @test-config-change="clearModelTest(entry)"
          @view-test-result="openModelTestResult(entry)"
          @update:advanced-settings-draft="value => advancedSettingsDrafts[idx] = value"
          @update:advanced-settings-expanded="value => advancedSettingsExpanded[idx] = value" />

        <el-button type="primary" :icon="Plus" @click="addModelEntry">{{ $t('channels.add_model') }}</el-button>
      </div>
    </div>

    <template #footer>
      <el-button @click="dialogVisible = false">{{ $t('channels.cancel') }}</el-button>
      <el-button type="primary" @click="submitForm" :loading="submitting">{{ $t('channels.confirm') }}</el-button>
    </template>
  </el-dialog>

  <ModelTestResultDialog
    v-model:visible="modelTestResultDialogVisible"
    :results="modelTestResults"
    :active-id="activeModelTestResultId"
    @update:active-id="activeModelTestResultId = $event" />

  <el-dialog
    v-model="chatTestDialogVisible"
    :title="$t('channels.chat_test_config_title')"
    width="520px"
    append-to-body
    :close-on-click-modal="true"
    @closed="resetChatTestDialogState">
    <el-form :model="chatTestForm" label-position="top" class="chat-test-form">
      <el-form-item :label="$t('channels.chat_test_mode')">
        <el-radio-group v-model="chatTestForm.testMode">
          <el-radio label="non_stream">{{ $t('channels.chat_test_non_stream') }}</el-radio>
          <el-radio label="stream">{{ $t('channels.chat_test_stream') }}</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item :label="$t('channels.chat_test_prompt')" :error="chatTestPromptError">
        <el-input
          v-model="chatTestForm.prompt"
          type="textarea"
          :rows="5"
          :placeholder="$t('channels.chat_test_prompt_placeholder')"
          @input="chatTestPromptError = ''" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="chatTestDialogVisible = false">{{ $t('channels.cancel') }}</el-button>
      <el-button type="primary" @click="confirmChatTest">{{ $t('channels.chat_test_start') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { h, reactive, ref, computed, watch, onBeforeUnmount } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { channelApi, openRouterApi } from '../api'
import { defaultChannelForm, defaultModelEntry, normalizeModelEntry } from '../constants'
import { truncateErrorMessage } from '../utils/errorMessage.js'
import { createChannelTestManager } from '../utils/channelTestManager.js'
import { normalizeHttpProxy, isValidHttpProxy } from '../utils/channelHttpProxy.js'
import { customHeadersTemplate, customHeadersPlaceholder, ensureAdvancedSettings, formatAdvancedSettings, parseAdvancedSettingsDraft, mergeCustomHeaders } from '../utils/channelAdvancedSettings.js'
import { getOpenRouterModelMatches, applyOpenRouterModelMetadata } from '../utils/channelModelMetadata.js'
import ChannelModelEntry from './ChannelModelEntry.vue'
import ModelTestResultDialog from './ModelTestResultDialog.vue'

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  channel: {
    type: Object,
    default: null
  }
})

const emit = defineEmits(['update:visible', 'saved'])
const { t } = useI18n()

const dialogVisible = computed({
  get: () => props.visible,
  set: value => emit('update:visible', value)
})
const isEdit = computed(() => Boolean(props.channel?.id))
const currentId = computed(() => props.channel?.id ?? null)

const modelProtocols = ref({})
const modelProtocolsLoaded = ref(false)
const modelUsages = ref([])
const submitting = ref(false)
const modelIdErrors = ref([])
const protocolErrors = ref([])
const advancedSettingsDrafts = ref([])
const advancedSettingsErrors = ref([])
const advancedSettingsExpanded = ref([])
const proxyError = ref('')
const selectedDetectedModels = ref([])
const detectedModels = ref([])
const detectingModels = ref(false)
const detectingDimensionIndex = ref(null)
const detectingMetadataIndex = ref(null)
const form = reactive(defaultChannelForm())
const channelTestManager = createChannelTestManager()
const modelTestStates = reactive(new Map())
const modelTestResultDialogVisible = ref(false)
const activeModelTestResultId = ref('')
const modelTestEntryIds = new WeakMap()
let nextModelTestEntryId = 0
const chatTestDialogVisible = ref(false)
const chatTestEntry = ref(null)
const chatTestIndex = ref(null)
const chatTestPromptError = ref('')
const chatTestForm = reactive({
  testMode: 'non_stream',
  prompt: ''
})

let openRouterModelsCache = null
let modelProtocolsPromise = null
let _previousDetectedSelection = []

const defaultModelProtocols = {
  CHAT: 'OPENAI',
  EMBEDDING: 'OPENAI_EMBEDDING',
  RERANK: 'COHERE_RERANK',
  IMAGE_GENERATION: 'OPENAI_IMAGE'
}

const getModelProtocols = (usage) => {
  const protocols = modelProtocols.value[usage]
  if (Array.isArray(protocols) && protocols.length > 0) return protocols
  return modelProtocolsLoaded.value ? [] : [defaultModelProtocols[usage]].filter(Boolean)
}

const getDefaultModelProtocol = (usage) => {
  return getModelProtocols(usage)[0] || defaultModelProtocols[usage] || ''
}

const getModelTestEntryId = (entry) => {
  let id = modelTestEntryIds.get(entry)
  if (!id) {
    id = `model-test-result-${++nextModelTestEntryId}`
    modelTestEntryIds.set(entry, id)
  }
  return id
}

const modelTestResults = computed(() => form.model_ids
  .map((entry, idx) => {
    const state = modelTestStates.get(entry)
    if (!state) return null

    const modelId = typeof entry.model_id === 'string' ? entry.model_id.trim() : ''
    return {
      id: getModelTestEntryId(entry),
      label: modelId || `${t('channels.model_entry')} #${idx + 1}`,
      state
    }
  })
  .filter(Boolean))

const syncModelTestResultDialog = () => {
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

const openModelTestResult = (entry) => {
  if (!modelTestStates.has(entry)) return
  activeModelTestResultId.value = getModelTestEntryId(entry)
  modelTestResultDialogVisible.value = true
}

const validateAdvancedSettings = () => {
  const results = form.model_ids.map((_, idx) => parseAdvancedSettingsDraft(advancedSettingsDrafts.value[idx], t))
  advancedSettingsErrors.value = results.map(result => result.error)
  if (advancedSettingsErrors.value.some(Boolean)) {
    advancedSettingsExpanded.value = results.map((result, idx) => (
      result.error ? [idx] : advancedSettingsExpanded.value[idx]
    ))
    ElMessage.warning(advancedSettingsErrors.value.find(Boolean))
    return null
  }
  return results.map(result => result.value)
}

const validateModelAdvancedSettings = (idx) => {
  const result = parseAdvancedSettingsDraft(advancedSettingsDrafts.value[idx], t)
  advancedSettingsErrors.value[idx] = result.error
  if (result.error) {
    advancedSettingsExpanded.value[idx] = [idx]
    ElMessage.warning(result.error)
    return null
  }
  return mergeCustomHeaders(form.model_ids[idx], result.value)
}

const syncModelEntryStates = () => {
  form.model_ids.forEach(ensureAdvancedSettings)
  modelIdErrors.value = form.model_ids.map((_, idx) => modelIdErrors.value[idx] || '')
  protocolErrors.value = form.model_ids.map((_, idx) => protocolErrors.value[idx] || '')
  advancedSettingsDrafts.value = form.model_ids.map((entry, idx) => (
    typeof advancedSettingsDrafts.value[idx] === 'string'
      ? advancedSettingsDrafts.value[idx]
      : formatAdvancedSettings(entry.advanced_settings)
  ))
  advancedSettingsErrors.value = form.model_ids.map((_, idx) => advancedSettingsErrors.value[idx] || '')
  advancedSettingsExpanded.value = form.model_ids.map((_, idx) => advancedSettingsExpanded.value[idx] || [])
}

const addModelEntry = () => {
  form.model_ids.push(defaultModelEntry())
  syncModelEntryStates()
}

const clearModelTest = (entry) => {
  channelTestManager.cancel(entry)
  modelTestStates.delete(entry)
  syncModelTestResultDialog()
}

const invalidateModelTests = () => {
  channelTestManager.invalidate()
  modelTestStates.clear()
  modelTestResultDialogVisible.value = false
  activeModelTestResultId.value = ''
}

const beginModelTest = (entry, state) => {
  const token = channelTestManager.begin(entry)
  if (!token) return null
  modelTestStates.set(entry, state)
  activeModelTestResultId.value = getModelTestEntryId(entry)
  modelTestResultDialogVisible.value = true
  return token
}

const removeModelEntry = (idx) => {
  const entry = form.model_ids[idx]
  if (entry?.lifecycle_status === 'pending_delete') return
  clearModelTest(entry)
  form.model_ids.splice(idx, 1)
  modelIdErrors.value.splice(idx, 1)
  protocolErrors.value.splice(idx, 1)
  advancedSettingsDrafts.value.splice(idx, 1)
  advancedSettingsErrors.value.splice(idx, 1)
  advancedSettingsExpanded.value.splice(idx, 1)
  syncDetectedSelection()
}

const resetDetectedModels = () => {
  selectedDetectedModels.value = []
  detectedModels.value = []
  _previousDetectedSelection = []
}

const handleModelIdInput = (idx) => {
  modelIdErrors.value[idx] = ''
  syncDetectedSelection()
}

const handleProtocolChange = (idx) => {
  protocolErrors.value[idx] = ''
}

const handleAdvancedSettingsInput = (idx) => {
  advancedSettingsErrors.value[idx] = ''
}

const fillCustomHeadersTemplate = (idx) => {
  const entry = form.model_ids[idx]
  clearModelTest(entry)
  advancedSettingsDrafts.value[idx] = JSON.stringify(customHeadersTemplate, null, 2)
  advancedSettingsErrors.value[idx] = ''
}

const handleModelUsageChange = (entry, idx) => {
  entry.protocol = getDefaultModelProtocol(entry.usage)
  protocolErrors.value[idx] = ''
}

const handleDetectedModelChange = (values) => {
  const added = values.filter(v => !_previousDetectedSelection.includes(v))

  for (const id of added) {
    const exists = form.model_ids.some(entry => (entry.model_id || '').trim() === id)
    if (exists) continue

    let targetIndex = form.model_ids.findIndex(entry => !entry.model_id || !entry.model_id.trim())
    if (targetIndex < 0) {
      addModelEntry()
      targetIndex = form.model_ids.length - 1
    }
    const targetEntry = form.model_ids[targetIndex]
    clearModelTest(targetEntry)
    targetEntry.model_id = id
    modelIdErrors.value[targetIndex] = ''
  }

  syncDetectedSelection()
}

watch(
  () => [form.base_url, form.api_key, form.http_proxy],
  () => {
    resetDetectedModels()
    invalidateModelTests()
  }
)

const syncDetectedSelection = () => {
  syncModelEntryStates()
  const matched = detectedModels.value
    .filter(m => form.model_ids.some(e => (e.model_id || '').trim() === m.id))
    .map(m => m.id)
  selectedDetectedModels.value = matched
  _previousDetectedSelection = [...matched]
}

const detectModelList = async () => {
  if (!form.base_url || !form.base_url.trim()) {
    return ElMessage.warning(t('channels.model_list_base_url_required'))
  }
  if (!form.api_key || !form.api_key.trim()) {
    return ElMessage.warning(t('channels.model_list_api_key_required'))
  }
  if (!validateChannelHttpProxy()) return

  detectingModels.value = true
  try {
    const payload = {
      api_key: form.api_key || null,
      base_url: form.base_url || null,
      http_proxy: normalizeHttpProxy(form.http_proxy) || null
    }
    const res = await channelApi.models(payload)
    const models = res.data?.data?.models || []
    detectedModels.value = Array.isArray(models) ? models : []
    if (detectedModels.value.length === 0) {
      ElMessage.warning(t('channels.model_list_empty'))
      return
    }
    ElMessage.success(t('channels.model_list_success', { count: detectedModels.value.length }))
    syncDetectedSelection()
  } catch (err) {
    ElMessage.error(err.message || t('channels.model_list_failed'))
  } finally {
    detectingModels.value = false
  }
}

const confirmConfigImpact = async (data) => {
  const syncedMemoryOrganizationSettings = data?.synced_memory_organization_settings || 0
  const retainedMemoryOrganizationSettings = data?.retained_memory_organization_settings || 0
  const disabledMemoryOrganizationSettings = data?.disabled_memory_organization_settings || 0
  const deferredMemoryOrganizationSettings = data?.deferred_memory_organization_settings || 0
  const pendingDeletionModels = data?.pending_deletion_models || 0
  const syncedProfileRules = data?.synced_profile_rules || 0
  const removedProfileRules = data?.removed_profile_rules || 0
  const syncedAuditRefs = data?.synced_audit_refs || 0
  const clearedAuditRefs = data?.cleared_audit_refs || 0

  if (!syncedMemoryOrganizationSettings && !retainedMemoryOrganizationSettings && !disabledMemoryOrganizationSettings && !deferredMemoryOrganizationSettings && !pendingDeletionModels && !syncedProfileRules && !removedProfileRules && !syncedAuditRefs && !clearedAuditRefs) return true

  const messages = []
  if (syncedMemoryOrganizationSettings) {
    messages.push(t('channels.memory_organization_synced', { count: syncedMemoryOrganizationSettings }))
  }
  if (retainedMemoryOrganizationSettings) {
    messages.push(t('channels.memory_organization_retained', { count: retainedMemoryOrganizationSettings }))
  }
  if (disabledMemoryOrganizationSettings) {
    messages.push(t('channels.memory_organization_disabled', { count: disabledMemoryOrganizationSettings }))
  }
  if (pendingDeletionModels) {
    messages.push(t('channels.pending_deletion_models_warning', { count: pendingDeletionModels }))
  }
  if (deferredMemoryOrganizationSettings) {
    messages.push(t('channels.memory_organization_deferred', { count: deferredMemoryOrganizationSettings }))
  }
  if (syncedProfileRules) {
    messages.push(t('channels.profile_rules_synced', { count: syncedProfileRules }))
  }
  if (removedProfileRules) {
    messages.push(t('channels.profile_rules_removed', { count: removedProfileRules }))
  }
  if (syncedAuditRefs) {
    messages.push(t('channels.audit_refs_synced', { count: syncedAuditRefs }))
  }
  if (clearedAuditRefs) {
    messages.push(t('channels.audit_refs_cleared', { count: clearedAuditRefs }))
  }

  try {
    await ElMessageBox.confirm(
      h('div', { class: 'config-impact-warning' }, messages.map((message, index) => (
        h('div', { class: 'config-impact-warning__item', key: index }, message)
      ))),
      t('channels.config_refs_changed_title'),
      {
        type: 'warning',
        confirmButtonText: t('channels.confirm'),
        cancelButtonText: t('channels.cancel'),
        showCancelButton: true
      }
    )
    return true
  } catch (action) {
    if (action === 'cancel' || action === 'close') return false
    return false
  }
}

const showConcurrentMemoryOrganizationDisabledNotice = async (data) => {
  const count = data?.concurrently_disabled_memory_organization_settings || 0
  if (count <= 0) return

  try {
    await ElMessageBox.alert(
      t('channels.memory_organization_concurrently_disabled_notice', { count }),
      t('channels.memory_organization_concurrently_disabled_title'),
      {
        type: 'warning',
        confirmButtonText: t('channels.confirm')
      }
    )
  } catch {
    return
  }
}

const formatErrorDetail = (err) => {
  const detail = err.response?.data
  let message
  if (detail && typeof detail === 'object') {
    message = detail.message || detail.error || JSON.stringify(detail)
  } else {
    message = err.message || t('channels.chat_test_failed')
  }
  return truncateErrorMessage(message)
}

const detectModelMetadata = async (entry, idx) => {
  if (entry.usage !== 'CHAT') {
    return ElMessage.warning(t('channels.model_metadata_chat_only'))
  }
  if (!entry.model_id || !entry.model_id.trim()) {
    modelIdErrors.value[idx] = t('channels.model_id_required')
    return ElMessage.warning(t('channels.model_id_required'))
  }

  detectingMetadataIndex.value = idx
  try {
    if (!openRouterModelsCache) {
      const res = await openRouterApi.models()
      const models = res.data?.data
      if (!Array.isArray(models) || models.some(model => !model || typeof model !== 'object' || Array.isArray(model))) {
        throw new Error(t('channels.model_metadata_invalid_response'))
      }
      openRouterModelsCache = models
    }

    const matches = getOpenRouterModelMatches(openRouterModelsCache, entry.model_id)
    if (matches.length === 0) {
      throw new Error(t('channels.model_metadata_not_found', { model: entry.model_id.trim() }))
    }
    if (matches.length > 1) {
      throw new Error(t('channels.model_metadata_ambiguous', { models: matches.map(model => model.id).join(', ') }))
    }

    const { fields: filledFields, model } = applyOpenRouterModelMetadata(entry, matches[0])
    if (filledFields.length === 0) {
      throw new Error(t('channels.model_metadata_no_mappable_fields'))
    }

    ElMessage.success(t('channels.model_metadata_detect_success', {
      model: typeof model.id === 'string' && model.id.trim() ? model.id : entry.model_id.trim(),
      fields: filledFields.map(field => t('channels.' + field)).join(', ')
    }))
  } catch (err) {
    ElMessage.error(err.message || t('channels.model_metadata_detect_failed'))
  } finally {
    detectingMetadataIndex.value = null
  }
}

const openChatTestDialog = (entry, idx) => {
  chatTestEntry.value = entry
  chatTestIndex.value = idx
  chatTestForm.testMode = 'non_stream'
  chatTestForm.prompt = ''
  chatTestPromptError.value = ''
  chatTestDialogVisible.value = true
}

const resetChatTestDialogState = () => {
  chatTestEntry.value = null
  chatTestIndex.value = null
  chatTestForm.testMode = 'non_stream'
  chatTestForm.prompt = ''
  chatTestPromptError.value = ''
}

const confirmChatTest = () => {
  const prompt = chatTestForm.prompt.trim()
  if (!prompt) {
    chatTestPromptError.value = t('channels.chat_test_prompt_required')
    return
  }

  const entry = chatTestEntry.value
  const idx = chatTestIndex.value
  const testMode = chatTestForm.testMode
  chatTestDialogVisible.value = false
  testChatModel(entry, idx, testMode, prompt)
}

const testChatModel = async (entry, idx, testMode, prompt) => {
  if (entry.usage !== 'CHAT') {
    return ElMessage.warning(t('channels.chat_test_chat_only'))
  }
  if (!entry.protocol) {
    protocolErrors.value[idx] = t('channels.model_protocol_required')
    return ElMessage.warning(t('channels.model_protocol_required'))
  }
  if (!form.base_url || !form.base_url.trim()) {
    return ElMessage.warning(t('channels.model_list_base_url_required'))
  }
  if (!form.api_key || !form.api_key.trim()) {
    return ElMessage.warning(t('channels.model_list_api_key_required'))
  }
  if (!entry.model_id || !entry.model_id.trim()) {
    modelIdErrors.value[idx] = t('channels.model_id_required')
    return ElMessage.warning(t('channels.model_id_required'))
  }
  if (!validateChannelHttpProxy()) return
  const advancedSettings = validateModelAdvancedSettings(idx)
  if (!advancedSettings) return

  const token = beginModelTest(entry, {
    status: 'running',
    kind: 'CHAT',
    testMode,
    data: null,
    error: ''
  })
  if (!token) return
  try {
    const res = await channelApi.testChat({
      api_key: form.api_key || null,
      base_url: form.base_url || null,
      http_proxy: normalizeHttpProxy(form.http_proxy) || null,
      model_id: entry.model_id.trim(),
      protocol: entry.protocol,
      temperature: entry.temperature,
      top_p: entry.top_p,
      max_tokens: entry.max_tokens || 0,
      test_mode: testMode,
      prompt,
      advanced_settings: advancedSettings
    }, { signal: token.signal })
    if (!channelTestManager.isCurrent(token)) return
    modelTestStates.set(entry, {
      status: 'success',
      kind: 'CHAT',
      testMode,
      data: res.data?.data || {},
      error: ''
    })
  } catch (err) {
    if (channelTestManager.isCurrent(token)) {
      modelTestStates.set(entry, {
        status: 'error',
        kind: 'CHAT',
        testMode,
        data: null,
        error: formatErrorDetail(err)
      })
    }
  } finally {
    channelTestManager.finish(token)
  }
}

const testImageGenerationModel = async (entry, idx) => {
  if (entry.usage !== 'IMAGE_GENERATION') {
    return ElMessage.warning(t('channels.image_generation_test_image_only'))
  }
  if (!entry.protocol) {
    protocolErrors.value[idx] = t('channels.model_protocol_required')
    return ElMessage.warning(t('channels.model_protocol_required'))
  }
  if (!form.base_url || !form.base_url.trim()) {
    return ElMessage.warning(t('channels.model_list_base_url_required'))
  }
  if (!form.api_key || !form.api_key.trim()) {
    return ElMessage.warning(t('channels.model_list_api_key_required'))
  }
  if (!entry.model_id || !entry.model_id.trim()) {
    modelIdErrors.value[idx] = t('channels.model_id_required')
    return ElMessage.warning(t('channels.model_id_required'))
  }
  if (!validateChannelHttpProxy()) return
  const advancedSettings = validateModelAdvancedSettings(idx)
  if (!advancedSettings) return

  const token = beginModelTest(entry, {
    status: 'running',
    kind: 'IMAGE_GENERATION',
    testMode: null,
    data: null,
    error: ''
  })
  if (!token) return
  try {
    const res = await channelApi.testImageGeneration({
      api_key: form.api_key || null,
      base_url: form.base_url || null,
      http_proxy: normalizeHttpProxy(form.http_proxy) || null,
      model_id: entry.model_id.trim(),
      protocol: entry.protocol,
      size: entry.size || '1024x1024',
      quality: entry.quality || 'auto',
      advanced_settings: advancedSettings
    }, { signal: token.signal })
    if (!channelTestManager.isCurrent(token)) return
    modelTestStates.set(entry, {
      status: 'success',
      kind: 'IMAGE_GENERATION',
      testMode: null,
      data: res.data?.data || {},
      error: ''
    })
  } catch (err) {
    if (channelTestManager.isCurrent(token)) {
      modelTestStates.set(entry, {
        status: 'error',
        kind: 'IMAGE_GENERATION',
        testMode: null,
        data: null,
        error: formatErrorDetail(err)
      })
    }
  } finally {
    channelTestManager.finish(token)
  }
}

const detectEmbeddingDimension = async (entry, idx) => {
  if (!isEdit.value || !currentId.value) {
    return ElMessage.warning(t('channels.detect_save_first'))
  }
  if (!entry.model_id || !entry.model_id.trim()) {
    modelIdErrors.value[idx] = t('channels.model_id_required')
    return ElMessage.warning(t('channels.detect_warn'))
  }

  detectingDimensionIndex.value = idx
  try {
    const res = await channelApi.testEmbeddingDimension(currentId.value, entry.model_id.trim())
    const dimension = res.data?.data?.dimension
    if (!dimension) {
      return ElMessage.error(t('channels.detect_failed'))
    }
    entry.embedding_dimensions = dimension
    ElMessage.success(t('channels.detect_success', { dim: dimension }))
  } catch (err) {
    ElMessage.error(err.message || t('channels.detect_failed'))
  } finally {
    detectingDimensionIndex.value = null
  }
}

const fetchModelProtocols = async () => {
  try {
    const res = await channelApi.types()
    const data = res.data.data
    modelProtocols.value = data?.model_protocols || {}
    modelProtocolsLoaded.value = true
    modelUsages.value = data?.model_usages || []
  } catch (err) {
    console.error(t('channels.load_types_failed'), err)
  }
}

const ensureModelProtocols = () => {
  if (modelProtocolsLoaded.value) return
  if (!modelProtocolsPromise) {
    modelProtocolsPromise = fetchModelProtocols().finally(() => {
      modelProtocolsPromise = null
    })
  }
}

const buildModelEntryPayload = (entry) => {
  const payload = {
    ...entry,
    model_id: (entry.model_id || '').trim(),
    advanced_settings: { ...entry.advanced_settings }
  }

  if (payload.usage !== 'IMAGE_GENERATION') {
    delete payload.size
    delete payload.quality
  } else {
    payload.size = payload.size || '1024x1024'
    payload.quality = payload.quality || 'auto'
  }

  return payload
}

const validateChannelHttpProxy = () => {
  const isValid = isValidHttpProxy(form.http_proxy)
  proxyError.value = isValid ? '' : t('channels.http_proxy_format_error')
  if (!isValid) {
    ElMessage.warning(t('channels.http_proxy_format_error'))
  }
  return isValid
}

const resetTemporaryState = () => {
  chatTestDialogVisible.value = false
  resetChatTestDialogState()
  invalidateModelTests()
  modelIdErrors.value = []
  protocolErrors.value = []
  advancedSettingsDrafts.value = []
  advancedSettingsErrors.value = []
  advancedSettingsExpanded.value = []
  proxyError.value = ''
  selectedDetectedModels.value = []
  detectedModels.value = []
  detectingModels.value = false
  detectingDimensionIndex.value = null
  detectingMetadataIndex.value = null
  _previousDetectedSelection = []
  openRouterModelsCache = null
}

const initializeForm = () => {
  const df = defaultChannelForm()
  Object.keys(df).forEach(key => { form[key] = df[key] })
  if (props.channel) {
    form.name = props.channel.name
    form.api_key = props.channel.api_key
    form.base_url = props.channel.base_url || ''
    form.http_proxy = props.channel.http_proxy || ''
    form.is_active = props.channel.is_active
    form.model_ids = props.channel.model_ids && props.channel.model_ids.length > 0
      ? JSON.parse(JSON.stringify(props.channel.model_ids)).map(entry => (
        entry.lifecycle_status === 'pending_delete' ? entry : normalizeModelEntry(entry)
      ))
      : []
  }
  if (props.channel) syncModelEntryStates()
}

const initializeDialog = () => {
  resetTemporaryState()
  initializeForm()
  ensureModelProtocols()
}

const submitForm = async () => {
  if (!form.name) {
    return ElMessage.warning(t('channels.fill_required'))
  }

  if (!form.api_key || !form.api_key.trim()) {
    return ElMessage.warning(t('channels.model_list_api_key_required'))
  }

  const parsedCustomHeaders = validateAdvancedSettings()
  if (!parsedCustomHeaders) return
  form.model_ids.forEach((entry, idx) => {
    mergeCustomHeaders(entry, parsedCustomHeaders[idx])
  })

  modelIdErrors.value = form.model_ids.map(m => m.model_id && m.model_id.trim() ? '' : t('channels.model_id_required'))
  if (modelIdErrors.value.some(Boolean)) {
    return ElMessage.warning(t('channels.fill_required'))
  }

  protocolErrors.value = form.model_ids.map(m => (
    !m.protocol ? t('channels.model_protocol_required') : ''
  ))
  if (protocolErrors.value.some(Boolean)) {
    return ElMessage.warning(t('channels.model_protocol_required'))
  }

  if (!validateChannelHttpProxy()) return

  // 校验同一用途下 model_id 不可重复；同一 model_id 允许用于不同用途
  const seen = new Map()
  let hasDuplicate = false
  form.model_ids.forEach((m, idx) => {
    const mid = (m.model_id || '').trim()
    if (!mid) return
    const key = `${m.usage}::${mid}`
    if (seen.has(key)) {
      modelIdErrors.value[idx] = t('channels.model_id_duplicate')
      modelIdErrors.value[seen.get(key)] = t('channels.model_id_duplicate')
      hasDuplicate = true
    } else {
      seen.set(key, idx)
    }
  })
  if (hasDuplicate) {
    return ElMessage.warning(t('channels.model_id_duplicate'))
  }

  submitting.value = true
  try {
    const payload = {
      name: form.name,
      api_key: form.api_key,
      base_url: form.base_url || null,
      http_proxy: normalizeHttpProxy(form.http_proxy) || null,
      is_active: form.is_active,
      model_ids: form.model_ids.map(buildModelEntryPayload)
    }
    if (isEdit.value) {
      let finalResponse = await channelApi.update(currentId.value, payload)
      if (finalResponse.data?.data?.requires_confirmation) {
        const confirmed = await confirmConfigImpact(finalResponse.data?.data)
        if (!confirmed) return
        finalResponse = await channelApi.update(currentId.value, { ...payload, confirm_config_impact: true })
      }
      ElMessage.success(t('channels.update_success'))
      await showConcurrentMemoryOrganizationDisabledNotice(finalResponse.data?.data)
    } else {
      await channelApi.create(payload)
      ElMessage.success(t('channels.create_success'))
    }
    dialogVisible.value = false
    emit('saved')
  } catch (err) {
    ElMessage.error(err.message || t('channels.submit_failed'))
  } finally {
    submitting.value = false
  }
}

watch(
  () => props.visible,
  (visible, previousVisible) => {
    if (visible && !previousVisible) initializeDialog()
    if (!visible && previousVisible) {
      chatTestDialogVisible.value = false
      resetChatTestDialogState()
      invalidateModelTests()
    }
  },
  { immediate: true }
)

onBeforeUnmount(invalidateModelTests)
</script>

<style lang="scss">
@import "@/assets/css/ChannelFormDialog.scss";
</style>
