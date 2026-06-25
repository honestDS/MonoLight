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
      <el-table-column :resizable="false" prop="channel_type" :label="$t('channels.type')" min-width="100" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('channels.models')" min-width="300" sortable>
        <template #default="scope">
          <div class="models-list" v-if="scope.row.model_ids && scope.row.model_ids.length > 0">
            <el-tag v-for="(m, idx) in scope.row.model_ids" :key="idx" class="model-tag">
              {{ m.model_id }} ({{ getModelUsageLabel(m.usage) }})
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
                <el-form-item :label="$t('channels.channel_type')">
                  <el-select v-model="form.channel_type" :placeholder="$t('channels.select_type')" class="full-width-input">
                    <el-option v-for="item in channelTypes" :key="item" :label="item" :value="item" />
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('channels.api_key')">
                  <el-input v-model="form.api_key" type="password" show-password :placeholder="$t('channels.api_key_placeholder')" />
                </el-form-item>
                <el-form-item>
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
                            <span v-if="isDetectedModelSelected(model.id)" class="detected-model-selected">{{ $t('channels.model_selected') }}</span>
                            <span v-if="model.owned_by" class="detected-model-owner" :title="model.owned_by">{{ model.owned_by }}</span>
                          </span>
                        </div>
                      </el-option>
                    </el-select>
                  </div>
                </el-form-item>
                <el-form-item :label="$t('channels.base_url')">
                  <el-input v-model="form.base_url" :placeholder="$t('channels.base_url_placeholder')" />
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
                <el-button type="text" class="remove" @click="removeModelEntry(idx)">
                  {{ $t('channels.remove') }}
                </el-button>
              </div>
            </div>

            <div class="model-entry-fields">
              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('channels.model_id_label')" :error="modelIdErrors[idx]">
                  <el-input v-model="entry.model_id" :placeholder="$t('channels.model_id_placeholder')" @input="modelIdErrors[idx] = ''" />
                </el-form-item>
              </div>
              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('channels.model_type_label')" >
                  <el-select v-model="entry.usage" class="full-width-input">
                    <el-option v-for="item in modelUsages" :key="item" :label="getModelUsageLabel(item)" :value="item" />
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
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { channelApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultChannelForm, defaultModelEntry } from '../constants'

const { t } = useI18n()

const channels = ref([])
const channelTypes = ref([])
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
const selectedDetectedModels = ref([])
const detectedModels = ref([])
const detectingModels = ref(false)
const detectingDimensionIndex = ref(null)
const testingModelIndex = ref(null)

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

const addModelEntry = () => {
  form.model_ids.push(defaultModelEntry())
  modelIdErrors.value.push('')
}

const removeModelEntry = (idx) => {
  form.model_ids.splice(idx, 1)
  modelIdErrors.value.splice(idx, 1)
}

const resetDetectedModels = () => {
  selectedDetectedModels.value = []
  detectedModels.value = []
  _previousDetectedSelection = []
}

const isDetectedModelSelected = (modelId) => {
  return form.model_ids.some(entry => (entry.model_id || '').trim() === modelId)
}

let _previousDetectedSelection = []

const handleDetectedModelChange = (values) => {
  const added = values.filter(v => !_previousDetectedSelection.includes(v))

  for (const id of added) {
    const exists = form.model_ids.some(entry => (entry.model_id || '').trim() === id)
    if (exists) continue

    let targetIndex = form.model_ids.findIndex(entry => !entry.model_id || !entry.model_id.trim())
    if (targetIndex < 0) {
      form.model_ids.push(defaultModelEntry())
      modelIdErrors.value.push('')
      targetIndex = form.model_ids.length - 1
    }
    form.model_ids[targetIndex].model_id = id
    modelIdErrors.value[targetIndex] = ''
  }

  syncDetectedSelection()
}
watch(
  () => [form.channel_type, form.base_url, form.api_key],
  () => {
    resetDetectedModels()
  }
)

const syncDetectedSelection = () => {
  const matched = detectedModels.value
    .filter(m => form.model_ids.some(e => (e.model_id || '').trim() === m.id))
    .map(m => m.id)
  selectedDetectedModels.value = matched
  _previousDetectedSelection = [...matched]
}

const detectModelList = async () => {
  if (!form.channel_type) {
    return ElMessage.warning(t('channels.select_type'))
  }
  if (!form.base_url || !form.base_url.trim()) {
    return ElMessage.warning(t('channels.model_list_base_url_required'))
  }
  if (!form.api_key || !form.api_key.trim()) {
    return ElMessage.warning(t('channels.model_list_api_key_required'))
  }

  detectingModels.value = true
  try {
    const payload = {
      channel_type: form.channel_type,
      api_key: form.api_key || null,
      base_url: form.base_url || null
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
  if (!form.channel_type) {
    return ElMessage.warning(t('channels.select_type'))
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

  testingModelIndex.value = idx
  try {
    const res = await channelApi.testChat({
      channel_type: form.channel_type,
      api_key: form.api_key || null,
      base_url: form.base_url || null,
      model_id: entry.model_id.trim(),
      temperature: entry.temperature,
      top_p: entry.top_p,
      max_tokens: entry.max_tokens || 0
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
  if (!form.channel_type) {
    return ElMessage.warning(t('channels.select_type'))
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

  testingModelIndex.value = idx
  try {
    const res = await channelApi.testImageGeneration({
      channel_type: form.channel_type,
      api_key: form.api_key || null,
      base_url: form.base_url || null,
      model_id: entry.model_id.trim(),
      size: entry.size || '1024x1024',
      quality: entry.quality || 'auto'
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

const fetchChannelTypes = async () => {
  try {
    const res = await channelApi.types()
    const data = res.data.data
    channelTypes.value = data?.channel_types || []
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
    model_id: (entry.model_id || '').trim()
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

const openCreateDialog = () => {
  isEdit.value = false
  currentId.value = null
  const df = defaultChannelForm()
  Object.keys(df).forEach(k => { form[k] = df[k] })
  modelIdErrors.value = []
  resetDetectedModels()
  dialogVisible.value = true
}

const handleEdit = (row) => {
  isEdit.value = true
  currentId.value = row.id
  form.name = row.name
  form.channel_type = row.channel_type
  form.api_key = row.api_key
  form.base_url = row.base_url || ''
  form.is_active = row.is_active
  form.model_ids = (row.model_ids && row.model_ids.length > 0)
    ? JSON.parse(JSON.stringify(row.model_ids))
    : []
  modelIdErrors.value = form.model_ids.map(() => '')
  resetDetectedModels()
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name || !form.channel_type) {
    return ElMessage.warning(t('channels.fill_required'))
  }

  modelIdErrors.value = form.model_ids.map(m => m.model_id && m.model_id.trim() ? '' : t('channels.model_id_required'))
  if (modelIdErrors.value.some(Boolean)) {
    return ElMessage.warning(t('channels.fill_required'))
  }

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
      channel_type: form.channel_type,
      api_key: form.api_key,
      base_url: form.base_url || null,
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
  fetchChannelTypes()
})
</script>

<style lang="scss">
@import "@/assets/css/common.scss";
@import "@/assets/css/ChannelsView.scss";
</style>
