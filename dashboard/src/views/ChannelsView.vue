<template>
  <div class="view-container">
    <BaseDataTable
      :data="channels"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('channels.create_channel')"
      :refresh-text="$t('channels.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @create="openCreateDialog"
      @refresh="handleRefresh"
      @page-change="fetchChannels"
      @size-change="handleSizeChange">
      <el-table-column :resizable="false" prop="name" :label="$t('channels.name')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('channels.models')" min-width="300" sortable>
        <template #default="scope">
          <div class="models-list" v-if="scope.row.model_ids && scope.row.model_ids.length > 0">
            <el-tag v-for="(m, idx) in scope.row.model_ids" :key="idx" class="model-tag">
              {{ m.model_id }} ({{ getModelUsageLabel(m.usage) }}<template v-if="m.protocol"> - {{ getModelProtocolLabel(m.protocol) }}</template>)
            </el-tag>
          </div>
          <span v-else class="text-muted">{{ $t('channels.no_models') }}</span>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="base_url" :label="$t('channels.base_url')" min-width="200" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('channels.status')" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" :active-text="$t('channels.enable')" :inactive-text="$t('channels.disable')" />
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('channels.actions')" width="360" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button :type="scope.row.is_active ? 'warning' : 'success'" size="small" @click="handleToggleActive(scope.row)">{{ scope.row.is_active ? $t('channels.disable') : $t('channels.enable') }}</el-button>
            <el-button type="primary" size="small" @click="handleEdit(scope.row)">{{ $t('channels.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('channels.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="isEdit ? $t('channels.edit_channel') : $t('channels.create_channel')" v-model="dialogVisible" width="65%" class="standard-dialog dialog-with-scroll-body" center align-center :close-on-click-modal="true">
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

          <div v-for="(entry, idx) in form.model_ids" :key="idx" class="model-entry-card">
            <div class="model-entry-header">
              <span>{{ $t('channels.model_entry') }} #{{ idx + 1 }}</span>
              <div class="model-entry-actions">
                <el-button type="text" :loading="testingModelIndex === idx" :disabled="!isTestableModel(entry)" @click="testModel(entry, idx)">
                  {{ $t('channels.test') }}
                </el-button>
                <el-button type="text" :loading="detectingMetadataIndex === idx" :disabled="entry.usage !== 'CHAT' || (detectingMetadataIndex !== null && detectingMetadataIndex !== idx)" @click="detectModelMetadata(entry, idx)">
                  {{ $t('channels.model_metadata_detect') }}
                </el-button>
                <el-button type="text" class="remove" @click="removeModelEntry(idx)">
                  {{ $t('channels.remove') }}
                </el-button>
              </div>
            </div>

            <div class="model-entry-fields">
              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('channels.model_id_label')" :error="modelIdErrors[idx]">
                  <el-input v-model="entry.model_id" :placeholder="$t('channels.model_id_placeholder')" @input="handleModelIdInput(idx)" />
                </el-form-item>
              </div>
              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('channels.model_type_label')" >
                  <el-select v-model="entry.usage" class="full-width-input" @change="handleModelUsageChange(entry, idx)">
                    <el-option v-for="item in modelUsages" :key="item" :label="getModelUsageLabel(item)" :value="item" />
                  </el-select>
                </el-form-item>
              </div>
              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('channels.model_protocol')" :error="protocolErrors[idx]">
                  <el-select v-model="entry.protocol" class="full-width-input" @change="handleProtocolChange(idx)">
                    <el-option v-for="item in getModelProtocols(entry.usage)" :key="item" :label="getModelProtocolLabel(item)" :value="item" />
                  </el-select>
                </el-form-item>
              </div>

              <template v-if="entry.usage === 'CHAT'">
                <div class="model-entry-field">
                  <el-form-item :label="$t('channels.temperature')" >
                    <el-input-number v-model="entry.temperature" :min="0" :max="2" :step="0.1" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-field">
                  <el-form-item :label="$t('channels.top_p')" >
                    <el-input-number v-model="entry.top_p" :min="0" :max="1" :step="0.05" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-field">
                  <el-form-item :label="$t('channels.max_tokens')">
                    <el-input-number v-model="entry.max_tokens" :min="0" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-field">
                  <el-form-item :label="$t('channels.context_window_k')">
                    <el-input-number v-model="entry.context_window_k" :min="1" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-understanding-row">
                  <div class="model-entry-field model-entry-field-third">
                    <el-form-item :label="$t('channels.image_understanding')">
                      <el-switch v-model="entry.image_understanding" />
                    </el-form-item>
                  </div>
                  <div class="model-entry-field model-entry-field-third">
                    <el-form-item :label="$t('channels.audio_understanding')">
                      <el-switch v-model="entry.audio_understanding" />
                    </el-form-item>
                  </div>
                  <div class="model-entry-field model-entry-field-third">
                    <el-form-item :label="$t('channels.video_understanding')">
                      <el-switch v-model="entry.video_understanding" />
                    </el-form-item>
                  </div>
                </div>
              </template>

              <template v-if="entry.usage === 'EMBEDDING'">
                <div class="model-entry-field model-entry-field-half">
                  <el-form-item :label="$t('channels.embedding_dimensions')">
                    <div class="embedding-dimension-row">
                      <el-input-number v-model="entry.embedding_dimensions" :min="1" controls-position="right" />
                      <el-button type="primary" plain :loading="detectingDimensionIndex === idx" @click="detectEmbeddingDimension(entry, idx)">
                        {{ $t('channels.auto_detect') }}
                      </el-button>
                    </div>
                  </el-form-item>
                </div>
              </template>

              <template v-if="entry.usage === 'IMAGE_GENERATION'">
                <div class="model-entry-field model-entry-field-half">
                  <el-form-item :label="$t('channels.image_generation_size')">
                    <el-select v-model="entry.size" class="full-width-input">
                      <el-option label="1024x1024" value="1024x1024" />
                      <el-option label="1024x1536" value="1024x1536" />
                      <el-option label="1536x1024" value="1536x1024" />
                    </el-select>
                  </el-form-item>
                </div>
                <div class="model-entry-field model-entry-field-half">
                  <el-form-item :label="$t('channels.image_generation_quality')">
                    <el-select v-model="entry.quality" class="full-width-input">
                      <el-option :label="$t('channels.image_generation_quality_auto')" value="auto" />
                      <el-option :label="$t('channels.image_generation_quality_low')" value="low" />
                      <el-option :label="$t('channels.image_generation_quality_medium')" value="medium" />
                      <el-option :label="$t('channels.image_generation_quality_high')" value="high" />
                    </el-select>
                  </el-form-item>
                </div>
              </template>

              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('channels.description')">
                  <el-input v-model="entry.description" :placeholder="$t('channels.description_placeholder')" />
                </el-form-item>
              </div>
            </div>

            <el-collapse v-model="advancedSettingsExpanded[idx]" class="model-entry-advanced-settings">
              <el-collapse-item :title="$t('channels.advanced_settings')" :name="idx">
                <div class="custom-request-headers-heading">
                  <span class="custom-request-headers-title">{{ $t('channels.custom_request_headers') }}</span>
                  <el-button size="small" type="primary" plain @click="fillCustomHeadersTemplate(idx)">
                    {{ $t('channels.fill_headers_template') }}
                  </el-button>
                </div>
                <el-form-item :error="advancedSettingsErrors[idx]" class="advanced-settings-form-item">
                  <el-input
                    v-model="advancedSettingsDrafts[idx]"
                    type="textarea"
                    :rows="4"
                    :placeholder="$t('channels.custom_headers_placeholder')"
                    @input="handleAdvancedSettingsInput(idx)" />
                </el-form-item>
              </el-collapse-item>
            </el-collapse>

          </div>

          <el-button type="primary" :icon="Plus" @click="addModelEntry">{{ $t('channels.add_model') }}</el-button>
        </div>
      </div>

      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('channels.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" :loading="submitting">{{ $t('channels.confirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive, watch } from 'vue'
import { Plus, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { channelApi, openRouterApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultChannelForm, defaultModelEntry } from '../constants'

const { t } = useI18n()

const channels = ref([])
const modelProtocols = ref({})
const modelProtocolsLoaded = ref(false)
const modelUsages = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const submitting = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const currentId = ref(null)
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
const testingModelIndex = ref(null)
const detectingMetadataIndex = ref(null)
let openRouterModelsCache = null

const defaultModelProtocols = {
  CHAT: 'OPENAI',
  EMBEDDING: 'OPENAI_EMBEDDING',
  RERANK: 'COHERE_RERANK',
  IMAGE_GENERATION: 'OPENAI_IMAGE'
}

const getModelProtocolLabel = (value) => {
  const map = {
    OPENAI: 'openai-completions',
    OPENAI_RESPONSES: 'openai-responses',
    OPENAI_EMBEDDING: 'openai-embedding',
    OPENAI_IMAGE: 'openai-image',
    COHERE_RERANK: 'cohere-rerank'
  }
  return map[value] || value
}

const getModelProtocols = (usage) => {
  const protocols = modelProtocols.value[usage]
  if (Array.isArray(protocols) && protocols.length > 0) return protocols
  return modelProtocolsLoaded.value ? [] : [defaultModelProtocols[usage]].filter(Boolean)
}

const getDefaultModelProtocol = (usage) => {
  return getModelProtocols(usage)[0] || defaultModelProtocols[usage] || ''
}

const getModelUsageLabel = (value) => {
  const map = {
    CHAT: t('channels.chat_model'),
    EMBEDDING: t('channels.embedding_model'),
    RERANK: t('channels.rerank_model'),
    IMAGE_GENERATION: t('channels.image_generation_model')
  }
  return map[value] || value
}

const form = reactive(defaultChannelForm())

const customHeadersTemplate = {
  'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
  accept: 'application/json, text/plain, */*',
  'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
  'cache-control': 'no-cache'
}
const httpHeaderNamePattern = /^[!#$%&'*+.^_\x60|~0-9A-Za-z-]+$/
const reservedCustomHeaderNames = new Set([
  'authorization',
  'content-type',
  'content-length',
  'host',
  'connection',
  'transfer-encoding',
  'proxy-authorization',
  'proxy-connection'
])

const isPlainJsonObject = (value) => (
  value !== null &&
  typeof value === 'object' &&
  !Array.isArray(value) &&
  Object.getPrototypeOf(value) === Object.prototype
)

const ensureAdvancedSettings = (entry) => {
  if (!isPlainJsonObject(entry.advanced_settings)) {
    entry.advanced_settings = {}
  }
}

const getCustomHeaders = (settings) => {
  if (!isPlainJsonObject(settings)) return {}
  if (isPlainJsonObject(settings.custom_headers)) return settings.custom_headers
  if (settings.custom_headers === undefined && typeof settings.user_agent === 'string') {
    return { 'user-agent': settings.user_agent }
  }
  return {}
}

const formatAdvancedSettings = (settings) => {
  const customHeaders = getCustomHeaders(settings)
  if (Object.keys(customHeaders).length === 0) return ''
  return JSON.stringify(customHeaders, null, 2)
}

const parseAdvancedSettingsDraft = (draft) => {
  const text = typeof draft === 'string' ? draft.trim() : ''
  if (!text) return { value: {}, error: '' }

  let settings
  try {
    settings = JSON.parse(text)
  } catch {
    return { value: null, error: t('channels.custom_headers_json_object_error') }
  }

  if (!isPlainJsonObject(settings)) {
    return { value: null, error: t('channels.custom_headers_json_object_error') }
  }

  const headerEntries = Object.entries(settings)
  if (headerEntries.length > 32) {
    return { value: null, error: t('channels.custom_headers_max_items_error') }
  }

  const normalizedHeaders = {}
  const headerNames = new Set()
  for (const [headerName, headerValue] of headerEntries) {
    if (!httpHeaderNamePattern.test(headerName)) {
      return { value: null, error: t('channels.custom_headers_name_error') }
    }

    const normalizedName = headerName.toLowerCase()
    if (headerNames.has(normalizedName)) {
      return { value: null, error: t('channels.custom_headers_name_error') }
    }
    if (reservedCustomHeaderNames.has(normalizedName)) {
      return {
        value: null,
        error: t('channels.custom_headers_reserved_header_error', { header: normalizedName })
      }
    }
    if (typeof headerValue !== 'string') {
      return { value: null, error: t('channels.custom_headers_value_error') }
    }

    const normalizedValue = headerValue.trim()
    if (!normalizedValue || normalizedValue.length > 4096 || /[^\x20-\x7E]/.test(normalizedValue)) {
      return { value: null, error: t('channels.custom_headers_value_error') }
    }
    Object.defineProperty(normalizedHeaders, normalizedName, {
      configurable: true,
      enumerable: true,
      value: normalizedValue,
      writable: true
    })
    headerNames.add(normalizedName)
  }

  return { value: normalizedHeaders, error: '' }
}

const mergeCustomHeaders = (entry, customHeaders) => {
  const advancedSettings = isPlainJsonObject(entry.advanced_settings)
    ? { ...entry.advanced_settings }
    : {}
  delete advancedSettings.user_agent
  if (Object.keys(customHeaders).length === 0) {
    delete advancedSettings.custom_headers
  } else {
    advancedSettings.custom_headers = { ...customHeaders }
  }
  entry.advanced_settings = advancedSettings
  return advancedSettings
}

const validateAdvancedSettings = () => {
  const results = form.model_ids.map((_, idx) => parseAdvancedSettingsDraft(advancedSettingsDrafts.value[idx]))
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
  const result = parseAdvancedSettingsDraft(advancedSettingsDrafts.value[idx])
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

const removeModelEntry = (idx) => {
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

let _previousDetectedSelection = []

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
    form.model_ids[targetIndex].model_id = id
    modelIdErrors.value[targetIndex] = ''
  }

  syncDetectedSelection()
}
watch(
  () => [form.base_url, form.api_key, form.http_proxy],
  () => {
    resetDetectedModels()
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

const fetchChannels = async () => {
  loading.value = true
  try {
    const res = await channelApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    channels.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('channels.load_failed'))
  } finally {
    loading.value = false
  }
}

const { handleDelete } = useDeleteConfirm(channelApi.delete, fetchChannels)

const showConfigImpactWarning = async (data) => {
  const syncedProfileRules = data?.synced_profile_rules || 0
  const removedProfileRules = data?.removed_profile_rules || 0
  const syncedAuditRefs = data?.synced_audit_refs || 0
  const clearedAuditRefs = data?.cleared_audit_refs || 0

  if (!syncedProfileRules && !removedProfileRules && !syncedAuditRefs && !clearedAuditRefs) return

  const messages = []
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
    await ElMessageBox.alert(messages.join('\n'), t('channels.config_refs_changed_title'), {
      type: 'warning',
      confirmButtonText: t('channels.confirm')
    })
  } catch (action) {
    if (action !== 'cancel' && action !== 'close') {
      console.error(t('channels.config_refs_changed_title'), action)
    }
  }
}

const handleToggleActive = async (row) => {
  try {
    await channelApi.update(row.id, { is_active: !row.is_active })
    ElMessage.success(row.is_active ? t('channels.disabled') : t('channels.enabled'))
    fetchChannels()
  } catch (err) {
    ElMessage.error(err.message || t('channels.action_failed'))
  }
}

const formatErrorDetail = (err) => {
  const detail = err.response?.data
  if (detail && typeof detail === 'object') {
    return detail.message || detail.error || JSON.stringify(detail)
  }
  return err.message || t('channels.chat_test_failed')
}

const isTestableModel = (entry) => {
  return entry.usage === 'CHAT' || entry.usage === 'IMAGE_GENERATION'
}

const getOpenRouterModelMatches = (models, modelId) => {
  const query = modelId.trim().toLowerCase()
  const matchByIdentifiers = (candidateQuery) => models.filter(model => {
    const identifiers = [model.id, model.canonical_slug]
    return identifiers.some(identifier => typeof identifier === 'string' && identifier.toLowerCase() === candidateQuery)
  })
  const uniqueMatches = (matches) => [...new Map(matches.map(model => [model.id, model])).values()]
  const exactMatches = uniqueMatches(matchByIdentifiers(query))
  if (exactMatches.length > 0) return exactMatches

  const removeProviderPrefix = (identifier) => {
    const separator = identifier.indexOf('/')
    return separator >= 0 ? identifier.slice(separator + 1) : identifier
  }
  const strippedQuery = removeProviderPrefix(query)
  return uniqueMatches(models.filter(model => {
    const identifiers = [model.id, model.canonical_slug]
    return identifiers.some(identifier => typeof identifier === 'string' && removeProviderPrefix(identifier.toLowerCase()) === strippedQuery)
  }))
}

const toPositiveInteger = (value) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null
  return Math.floor(value)
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

    const model = matches[0]
    const filledFields = []
    const contextLength = toPositiveInteger(model.top_provider?.context_length) || toPositiveInteger(model.context_length)
    if (contextLength) {
      entry.context_window_k = Math.max(1, Math.floor(contextLength / 1000))
      filledFields.push(t('channels.context_window_k'))
    }
    if (Array.isArray(model.architecture?.input_modalities)) {
      const modalities = new Set(model.architecture.input_modalities
        .filter(modality => typeof modality === 'string')
        .map(modality => modality.toLowerCase()))
      entry.image_understanding = modalities.has('image')
      entry.audio_understanding = modalities.has('audio')
      entry.video_understanding = modalities.has('video')
      filledFields.push(
        t('channels.image_understanding'),
        t('channels.audio_understanding'),
        t('channels.video_understanding')
      )
    }
    if (typeof model.description === 'string' && model.description.trim() && (!entry.description || !entry.description.trim())) {
      entry.description = model.description.trim()
      filledFields.push(t('channels.description'))
    }
    if (filledFields.length === 0) {
      throw new Error(t('channels.model_metadata_no_mappable_fields'))
    }

    ElMessage.success(t('channels.model_metadata_detect_success', {
      model: typeof model.id === 'string' && model.id.trim() ? model.id : entry.model_id.trim(),
      fields: filledFields.join(', ')
    }))
  } catch (err) {
    ElMessage.error(err.message || t('channels.model_metadata_detect_failed'))
  } finally {
    detectingMetadataIndex.value = null
  }
}

const testModel = async (entry, idx) => {
  if (entry.usage === 'CHAT') {
    return testChatModel(entry, idx)
  }
  if (entry.usage === 'IMAGE_GENERATION') {
    return testImageGenerationModel(entry, idx)
  }
  return ElMessage.warning(t('channels.model_test_supported_only'))
}

const formatUsage = (usage) => {
  if (!usage) return '-'
  if (typeof usage === 'string') return usage
  try {
    const keys = ['prompt_tokens', 'completion_tokens', 'total_tokens']
    const parts = keys
      .filter(k => usage[k] !== undefined && usage[k] !== null)
      .map(k => `${k}: ${usage[k]}`)
    if (parts.length > 0) return parts.join(', ')
    return JSON.stringify(usage)
  } catch {
    return String(usage)
  }
}

const testChatModel = async (entry, idx) => {
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

  testingModelIndex.value = idx
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
      advanced_settings: advancedSettings
    })
    ElMessage.success(t('channels.chat_test_success'))
    const data = res.data?.data || {}
    const content = [
      `${t('channels.chat_test_model')}: ${data.model || '-'}`,
      `${t('channels.chat_test_reply')}: ${data.reply || '-'}`,
      `${t('channels.chat_test_usage')}: ${formatUsage(data.usage)}`
    ].join('\n')
    try {
      await ElMessageBox.alert(content, t('channels.chat_test_result_title'), {
        confirmButtonText: t('channels.confirm'),
        closeOnClickModal: true,
        customClass: 'chat-test-result-box'
      })
    } catch (action) {
      if (action !== 'cancel' && action !== 'close') {
        console.error(t('channels.chat_test_result_title'), action)
      }
    }
  } catch (err) {
    try {
      await ElMessageBox.alert(formatErrorDetail(err), t('channels.chat_test_failed'), {
        type: 'error',
        confirmButtonText: t('channels.confirm'),
        closeOnClickModal: true
      })
    } catch (action) {
      if (action !== 'cancel' && action !== 'close') {
        console.error(t('channels.chat_test_failed'), action)
      }
    }
  } finally {
    testingModelIndex.value = null
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

  testingModelIndex.value = idx
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
    })
    ElMessage.success(t('channels.image_generation_test_success'))
    const data = res.data?.data || {}
    const image = data.image || {}
    const imageUrl = image.url || (image.b64_json ? `data:image/png;base64,${image.b64_json}` : '')
    const content = imageUrl
      ? `<div class="image-test-result"><p>${t('channels.chat_test_model')}: ${data.model || '-'}</p><img src="${imageUrl}" alt="image generation test result" /></div>`
      : `${t('channels.chat_test_model')}: ${data.model || '-'}`
    try {
      await ElMessageBox.alert(content, t('channels.image_generation_test_result_title'), {
        dangerouslyUseHTMLString: Boolean(imageUrl),
        confirmButtonText: t('channels.confirm'),
        closeOnClickModal: true,
        customClass: 'image-test-result-box'
      })
    } catch (action) {
      if (action !== 'cancel' && action !== 'close') {
        console.error(t('channels.image_generation_test_result_title'), action)
      }
    }
  } catch (err) {
    try {
      await ElMessageBox.alert(formatErrorDetail(err), t('channels.image_generation_test_failed'), {
        type: 'error',
        confirmButtonText: t('channels.confirm'),
        closeOnClickModal: true
      })
    } catch (action) {
      if (action !== 'cancel' && action !== 'close') {
        console.error(t('channels.image_generation_test_failed'), action)
      }
    }
  } finally {
    testingModelIndex.value = null
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

const handleRefresh = () => {
  currentPage.value = 1
  fetchChannels()
}

const handleSizeChange = () => {
  currentPage.value = 1
  fetchChannels()
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

const httpProxyPattern = /^http:\/\/(?:(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+:(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+@)?(?:\[[^\]\s/@?#]+\]|[^:/\s@?#]+):(\d+)\/?$/

const normalizeHttpProxy = (value) => typeof value === 'string' ? value.trim() : ''

const isValidHttpProxy = (value) => {
  const proxy = typeof value === 'string' ? value : ''
  if (!proxy.trim()) return true

  const match = proxy.match(httpProxyPattern)
  if (/\s/.test(proxy) || !match) return false

  try {
    const url = new URL(proxy)
    const port = Number(match[1])
    return url.protocol === 'http:' && Boolean(url.hostname) &&
      Number.isInteger(port) && port >= 1 && port <= 65535 &&
      Boolean(url.username) === Boolean(url.password) && url.pathname === '/' &&
      !url.search && !url.hash
  } catch {
    return false
  }
}

const validateChannelHttpProxy = () => {
  const isValid = isValidHttpProxy(form.http_proxy)
  proxyError.value = isValid ? '' : t('channels.http_proxy_format_error')
  if (!isValid) {
    ElMessage.warning(t('channels.http_proxy_format_error'))
  }
  return isValid
}

const openCreateDialog = () => {
  isEdit.value = false
  currentId.value = null
  const df = defaultChannelForm()
  Object.keys(df).forEach(k => { form[k] = df[k] })
  modelIdErrors.value = []
  protocolErrors.value = []
  advancedSettingsDrafts.value = []
  advancedSettingsErrors.value = []
  advancedSettingsExpanded.value = []
  proxyError.value = ''
  resetDetectedModels()
  dialogVisible.value = true
}

const handleEdit = (row) => {
  isEdit.value = true
  currentId.value = row.id
  form.name = row.name
  form.api_key = row.api_key
  form.base_url = row.base_url || ''
  form.http_proxy = row.http_proxy || ''
  form.is_active = row.is_active
  form.model_ids = (row.model_ids && row.model_ids.length > 0)
    ? JSON.parse(JSON.stringify(row.model_ids))
    : []
  modelIdErrors.value = []
  protocolErrors.value = []
  advancedSettingsDrafts.value = []
  advancedSettingsErrors.value = []
  advancedSettingsExpanded.value = []
  proxyError.value = ''
  syncModelEntryStates()
  resetDetectedModels()
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name) {
    return ElMessage.warning(t('channels.fill_required'))
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
      const res = await channelApi.update(currentId.value, payload)
      ElMessage.success(t('channels.update_success'))
      await showConfigImpactWarning(res.data?.data)
    } else {
      await channelApi.create(payload)
      ElMessage.success(t('channels.create_success'))
    }
    dialogVisible.value = false
    fetchChannels()
  } catch (err) {
    ElMessage.error(err.message || t('channels.submit_failed'))
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  fetchChannels()
  fetchModelProtocols()
})
</script>

<style lang="scss">
@import "@/assets/css/common.scss";
@import "@/assets/css/ChannelsView.scss";
</style>
