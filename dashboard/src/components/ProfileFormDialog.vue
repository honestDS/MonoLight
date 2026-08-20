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
                  <el-select v-model="form.uid" :placeholder="$t('profiles.select_owner')" filterable class="full-width-input" @change="$emit('owner-change', $event)">
                    <el-option v-for="item in users" :key="item.uid" :label="item.username" :value="item.uid"></el-option>
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('profiles.associated_prompt')">
                  <el-select v-model="form.prompt_id" :placeholder="$t('profiles.optional_prompt')" clearable class="full-width-input">
                    <el-option v-for="item in prompts" :key="item.id" :label="item.name" :value="item.id"></el-option>
                  </el-select>
                </el-form-item>
                <el-form-item>
                  <template #label>
                    {{ $t('profiles.context_summary_threshold') }}
                    <HelpTooltip :content="$t('profiles.context_summary_threshold_hint')" />
                  </template>
                  <el-select v-model="form.configs.other.context_summary_threshold_percent" class="full-width-input">
                    <el-option
                      v-for="percent in contextSummaryThresholdOptions"
                      :key="percent"
                      :label="`${percent}%`"
                      :value="percent"
                    ></el-option>
                  </el-select>
                </el-form-item>
              </div>
            </div>
          </el-tab-pane>

          <!-- 记忆设置 -->
          <el-tab-pane :label="$t('profiles.memory_settings')" name="memory">
            <div class="tab-pane-content">
              <el-alert
                v-if="memorySettingsUnavailable"
                type="warning"
                :closable="false"
                show-icon
                :title="$t('profiles.memory_settings_unavailable')"
                class="mb-15"
              />
              <el-alert
                v-else-if="memorySettingsReady && memoryStorageConfigured === false"
                type="warning"
                :closable="false"
                show-icon
                :title="$t('profiles.memory_storage_not_configured')"
                class="mb-15"
              />
              <div class="settings-section">
                <div class="settings-section-title">
                  {{ $t('profiles.long_term_memory_settings') }}
                  <HelpTooltip v-if="dialogType !== 'edit'" :content="$t('profiles.memory_embedding_create_hint')" />
                </div>
                <el-form-item>
                  <template #label>
                    {{ $t('profiles.long_term_memory_enabled') }}
                    <HelpTooltip :content="$t('profiles.long_term_memory_enabled_hint')" />
                  </template>
                  <el-switch v-model="form.configs.memory.enabled"></el-switch>
                </el-form-item>
                <el-row :gutter="20">
                  <el-col :xs="24" :sm="12" :md="8">
                    <el-form-item>
                      <template #label>
                        {{ $t('profiles.memory_top_k') }}
                        <HelpTooltip :content="$t('profiles.memory_top_k_hint')" />
                      </template>
                      <el-input-number v-model="form.configs.memory.top_k" :min="1" :max="50" class="full-width-input" controls-position="right" />
                    </el-form-item>
                  </el-col>
                  <el-col :xs="24" :sm="12" :md="8">
                    <el-form-item>
                      <template #label>
                        {{ $t('profiles.memory_candidate_k') }}
                        <HelpTooltip :content="$t('profiles.memory_candidate_k_hint')" />
                      </template>
                      <el-input-number v-model="form.configs.memory.candidate_k" :min="1" :max="100" class="full-width-input" controls-position="right" />
                    </el-form-item>
                  </el-col>
                  <el-col :xs="24" :sm="12" :md="8">
                    <el-form-item>
                      <template #label>
                        {{ $t('profiles.memory_result_max_chars') }}
                        <HelpTooltip :content="$t('profiles.memory_result_max_chars_hint')" />
                      </template>
                      <el-input-number v-model="form.configs.memory.result_max_chars" :min="256" :max="50000" class="full-width-input" controls-position="right" />
                    </el-form-item>
                  </el-col>
                </el-row>
              </div>

              <div v-if="dialogType === 'edit'" class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.memory_embedding_settings') }}</div>
                <div class="help-text memory-embedding-scope">{{ $t('profiles.memory_embedding_scope') }}</div>
                <div class="model-summary memory-embedding-summary">
                  <div class="config-line">
                    <span>{{ $t('profiles.memory_embedding_current') }}</span>
                    <b>{{ memoryEmbeddingCurrentLabel || $t('profiles.memory_embedding_not_configured') }}</b>
                  </div>
                  <div v-if="memoryEmbeddingTargetLabel" class="config-line">
                    <span>{{ $t('profiles.memory_embedding_migration_target') }}</span>
                    <b>{{ memoryEmbeddingTargetLabel }}</b>
                  </div>
                  <div v-if="memoryEmbeddingMigrationStatusText" class="config-line">
                    <span>{{ $t('profiles.memory_embedding_migration_status') }}</span>
                    <el-tag :type="memoryEmbeddingMigrationStatusType">{{ memoryEmbeddingMigrationStatusText }}</el-tag>
                  </div>
                </div>
                <el-alert
                  v-if="memoryEmbeddingMigrationActive"
                  type="warning"
                  :closable="false"
                  show-icon
                  :title="$t('profiles.memory_embedding_migration_notice')"
                  class="mb-15"
                />
                <div class="el-form-item__content memory-embedding-action">
                  <HelpTooltip :content="$t('profiles.memory_embedding_workflow_hint')" />
                  <el-button
                    type="primary"
                    plain
                    :disabled="memoryEmbeddingMigrationActive"
                    @click="$emit('manage-memory-embedding')"
                  >
                    {{ $t(memoryEmbeddingMigrationActive ? 'profiles.memory_embedding_migration_active' : (memoryEmbeddingConfigured ? 'profiles.memory_embedding_change' : 'profiles.memory_embedding_configure')) }}
                  </el-button>
                </div>
              </div>

              <div v-loading="memorySettingsLoading" class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.memory_organization_settings') }}</div>
                <el-form-item>
                  <template #label>
                    {{ $t('profiles.auto_organize_enabled') }}
                    <HelpTooltip :content="$t('profiles.auto_organize_enabled_hint')" />
                  </template>
                  <el-switch v-model="form.memory_organization.auto_organize_enabled" :disabled="memorySettingsLoading || memorySettingsUnavailable || !memorySettingsReady" />
                </el-form-item>
                <el-form-item :label="$t('profiles.organization_channel')">
                  <el-select
                    v-model="form.memory_organization.organization_channel_id"
                    clearable
                    filterable
                    class="full-width-input"
                    :placeholder="$t('profiles.organization_channel_placeholder')"
                    :disabled="memorySettingsLoading || memorySettingsUnavailable || !memorySettingsReady"
                  >
                    <el-option v-for="channel in memoryOrganizationChannels" :key="channel.id" :label="channel.name" :value="channel.id" />
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('profiles.organization_model')">
                  <el-select
                    v-model="form.memory_organization.organization_model_id"
                    clearable
                    filterable
                    class="full-width-input"
                    :placeholder="$t('profiles.organization_model_placeholder')"
                    :disabled="memorySettingsLoading || memorySettingsUnavailable || !memorySettingsReady || !form.memory_organization.organization_channel_id"
                  >
                    <el-option v-for="model in memoryOrganizationModels" :key="model.model_id" :label="model.model_id" :value="model.model_id" />
                  </el-select>
                </el-form-item>
                <div v-if="memoryOrganizationModel" class="model-summary">
                  <div class="config-line"><span>{{ $t('profiles.selected_model') }}</span><b>{{ memoryOrganizationModel.model_id }}</b></div>
                  <div class="config-line"><span>{{ $t('profiles.context_window_k') }}</span><b>{{ memoryOrganizationModel.context_window_k ?? '-' }}</b></div>
                  <div class="config-line"><span>{{ $t('profiles.model_max_tokens') }}</span><b>{{ memoryOrganizationModel.max_tokens ?? '-' }}</b></div>
                  <div class="config-line"><span>{{ $t('profiles.required_output_tokens') }}</span><b>{{ memoryOrganizationRequiredOutputTokens ?? '-' }}</b></div>
                </div>
                <div v-else class="help-text">{{ $t('profiles.organization_model_not_selected') }}</div>
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
                <div class="settings-section-title">
                  {{ $t('profiles.context_summary_channel') }}
                  <HelpTooltip :content="$t('profiles.context_summary_channel_hint')" />
                </div>
                <ChannelEditor
                  :channel="form.configs.channel.context_summary_channel"
                  :channels="channels"
                  usage="CHAT"
                  :label="$t('profiles.context_summary_model')"
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
                <el-form-item>
                  <template #label>
                    {{ $t('profiles.bound_knowledge_bases') }}
                    <HelpTooltip :content="$t('profiles.knowledge_base_binding_hint')" />
                  </template>
                  <el-select
                    v-model="form.knowledge_base_ids"
                    :placeholder="$t('profiles.select_knowledge_bases')"
                    class="full-width-input"
                    multiple
                    filterable
                    collapse-tags
                    collapse-tags-tooltip
                    :disabled="knowledgeBasesLoading || knowledgeBasesUnavailable || !knowledgeBasesReady"
                  >
                    <el-option v-for="item in knowledgeBaseOptions" :key="item.value" :label="item.label" :value="item.value">
                      <div class="option-with-description">
                        <span>{{ item.label }}</span>
                        <span v-if="item.description" class="text-muted">{{ item.description }}</span>
                      </div>
                    </el-option>
                  </el-select>
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
                <el-form-item label-width="auto">
                  <template #label>
                    {{ $t('profiles.audit_report_language') }}
                    <HelpTooltip :content="$t('profiles.audit_report_language_hint')" />
                  </template>
                  <el-select v-model="form.configs.security.audit_report_language" class="full-width-input">
                    <el-option
                      v-for="locale in localeOptions"
                      :key="locale.value"
                      :label="locale.label"
                      :value="locale.value"
                    ></el-option>
                  </el-select>
                </el-form-item>
                <el-form-item label-width="auto">
                  <template #label>
                    {{ $t('profiles.secondary_confirmation') }}
                    <HelpTooltip :content="$t('profiles.secondary_confirmation_hint')" />
                  </template>
                  <el-switch
                    :model-value="form.configs.security.audit_threshold > 0"
                    @update:model-value="form.configs.security.audit_threshold = $event ? 5 : 0"
                  ></el-switch>
                </el-form-item>
                <el-form-item v-if="form.configs.security.audit_threshold > 0" label-width="auto">
                  <template #label>
                    {{ $t('profiles.audit_threshold') }}
                    <HelpTooltip :content="$t('profiles.audit_threshold_hint')" />
                  </template>
                  <el-slider v-model="form.configs.security.audit_threshold" :min="1" :max="7" show-stops show-input></el-slider>
                </el-form-item>
                <el-form-item label-width="auto">
                  <template #label>
                    {{ $t('profiles.audit_confirmation_timeout_seconds') }}
                    <HelpTooltip :content="$t('profiles.audit_confirmation_timeout_seconds_hint')" />
                  </template>
                  <el-input-number v-model="form.configs.security.audit_confirmation_timeout_seconds" :min="1" :max="86400" :step="1" class="full-width-input" controls-position="right"></el-input-number>
                </el-form-item>
              </div>
            </div>
          </el-tab-pane>

          <!-- 工具设置 -->
          <el-tab-pane :label="$t('profiles.tool_settings')" name="tool">
            <div class="tab-pane-content">
              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.common_tool_config') }}</div>
                <el-form-item>
                  <template #label>
                    {{ $t('profiles.allowed_operation_dirs') }}
                    <HelpTooltip :content="$t('profiles.allowed_operation_dirs_hint')" />
                  </template>
                  <div class="tag-input-panel full-width-input">
                    <el-input
                      :model-value="allowedOperationDirInput" @update:model-value="$emit('update:allowedOperationDirInput', $event)"
                      :placeholder="$t('profiles.allowed_operation_dirs_placeholder')"
                      @keyup.enter="$emit('add-allowed-operation-dir')"
                    >
                      <template #append>
                        <el-button @click="$emit('add-allowed-operation-dir')">{{ $t('profiles.add') }}</el-button>
                      </template>
                    </el-input>
                    <div v-if="form.configs.tool.allowed_operation_dirs.length" class="tag-list">
                      <el-tag
                        v-for="item in form.configs.tool.allowed_operation_dirs"
                        :key="item"
                        closable
                        @close="$emit('remove-allowed-operation-dir', item)"
                      >
                        {{ item }}
                      </el-tag>
                    </div>
                  </div>
                </el-form-item>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.scheduling_control') }}</div>
                <div class="scheduling-control-list">
                  <el-form-item>
                    <template #label>
                      {{ $t('profiles.max_parallel_tools') }}
                      <HelpTooltip :content="$t('profiles.max_parallel_tools_hint')" />
                    </template>
                    <el-input-number v-model="form.configs.tool.max_parallel_tools" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                  <el-form-item>
                    <template #label>
                      {{ $t('profiles.executor_max_workers') }}
                      <HelpTooltip :content="$t('profiles.executor_max_workers_hint')" />
                    </template>
                    <el-input-number v-model="form.configs.tool.executor_max_workers" :min="1" :max="100" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                  <el-form-item>
                    <template #label>
                      {{ $t('profiles.background_task_max_concurrency') }}
                      <HelpTooltip :content="$t('profiles.background_task_max_concurrency_hint')" />
                    </template>
                    <el-input-number v-model="form.configs.tool.background_task_max_concurrency" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                  <el-form-item>
                    <template #label>
                      {{ $t('profiles.scheduled_task_max_concurrency') }}
                      <HelpTooltip :content="$t('profiles.scheduled_task_max_concurrency_hint')" />
                    </template>
                    <el-input-number v-model="form.configs.tool.scheduled_task_max_concurrency" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                  <el-form-item :label="$t('profiles.max_turns')">
                    <el-input-number v-model="form.configs.tool.max_turns" :min="1" :max="20" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                  <el-form-item>
                    <template #label>
                      {{ $t('profiles.tool_timeout') }}
                      <HelpTooltip :content="$t('profiles.tool_timeout_hint')" />
                    </template>
                    <el-input-number v-model="form.configs.tool.tool_timeout" :min="1" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                  <el-form-item>
                    <template #label>
                      {{ $t('profiles.image_generation_tool_timeout') }}
                      <HelpTooltip :content="$t('profiles.image_generation_tool_timeout_hint')" />
                    </template>
                    <el-input-number v-model="form.configs.tool.image_generation_timeout" :min="1" :max="600" class="full-width-input" controls-position="right"></el-input-number>
                  </el-form-item>
                </div>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.tool_visibility_config') }}</div>
                <el-form-item>
                  <template #label>
                    {{ $t('profiles.enabled_tools') }}
                    <HelpTooltip :content="$t('profiles.enabled_tools_hint')" />
                  </template>
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
                </el-form-item>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.file_send_config') }}</div>
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
                <el-form-item>
                  <template #label>
                    {{ $t('profiles.file_send_blocked_extensions') }}
                    <HelpTooltip :content="$t('profiles.file_send_blocked_extensions_hint')" />
                  </template>
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
                </el-form-item>
              </div>

              <div class="settings-section">
                <div class="settings-section-title">{{ $t('profiles.firecrawl_config') }}</div>
                <el-row :gutter="20">
                  <el-col :span="24">
                    <el-form-item>
                      <template #label>
                        {{ $t('profiles.api_key') }}
                        <HelpTooltip :ariaLabel="$t('profiles.firecrawl_hint_1') + $t('profiles.firecrawl_hint_2') + $t('profiles.firecrawl_hint_3')">
                          <span>
                            {{ $t('profiles.firecrawl_hint_1') }}
                            <el-link type="primary" href="https://www.firecrawl.dev/" target="_blank" underline="never">{{ $t('profiles.firecrawl_hint_2') }}</el-link>
                            {{ $t('profiles.firecrawl_hint_3') }}
                          </span>
                        </HelpTooltip>
                      </template>
                      <el-input v-model="form.configs.tool.firecrawl_api_key" :placeholder="$t('profiles.firecrawl_key_placeholder')" show-password></el-input>
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
        <el-button type="primary" @click="$emit('submit')" size="default" :loading="submitting" :disabled="memorySettingsLoading || memorySettingsUnavailable || !memorySettingsReady">{{ $t('profiles.save') }}</el-button>
      </template>
    </el-dialog>
</template>

<script setup>
import ChannelEditor from './ChannelEditor.vue'
import HelpTooltip from './HelpTooltip.vue'

defineProps({
  activeTab: { type: String, required: true },
  allowedOperationDirInput: { type: String, required: true },
  auditModelKey: { type: String, default: null },
  auditModelOptions: { type: Array, required: true },
  channels: { type: Array, required: true },
  contextSummaryThresholdOptions: { type: Array, required: true },
  dialogType: { type: String, required: true },
  dialogVisible: { type: Boolean, required: true },
  fileSendBlockedExtensionInput: { type: String, required: true },
  form: { type: Object, required: true },
  knowledgeBaseOptions: { type: Array, required: true },
  knowledgeBasesLoading: { type: Boolean, required: true },
  knowledgeBasesReady: { type: Boolean, required: true },
  knowledgeBasesUnavailable: { type: Boolean, required: true },
  localeOptions: { type: Array, required: true },
  memoryEmbeddingConfigured: { type: Boolean, default: false },
  memoryEmbeddingCurrentLabel: { type: String, default: '' },
  memoryEmbeddingMigrationActive: { type: Boolean, default: false },
  memoryEmbeddingMigrationStatusText: { type: String, default: '' },
  memoryEmbeddingMigrationStatusType: { type: String, default: 'warning' },
  memoryEmbeddingTargetLabel: { type: String, default: '' },
  memoryOrganizationChannels: { type: Array, required: true },
  memoryOrganizationModels: { type: Array, required: true },
  memoryOrganizationModel: { type: Object, default: null },
  memoryOrganizationRequiredOutputTokens: { type: Number, default: 0 },
  memorySettingsLoading: { type: Boolean, required: true },
  memorySettingsReady: { type: Boolean, required: true },
  memorySettingsUnavailable: { type: Boolean, required: true },
  memoryStorageConfigured: { type: Boolean, default: true },
  prompts: { type: Array, required: true },
  showOwnerColumn: { type: Boolean, required: true },
  submitting: { type: Boolean, required: true },
  toolOptions: { type: Array, required: true },
  users: { type: Array, required: true }
})

defineEmits([
  'add-allowed-operation-dir',
  'add-file-send-blocked-extension',
  'manage-memory-embedding',
  'owner-change',
  'remove-allowed-operation-dir',
  'remove-file-send-blocked-extension',
  'submit',
  'update:activeTab',
  'update:allowedOperationDirInput',
  'update:auditModelKey',
  'update:dialogVisible',
  'update:fileSendBlockedExtensionInput'
])
</script>
