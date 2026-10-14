<template>
  <div class="view-container">
    <div v-if="userPermissions.isSuperuser" class="profile-filter-card">
      <el-form :inline="true" :model="filters" @submit.prevent>
        <el-form-item :label="$t('profiles.owner_username')">
          <el-input
            v-model="filters.ownerUsername"
            :placeholder="$t('profiles.owner_search_placeholder')"
            clearable
            class="profile-owner-filter-input"
            @keyup.enter="handleSearch"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">{{ $t('profiles.search') }}</el-button>
          <el-button @click="resetFilters">{{ $t('profiles.reset') }}</el-button>
        </el-form-item>
      </el-form>
    </div>

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
        <el-button v-if="userPermissions.isSuperuser" size="default" @click="showSystemSettingsDialog">{{ $t('profiles.global_settings') }}</el-button>
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
          <StatusTag :status="scope.row.is_default" :active-text="$t('profiles.default')" :inactive-text="$t('profiles.not_default')" />
        </template>
      </el-table-column>

      <el-table-column :resizable="false" :label="$t('profiles.actions')" width="380" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button v-if="canSetDefaultProfile(scope.row)" :type="scope.row.is_default ? 'info' : 'success'" size="small" :disabled="scope.row.is_default" @click="handleSetDefault(scope.row.id)">{{ $t('profiles.set_default') }}</el-button>
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('profiles.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('profiles.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <ProfileFormDialog
      v-model:active-tab="activeTab"
      v-model:allowed-operation-dir-input="allowedOperationDirInput"
      v-model:audit-model-key="auditModelKey"
      v-model:dialog-visible="dialogVisible"
      v-model:file-send-blocked-extension-input="fileSendBlockedExtensionInput"
      :audit-model-options="auditModelOptions"
      :channels="channels"
      :context-summary-threshold-options="contextSummaryThresholdOptions"
      :dialog-type="dialogType"
      :form="form"
      :knowledge-base-options="knowledgeBaseOptions"
      :knowledge-bases-loading="knowledgeBasesLoading"
      :knowledge-bases-ready="knowledgeBasesReady"
      :knowledge-bases-unavailable="knowledgeBasesUnavailable"
      :locale-options="localeOptions"
      :memory-embedding-configured="memoryEmbeddingConfigured"
      :memory-embedding-current-label="memoryEmbeddingCurrentLabel"
      :memory-embedding-migration-active="memoryEmbeddingMigrationActive"
      :memory-embedding-migration-status-text="memoryEmbeddingMigrationStatusText"
      :memory-embedding-migration-status-type="memoryEmbeddingMigrationStatusType"
      :memory-embedding-target-label="memoryEmbeddingTargetLabel"
      v-model:memory-organization-model-key="memoryOrganizationModelKey"
      :memory-organization-model-options="memoryOrganizationModelOptions"
      :memory-organization-model="memoryOrganizationModel"
      :memory-organization-required-output-tokens="memoryOrganizationRequiredOutputTokens"
      :memory-settings-loading="memorySettingsLoading"
      :memory-settings-ready="memorySettingsReady"
      :memory-settings-unavailable="memorySettingsUnavailable"
      :memory-storage-configured="memoryStorageConfigured"
      :prompts="prompts"
      :show-owner-column="showOwnerColumn"
      :submitting="submitting"
      :tool-options="toolOptions"
      :users="users"
      @update:dialog-visible="handleDialogVisibilityChange"
      @owner-change="handleMemoryOwnerChange"
      @add-allowed-operation-dir="addAllowedOperationDir"
      @add-file-send-blocked-extension="addFileSendBlockedExtension"
      @remove-allowed-operation-dir="removeAllowedOperationDir"
      @remove-file-send-blocked-extension="removeFileSendBlockedExtension"
      @manage-memory-embedding="openMemoryEmbeddingDialog"
      @submit="submitForm"
    />

    <MemoryEmbeddingDialog
      v-model:visible="memoryEmbeddingDialogVisible"
      v-model:target-key="memoryEmbeddingTargetKey"
      v-model:confirmation-checked="memoryConfirmationChecked"
      :configured="memoryEmbeddingConfigured"
      :current-label="memoryEmbeddingCurrentLabel"
      :options="memoryEmbeddingOptions"
      :preview="memoryPreview"
      :previewing="memoryEmbeddingPreviewing"
      :confirming="memoryEmbeddingConfirming"
      :requires-migration="memoryPreviewRequiresMigration"
      @detect="previewMemoryEmbedding"
      @confirm="confirmMemoryEmbedding"
      @closed="closeMemoryEmbeddingDialog"
    />

    <el-dialog v-if="userPermissions.isSuperuser" :title="$t('profiles.global_settings')" v-model="settingsDialogVisible" width="520px" class="standard-dialog" center align-center>
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
import '@/assets/css/ProfilesView.scss'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import ProfileFormDialog from '../components/ProfileFormDialog.vue'
import MemoryEmbeddingDialog from '../components/MemoryEmbeddingDialog.vue'
import { useProfilesView } from '../composables/profiles/useProfilesView.js'

const {
  userPermissions,
  filters,
  handleSearch,
  resetFilters,
  profiles,
  loading,
  total,
  currentPage,
  pageSize,
  showDialog,
  handleRefresh,
  loadProfiles,
  handleSizeChange,
  showSystemSettingsDialog,
  showOwnerColumn,
  canSetDefaultProfile,
  handleSetDefault,
  handleDelete,
  activeTab,
  allowedOperationDirInput,
  auditModelKey,
  dialogVisible,
  fileSendBlockedExtensionInput,
  auditModelOptions,
  channels,
  contextSummaryThresholdOptions,
  dialogType,
  form,
  knowledgeBaseOptions,
  knowledgeBasesLoading,
  knowledgeBasesReady,
  knowledgeBasesUnavailable,
  localeOptions,
  memoryEmbeddingConfigured,
  memoryEmbeddingCurrentLabel,
  memoryEmbeddingMigrationActive,
  memoryEmbeddingMigrationStatusText,
  memoryEmbeddingMigrationStatusType,
  memoryEmbeddingTargetLabel,
  memoryOrganizationModelKey,
  memoryOrganizationModelOptions,
  memoryOrganizationModel,
  memoryOrganizationRequiredOutputTokens,
  memorySettingsLoading,
  memorySettingsReady,
  memorySettingsUnavailable,
  memoryStorageConfigured,
  prompts,
  submitting,
  toolOptions,
  users,
  handleDialogVisibilityChange,
  handleMemoryOwnerChange,
  addAllowedOperationDir,
  addFileSendBlockedExtension,
  removeAllowedOperationDir,
  removeFileSendBlockedExtension,
  openMemoryEmbeddingDialog,
  submitForm,
  memoryEmbeddingDialogVisible,
  memoryEmbeddingTargetKey,
  memoryConfirmationChecked,
  memoryEmbeddingOptions,
  memoryPreview,
  memoryEmbeddingPreviewing,
  memoryEmbeddingConfirming,
  memoryPreviewRequiresMigration,
  previewMemoryEmbedding,
  confirmMemoryEmbedding,
  closeMemoryEmbeddingDialog,
  settingsDialogVisible,
  systemSettings,
  settingsSubmitting,
  saveSystemSettings,
} = useProfilesView()
</script>
