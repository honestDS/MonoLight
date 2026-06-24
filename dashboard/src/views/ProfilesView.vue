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
            <el-button :type="scope.row.is_active ? 'info' : 'success'" size="small" :disabled="scope.row.is_active" @click="handleActivate(scope.row.id)">{{ $t('profiles.activate') }}</el-button>
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('profiles.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('profiles.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? $t('profiles.create_profile') : $t('profiles.edit_profile')" v-model="dialogVisible" width="50%" class="standard-dialog dialog-with-scroll-body" center align-center>
      <el-form :model="form" label-width="120px" size="default">
        <el-tabs v-model="activeTab">
          <!-- 基础设置 -->
          <el-tab-pane :label="$t('profiles.base_settings')" name="base">
            <div class="tab-pane-content">
              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.base_settings') }}</div>
                <el-form-item :label="$t('profiles.profile_name')">
                  <el-input v-model="form.name" :placeholder="$t('profiles.unique_name')"></el-input>
                </el-form-item>
                <el-form-item :label="$t('profiles.associated_prompt')">
                  <el-select v-model="form.prompt_id" :placeholder="$t('profiles.optional_prompt')" clearable class="full-width-input">
                    <el-option v-for="item in prompts" :key="item.id" :label="item.name" :value="item.id"></el-option>
                  </el-select>
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
                <div class="settings-section-title">{{ $t('profiles.embedding_channel') }}</div>
                <ChannelEditor
                  :channel="form.configs.channel.embedding_channel"
                  :channels="channels"
                  usage="EMBEDDING"
                  :label="$t('profiles.embedding_model')"
                />
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


            </div>
          </el-tab-pane>

          <!-- 安全设置 -->
          <el-tab-pane :label="$t('profiles.security_settings')" name="security">
            <div class="tab-pane-content">
              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.security_settings') }}</div>
                <el-form-item :label="$t('profiles.audit_model_id')">
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
                <el-form-item :label="$t('profiles.audit_threshold')">
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
                <el-row :gutter="20">
                  <el-col :span="8">
                    <el-form-item :label="$t('profiles.max_parallel_tools')">
                      <el-input-number v-model="form.configs.tool.max_parallel_tools" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                      <div class="help-text mt-5">{{ $t('profiles.max_parallel_tools_hint') }}</div>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="$t('profiles.executor_max_workers')">
                      <el-input-number v-model="form.configs.tool.executor_max_workers" :min="1" :max="100" class="full-width-input" controls-position="right"></el-input-number>
                      <div class="help-text mt-5">{{ $t('profiles.executor_max_workers_hint') }}</div>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="$t('profiles.max_turns')">
                      <el-input-number v-model="form.configs.tool.max_turns" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="$t('profiles.tool_timeout')">
                      <el-input-number v-model="form.configs.tool.tool_timeout" :min="1" class="full-width-input" controls-position="right"></el-input-number>
                      <div class="help-text mt-5">{{ $t('profiles.tool_timeout_hint') }}</div>
                    </el-form-item>
                  </el-col>
                </el-row>
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

          <!-- 其他设置 -->
          <el-tab-pane :label="$t('profiles.other_settings')" name="other">
            <div class="tab-pane-content">
                <div class="settings-section">
                  <div class="settings-section-title">{{ $t('profiles.log_storage_settings') }}</div>
                  <el-form-item :label="$t('profiles.log_locale')">
                    <el-select v-model="form.configs.other.log_locale" class="full-width-input">
                      <el-option
                        v-for="item in logLocaleOptions"
                        :key="item.value"
                        :label="item.label"
                        :value="item.value"
                      ></el-option>
                    </el-select>
                    <div class="help-text mt-5">{{ $t('profiles.log_locale_hint') }}</div>
                  </el-form-item>
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
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { profileApi, channelApi, promptApi, systemApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import ChannelEditor from '../components/ChannelEditor.vue'
import { defaultProfileConfigs } from '../constants'
import { SUPPORT_LOCALES } from '../i18n'

const { t } = useI18n()

const profiles = ref([])
const channels = ref([])
const prompts = ref([])
const backendLocales = ref([])
const toolOptions = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const dialogVisible = ref(false)
const dialogType = ref('create')
const submitting = ref(false)
const activeTab = ref('base')
const allowedFileSendDirInput = ref('')
const fileSendBlockedExtensionInput = ref('')

const logLocaleOptions = computed(() => {
  const localeItems = backendLocales.value.length ? backendLocales.value : ['zh']
  return localeItems.map(value => {
    const matched = SUPPORT_LOCALES.find(item => item.value === value)
    return {
      value,
      label: matched ? matched.label : value
    }
  })
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
  name: '',
  prompt_id: null,
  configs: defaultProfileConfigs()
})

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

const fetchBackendLocales = async () => {
  try {
    const res = await systemApi.i18nLocales()
    backendLocales.value = res.data.data.items || []
  } catch (err) {
    backendLocales.value = ['zh']
  }
}

const fetchChannels = async () => {
  try {
    const res = await channelApi.list({ page: 1, size: 1000 })
    channels.value = res.data.data.items || []
  } catch (err) {
    ElMessage.error(err.message || t('profiles.load_channels_failed'))
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  loadProfiles()
  fetchChannels()
  fetchPrompts()
  fetchBackendLocales()
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
    form.name = row.name
    form.prompt_id = row.prompt_id
    const base = defaultProfileConfigs()
    if (row.configs) {
      if (row.configs.tool) migrateToolConfig(row.configs.tool)
      if (row.configs.channel) {
        const p = row.configs.channel
        // 深合并渠道配置
        if (p.chat_channel) Object.assign(base.channel.chat_channel, JSON.parse(JSON.stringify(p.chat_channel)))
        if (p.embedding_channel) Object.assign(base.channel.embedding_channel, JSON.parse(JSON.stringify(p.embedding_channel)))
        if (p.rerank_channel) Object.assign(base.channel.rerank_channel, JSON.parse(JSON.stringify(p.rerank_channel)))
      }
      if (row.configs.security) Object.assign(base.security, row.configs.security)
      if (row.configs.tool) Object.assign(base.tool, row.configs.tool)
      if (row.configs.other) Object.assign(base.other, row.configs.other)
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
  if (!form.name) {
    return ElMessage.warning(t('profiles.fill_required'))
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
  cleanChannel(form.configs.channel.embedding_channel)
  cleanChannel(form.configs.channel.rerank_channel)
  addAllowedFileSendDir()
  addFileSendBlockedExtension()

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
  fetchChannels()
  fetchPrompts()
  fetchBackendLocales()
})
</script>

<style lang="scss">
@import "@/assets/css/ProfilesView.scss";
</style>
