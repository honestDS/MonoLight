<template>
  <div class="view-container">
    <BaseDataTable
      :data="tableData"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('knowledgeBase.create_kb')"
      :refresh-text="$t('knowledgeBase.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @create="showDialog"
      @refresh="handleRefresh"
      @page-change="fetchData"
      @size-change="handleSizeChange"
    >
      <el-table-column
        prop="name"
        :label="$t('knowledgeBase.kb_name')"
        show-overflow-tooltip
        :resizable="false"
        min-width="180px"
      />
      <el-table-column
        prop="username"
        :label="$t('knowledgeBase.owner_username')"
        min-width="180"
        show-overflow-tooltip
        :resizable="false"
      >
        <template #default="{ row }">{{ getKnowledgeBaseOwnerLabel(row) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('knowledgeBase.owner_profiles')" min-width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ getKnowledgeBaseProfileLabel(row) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('knowledgeBase.knowledge_base_type')" align="center">
        <template #default="{ row }">
          <el-tag :type="canManageManagedKnowledge(row) ? 'warning' : 'info'">
            {{ canManageManagedKnowledge(row) ? $t('knowledgeBase.type_managed') : $t('knowledgeBase.type_user') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="description" :label="$t('knowledgeBase.description')" min-width="220" show-overflow-tooltip />
      <el-table-column :resizable="false" :label="$t('knowledgeBase.active_embedding')" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ getEmbeddingModelName(row) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('knowledgeBase.target_configuration')" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">{{ getTargetEmbeddingModelName(row) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('knowledgeBase.migration_status')" width="130" align="center">
        <template #default="{ row }">{{ getMigrationStatusLabel(row.migration_status) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="created_at" :label="$t('knowledgeBase.created_at')" width="180" sortable>
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>

      <el-table-column :resizable="false" :label="$t('knowledgeBase.actions')" width="300" align="center" fixed="right">
        <template #default="{ row }">
          <div class="action-buttons knowledge-base-action-buttons">
            <el-button v-if="canManageKnowledgeBaseDocuments(row)" type="success" size="small" @click="showDocumentDialog(row)">{{ $t('knowledgeBase.documents') }}</el-button>
            <el-button v-if="canManageManagedKnowledge(row)" type="success" size="small" @click="showManagedKnowledgeDialog(row)">{{ $t('knowledgeBase.documents') }}</el-button>
            <el-button type="warning" size="small" @click="showQueryTestDialog(row)">{{ $t('knowledgeBase.test') }}</el-button>
            <el-dropdown trigger="click" popper-class="knowledge-base-more-dropdown" @command="handleKnowledgeBaseMoreAction($event, row)">
              <el-button size="small">{{ $t('knowledgeBase.more') }}</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item v-if="canManageKnowledgeBaseDocuments(row)" command="import_document">{{ $t('knowledgeBase.import_doc') }}</el-dropdown-item>
                  <el-dropdown-item command="embedding_status">{{ $t('knowledgeBase.embedding_status') }}</el-dropdown-item>
                  <el-dropdown-item command="edit">{{ $t('knowledgeBase.edit') }}</el-dropdown-item>
                  <el-dropdown-item command="delete" class="danger-dropdown-item">{{ $t('knowledgeBase.delete') }}</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <!-- 知识库弹窗 -->
    <el-dialog
      :title="isEditing ? $t('knowledgeBase.edit_kb') : $t('knowledgeBase.create_kb')"
      v-model="dialogVisible"
      width="50%"
      class="standard-dialog"
      center
      align-center
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="120px" size="default">
        <el-form-item
          v-if="!isEditing && isSuperuser"
          prop="uid"
          :label="$t('knowledgeBase.owner_username')"
          :error="ownerUsersError || undefined"
          for="knowledge-base-owner-select"
        >
          <el-select
            id="knowledge-base-owner-select"
            v-model="form.uid"
            filterable
            class="full-width-input"
            :placeholder="$t('knowledgeBase.select_owner_user')"
            :loading="ownerUsersLoading"
            :disabled="ownerUsersLoading || submitting"
          >
            <el-option
              v-for="user in ownerUsers"
              :key="user.uid"
              :label="user.username || $t('knowledgeBase.owner_unknown')"
              :value="user.uid"
            />
          </el-select>
          <el-button v-if="ownerUsersError" link type="primary" :disabled="submitting" @click="fetchOwnerUsers">
            {{ $t('knowledgeBase.refresh') }}
          </el-button>
        </el-form-item>
        <el-form-item v-else-if="isEditing" :label="$t('knowledgeBase.owner_username')">
          <el-input :model-value="form.username || $t('knowledgeBase.owner_unknown')" readonly />
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.name')" prop="name">
          <el-input v-model="form.name" :placeholder="$t('knowledgeBase.input_kb_name')" />
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.description')" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :placeholder="$t('knowledgeBase.input_kb_desc')"
            :rows="3"
          />
        </el-form-item>
        <el-form-item v-if="!isEditing" :label="$t('knowledgeBase.embedding_model')" prop="embedding_model_key">
          <el-select
            v-model="form.embedding_model_key"
            :placeholder="$t('knowledgeBase.select_embedding_model')"
            class="full-width-input"
            filterable
          >
            <el-option
              v-for="item in embeddingModelOptions"
              :key="item.key"
              :label="item.label"
              :value="item.key"
            />
          </el-select>
          <div class="help-text mt-5">{{ $t('knowledgeBase.embedding_model_hint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('knowledgeBase.cancel') }}</el-button>
        <el-button
          type="primary"
          :loading="submitting"
          :disabled="!isEditing && isSuperuser && (ownerUsersLoading || !!ownerUsersError)"
          @click="submitForm"
          size="default"
        >
          {{ $t('knowledgeBase.confirm') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      :title="$t('knowledgeBase.embedding_migration_title', { name: selectedKb?.name || '' })"
      v-model="migrationDialogVisible"
      width="680px"
      class="standard-dialog"
      center
      align-center
      @closed="stopMigrationPolling"
    >
      <el-form label-width="150px" size="default">
        <el-form-item :label="$t('knowledgeBase.knowledge_base_type')">
          <el-tag :type="selectedKb?.knowledge_base_type === 'llm_managed' ? 'warning' : 'info'">
            {{ selectedKb?.knowledge_base_type === 'llm_managed' ? $t('knowledgeBase.type_managed') : $t('knowledgeBase.type_user') }}
          </el-tag>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.active_embedding')">
          <div>
            <div>{{ getEmbeddingModelName(selectedKb) }}</div>
            <div class="help-text">
              {{ $t('knowledgeBase.embedding_dimensions_value', { dimensions: selectedKb?.active_embedding_dimensions || '-' }) }}
              · {{ $t('knowledgeBase.embedding_revision_value', { revision: selectedKb?.active_embedding_revision || '-' }) }}
            </div>
          </div>
        </el-form-item>

        <el-alert
          v-if="selectedKb?.knowledge_base_type === 'llm_managed'"
          :title="$t('knowledgeBase.managed_embedding_follow_hint')"
          type="info"
          :closable="false"
          class="mb-15"
        />
        <el-alert
          v-if="managedMigrationTerminalHintKey"
          :title="$t(managedMigrationTerminalHintKey)"
          type="warning"
          :closable="false"
          class="mb-15"
        />

        <el-form-item
          v-if="selectedKb?.knowledge_base_type === 'user'"
          :label="$t('knowledgeBase.target_embedding')"
        >
          <el-select
            v-model="migrationTargetKey"
            :placeholder="$t('knowledgeBase.select_embedding_model')"
            class="full-width-input"
            filterable
            :disabled="!canStartKnowledgeBaseMigration(selectedKb)"
          >
            <el-option
              v-for="item in embeddingModelOptions"
              :key="item.key"
              :label="item.label"
              :value="item.key"
            />
          </el-select>
          <div class="help-text mt-5">{{ $t('knowledgeBase.embedding_migration_hint') }}</div>
        </el-form-item>

        <el-form-item :label="$t('knowledgeBase.target_configuration')">
          {{ getTargetEmbeddingModelName(selectedKb) }}
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.migration_status')">
          {{ getMigrationStatusLabel(selectedKb?.migration_status) }}
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.migration_progress')">
          <div class="migration-progress-content">
            <el-progress :percentage="migrationProgress" />
            <div class="help-text mt-5">
              {{ $t('knowledgeBase.migration_counts', {
                success: selectedKb?.migration_success_count || 0,
                total: selectedKb?.migration_total_count || 0,
                failed: selectedKb?.migration_failure_count || 0
              }) }}
            </div>
          </div>
        </el-form-item>
        <el-form-item v-if="selectedKb?.migration_error" :label="$t('knowledgeBase.migration_error')">
          <el-alert type="error" :closable="false" :title="selectedKb.migration_error" />
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.old_collection_cleanup')">
          {{ getCleanupStatusLabel(selectedKb?.old_collection_cleanup_status) }}
        </el-form-item>
        <el-form-item v-if="selectedKb?.old_collection_cleanup_error" :label="$t('knowledgeBase.cleanup_error')">
          <el-alert type="error" :closable="false" :title="selectedKb.old_collection_cleanup_error" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="migrationDialogVisible = false" size="default">{{ $t('knowledgeBase.close') }}</el-button>
        <el-button
          v-if="selectedKb?.knowledge_base_type === 'user'"
          type="primary"
          :loading="migrationSubmitting"
          :disabled="!migrationTargetKey || !canStartKnowledgeBaseMigration(selectedKb)"
          @click="submitEmbeddingMigration"
        >
          {{ $t('knowledgeBase.start_migration') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 导入文档弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.import_doc')"
      v-model="importDialogVisible"
      width="560px"
      class="standard-dialog"
      center
      align-center
    >
      <el-alert
        :title="$t('knowledgeBase.import_alert')"
        type="info"
        :closable="false"
        class="mb-15"
      />
      <el-form :model="importForm" :rules="importRules" ref="importFormRef" label-width="120px" size="default">
        <el-form-item :label="$t('knowledgeBase.target_kb')">
          <el-input :model-value="selectedKb?.name || '-'" disabled />
          <div class="help-text mt-5">{{ $t('knowledgeBase.target_kb_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.select_doc')" prop="file">
          <el-upload
            class="full-width-input"
            action=""
            :auto-upload="false"
            :limit="1"
            :file-list="uploadFileList"
            :on-change="handleFileChange"
            :on-remove="handleFileRemove"
          >
            <el-button type="primary">{{ $t('knowledgeBase.select_file') }}</el-button>
          </el-upload>
          <div class="help-text mt-5">{{ $t('knowledgeBase.select_doc_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.chunk_size')" prop="chunk_size">
          <el-input-number v-model="importForm.chunk_size" :min="100" :max="20000" :step="100" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.chunk_size_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.chunk_overlap')" prop="chunk_overlap">
          <el-input-number v-model="importForm.chunk_overlap" :min="0" :max="5000" :step="50" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.chunk_overlap_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.batch_size')" prop="batch_size">
          <el-input-number v-model="importForm.batch_size" :min="1" :max="256" :step="1" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.batch_size_hint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importDialogVisible = false" size="default">{{ $t('knowledgeBase.cancel') }}</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport" size="default">{{ $t('knowledgeBase.start_import') }}</el-button>
      </template>
    </el-dialog>

    <!-- 文档管理弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.doc_management', { name: selectedKb?.name || '' })"
      v-model="documentDialogVisible"
      width="1040px"
      class="standard-dialog"
      center
      align-center
    >
      <el-table :data="documentList" :loading="documentLoading">
        <el-table-column prop="filename" :label="$t('knowledgeBase.filename')" min-width="220" show-overflow-tooltip />
        <el-table-column prop="chunk_count" :label="$t('knowledgeBase.chunk_count')" width="90" align="center" />
        <el-table-column prop="chunk_size" :label="$t('knowledgeBase.chunk_size')" width="100" align="center" />
        <el-table-column prop="chunk_overlap" :label="$t('knowledgeBase.overlap')" width="90" align="center" />
        <el-table-column prop="batch_size" :label="$t('knowledgeBase.batch_size_col')" width="90" align="center" />
        <el-table-column :label="$t('knowledgeBase.import_time')" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.actions')" width="210" align="center" fixed="right">
          <template #default="{ row }">
            <div class="action-buttons document-action-buttons">
              <el-button type="primary" size="small" @click="showContentDialog(row)">{{ $t('knowledgeBase.original_text') }}</el-button>
              <el-button type="danger" size="small" @click="handleDeleteDocument(row)">{{ $t('knowledgeBase.delete') }}</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="document-pagination">
        <el-pagination
          v-model:current-page="documentPage"
          v-model:page-size="documentPageSize"
          :total="documentTotal"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="fetchDocuments"
          @size-change="handleDocumentSizeChange"
        />
      </div>
    </el-dialog>

    <!-- 原文查看弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.view_original', { title: contentTitle })"
      v-model="contentDialogVisible"
      width="760px"
      class="standard-dialog"
      center
      align-center
    >
      <pre class="document-content">{{ documentContent }}</pre>
    </el-dialog>

    <el-dialog
      :title="$t('knowledgeBase.managed_items_title', { name: selectedKb?.name || '' })"
      v-model="managedKnowledgeDialogVisible"
      width="1180px"
      class="standard-dialog managed-knowledge-dialog"
      center
      align-center
      @closed="stopManagedKnowledgePolling"
    >
      <div class="managed-knowledge-toolbar">
        <el-input
          v-model="managedKnowledgeQuery"
          clearable
          :placeholder="$t('knowledgeBase.managed_search_placeholder')"
          @keyup.enter="handleManagedKnowledgeSearch"
          @clear="handleManagedKnowledgeSearch"
        />
        <el-button type="primary" @click="handleManagedKnowledgeSearch">{{ $t('knowledgeBase.search') }}</el-button>
        <el-button type="success" @click="showManagedKnowledgeCreateDialog">{{ $t('knowledgeBase.managed_add') }}</el-button>
        <el-button type="warning" @click="showOrganizationDialog">{{ $t('knowledgeBase.organization') }}</el-button>
      </div>
      <el-table
        :data="managedKnowledgeItems"
        :loading="managedKnowledgeLoading"
        row-key="id"
        class="managed-knowledge-table"
      >
        <el-table-column prop="knowledge_key" :label="$t('knowledgeBase.managed_key')" min-width="180" show-overflow-tooltip />
        <el-table-column prop="content_preview" :label="$t('knowledgeBase.managed_content_preview')" min-width="280" show-overflow-tooltip />
        <el-table-column prop="version" :label="$t('knowledgeBase.managed_version')" width="80" align="center" />
        <el-table-column :label="$t('knowledgeBase.managed_source')" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <div>{{ getManagedSourceLabel(row.source_type) }}</div>
            <div v-if="formatManagedSourceReference(row.source_reference) !== '-'" class="help-text">
              {{ formatManagedSourceReference(row.source_reference) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_created_by')" width="110" align="center">
          <template #default="{ row }">{{ getManagedActorLabel(row.created_by) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_modified_by')" width="110" align="center">
          <template #default="{ row }">{{ getManagedActorLabel(row.last_modified_by) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_llm_maintainable')" width="140" align="center">
          <template #default="{ row }">
            <el-tag :type="row.llm_maintainable ? 'success' : 'info'">
              {{ row.llm_maintainable ? $t('knowledgeBase.yes') : $t('knowledgeBase.no') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_status')" width="120" align="center">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.publication_job_status === 'failed' && row.publication_job_error"
              :content="row.publication_job_error"
              placement="top"
            >
              <span>{{ getManagedKnowledgeStatusLabel(row) }}</span>
            </el-tooltip>
            <span v-else>{{ getManagedKnowledgeStatusLabel(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_updated_at')" width="170">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.actions')" width="180" align="center" fixed="right">
          <template #default="{ row }">
            <div class="action-buttons managed-knowledge-action-buttons">
              <el-button type="primary" size="small" @click="showManagedKnowledgeEditDialog(row)">{{ $t('knowledgeBase.edit') }}</el-button>
              <el-dropdown trigger="click" popper-class="knowledge-base-more-dropdown" @command="handleManagedKnowledgeMoreAction($event, row)">
                <el-button size="small">{{ $t('knowledgeBase.more') }}</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="history">{{ $t('knowledgeBase.managed_history') }}</el-dropdown-item>
                    <el-dropdown-item v-if="row.publication_job_status === 'failed'" command="retry">{{ $t('knowledgeBase.managed_retry') }}</el-dropdown-item>
                    <el-dropdown-item command="delete" divided class="danger-dropdown-item">{{ $t('knowledgeBase.delete') }}</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="document-pagination">
        <el-pagination
          v-model:current-page="managedKnowledgePage"
          v-model:page-size="managedKnowledgePageSize"
          :total="managedKnowledgeTotal"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="handleManagedKnowledgePageChange"
          @size-change="handleManagedKnowledgeSizeChange"
        />
      </div>
    </el-dialog>

    <el-dialog
      :title="$t('knowledgeBase.organization_title', { name: selectedKb?.name || '' })"
      v-model="organizationDialogVisible"
      width="1020px"
      class="standard-dialog organization-dialog dialog-with-scroll-body"
      align-center
      @closed="stopOrganizationPolling"
    >
      <div class="organization-toolbar">
        <p class="organization-intro">{{ $t('knowledgeBase.organization_intro') }}</p>
        <el-button
          type="primary"
          :loading="organizationSubmitting"
          @click="submitKnowledgeOrganization"
        >
          {{ $t('knowledgeBase.organization_start') }}
        </el-button>
      </div>

      <section v-if="latestOrganizationJob" class="organization-latest">
        <div class="organization-latest__header">
          <div class="organization-latest__heading">
            <h3>{{ $t('knowledgeBase.organization_latest') }}</h3>
            <div class="organization-latest__meta">
              <span>{{ $t('knowledgeBase.organization_job_id') }}: {{ getOrganizationJobId(latestOrganizationJob) || '-' }}</span>
              <span>{{ $t('knowledgeBase.organization_snapshot') }}: {{ getOrganizationSnapshotId(latestOrganizationJob) }}</span>
            </div>
          </div>
          <div class="organization-latest__actions">
            <el-tag :type="getOrganizationStatusType(latestOrganizationJob.status)">
              {{ getOrganizationStatusLabel(latestOrganizationJob.status) }}
            </el-tag>
            <el-button
              v-if="canCancelOrganizationJob(latestOrganizationJob)"
              type="warning"
              size="small"
              :loading="organizationActionJobId === getOrganizationJobId(latestOrganizationJob)"
              @click="handleCancelOrganizationJob(latestOrganizationJob)"
            >
              {{ $t('knowledgeBase.organization_cancel') }}
            </el-button>
            <el-button
              v-if="canRetryOrganizationJob(latestOrganizationJob)"
              type="primary"
              size="small"
              :loading="organizationActionJobId === getOrganizationJobId(latestOrganizationJob)"
              @click="handleRetryOrganizationJob(latestOrganizationJob)"
            >
              {{ $t('knowledgeBase.organization_retry') }}
            </el-button>
          </div>
        </div>

        <div v-if="['pending', 'running', 'retry'].includes(latestOrganizationJob.status)" class="organization-progress-grid">
          <div class="organization-progress-item">
            <div class="organization-progress-label">
              <span>{{ $t('knowledgeBase.organization_stage_progress') }}</span>
              <span>{{ getOrganizationStageProgress(latestOrganizationJob) }}</span>
            </div>
            <el-progress :percentage="getOrganizationStageProgressPercentage(latestOrganizationJob)" :show-text="false" />
          </div>
          <div class="organization-progress-item">
            <div class="organization-progress-label">
              <span>{{ $t('knowledgeBase.organization_fragment_progress') }}</span>
              <span>{{ getOrganizationFragmentProgress(latestOrganizationJob) }}</span>
            </div>
            <el-progress :percentage="getOrganizationFragmentProgressPercentage(latestOrganizationJob)" :show-text="false" />
          </div>
        </div>

        <div class="organization-latest__counts">
          <span>{{ $t('knowledgeBase.organization_counts') }}</span>
          <span>{{ getOrganizationCountsText(latestOrganizationJob) }}</span>
        </div>

        <div v-if="latestOrganizationJob.status === 'failed'" class="organization-job-error" role="alert">
          {{ latestOrganizationJob.error || $t('knowledgeBase.organization_error') }}
        </div>
      </section>

      <el-empty
        v-if="!organizationLoading && !organizationJobs.length"
        :description="$t('knowledgeBase.organization_no_jobs')"
      />
      <details v-if="organizationDialogVisible && organizationJobs.length > 1" class="organization-history">
        <summary class="organization-history__summary">{{ $t('knowledgeBase.organization_history') }}</summary>
        <el-table
          :data="organizationJobs.slice(1)"
          :loading="organizationLoading"
          :row-key="getOrganizationJobId"
          max-height="240"
          class="organization-job-table"
        >
          <el-table-column type="expand" width="76" :label="$t('knowledgeBase.organization_details')">
            <template #default="{ row }">
              <div class="organization-history-details">
                <div>{{ $t('knowledgeBase.organization_snapshot') }}: {{ getOrganizationSnapshotId(row) }}</div>
                <div>{{ $t('knowledgeBase.organization_stage_progress') }}: {{ getOrganizationStageProgress(row) }}</div>
                <div>{{ $t('knowledgeBase.organization_fragment_progress') }}: {{ getOrganizationFragmentProgress(row) }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="$t('knowledgeBase.organization_job_id')" width="90" align="center">
            <template #default="{ row }">{{ getOrganizationJobId(row) || '-' }}</template>
          </el-table-column>
          <el-table-column :label="$t('knowledgeBase.organization_status')" width="148" align="center">
            <template #default="{ row }">
              <el-tag :type="getOrganizationStatusType(row.status)">
                {{ getOrganizationStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('knowledgeBase.organization_counts')" min-width="220">
            <template #default="{ row }">
              <div class="organization-history__result">
                <div>{{ getOrganizationCountsText(row) }}</div>
                <div v-if="row.status === 'failed'" class="organization-history__error">{{ row.error || $t('knowledgeBase.organization_error') }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="$t('knowledgeBase.actions')" width="200" align="center" fixed="right">
            <template #default="{ row }">
              <div class="action-buttons">
                <el-button
                  v-if="canCancelOrganizationJob(row)"
                  type="warning"
                  size="small"
                  :loading="organizationActionJobId === getOrganizationJobId(row)"
                  @click="handleCancelOrganizationJob(row)"
                >
                  {{ $t('knowledgeBase.organization_cancel') }}
                </el-button>
                <el-button
                  v-if="canRetryOrganizationJob(row)"
                  type="primary"
                  size="small"
                  :loading="organizationActionJobId === getOrganizationJobId(row)"
                  @click="handleRetryOrganizationJob(row)"
                >
                  {{ $t('knowledgeBase.organization_retry') }}
                </el-button>
                <span v-if="!canCancelOrganizationJob(row) && !canRetryOrganizationJob(row)">-</span>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </details>
    </el-dialog>

    <el-dialog
      :title="managedKnowledgeEditingId ? $t('knowledgeBase.managed_edit_title') : $t('knowledgeBase.managed_create_title')"
      v-model="managedKnowledgeEditDialogVisible"
      width="720px"
      class="standard-dialog"
      center
      align-center
    >
      <el-form :model="managedKnowledgeForm" :rules="managedKnowledgeRules" ref="managedKnowledgeFormRef" label-width="150px" size="default">
        <el-form-item :label="$t('knowledgeBase.managed_key')" prop="knowledge_key">
          <el-input v-model="managedKnowledgeForm.knowledge_key" maxlength="255" show-word-limit />
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.managed_content')" prop="content">
          <el-input v-model="managedKnowledgeForm.content" type="textarea" :rows="10" />
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.managed_llm_maintainable')">
          <el-switch v-model="managedKnowledgeForm.llm_maintainable" />
          <div class="help-text managed-maintenance-hint">{{ $t('knowledgeBase.managed_llm_maintainable_hint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="managedKnowledgeEditDialogVisible = false">{{ $t('knowledgeBase.cancel') }}</el-button>
        <el-button type="primary" :loading="managedKnowledgeSubmitting" @click="submitManagedKnowledge">{{ $t('knowledgeBase.confirm') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog
      :title="$t('knowledgeBase.managed_history_title', { key: managedKnowledgeHistoryKey })"
      v-model="managedKnowledgeHistoryDialogVisible"
      width="980px"
      class="standard-dialog"
      center
      align-center
    >
      <el-table :data="managedKnowledgeHistory" :loading="managedKnowledgeHistoryLoading">
        <el-table-column prop="version" :label="$t('knowledgeBase.managed_version')" width="80" align="center" />
        <el-table-column :label="$t('knowledgeBase.managed_operation')" width="100" align="center">
          <template #default="{ row }">{{ getManagedOperationLabel(row.operation) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_source')" width="120" align="center">
          <template #default="{ row }">{{ getManagedSourceLabel(row.source_type) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_modified_by')" width="110" align="center">
          <template #default="{ row }">{{ getManagedActorLabel(row.modified_by) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_snapshot')" min-width="360" show-overflow-tooltip>
          <template #default="{ row }">{{ formatManagedSnapshot(row.after_snapshot) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.managed_updated_at')" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 检索测试弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.query_test', { name: selectedKb?.name || '' })"
      v-model="queryTestDialogVisible"
      width="820px"
      class="standard-dialog"
      center
      align-center
    >
      <el-form :model="queryTestForm" :rules="queryTestRules" ref="queryTestFormRef" label-width="100px" size="default">
        <el-form-item label="TopK" prop="top_k">
          <el-input-number v-model="queryTestForm.top_k" :min="1" :max="50" :step="1" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.top_k_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.query_text')" prop="query">
          <el-input
            v-model="queryTestForm.query"
            type="textarea"
            :rows="4"
            :placeholder="$t('knowledgeBase.input_query')"
          />
          <div class="help-text mt-5">{{ $t('knowledgeBase.query_hint') }}</div>
        </el-form-item>
      </el-form>
      <div class="query-test-actions">
        <el-button type="primary" :loading="queryTesting" @click="submitQueryTest">{{ $t('knowledgeBase.start_test') }}</el-button>
      </div>
      <el-alert
        v-if="queryTested && rerankError"
        type="warning"
        :closable="false"
        show-icon
        class="mt-5"
        :title="$t('knowledgeBase.downgraded_to_hybrid')"
        :description="$t('knowledgeBase.reranker_error', { error: rerankError })"
      />
      <el-alert
        v-else-if="queryTested && retrievalMode === 'hybrid_rerank'"
        type="success"
        :closable="false"
        show-icon
        class="mt-5"
        :title="$t('knowledgeBase.hybrid_rerank_enabled')"
      />
      <el-table v-if="queryTestResults.length" :data="queryTestResults" class="query-result-table">
        <el-table-column label="#" width="60" align="center">
          <template #default="{ $index }">{{ $index + 1 }}</template>
        </el-table-column>
        <el-table-column v-if="retrievalMode === 'hybrid_rerank'" :label="$t('knowledgeBase.rerank_score')" width="110" align="center">
          <template #default="{ row }">{{ formatScore(row.metadata?.rerank_score) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.distance')" width="110" align="center">
          <template #default="{ row }">{{ formatDistance(row.distance) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.source')" width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.metadata?.filename || '-' }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.chunk_content')" min-width="360">
          <template #default="{ row }">
            <div class="query-result-content">{{ row.content }}</div>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else-if="queryTested" :description="$t('knowledgeBase.no_results')" />

    </el-dialog>
  </div>
</template>

<script setup>
import BaseDataTable from '@/components/BaseDataTable.vue'
import { useKnowledgeBaseView } from '@/composables/knowledgeBase/useKnowledgeBaseView.js'

const {
  tableData,
  loading,
  total,
  currentPage,
  pageSize,
  showDialog,
  handleRefresh,
  fetchData,
  handleSizeChange,
  canManageManagedKnowledge,
  getKnowledgeBaseOwnerLabel,
  getKnowledgeBaseProfileLabel,
  getEmbeddingModelName,
  getTargetEmbeddingModelName,
  getMigrationStatusLabel,
  getCleanupStatusLabel,
  formatTime,
  canManageKnowledgeBaseDocuments,
  showDocumentDialog,
  showManagedKnowledgeDialog,
  showQueryTestDialog,
  handleKnowledgeBaseMoreAction,
  dialogVisible,
  isEditing,
  isSuperuser,
  form,
  ownerUsers,
  ownerUsersLoading,
  ownerUsersError,
  fetchOwnerUsers,
  rules,
  formRef,
  embeddingModelOptions,
  submitting,
  submitForm,
  migrationDialogVisible,
  selectedKb,
  stopMigrationPolling,
  managedMigrationTerminalHintKey,
  migrationTargetKey,
  canStartKnowledgeBaseMigration,
  migrationProgress,
  migrationSubmitting,
  submitEmbeddingMigration,
  importDialogVisible,
  importForm,
  importRules,
  importFormRef,
  uploadFileList,
  handleFileChange,
  handleFileRemove,
  importing,
  submitImport,
  documentDialogVisible,
  documentList,
  documentLoading,
  documentPage,
  documentPageSize,
  documentTotal,
  fetchDocuments,
  handleDocumentSizeChange,
  contentDialogVisible,
  contentTitle,
  documentContent,
  showContentDialog,
  handleDeleteDocument,
  managedKnowledgeDialogVisible,
  stopManagedKnowledgePolling,
  managedKnowledgeQuery,
  handleManagedKnowledgeSearch,
  showManagedKnowledgeCreateDialog,
  showOrganizationDialog,
  managedKnowledgeItems,
  managedKnowledgeLoading,
  getManagedSourceLabel,
  formatManagedSourceReference,
  getManagedActorLabel,
  getManagedKnowledgeStatusLabel,
  showManagedKnowledgeEditDialog,
  handleManagedKnowledgeMoreAction,
  managedKnowledgePage,
  managedKnowledgePageSize,
  managedKnowledgeTotal,
  handleManagedKnowledgePageChange,
  handleManagedKnowledgeSizeChange,
  organizationDialogVisible,
  stopOrganizationPolling,
  organizationSubmitting,
  submitKnowledgeOrganization,
  latestOrganizationJob,
  getOrganizationJobId,
  getOrganizationStatusType,
  getOrganizationStatusLabel,
  getOrganizationSnapshotId,
  getOrganizationStageProgress,
  getOrganizationStageProgressPercentage,
  getOrganizationFragmentProgress,
  getOrganizationFragmentProgressPercentage,
  getOrganizationCountsText,
  organizationLoading,
  organizationJobs,
  canCancelOrganizationJob,
  organizationActionJobId,
  handleCancelOrganizationJob,
  canRetryOrganizationJob,
  handleRetryOrganizationJob,
  managedKnowledgeEditDialogVisible,
  managedKnowledgeForm,
  managedKnowledgeRules,
  managedKnowledgeFormRef,
  managedKnowledgeEditingId,
  managedKnowledgeSubmitting,
  submitManagedKnowledge,
  managedKnowledgeHistoryDialogVisible,
  managedKnowledgeHistoryKey,
  managedKnowledgeHistory,
  managedKnowledgeHistoryLoading,
  getManagedOperationLabel,
  formatManagedSnapshot,
  queryTestDialogVisible,
  queryTestForm,
  queryTestRules,
  queryTestFormRef,
  queryTesting,
  submitQueryTest,
  queryTested,
  rerankError,
  retrievalMode,
  queryTestResults,
  formatScore,
  formatDistance
} = useKnowledgeBaseView()
</script>

<style lang="scss">
@import "@/assets/css/KnowledgeBase.scss";
</style>
