<template>
  <div class="view-container">
    <BaseDataTable
      :data="profiles"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="''"
      :refresh-text="''"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadProfiles"
      @size-change="handleSizeChange">
      <template #actions>
        <el-button type="primary" size="default" @click="showDialog('create')">{{ $t('profiles.create_profile') }}</el-button>
        <el-button size="default" @click="handleRefresh">{{ $t('profiles.refresh') }}</el-button>
        <el-button size="default" @click="showSystemSettingsDialog">{{ $t('profiles.global_settings') }}</el-button>
      </template>

      <el-table-column :resizable="false" prop="name" :label="$t('profiles.profile_name')" min-width="120" sortable></el-table-column>
      <el-table-column v-if="showOwnerColumn" :resizable="false" prop="username" :label="$t('profiles.owner_username')" min-width="120" sortable>
        <template #default="scope">
          <span>{{ scope.row.username || $t('profiles.owner_unknown') }}</span>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('profiles.chat_channel_label')" min-width="200">
        <template #default="scope">
          <div class="models-list" v-if="scope.row.configs?.channel?.chat_channel?.rules?.length">
            <el-tag v-for="(r, idx) in scope.row.configs.channel.chat_channel.rules" :key="idx" class="model-tag">
              {{ r.model_id }}
            </el-tag>
          </div>
          <span v-else class="text-muted">{{ $t('profiles.not_set') }}</span>
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
            <el-button v-if="canActivateProfile(scope.row)" :type="scope.row.is_active ? 'info' : 'success'" size="small" :disabled="scope.row.is_active" @click="handleActivate(scope.row.id)">{{ $t('profiles.activate') }}</el-button>
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('profiles.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('profiles.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? $t('profiles.create_profile') : $t('profiles.edit_profile')" v-model="dialogVisible" width="50%" class="standard-dialog dialog-with-scroll-body profile-dialog" center align-center>
      <el-form :model="form" size="default" label-position="top">
        <el-tabs v-model="activeTab">
          <!-- 基础设置 -->
          <el-tab-pane :label="$t('profiles.base_settings')" name="base">
            <div class="tab-pane-content">
              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.base_settings') }}</div>
                <el-form-item :label="$t('profiles.profile_name')">
                  <el-input v-model="form.name" :placeholder="$t('profiles.unique_name')"></el-input>
                </el-form-item>
                <el-form-item v-if="showOwnerColumn && dialogType === 'create'" :label="$t('profiles.owner_username')">
                  <el-select v-model="form.uid" :placeholder="$t('profiles.select_owner')" filterable class="full-width-input">
                    <el-option v-for="item in users" :key="item.uid" :label="item.username" :value="item.uid"></el-option>
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('profiles.associated_prompt')">
                  <el-select v-model="form.prompt_id" :placeholder="$t('profiles.optional_prompt')" clearable class="full-width-input">
                    <el-option v-for="item in prompts" :key="item.id" :label="item.name" :value="item.id"></el-option>
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('profiles.context_summary_threshold')">
                  <el-select v-model="form.configs.other.context_summary_threshold_percent" class="full-width-input">
                    <el-option
                      v-for="percent in contextSummaryThresholdOptions"
                      :key="percent"
                      :label="`${percent}%`"
                      :value="percent"
                    ></el-option>
                  </el-select>
                  <div class="help-text mt-5">{{ $t('profiles.context_summary_threshold_hint') }}</div>
                </el-form-item>
              </div>
            </div>
          </el-tab-pane>

          <!-- 模型设置（渠道管理） -->
          <el-tab-pane :label="$t('profiles.model_settings')" name="model">
            <div class="tab-pane-content">

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.chat_channel') }}</div>
                <ChannelEditor
                  :channel="form.configs.channel.chat_channel"
                  :channels="channels"
                  usage="CHAT"
                  :label="$t('profiles.chat_model')"
                />
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.context_summary_channel') }}</div>
                <ChannelEditor
                  :channel="form.configs.channel.context_summary_channel"
                  :channels="channels"
                  usage="CHAT"
                  :label="$t('profiles.context_summary_model')"
                />
                <div class="help-text mt-5">{{ $t('profiles.context_summary_channel_hint') }}</div>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.rerank_channel') }}</div>
                <ChannelEditor
                  :channel="form.configs.channel.rerank_channel"
                  :channels="channels"
                  usage="RERANK"
                  :label="$t('profiles.rerank_model')"
                />
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.image_generation_channel') }}</div>
                <ChannelEditor
                  :channel="form.configs.channel.image_generation_channel"
                  :channels="channels"
                  usage="IMAGE_GENERATION"
                  :label="$t('profiles.image_generation_model')"
                />
              </div>


            </div>
          </el-tab-pane>

          <el-tab-pane :label="$t('profiles.knowledge_base_settings')" name="knowledge_base">
            <div class="tab-pane-content">
              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.knowledge_base_settings') }}</div>
                <el-form-item :label="$t('profiles.bound_knowledge_bases')">
                  <el-select
                    v-model="form.knowledge_base_ids"
                    :placeholder="$t('profiles.select_knowledge_bases')"
                    class="full-width-input"
                    multiple
                    filterable
                    collapse-tags
                    collapse-tags-tooltip
                  >
                    <el-option v-for="item in knowledgeBaseOptions" :key="item.value" :label="item.label" :value="item.value">
                      <div class="option-with-description">
                        <span>{{ item.label }}</span>
                        <span v-if="item.description" class="text-muted">{{ item.description }}</span>
                      </div>
                    </el-option>
                  </el-select>
                  <div class="help-text mt-5">{{ $t('profiles.knowledge_base_binding_hint') }}</div>
                </el-form-item>
              </div>
            </div>
          </el-tab-pane>

          <!-- 安全设置 -->
          <el-tab-pane :label="$t('profiles.security_settings')" name="security">
            <div class="tab-pane-content">
              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.security_settings') }}</div>
                <el-form-item :label="$t('profiles.audit_model_id')" label-width="auto">
                  <el-select
                    v-model="auditModelKey"
                    :placeholder="$t('profiles.audit_model_hint')"
                    clearable
                    filterable
                    class="full-width-input"
                  >
                    <el-option
                      v-for="item in auditModelOptions"
                      :key="item.key"
                      :label="item.label"
                      :value="item.key"
                    ></el-option>
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('profiles.audit_threshold')" label-width="auto">
                  <el-slider v-model="form.configs.security.audit_threshold" :min="0" :max="7" show-stops show-input></el-slider>
                  <div class="help-text">{{ $t('profiles.audit_threshold_hint') }}</div>
                </el-form-item>
              </div>
            </div>
          </el-tab-pane>

          <!-- 工具设置 -->
          <el-tab-pane :label="$t('profiles.tool_settings')" name="tool">
            <div class="tab-pane-content">
              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.scheduling_control') }}</div>
                <div class="scheduling-control-list">
                  <el-form-item :label="$t('profiles.max_parallel_tools')">
                    <el-input-number v-model="form.configs.tool.max_parallel_tools" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.max_parallel_tools_hint') }}</div>
                  </el-form-item>
                  <el-form-item :label="$t('profiles.executor_max_workers')">
                    <el-input-number v-model="form.configs.tool.executor_max_workers" :min="1" :max="100" class="full-width-input" controls-position="right"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.executor_max_workers_hint') }}</div>
                  </el-form-item>
                  <el-form-item :label="$t('profiles.background_task_max_concurrency')">
                    <el-input-number v-model="form.configs.tool.background_task_max_concurrency" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.background_task_max_concurrency_hint') }}</div>
                  </el-form-item>
                  <el-form-item :label="$t('profiles.scheduled_task_max_concurrency')">
                    <el-input-number v-model="form.configs.tool.scheduled_task_max_concurrency" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.scheduled_task_max_concurrency_hint') }}</div>
                  </el-form-item>
                  <el-form-item :label="$t('profiles.max_turns')">
                    <el-input-number v-model="form.configs.tool.max_turns" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                  <el-form-item :label="$t('profiles.tool_timeout')">
                    <el-input-number v-model="form.configs.tool.tool_timeout" :min="1" class="full-width-input" controls-position="right"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.tool_timeout_hint') }}</div>
                  </el-form-item>
                  <el-form-item :label="$t('profiles.image_generation_tool_timeout')">
                    <el-input-number v-model="form.configs.tool.image_generation_timeout" :min="1" :max="600" class="full-width-input" controls-position="right"></el-input-number>
                    <div class="help-text mt-5">{{ $t('profiles.image_generation_tool_timeout_hint') }}</div>
                  </el-form-item>
                </div>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.tool_visibility_config') }}</div>
                <el-form-item :label="$t('profiles.enabled_tools')">
                  <el-select
                    v-model="form.configs.tool.enabled_tools"
                    multiple
                    class="full-width-input"
                    :placeholder="$t('profiles.enabled_tools_placeholder')"
                  >
                    <el-option
                      v-for="item in toolOptions"
                      :key="item.value"
                      :label="item.label"
                      :value="item.value"
                    ></el-option>
                  </el-select>
                  <div class="help-text mt-5">{{ $t('profiles.enabled_tools_hint') }}</div>
                </el-form-item>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.file_send_config') }}</div>
                <el-form-item :label="$t('profiles.allowed_file_send_dirs')">
                  <div class="tag-input-panel full-width-input">
                    <el-input
                      v-model="allowedFileSendDirInput"
                      :placeholder="$t('profiles.allowed_file_send_dirs_placeholder')"
                      @keyup.enter="addAllowedFileSendDir"
                    >
                      <template #append>
                        <el-button @click="addAllowedFileSendDir">{{ $t('profiles.add') }}</el-button>
                      </template>
                    </el-input>
                    <div v-if="form.configs.tool.allowed_file_send_dirs.length" class="tag-list">
                      <el-tag
                        v-for="item in form.configs.tool.allowed_file_send_dirs"
                        :key="item"
                        closable
                        @close="removeAllowedFileSendDir(item)"
                      >
                        {{ item }}
                      </el-tag>
                    </div>
                  </div>
                  <div class="help-text mt-5">{{ $t('profiles.allowed_file_send_dirs_hint') }}</div>
                </el-form-item>
                <el-row :gutter="20">
                  <el-col :span="8">
                    <el-form-item :label="$t('profiles.file_send_max_count')">
                      <el-input-number v-model="form.configs.tool.file_send_max_count" :min="1" :max="100" class="full-width-input" controls-position="right"></el-input-number>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="$t('profiles.file_send_max_single_size_mb')">
                      <el-input-number v-model="form.configs.tool.file_send_max_single_size_mb" :min="1" :max="1024" class="full-width-input" controls-position="right"></el-input-number>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="$t('profiles.file_send_max_total_size_mb')">
                      <el-input-number v-model="form.configs.tool.file_send_max_total_size_mb" :min="1" :max="4096" class="full-width-input" controls-position="right"></el-input-number>
                    </el-form-item>
                  </el-col>
                </el-row>
                <el-form-item :label="$t('profiles.file_send_blocked_extensions')">
                  <div class="tag-input-panel full-width-input">
                    <el-input
                      v-model="fileSendBlockedExtensionInput"
                      :placeholder="$t('profiles.file_send_blocked_extensions_placeholder')"
                      @keyup.enter="addFileSendBlockedExtension"
                    >
                      <template #append>
                        <el-button @click="addFileSendBlockedExtension">{{ $t('profiles.add') }}</el-button>
                      </template>
                    </el-input>
                    <div v-if="form.configs.tool.file_send_blocked_extensions.length" class="tag-list">
                      <el-tag
                        v-for="item in form.configs.tool.file_send_blocked_extensions"
                        :key="item"
                        closable
                        @close="removeFileSendBlockedExtension(item)"
                      >
                        {{ item }}
                      </el-tag>
                    </div>
                  </div>
                  <div class="help-text mt-5">{{ $t('profiles.file_send_blocked_extensions_hint') }}</div>
                </el-form-item>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.firecrawl_config') }}</div>
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
            </div>
          </el-tab-pane>

        </el-tabs>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('profiles.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">{{ $t('profiles.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog :title="$t('profiles.global_settings')" v-model="settingsDialogVisible" width="520px" class="standard-dialog" center align-center>
      <el-form :model="systemSettings" label-width="150px" size="default">
        <el-form-item :label="$t('profiles.log_locale')">
          <el-select v-model="systemSettings.log_locale" class="full-width-input">
            <el-option v-for="locale in localeOptions" :key="locale.value" :label="locale.label" :value="locale.value" />
          </el-select>
          <div class="help-text mt-5">{{ $t('profiles.log_locale_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('profiles.temp_dir_max_size_mb')">
          <el-input-number v-model="systemSettings.temp_dir_max_size_mb" :min="1" :max="1048576" class="full-width-input" controls-position="right" />
          <div class="help-text mt-5">{{ $t('profiles.temp_dir_max_size_mb_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('profiles.session_reply_max_concurrency')">
          <el-input-number v-model="systemSettings.session_reply_max_concurrency" :min="1" :max="100" class="full-width-input" controls-position="right" />
          <div class="help-text mt-5">{{ $t('profiles.session_reply_max_concurrency_hint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="settingsDialogVisible = false" size="default">{{ $t('profiles.cancel') }}</el-button>
        <el-button type="primary" @click="saveSystemSettings" size="default" :loading="settingsSubmitting">{{ $t('profiles.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { profileApi, channelApi, promptApi, systemApi, adminApi, knowledgeBaseApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import ChannelEditor from '../components/ChannelEditor.vue'
import { defaultProfileConfigs } from '../constants'
import { SUPPORT_LOCALES } from '../i18n'

const { t } = useI18n()

const profiles = ref([])
const users = ref([])
const channels = ref([])
const prompts = ref([])
const knowledgeBases = ref([])
const toolOptions = ref([])
const showOwnerColumn = ref(false)
const currentUid = ref(null)
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const dialogVisible = ref(false)
const settingsDialogVisible = ref(false)
const dialogType = ref('create')
const submitting = ref(false)
const settingsSubmitting = ref(false)
const activeTab = ref('base')
const allowedFileSendDirInput = ref('')
const fileSendBlockedExtensionInput = ref('')
const localeOptions = SUPPORT_LOCALES
const contextSummaryThresholdOptions = [50, 60, 70, 80, 90]

const systemSettings = reactive({
  log_locale: 'zh',
  temp_dir_max_size_mb: 1024,
  session_reply_max_concurrency: 4
})

const auditModelOptions = computed(() => {
  const options = []
  channels.value
    .filter(channel => channel.is_active !== false)
    .forEach(channel => {
      ;(channel.model_ids || [])
        .filter(model => model.usage === 'CHAT' && model.model_id)
        .forEach(model => {
          options.push({
            key: `${channel.id}::${model.model_id}`,
            channel_id: channel.id,
            model_id: model.model_id,
            label: `${channel.name} / ${model.model_id}`
          })
        })
    })
  return options
})

const form = reactive({
  id: null,
  uid: null,
  name: '',
  prompt_id: null,
  knowledge_base_ids: [],
  configs: defaultProfileConfigs()
})

const knowledgeBaseOptions = computed(() => knowledgeBases.value
  .filter(item => !form.uid || item.uid === form.uid)
  .map(item => ({
    value: item.id,
    label: item.name,
    description: item.description || ''
  })))

watch(() => form.uid, () => {
  form.knowledge_base_ids = form.knowledge_base_ids.filter(id => knowledgeBaseOptions.value.some(item => item.value === id))
})

const canActivateProfile = (row) => !showOwnerColumn.value || row.uid === currentUid.value

const auditModelKey = computed({
  get() {
    const security = form.configs.security
    if (!security.audit_channel_id || !security.audit_model_id) return null
    return `${security.audit_channel_id}::${security.audit_model_id}`
  },
  set(key) {
    if (!key) {
      form.configs.security.audit_channel_id = null
      form.configs.security.audit_model_id = null
      return
    }

    const option = auditModelOptions.value.find(item => item.key === key)
    if (!option) return
    form.configs.security.audit_channel_id = option.channel_id
    form.configs.security.audit_model_id = option.model_id
  }
})

const loadProfiles = async () => {
  loading.value = true
  try {
    const res = await profileApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    profiles.value = res.data.data.items || []
    total.value = res.data.data.total || 0
    toolOptions.value = res.data.data.meta?.tool_options || []
    showOwnerColumn.value = Boolean(res.data.data.meta?.show_owner)
    currentUid.value = res.data.data.meta?.current_uid || null
    if (showOwnerColumn.value) fetchUsers()
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_failed'))
  } finally {
    loading.value = false
  }
}

const { handleDelete } = useDeleteConfirm(profileApi.delete, loadProfiles)

const fetchPrompts = async () => {
  try {
    const res = await promptApi.list({ page: 1, size: 1000 })
    prompts.value = res.data.data.items || []
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_prompts_failed'))
  }
}

const loadSystemSettings = async () => {
  try {
    const settingsRes = await systemApi.settings()
    Object.assign(systemSettings, settingsRes.data.data || {})
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_settings_failed'))
  }
}

const saveSystemSettings = async () => {
  settingsSubmitting.value = true
  try {
    const res = await systemApi.updateSettings({ ...systemSettings })
    Object.assign(systemSettings, res.data.data || {})
    settingsDialogVisible.value = false
    ElMessage.success(t('profiles.system_settings_saved'))
  } catch (err) {
    ElMessage.error(err.message || t('profiles.save_settings_failed'))
  } finally {
    settingsSubmitting.value = false
  }
}

const showSystemSettingsDialog = async () => {
  await loadSystemSettings()
  settingsDialogVisible.value = true
}

const fetchChannels = async () => {
  try {
    const res = await channelApi.list({ page: 1, size: 1000 })
    channels.value = res.data.data.items || []
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_channels_failed'))
  }
}

const fetchKnowledgeBases = async () => {
  try {
    const res = await knowledgeBaseApi.list({ page: 1, size: 1000 })
    knowledgeBases.value = res.data.data.items || []
  } catch (err) {
    knowledgeBases.value = []
  }
}

const fetchUsers = async () => {
  if (!showOwnerColumn.value) return
  try {
    const res = await adminApi.userList({ page: 1, size: 1000 })
    users.value = res.data.data.items || []
  } catch (err) {
    users.value = []
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  loadProfiles()
  fetchChannels()
  fetchPrompts()
  loadSystemSettings()
  fetchKnowledgeBases()
  fetchUsers()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadProfiles()
}

const addUniqueListValue = (targetList, rawValue, normalizeValue = value => value) => {
  const value = normalizeValue((rawValue || '').trim())
  if (!value || targetList.includes(value)) return false
  targetList.push(value)
  return true
}

const addAllowedFileSendDir = () => {
  if (addUniqueListValue(form.configs.tool.allowed_file_send_dirs, allowedFileSendDirInput.value)) {
    allowedFileSendDirInput.value = ''
  }
}

const removeAllowedFileSendDir = (value) => {
  form.configs.tool.allowed_file_send_dirs = form.configs.tool.allowed_file_send_dirs.filter(item => item !== value)
}

const normalizeExtension = (value) => {
  if (!value) return ''
  return value.startsWith('.') ? value.toLowerCase() : `.${value.toLowerCase()}`
}

const addFileSendBlockedExtension = () => {
  if (addUniqueListValue(form.configs.tool.file_send_blocked_extensions, fileSendBlockedExtensionInput.value, normalizeExtension)) {
    fileSendBlockedExtensionInput.value = ''
  }
}

const removeFileSendBlockedExtension = (value) => {
  form.configs.tool.file_send_blocked_extensions = form.configs.tool.file_send_blocked_extensions.filter(item => item !== value)
}

const migrateToolConfig = (toolConfig) => {
  if (!toolConfig || toolConfig.tool_timeout !== undefined) return toolConfig
  if (toolConfig.tool_timeout !== undefined) {
    toolConfig.tool_timeout = toolConfig.tool_timeout
  } else if (toolConfig.tool_timeout !== undefined) {
    toolConfig.tool_timeout = toolConfig.tool_timeout
  }
  return toolConfig
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
  allowedFileSendDirInput.value = ''
  fileSendBlockedExtensionInput.value = ''
  if (type === 'edit' && row) {
    form.id = row.id
    form.uid = row.uid || null
    form.name = row.name
    form.prompt_id = row.prompt_id
    form.knowledge_base_ids = [...(row.knowledge_base_ids || [])]
    const base = defaultProfileConfigs()
    if (row.configs) {
      if (row.configs.tool) migrateToolConfig(row.configs.tool)
      if (row.configs.channel) {
        const p = row.configs.channel
        // 深合并渠道配置
        if (p.chat_channel) Object.assign(base.channel.chat_channel, JSON.parse(JSON.stringify(p.chat_channel)))
        if (p.context_summary_channel) {
          Object.assign(base.channel.context_summary_channel, JSON.parse(JSON.stringify(p.context_summary_channel)))
        } else {
          base.channel.context_summary_channel = JSON.parse(JSON.stringify(base.channel.chat_channel))
        }
        if (p.rerank_channel) Object.assign(base.channel.rerank_channel, JSON.parse(JSON.stringify(p.rerank_channel)))
        if (p.image_generation_channel) Object.assign(base.channel.image_generation_channel, JSON.parse(JSON.stringify(p.image_generation_channel)))
      }
      if (row.configs.security) Object.assign(base.security, row.configs.security)
      if (row.configs.tool) Object.assign(base.tool, row.configs.tool)
      if (row.configs.other) Object.assign(base.other, row.configs.other)
    }
    form.configs = base
  } else {
    form.id = null
    form.uid = users.value[0]?.uid || null
    form.name = ''
    form.prompt_id = null
    form.knowledge_base_ids = []
    form.configs = defaultProfileConfigs()
  }
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name) {
    return ElMessage.warning(t('profiles.fill_required'))
  }
  if (dialogType.value === 'create' && showOwnerColumn.value && !form.uid) {
    return ElMessage.warning(t('profiles.select_owner'))
  }

  // 清理无效规则与旧版规则级启用状态，并按后端规则排序：priority 数字越小越优先。
  // 同一 priority 内保留当前顺序，因为该顺序就是加权轮询周期内的使用顺序。
  const compareRules = (left, right) => {
    return (left.priority || 1) - (right.priority || 1)
  }

  const cleanChannel = (ch) => {
    if (ch && ch.rules) {
      ch.rules = ch.rules
        .filter(r => r.channel_id && r.model_id)
        .map(({ channel_id, model_id, priority, weight }) => ({ channel_id, model_id, priority, weight }))
        .sort(compareRules)
    }
  }
  cleanChannel(form.configs.channel.chat_channel)
  cleanChannel(form.configs.channel.context_summary_channel)
  cleanChannel(form.configs.channel.rerank_channel)
  cleanChannel(form.configs.channel.image_generation_channel)
  addAllowedFileSendDir()
  addFileSendBlockedExtension()

  submitting.value = true
  try {
    if (dialogType.value === 'create') {
      await profileApi.create({
        uid: form.uid,
        name: form.name,
        prompt_id: form.prompt_id,
        knowledge_base_ids: form.knowledge_base_ids,
        configs: form.configs
      })
    } else {
      await profileApi.update(form.id, {
        name: form.name,
        prompt_id: form.prompt_id,
        knowledge_base_ids: form.knowledge_base_ids,
        configs: form.configs
      })
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
  fetchChannels()
  fetchPrompts()
  loadSystemSettings()
  fetchKnowledgeBases()
  fetchUsers()
})
</script>

<style lang="scss">
@import "@/assets/css/ProfilesView.scss";
</style>
