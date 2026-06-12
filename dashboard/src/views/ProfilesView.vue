<template>
  <div class="view-container">
    <BaseDataTable
      :data="profiles"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('profiles.create_profile')"
      :refresh-text="$t('profiles.refresh')"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadProfiles"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="name" :label="$t('profiles.profile_name')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" prop="provider_name" :label="$t('profiles.provider')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('profiles.model_id')" min-width="150" sortable>
        <template #default="scope">
          {{ scope.row.configs?.provider?.model_id || $t('profiles.not_set') }}
        </template>
      </el-table-column>
      
      <el-table-column :resizable="false" :label="$t('profiles.status')" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" :active-text="$t('profiles.active')" :inactive-text="$t('profiles.inactive')" />
        </template>
      </el-table-column>

      <el-table-column :resizable="false" :label="$t('profiles.actions')" width="380" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button :type="scope.row.is_active ? 'info' : 'success'" size="small" :disabled="scope.row.is_active" @click="handleActivate(scope.row.id)">{{ $t('profiles.activate') }}</el-button>
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('profiles.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('profiles.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? $t('profiles.create_profile') : $t('profiles.edit_profile')" v-model="dialogVisible" width="60%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="120px" size="default">
        <el-tabs v-model="activeTab">
          <!-- 基础设置 -->
          <el-tab-pane :label="$t('profiles.base_settings')" name="base">
            <div class="tab-pane-content">
              <el-form-item :label="$t('profiles.profile_name')">
                <el-input v-model="form.name" :placeholder="$t('profiles.unique_name')"></el-input>
              </el-form-item>
              <el-form-item :label="$t('profiles.associated_prompt')">
                <el-select v-model="form.prompt_id" :placeholder="$t('profiles.optional_prompt')" clearable class="full-width-input">
                  <el-option v-for="item in prompts" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>
              </el-form-item>
            </div>
          </el-tab-pane>

          <!-- 模型设置 -->
          <el-tab-pane :label="$t('profiles.model_settings')" name="model">
            <div class="tab-pane-content">
              <el-divider content-position="left"><span class="gray-divider-text">{{ $t('profiles.chat_model') }}</span></el-divider>
              <el-form-item :label="$t('profiles.provider')">
                <el-select v-model="form.configs.provider.provider_id" :placeholder="$t('profiles.select_chat_provider')" class="full-width-input">
                  <el-option v-for="item in chatProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>
              </el-form-item>
              <el-form-item :label="$t('profiles.model_id')">
                <el-input v-model="form.configs.provider.model_id" :placeholder="$t('profiles.model_id_placeholder')"></el-input>
              </el-form-item>

              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.temperature')">
                    <el-input-number v-model="form.configs.provider.temperature" :min="0" :max="2" :step="0.1" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.top_p')">
                    <el-input-number v-model="form.configs.provider.top_p" :min="0" :max="1" :step="0.05" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
              </el-row>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.max_tokens')">
                    <el-input-number v-model="form.configs.provider.max_tokens" :min="0" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.multimodal')">
                    <el-switch v-model="form.configs.provider.multimodal"></el-switch>
                  </el-form-item>
                </el-col>
              </el-row>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.context_window')">
                    <el-input-number v-model="form.configs.provider.context_window_k" :min="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.context_window_hint') }}</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.chat_timeout')">
                    <el-input-number v-model="form.configs.provider.chat_timeout" :min="1" :max="600" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.chat_timeout_hint') }}</div>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-divider content-position="left"><span class="gray-divider-text">{{ $t('profiles.embedding_model') }}</span></el-divider>
              <el-form-item :label="$t('profiles.provider')">
                <el-select v-model="form.configs.provider.embedding_provider_id" :placeholder="$t('profiles.select_embedding_provider')" clearable class="full-width-input">
                  <el-option v-for="item in embeddingProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>

              </el-form-item>
              <el-form-item :label="$t('profiles.embedding_model_id')">
                <el-input v-model="form.configs.provider.embedding_model_id" :placeholder="$t('profiles.embedding_model_hint')"></el-input>
              </el-form-item>
              <el-form-item :label="$t('profiles.embedding_dimensions')">
                <div style="display: flex; gap: 10px; width: 100%;">
                  <el-input-number v-model="form.configs.provider.embedding_dimensions" :min="1" :step="1" :placeholder="$t('profiles.embedding_dimensions_placeholder')" style="flex: 1;"></el-input-number>
                  <el-button type="primary" @click="handleDetectDimension" :loading="detectingDimension" :disabled="!form.configs.provider.embedding_provider_id || !form.configs.provider.embedding_model_id">{{ $t('profiles.auto_detect') }}</el-button>
                </div>
                <div class="help-text mt-5">{{ $t('profiles.embedding_dimensions_hint') }}</div>
              </el-form-item>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.embedding_timeout')">
                    <el-input-number v-model="form.configs.provider.embedding_timeout" :min="1" :max="600" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.embedding_timeout_hint') }}</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.kb_query_top_k')">
                    <el-input-number v-model="form.configs.provider.kb_query_top_k" :min="1" :max="50" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.kb_query_top_k_hint') }}</div>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-divider content-position="left"><span class="gray-divider-text">{{ $t('profiles.rerank_model') }}</span></el-divider>

              <el-form-item :label="$t('profiles.provider')">
                <el-select v-model="form.configs.provider.rerank_provider_id" :placeholder="$t('profiles.select_rerank_provider')" clearable class="full-width-input">
                  <el-option v-for="item in rerankProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>

                <div class="help-text mt-5">{{ $t('profiles.rerank_provider_hint') }}</div>
              </el-form-item>
              <el-form-item :label="$t('profiles.rerank_model_id')">
                <el-input v-model="form.configs.provider.rerank_model_id" :placeholder="$t('profiles.rerank_model_hint')"></el-input>
              </el-form-item>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.rerank_candidate_k')">
                    <el-input-number v-model="form.configs.provider.rerank_candidate_k" :min="1" :max="50" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.rerank_candidate_k_hint') }}</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.rerank_timeout')">
                    <el-input-number v-model="form.configs.provider.rerank_timeout" :min="1" :max="120" :step="1" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
              </el-row>
            </div>
          </el-tab-pane>

          <!-- 安全设置 -->
          <el-tab-pane :label="$t('profiles.security_settings')" name="security">
            <div class="tab-pane-content">
              <el-form-item :label="$t('profiles.audit_provider')">
                <el-select v-model="form.configs.security.audit_provider_id" :placeholder="$t('profiles.select_audit_provider')" clearable class="full-width-input">
                  <el-option v-for="item in auditProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>

              </el-form-item>
              <el-form-item :label="$t('profiles.audit_model_id')">
                <el-input v-model="form.configs.security.audit_model_id" :placeholder="$t('profiles.audit_model_hint')"></el-input>
              </el-form-item>
              <el-form-item :label="$t('profiles.audit_threshold')">
                <el-slider v-model="form.configs.security.audit_threshold" :min="0" :max="7" show-stops show-input></el-slider>
                <div class="help-text">{{ $t('profiles.audit_threshold_hint') }}</div>
              </el-form-item>
            </div>
          </el-tab-pane>

          <!-- 工具设置 -->
          <el-tab-pane :label="$t('profiles.tool_settings')" name="tool">
            <div class="tab-pane-content">
              <el-divider content-position="left"><span class="gray-divider-text">{{ $t('profiles.scheduling_control') }}</span></el-divider>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.max_parallel_tools')">
                    <el-input-number v-model="form.configs.tool.max_parallel_tools" :min="1" :max="20" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.max_turns')">
                    <el-input-number v-model="form.configs.tool.max_turns" :min="1" :max="20" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-divider content-position="left"><span class="gray-divider-text">{{ $t('profiles.shell_config') }}</span></el-divider>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('profiles.shell_timeout')">
                    <el-input-number v-model="form.configs.tool.shell_timeout" :min="1" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-divider content-position="left"><span class="gray-divider-text">{{ $t('profiles.firecrawl_config') }}</span></el-divider>
              <el-row :gutter="20">
                <el-col :span="24">
                  <el-form-item :label="$t('profiles.api_key')">
                    <el-input v-model="form.configs.tool.firecrawl_api_key" :placeholder="$t('profiles.firecrawl_key_placeholder')" show-password></el-input>
                    <div class="help-text mt-5">
                      {{ $t('profiles.firecrawl_hint_1') }}
                      <el-link type="primary" href="https://www.firecrawl.dev/" target="_blank" :underline="false">{{ $t('profiles.firecrawl_hint_2') }}</el-link> 
                      {{ $t('profiles.firecrawl_hint_3') }}
                    </div>
                  </el-form-item>
                </el-col>
              </el-row>
            </div>
          </el-tab-pane>

          <!-- 其他设置 -->
          <el-tab-pane :label="$t('profiles.other_settings')" name="other">
            <div class="tab-pane-content">
              <div class="help-text">{{ $t('profiles.no_other_settings') }}</div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('profiles.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">{{ $t('profiles.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { profileApi, providerApi, promptApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultProfileConfigs } from '../constants'

const { t } = useI18n()

const profiles = ref([])
const providers = ref([])
const prompts = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const dialogVisible = ref(false)
const dialogType = ref('create')
const submitting = ref(false)
const activeTab = ref('base')
const detectingDimension = ref(false)

// 按模型类型与启用状态过滤提供商，供各模型配置下拉仅展示“未禁用且类型符合”的选项。
// 为兼容编辑场景下已选中但当前被禁用/类型不符的存量值，额外把当前已选 id 保留在候选中，避免回显丢失。
const filterProvidersByUsage = (usage, selectedId) => {
  return providers.value.filter(
    (item) => (item.usage === usage && item.is_active) || item.id === selectedId
  )
}

const chatProviders = computed(() => filterProvidersByUsage('CHAT', form.configs.provider.provider_id))
const embeddingProviders = computed(() => filterProvidersByUsage('EMBEDDING', form.configs.provider.embedding_provider_id))
const rerankProviders = computed(() => filterProvidersByUsage('RERANK', form.configs.provider.rerank_provider_id))
const auditProviders = computed(() => filterProvidersByUsage('CHAT', form.configs.security.audit_provider_id))


const form = reactive({
  id: null,
  name: '',
  prompt_id: null,
  configs: defaultProfileConfigs()
})

// 加载配置列表
const loadProfiles = async () => {
  loading.value = true
  try {
    const res = await profileApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    profiles.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_failed'))
  } finally {
    loading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete } = useDeleteConfirm(profileApi.delete, loadProfiles)

const fetchPrompts = async () => {
  try {
    const res = await promptApi.list({ page: 1, size: 1000 })
    prompts.value = res.data.data.items || []
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_prompts_failed'))
  }
}

const fetchProviders = async () => {
  try {
    const res = await providerApi.list({ page: 1, size: 1000 })
    providers.value = res.data.data.items || []
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_providers_failed'))
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  loadProfiles()
  fetchProviders()
  fetchPrompts()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadProfiles()
}

const handleDetectDimension = async () => {
  if (!form.configs.provider.embedding_provider_id || !form.configs.provider.embedding_model_id) {
    return ElMessage.warning(t('profiles.detect_warn'))
  }
  detectingDimension.value = true
  try {
    const res = await providerApi.testEmbeddingDimension(form.configs.provider.embedding_provider_id, form.configs.provider.embedding_model_id)
    const dim = res.data.data.dimension
    form.configs.provider.embedding_dimensions = dim
    ElMessage.success(res.data.message || t('profiles.detect_success', { dim }))
  } catch (err) {
    ElMessage.error(err.message || t('profiles.detect_failed'))
  } finally {
    detectingDimension.value = false
  }
}

const handleActivate = async (id) => {
  try {
    const res = await profileApi.activate(id)
    ElMessage.success(res.data.message || t('profiles.activate_success'))
    loadProfiles()
  } catch (err) {
    ElMessage.error(err.message || t('profiles.activate_failed'))
  }
}

const showDialog = (type, row = null) => {
  dialogType.value = type
  activeTab.value = 'base'
  if (type === 'edit' && row) {
    form.id = row.id
    form.name = row.name
    form.prompt_id = row.prompt_id
    const base = defaultProfileConfigs()
    if (row.configs) {
      Object.keys(base).forEach(key => {
        if (row.configs[key]) {
          Object.assign(base[key], row.configs[key])
        }
      })
    }
    form.configs = base
  } else {
    form.id = null
    form.name = ''
    form.prompt_id = null
    form.configs = defaultProfileConfigs()
  }
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name || !form.configs.provider.provider_id || !form.configs.provider.model_id) {
    return ElMessage.warning(t('profiles.fill_required'))
  }
  // Reranker 启用判定：同时配置了提供商与模型 ID 即视为启用
  const rerankProviderId = form.configs.provider.rerank_provider_id
  const rerankModelId = (form.configs.provider.rerank_model_id || '').trim()
  const hasRerankProvider = !!rerankProviderId
  const hasRerankModel = !!rerankModelId
  if (hasRerankProvider || hasRerankModel) {
    // 仅配置其一视为配置不完整
    if (!hasRerankProvider || !hasRerankModel) {
      return ElMessage.warning(t('profiles.rerank_warn_1'))
    }
    // 候选数量 K 必须大于等于知识库返回数量，否则精排不会生效
    const candidateK = form.configs.provider.rerank_candidate_k
    const topK = form.configs.provider.kb_query_top_k
    if (Number(candidateK) < Number(topK)) {
      return ElMessage.warning(t('profiles.rerank_warn_2'))
    }
  }

  submitting.value = true

  try {
    if (dialogType.value === 'create') {
      await profileApi.create(form)
    } else {
      await profileApi.update(form.id, form)
    }
    ElMessage.success(t('profiles.save_success'))
    dialogVisible.value = false
    loadProfiles()
  } catch (err) {
    ElMessage.error(err.message || t('profiles.submit_failed'))
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadProfiles()
  fetchProviders()
  fetchPrompts()
})
</script>

<style lang="scss">
@import "@/assets/css/ProfilesView.scss";
.el-form-item__content{
  gap: 10px;
}
</style>
