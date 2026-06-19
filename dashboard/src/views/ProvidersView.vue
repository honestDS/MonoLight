<template>
  <div class="view-container">
    <BaseDataTable
      :data="providers"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('providers.create_provider')"
      :refresh-text="$t('providers.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @create="openCreateDialog"
      @refresh="handleRefresh"
      @page-change="fetchProviders"
      @size-change="handleSizeChange">
      <el-table-column :resizable="false" prop="name" :label="$t('providers.name')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" prop="provider_type" :label="$t('providers.type')" min-width="100" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('providers.models')" min-width="300" sortable>
        <template #default="scope">
          <div class="models-list" v-if="scope.row.model_ids && scope.row.model_ids.length > 0">
            <el-tag v-for="(m, idx) in scope.row.model_ids" :key="idx" class="model-tag">
              {{ m.model_id }} ({{ getModelUsageLabel(m.usage) }})
            </el-tag>
          </div>
          <span v-else class="text-muted">{{ $t('providers.no_models') }}</span>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="base_url" :label="$t('providers.base_url')" min-width="200" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('providers.status')" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" :active-text="$t('providers.enable')" :inactive-text="$t('providers.disable')" />
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('providers.actions')" width="360" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button :type="scope.row.is_active ? 'warning' : 'success'" size="small" @click="handleToggleActive(scope.row)">{{ scope.row.is_active ? $t('providers.disable') : $t('providers.enable') }}</el-button>
            <el-button type="primary" size="small" @click="handleEdit(scope.row)">{{ $t('providers.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('providers.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="isEdit ? $t('providers.edit_provider') : $t('providers.create_provider')" v-model="dialogVisible" width="65%" class="standard-dialog" center align-center>
      <div class="provider-settings-shell">
        <div class="provider-settings-title">{{ $t('providers.provider_settings') }}</div>
        <div class="provider-settings-body">
          <div class="provider-settings-top">
            <el-form :model="form" label-width="100px" class="provider-settings-form">
              <div class="provider-settings-row provider-settings-row--fields">
                <el-form-item :label="$t('providers.provider_name')">
                  <el-input v-model="form.name" :placeholder="$t('providers.provider_name_placeholder')" />
                </el-form-item>
                <el-form-item :label="$t('providers.provider_type')">
                  <el-select v-model="form.provider_type" :placeholder="$t('providers.select_type')" class="full-width-input">
                    <el-option v-for="item in providerTypes" :key="item" :label="item" :value="item" />
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('providers.api_key')">
                  <el-input v-model="form.api_key" type="password" show-password :placeholder="$t('providers.api_key_placeholder')" />
                </el-form-item>
                <el-form-item :label="$t('providers.base_url')">
                  <el-input v-model="form.base_url" :placeholder="$t('providers.base_url_placeholder')" />
                </el-form-item>
              </div>
            </el-form>
          </div>

          <div v-for="(entry, idx) in form.model_ids" :key="idx" class="model-entry-card">
            <div class="model-entry-header">
              <span>{{ $t('providers.model_entry') }} #{{ idx + 1 }}</span>
              <el-button type="text" class="remove" @click="removeModelEntry(idx)">
                {{ $t('providers.remove') }}
              </el-button>
            </div>

            <div class="model-entry-fields">
              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('providers.model_id_label')" :error="modelIdErrors[idx]">
                  <el-input v-model="entry.model_id" :placeholder="$t('providers.model_id_placeholder')" @input="modelIdErrors[idx] = ''" />
                </el-form-item>
              </div>
              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('providers.model_type_label')" >
                  <el-select v-model="entry.usage" class="full-width-input">
                    <el-option v-for="item in modelUsages" :key="item" :label="getModelUsageLabel(item)" :value="item" />
                  </el-select>
                </el-form-item>
              </div>

              <template v-if="entry.usage === 'CHAT'">
                <div class="model-entry-field">
                  <el-form-item :label="$t('providers.temperature')" >
                    <el-input-number v-model="entry.temperature" :min="0" :max="2" :step="0.1" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-field">
                  <el-form-item :label="$t('providers.top_p')" >
                    <el-input-number v-model="entry.top_p" :min="0" :max="1" :step="0.05" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-field">
                  <el-form-item :label="$t('providers.max_tokens')">
                    <el-input-number v-model="entry.max_tokens" :min="0" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-field">
                  <el-form-item :label="$t('providers.context_window_k')">
                    <el-input-number v-model="entry.context_window_k" :min="1" controls-position="right" />
                  </el-form-item>
                </div>
                <div class="model-entry-understanding-row">
                  <div class="model-entry-field model-entry-field-third">
                    <el-form-item :label="$t('providers.image_understanding')">
                      <el-switch v-model="entry.image_understanding" />
                    </el-form-item>
                  </div>
                  <div class="model-entry-field model-entry-field-third">
                    <el-form-item :label="$t('providers.audio_understanding')">
                      <el-switch v-model="entry.audio_understanding" />
                    </el-form-item>
                  </div>
                  <div class="model-entry-field model-entry-field-third">
                    <el-form-item :label="$t('providers.video_understanding')">
                      <el-switch v-model="entry.video_understanding" />
                    </el-form-item>
                  </div>
                </div>
              </template>

              <template v-if="entry.usage === 'EMBEDDING'">
                <div class="model-entry-field model-entry-field-half">
                  <el-form-item :label="$t('providers.embedding_dimensions')">
                    <el-input-number v-model="entry.embedding_dimensions" :min="1" controls-position="right" />
                  </el-form-item>
                </div>
              </template>

              <div class="model-entry-field model-entry-field-half">
                <el-form-item :label="$t('providers.description')">
                  <el-input v-model="entry.description" :placeholder="$t('providers.description_placeholder')" />
                </el-form-item>
              </div>
            </div>
          </div>

          <el-button type="primary" :icon="Plus" @click="addModelEntry">{{ $t('providers.add_model') }}</el-button>
        </div>
      </div>

      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('providers.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" :loading="submitting">{{ $t('providers.confirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { providerApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultProviderForm, defaultModelEntry } from '../constants'

const { t } = useI18n()

const providers = ref([])
const providerTypes = ref([])
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

const getModelUsageLabel = (value) => {
  const map = {
    CHAT: t('providers.chat_model'),
    EMBEDDING: t('providers.embedding_model'),
    RERANK: t('providers.rerank_model')
  }
  return map[value] || value
}

const form = reactive(defaultProviderForm())

const addModelEntry = () => {
  form.model_ids.push(defaultModelEntry())
  modelIdErrors.value.push('')
}

const removeModelEntry = (idx) => {
  form.model_ids.splice(idx, 1)
  modelIdErrors.value.splice(idx, 1)
}

const fetchProviders = async () => {
  loading.value = true
  try {
    const res = await providerApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    providers.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('providers.load_failed'))
  } finally {
    loading.value = false
  }
}

const { handleDelete } = useDeleteConfirm(providerApi.delete, fetchProviders)

const handleToggleActive = async (row) => {
  try {
    await providerApi.update(row.id, { is_active: !row.is_active })
    ElMessage.success(row.is_active ? t('providers.disabled') : t('providers.enabled'))
    fetchProviders()
  } catch (err) {
    ElMessage.error(err.message || t('providers.action_failed'))
  }
}

const fetchProviderTypes = async () => {
  try {
    const res = await providerApi.types()
    const data = res.data.data
    providerTypes.value = data?.provider_types || []
    modelUsages.value = data?.model_usages || []
  } catch (err) {
    console.error(t('providers.load_types_failed'), err)
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  fetchProviders()
}

const handleSizeChange = () => {
  currentPage.value = 1
  fetchProviders()
}

const openCreateDialog = () => {
  isEdit.value = false
  currentId.value = null
  const df = defaultProviderForm()
  Object.keys(df).forEach(k => { form[k] = df[k] })
  modelIdErrors.value = []
  dialogVisible.value = true
}

const handleEdit = (row) => {
  isEdit.value = true
  currentId.value = row.id
  form.name = row.name
  form.provider_type = row.provider_type
  form.api_key = row.api_key
  form.base_url = row.base_url || ''
  form.is_active = row.is_active
  form.model_ids = (row.model_ids && row.model_ids.length > 0)
    ? JSON.parse(JSON.stringify(row.model_ids))
    : []
  modelIdErrors.value = form.model_ids.map(() => '')
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name || !form.provider_type) {
    return ElMessage.warning(t('providers.fill_required'))
  }

  modelIdErrors.value = form.model_ids.map(m => m.model_id && m.model_id.trim() ? '' : t('providers.model_id_required'))
  if (modelIdErrors.value.some(Boolean)) {
    return ElMessage.warning(t('providers.fill_required'))
  }

  // 校验同一用途下 model_id 不可重复；同一 model_id 允许用于不同用途
  const seen = new Map()
  let hasDuplicate = false
  form.model_ids.forEach((m, idx) => {
    const mid = (m.model_id || '').trim()
    if (!mid) return
    const key = `${m.usage}::${mid}`
    if (seen.has(key)) {
      modelIdErrors.value[idx] = t('providers.model_id_duplicate')
      modelIdErrors.value[seen.get(key)] = t('providers.model_id_duplicate')
      hasDuplicate = true
    } else {
      seen.set(key, idx)
    }
  })
  if (hasDuplicate) {
    return ElMessage.warning(t('providers.model_id_duplicate'))
  }

  submitting.value = true
  try {
    const payload = {
      name: form.name,
      provider_type: form.provider_type,
      api_key: form.api_key,
      base_url: form.base_url || null,
      is_active: form.is_active,
      model_ids: form.model_ids
    }
    if (isEdit.value) {
      await providerApi.update(currentId.value, payload)
      ElMessage.success(t('providers.update_success'))
    } else {
      await providerApi.create(payload)
      ElMessage.success(t('providers.create_success'))
    }
    dialogVisible.value = false
    fetchProviders()
  } catch (err) {
    ElMessage.error(err.message || t('providers.submit_failed'))
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  fetchProviders()
  fetchProviderTypes()
})
</script>

<style lang="scss">
@import "@/assets/css/common.scss";
@import "@/assets/css/ProvidersView.scss";
</style>