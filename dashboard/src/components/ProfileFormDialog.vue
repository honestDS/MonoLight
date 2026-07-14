<template>
    <el-dialog :title="dialogType === 'create' ? $t('profiles.create_profile') : $t('profiles.edit_profile')" :model-value="dialogVisible" @update:model-value="$emit('update:dialogVisible', $event)" width="50%" class="standard-dialog dialog-with-scroll-body profile-dialog" center align-center>
      <el-form :model="form" size="default" label-position="top">
        <el-tabs :model-value="activeTab" @update:model-value="$emit('update:activeTab', $event)">
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
                    :model-value="auditModelKey" @update:model-value="$emit('update:auditModelKey', $event)"
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
                      :model-value="allowedFileSendDirInput" @update:model-value="$emit('update:allowedFileSendDirInput', $event)"
                      :placeholder="$t('profiles.allowed_file_send_dirs_placeholder')"
                      @keyup.enter="$emit('add-allowed-file-send-dir')"
                    >
                      <template #append>
                        <el-button @click="$emit('add-allowed-file-send-dir')">{{ $t('profiles.add') }}</el-button>
                      </template>
                    </el-input>
                    <div v-if="form.configs.tool.allowed_file_send_dirs.length" class="tag-list">
                      <el-tag
                        v-for="item in form.configs.tool.allowed_file_send_dirs"
                        :key="item"
                        closable
                        @close="$emit('remove-allowed-file-send-dir', item)"
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
                      :model-value="fileSendBlockedExtensionInput" @update:model-value="$emit('update:fileSendBlockedExtensionInput', $event)"
                      :placeholder="$t('profiles.file_send_blocked_extensions_placeholder')"
                      @keyup.enter="$emit('add-file-send-blocked-extension')"
                    >
                      <template #append>
                        <el-button @click="$emit('add-file-send-blocked-extension')">{{ $t('profiles.add') }}</el-button>
                      </template>
                    </el-input>
                    <div v-if="form.configs.tool.file_send_blocked_extensions.length" class="tag-list">
                      <el-tag
                        v-for="item in form.configs.tool.file_send_blocked_extensions"
                        :key="item"
                        closable
                        @close="$emit('remove-file-send-blocked-extension', item)"
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
        <el-button @click="$emit('update:dialogVisible', false)" size="default">{{ $t('profiles.cancel') }}</el-button>
        <el-button type="primary" @click="$emit('submit')" size="default" :loading="submitting">{{ $t('profiles.save') }}</el-button>
      </template>
    </el-dialog>
</template>

<script setup>
import ChannelEditor from './ChannelEditor.vue'

defineProps({
  activeTab: { type: String, required: true },
  allowedFileSendDirInput: { type: String, required: true },
  auditModelKey: { type: String, default: null },
  auditModelOptions: { type: Array, required: true },
  channels: { type: Array, required: true },
  contextSummaryThresholdOptions: { type: Array, required: true },
  dialogType: { type: String, required: true },
  dialogVisible: { type: Boolean, required: true },
  fileSendBlockedExtensionInput: { type: String, required: true },
  form: { type: Object, required: true },
  knowledgeBaseOptions: { type: Array, required: true },
  prompts: { type: Array, required: true },
  showOwnerColumn: { type: Boolean, required: true },
  submitting: { type: Boolean, required: true },
  toolOptions: { type: Array, required: true },
  users: { type: Array, required: true }
})

defineEmits([
  'add-allowed-file-send-dir',
  'add-file-send-blocked-extension',
  'remove-allowed-file-send-dir',
  'remove-file-send-blocked-extension',
  'submit',
  'update:activeTab',
  'update:allowedFileSendDirInput',
  'update:auditModelKey',
  'update:dialogVisible',
  'update:fileSendBlockedExtensionInput'
])
</script>
