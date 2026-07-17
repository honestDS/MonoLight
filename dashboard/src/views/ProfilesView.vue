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

    <ProfileFormDialog
      v-model:active-tab="activeTab"
      v-model:allowed-file-send-dir-input="allowedFileSendDirInput"
      v-model:audit-model-key="auditModelKey"
      v-model:dialog-visible="dialogVisible"
      v-model:file-send-blocked-extension-input="fileSendBlockedExtensionInput"
      :audit-model-options="auditModelOptions"
      :channels="channels"
      :context-summary-threshold-options="contextSummaryThresholdOptions"
      :dialog-type="dialogType"
      :form="form"
      :knowledge-base-options="knowledgeBaseOptions"
      :locale-options="localeOptions"
      :prompts="prompts"
      :show-owner-column="showOwnerColumn"
      :submitting="submitting"
      :tool-options="toolOptions"
      :users="users"
      @add-allowed-file-send-dir="addAllowedFileSendDir"
      @add-file-send-blocked-extension="addFileSendBlockedExtension"
      @remove-allowed-file-send-dir="removeAllowedFileSendDir"
      @remove-file-send-blocked-extension="removeFileSendBlockedExtension"
      @submit="submitForm"
    />

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
        <el-form-item :label="$t('profiles.audit_retention_days')">
          <el-input-number v-model="systemSettings.audit_retention_days" :min="1" :max="3650" class="full-width-input" controls-position="right" />
          <div class="help-text mt-5">{{ $t('profiles.audit_retention_days_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('profiles.audit_report_email')">
          <el-input v-model.trim="systemSettings.audit_report_email" type="email" :placeholder="$t('profiles.audit_report_email_placeholder')" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('profiles.audit_report_email_hint') }}</div>
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
import ProfileFormDialog from '../components/ProfileFormDialog.vue'
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
  audit_retention_days: 90,
  audit_report_email: '',
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
