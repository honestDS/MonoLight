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
      <el-table-column :resizable="false" :label="$t('knowledgeBase.knowledge_base_type')" align="center">
        <template #default="{ row }">
          <el-tag :type="canManageManagedKnowledge(row) ? 'warning' : 'info'">
            {{ canManageManagedKnowledge(row) ? $t('knowledgeBase.type_managed') : $t('knowledgeBase.type_user') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('knowledgeBase.owner_profiles')" min-width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ getKnowledgeBaseProfileLabel(row) }}</template>
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
                  <el-dropdown-item command="delete" divided class="danger-dropdown-item">{{ $t('knowledgeBase.delete') }}</el-dropdown-item>
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
        <el-button type="primary" :loading="submitting" @click="submitForm" size="default">{{ $t('knowledgeBase.confirm') }}</el-button>
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
          <div style="width: 100%">
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
        <span v-if="managedKnowledgeSelectedIds.length" class="help-text">
          {{ $t('knowledgeBase.organization_selected_count', { count: managedKnowledgeSelectedIds.length }) }}
        </span>
      </div>
      <el-table
        ref="managedKnowledgeTableRef"
        :data="managedKnowledgeItems"
        :loading="managedKnowledgeLoading"
        row-key="id"
        class="managed-knowledge-table"
        @selection-change="handleManagedKnowledgeSelectionChange"
      >
        <el-table-column type="selection" width="55" :reserve-selection="true" :selectable="isManagedKnowledgeOrganizable" />
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
      width="1180px"
      class="standard-dialog organization-dialog"
      center
      align-center
      @closed="stopOrganizationPolling"
    >
      <el-alert
        v-if="selectedKb?.organization_error"
        :title="$t('knowledgeBase.organization_error')"
        :description="selectedKb.organization_error"
        type="error"
        :closable="false"
        class="mb-15"
      />

      <div class="managed-knowledge-toolbar">
        <span class="help-text">{{ $t('knowledgeBase.organization_select_hint') }}</span>
        <div>
          <el-button
            type="primary"
            :loading="organizationSubmitting"
            @click="submitKnowledgeOrganization(false)"
          >
            {{ $t('knowledgeBase.organization_start_full') }}
          </el-button>
          <el-button
            type="success"
            :loading="organizationSubmitting"
            :disabled="!managedKnowledgeSelectedIds.length"
            @click="submitKnowledgeOrganization(true)"
          >
            {{ $t('knowledgeBase.organization_start_selected') }}
          </el-button>
        </div>
      </div>

      <el-descriptions
        v-if="latestOrganizationJob"
        :title="$t('knowledgeBase.organization_jobs')"
        :column="3"
        border
        class="mb-15"
      >
        <el-descriptions-item :label="$t('knowledgeBase.organization_job_id')">
          {{ getOrganizationJobId(latestOrganizationJob) || '-' }}
        </el-descriptions-item>
        <el-descriptions-item :label="$t('knowledgeBase.organization_status')">
          <el-tag :type="getOrganizationStatusType(latestOrganizationJob.status)">
            {{ getOrganizationStatusLabel(latestOrganizationJob.status) }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item :label="$t('knowledgeBase.organization_snapshot')">
          {{ getOrganizationSnapshotId(latestOrganizationJob) }}
        </el-descriptions-item>
        <el-descriptions-item :label="$t('knowledgeBase.organization_stage_progress')">
          <div>{{ getOrganizationStageProgress(latestOrganizationJob) }}</div>
          <el-progress :percentage="getOrganizationStageProgressPercentage(latestOrganizationJob)" :show-text="false" />
        </el-descriptions-item>
        <el-descriptions-item :label="$t('knowledgeBase.organization_fragment_progress')">
          <div>{{ getOrganizationFragmentProgress(latestOrganizationJob) }}</div>
          <el-progress :percentage="getOrganizationFragmentProgressPercentage(latestOrganizationJob)" :show-text="false" />
        </el-descriptions-item>
        <el-descriptions-item :label="$t('knowledgeBase.organization_counts')">
          {{ getOrganizationCountsText(latestOrganizationJob) }}
        </el-descriptions-item>
        <el-descriptions-item v-if="latestOrganizationJob.error" :label="$t('knowledgeBase.organization_error')" :span="3">
          <el-alert type="error" :closable="false" :title="latestOrganizationJob.error" />
        </el-descriptions-item>
      </el-descriptions>

      <el-empty
        v-if="!organizationLoading && !organizationJobs.length"
        :description="$t('knowledgeBase.organization_no_jobs')"
      />
      <el-table v-else :data="organizationJobs" :loading="organizationLoading" class="organization-job-table">
        <el-table-column :label="$t('knowledgeBase.organization_job_id')" width="90" align="center">
          <template #default="{ row }">{{ getOrganizationJobId(row) || '-' }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.organization_status')" width="120" align="center">
          <template #default="{ row }">
            <el-tag :type="getOrganizationStatusType(row.status)">
              {{ getOrganizationStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.organization_snapshot')" width="120" align="center">
          <template #default="{ row }">{{ getOrganizationSnapshotId(row) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.organization_stage_progress')" min-width="170">
          <template #default="{ row }">
            <div>{{ getOrganizationStageProgress(row) }}</div>
            <el-progress :percentage="getOrganizationStageProgressPercentage(row)" :show-text="false" />
          </template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.organization_fragment_progress')" min-width="170">
          <template #default="{ row }">
            <div>{{ getOrganizationFragmentProgress(row) }}</div>
            <el-progress :percentage="getOrganizationFragmentProgressPercentage(row)" :show-text="false" />
          </template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.organization_counts')" min-width="150">
          <template #default="{ row }">{{ getOrganizationCountsText(row) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.organization_error')" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.error || '-' }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.actions')" width="220" align="center" fixed="right">
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
import { ref, onMounted, onBeforeUnmount, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import BaseDataTable from '@/components/BaseDataTable.vue'
import { knowledgeBaseApi } from '@/api'
import { formatTime } from '@/utils'
import {
  canStartKnowledgeBaseMigration,
  getKnowledgeBaseMigrationProgress,
  getManagedKnowledgeBaseMigrationTerminalHintKey,
  isKnowledgeBaseMigrationActive
} from '@/utils/knowledgeBaseMigration'
import {
  canManageKnowledgeBaseDocuments,
  canManageManagedKnowledge,
  createManagedKnowledgeDedupeKey,
  getManagedKnowledgeMutationFeedback,
  getKnowledgeBaseProfileIds,
  getKnowledgeOrganizationPublishedSuccessCount,
  normalizeKnowledgeBase
} from '@/utils/knowledgeBaseManagement'
import { createAbortableTaskManager, createLatestRequestTracker } from '@/utils/requestTaskManager'
import { useDeleteConfirm } from '@/composables/useDeleteConfirm'

const { t } = useI18n()

const tableData = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)
const submitting = ref(false)
const isEditing = ref(false)
const editingId = ref(null)
const embeddingModels = ref([])
const formRef = ref(null)
const importDialogVisible = ref(false)
const importing = ref(false)
const importFormRef = ref(null)
const uploadFileList = ref([])
const selectedKb = ref(null)
const documentDialogVisible = ref(false)
const documentLoading = ref(false)
const documentList = ref([])
const documentTotal = ref(0)
const documentPage = ref(1)
const documentPageSize = ref(10)
const contentDialogVisible = ref(false)
const documentContent = ref('')
const contentTitle = ref('')
const queryTestDialogVisible = ref(false)
const queryTesting = ref(false)
const queryTested = ref(false)
const queryTestFormRef = ref(null)
const queryTestResults = ref([])
const retrievalMode = ref(null)
const rerankError = ref(null)
const migrationDialogVisible = ref(false)
const migrationTargetKey = ref('')
const migrationSubmitting = ref(false)
let migrationPollTimer = null
const managedKnowledgeDialogVisible = ref(false)
const managedKnowledgeLoading = ref(false)
const managedKnowledgeItems = ref([])
const managedKnowledgeTotal = ref(0)
const managedKnowledgePage = ref(1)
const managedKnowledgePageSize = ref(10)
const managedKnowledgeQuery = ref('')
const managedKnowledgeTableRef = ref(null)
const managedKnowledgeSelectedIds = ref([])
const managedKnowledgeEditDialogVisible = ref(false)
const managedKnowledgeEditingId = ref(null)
const managedKnowledgeFormRef = ref(null)
const managedKnowledgeSubmitting = ref(false)
const managedKnowledgeHistoryDialogVisible = ref(false)
const managedKnowledgeHistoryLoading = ref(false)
const managedKnowledgeHistory = ref([])
const managedKnowledgeHistoryKey = ref('')
let managedKnowledgePollTimer = null
const managedKnowledgeTaskManager = createAbortableTaskManager()
const managedKnowledgeRequestTracker = createLatestRequestTracker()
const organizationDialogVisible = ref(false)
const organizationLoading = ref(false)
const organizationSubmitting = ref(false)
const organizationActionJobId = ref(null)
const organizationJobs = ref([])
const organizationTotal = ref(0)
const organizationJobPageSize = 20
let organizationPollTimer = null
let organizationPollingSession = 0
const organizationTaskManager = createAbortableTaskManager()
const organizationRequestTracker = createLatestRequestTracker()


const form = reactive({
  name: '',
  description: '',
  embedding_model_key: ''
})

const rules = computed(() => ({
  name: [{ required: true, message: t('knowledgeBase.input_kb_name'), trigger: 'blur' }],
  embedding_model_key: [{ required: !isEditing.value, message: t('knowledgeBase.select_embedding_model_err'), trigger: 'change' }]
}))

const embeddingModelOptions = computed(() => embeddingModels.value.map(item => ({
  ...item,
  key: `${item.channel_id}::${item.model_id}`,
  label: `${item.channel_name} / ${item.model_id}${item.embedding_dimensions ? ` (${item.embedding_dimensions})` : ''}`
})))

const migrationProgress = computed(() => getKnowledgeBaseMigrationProgress(selectedKb.value))
const managedMigrationTerminalHintKey = computed(() => getManagedKnowledgeBaseMigrationTerminalHintKey(selectedKb.value))
const latestOrganizationJob = computed(() => organizationJobs.value[0] || null)

const importForm = reactive({
  file: null,
  chunk_size: 1000,
  chunk_overlap: 100,
  batch_size: 16
})

const importRules = computed(() => ({
  file: [{ required: true, message: t('knowledgeBase.select_doc_err'), trigger: 'change' }],
  chunk_size: [{ required: true, message: t('knowledgeBase.set_chunk_size'), trigger: 'blur' }],
  chunk_overlap: [{ required: true, message: t('knowledgeBase.set_chunk_overlap'), trigger: 'blur' }],
  batch_size: [{ required: true, message: t('knowledgeBase.set_batch_size'), trigger: 'blur' }]
}))

const managedKnowledgeForm = reactive({
  knowledge_key: '',
  content: '',
  expected_version: null,
  llm_maintainable: false
})

const managedKnowledgeRules = computed(() => ({
  knowledge_key: [{ required: true, message: t('knowledgeBase.managed_key_required'), trigger: 'blur' }],
  content: [{ required: true, message: t('knowledgeBase.managed_content_required'), trigger: 'blur' }]
}))

const queryTestForm = reactive({
  query: '',
  top_k: 5
})

const queryTestRules = computed(() => ({
  query: [{ required: true, message: t('knowledgeBase.input_query_err'), trigger: 'blur' }],
  top_k: [{ required: true, message: t('knowledgeBase.set_top_k'), trigger: 'blur' }]
}))

const fetchData = async () => {
  loading.value = true
  try {
    const res = await knowledgeBaseApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    const { items, total: totalCount, embedding_models: embeddingModelItems } = res.data.data
    tableData.value = (items || []).map(normalizeKnowledgeBase)
    total.value = totalCount || 0
    embeddingModels.value = embeddingModelItems || []
  } catch (error) {
    ElMessage.error(t('knowledgeBase.fetch_list_failed') + error.message)
  } finally {
    loading.value = false
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  fetchData()
}

const handleSizeChange = () => {
  currentPage.value = 1
  fetchData()
}

const getKnowledgeBaseProfileLabel = (row) => {
  const profileIds = getKnowledgeBaseProfileIds(row)
  return profileIds.length ? profileIds.map(id => `#${id}`).join(', ') : '-'
}

const getEmbeddingModelName = (row) => {
  if (!row) return '-'
  const channelId = row.active_embedding_channel_id || row.embedding_channel_id
  const modelId = row.active_embedding_model_id || row.embedding_model_id
  const option = embeddingModelOptions.value.find(item => item.channel_id === channelId && item.model_id === modelId)
  return option?.label || modelId || '-'
}

const getTargetEmbeddingModelName = (row) => {
  if (!row?.target_embedding_channel_id || !row?.target_embedding_model_id) return '-'
  const option = embeddingModelOptions.value.find(
    item => item.channel_id === row.target_embedding_channel_id && item.model_id === row.target_embedding_model_id
  )
  const name = option ? `${option.channel_name} / ${option.model_id}` : row.target_embedding_model_id
  return row.target_embedding_dimensions ? `${name} (${row.target_embedding_dimensions})` : name
}

const getMigrationStatusLabel = (status) => {
  const key = status || 'idle'
  return t(`knowledgeBase.migration_status_${key}`)
}

const getCleanupStatusLabel = (status) => {
  const key = status || 'none'
  return t(`knowledgeBase.cleanup_status_${key}`)
}

const stopMigrationPolling = () => {
  if (migrationPollTimer) {
    clearTimeout(migrationPollTimer)
    migrationPollTimer = null
  }
}

const shouldPollMigration = () => {
  if (!selectedKb.value) return false
  return (
    isKnowledgeBaseMigrationActive(selectedKb.value.migration_status) ||
    ['pending', 'running'].includes(selectedKb.value.old_collection_cleanup_status)
  )
}

const refreshMigrationState = async () => {
  if (!selectedKb.value) return
  const selectedId = selectedKb.value.id
  const res = await knowledgeBaseApi.list({
    page: currentPage.value,
    size: pageSize.value
  })
  const { items, total: totalCount, embedding_models: embeddingModelItems } = res.data.data
  tableData.value = (items || []).map(normalizeKnowledgeBase)
  total.value = totalCount || 0
  embeddingModels.value = embeddingModelItems || []
  const refreshed = tableData.value.find(item => item.id === selectedId)
  if (refreshed) selectedKb.value = refreshed
}

const scheduleMigrationPolling = () => {
  stopMigrationPolling()
  if (!migrationDialogVisible.value || !shouldPollMigration()) return
  migrationPollTimer = setTimeout(async () => {
    try {
      await refreshMigrationState()
    } catch (error) {
      ElMessage.error(t('knowledgeBase.fetch_migration_status_failed') + error.message)
    } finally {
      scheduleMigrationPolling()
    }
  }, 2000)
}

const showMigrationDialog = (row) => {
  selectedKb.value = row
  migrationTargetKey.value = row.target_embedding_channel_id && row.target_embedding_model_id
    ? `${row.target_embedding_channel_id}::${row.target_embedding_model_id}`
    : ''
  migrationDialogVisible.value = true
  scheduleMigrationPolling()
}

const submitEmbeddingMigration = async () => {
  if (!selectedKb.value || !canStartKnowledgeBaseMigration(selectedKb.value)) return
  const target = embeddingModelOptions.value.find(item => item.key === migrationTargetKey.value)
  if (!target) {
    ElMessage.error(t('knowledgeBase.select_embedding_model_err'))
    return
  }
  try {
    await ElMessageBox.confirm(
      t('knowledgeBase.embedding_migration_confirm', {
        name: selectedKb.value.name,
        target: target.label
      }),
      t('knowledgeBase.embedding_migration_confirm_title'),
      {
        confirmButtonText: t('knowledgeBase.start_migration'),
        cancelButtonText: t('knowledgeBase.cancel'),
        type: 'warning'
      }
    )
  } catch (action) {
    if (action === 'cancel' || action === 'close') return
    throw action
  }

  migrationSubmitting.value = true
  try {
    const res = await knowledgeBaseApi.migrateEmbedding(selectedKb.value.id, {
      embedding_channel_id: target.channel_id,
      embedding_model_id: target.model_id
    })
    const refreshed = normalizeKnowledgeBase(res.data.data)
    selectedKb.value = refreshed
    const index = tableData.value.findIndex(item => item.id === refreshed.id)
    if (index >= 0) tableData.value.splice(index, 1, refreshed)
    ElMessage.success(t('knowledgeBase.embedding_migration_submitted'))
    scheduleMigrationPolling()
  } catch (error) {
    ElMessage.error(t('knowledgeBase.embedding_migration_failed') + error.message)
  } finally {
    migrationSubmitting.value = false
  }
}

const handleKnowledgeBaseMoreAction = (command, row) => {
  if (command === 'import_document') {
    showImportDialog(row)
    return
  }
  if (command === 'embedding_status') {
    showMigrationDialog(row)
    return
  }
  if (command === 'edit') {
    showEditDialog(row)
    return
  }
  if (command === 'delete') handleDelete(row)
}

const showDialog = () => {
  resetFormFields()
  isEditing.value = false
  editingId.value = null
  dialogVisible.value = true
}

const showEditDialog = (row) => {
  resetFormFields()
  isEditing.value = true
  editingId.value = row.id
  form.name = row.name
  form.description = row.description || ''
  form.embedding_model_key = `${row.embedding_channel_id}::${row.embedding_model_id}`
  dialogVisible.value = true
}

const resetFormFields = () => {
  if (formRef.value) formRef.value.resetFields()
  form.name = ''
  form.description = ''
  form.embedding_model_key = ''
}

const resetManagedKnowledgeForm = () => {
  if (managedKnowledgeFormRef.value) managedKnowledgeFormRef.value.clearValidate()
  managedKnowledgeForm.knowledge_key = ''
  managedKnowledgeForm.content = ''
  managedKnowledgeForm.expected_version = null
  managedKnowledgeForm.llm_maintainable = false
}

const clearManagedKnowledgeSelection = () => {
  managedKnowledgeSelectedIds.value = []
  managedKnowledgeTableRef.value?.clearSelection()
}

const isManagedKnowledgeOrganizable = (item) => Boolean(
  item?.llm_maintainable === true
  && item?.is_recallable === true
  && !item?.pending_job_id
  && item?.indexed_version === item?.version
)

const handleManagedKnowledgeSelectionChange = (selection) => {
  managedKnowledgeSelectedIds.value = selection
    .filter(isManagedKnowledgeOrganizable)
    .map(item => item.id)
    .filter(id => id !== null && id !== undefined)
}

const managedKnowledgeListTaskKey = 'managed-knowledge-list'

const fetchManagedKnowledgeItems = async (notifyError = true, replaceCurrent = false) => {
  if (!selectedKb.value || !canManageManagedKnowledge(selectedKb.value)) return
  const selectedId = selectedKb.value.id
  if (replaceCurrent) {
    managedKnowledgeTaskManager.cancel(managedKnowledgeListTaskKey)
    clearManagedKnowledgeSelection()
  }
  const token = managedKnowledgeTaskManager.begin(managedKnowledgeListTaskKey)
  if (!token) return
  const requestSeq = managedKnowledgeRequestTracker.begin()
  if (managedKnowledgeTaskManager.isCurrent(token)) managedKnowledgeLoading.value = true
  try {
    const res = await knowledgeBaseApi.managedItems(selectedId, {
      page: managedKnowledgePage.value,
      size: managedKnowledgePageSize.value,
      query: managedKnowledgeQuery.value
    }, { signal: token.signal })
    if (
      !managedKnowledgeTaskManager.isCurrent(token)
      || !managedKnowledgeRequestTracker.isCurrent(requestSeq)
      || selectedKb.value?.id !== selectedId
    ) return
    managedKnowledgeItems.value = res.data.data.items || []
    managedKnowledgeTotal.value = res.data.data.total || 0
    const pageIds = new Set(managedKnowledgeItems.value.map(item => item.id))
    managedKnowledgeSelectedIds.value = managedKnowledgeSelectedIds.value.filter(id => pageIds.has(id))
  } catch (error) {
    if (token.signal.aborted || !managedKnowledgeTaskManager.isCurrent(token)) return
    if (notifyError && managedKnowledgeRequestTracker.isCurrent(requestSeq)) {
      ElMessage.error(t('knowledgeBase.managed_fetch_failed') + error.message)
    }
  } finally {
    if (managedKnowledgeTaskManager.isCurrent(token) && managedKnowledgeRequestTracker.isCurrent(requestSeq)) {
      managedKnowledgeLoading.value = false
    }
    managedKnowledgeTaskManager.finish(token)
  }
}

const stopManagedKnowledgePolling = () => {
  if (managedKnowledgePollTimer) {
    clearTimeout(managedKnowledgePollTimer)
    managedKnowledgePollTimer = null
  }
  managedKnowledgeTaskManager.invalidate()
  managedKnowledgeRequestTracker.invalidate()
  managedKnowledgeLoading.value = false
}

const scheduleManagedKnowledgePolling = () => {
  if (managedKnowledgePollTimer) {
    clearTimeout(managedKnowledgePollTimer)
    managedKnowledgePollTimer = null
  }
  if (!managedKnowledgeDialogVisible.value || !managedKnowledgeItems.value.some(item => item.pending_job_id)) return
  managedKnowledgePollTimer = setTimeout(async () => {
    try {
      await fetchManagedKnowledgeItems(false)
    } finally {
      scheduleManagedKnowledgePolling()
    }
  }, 2000)
}

const organizationTerminalStatuses = ['succeeded', 'failed', 'cancelled']

const getOrganizationJobId = (job) => job?.job_id ?? job?.id ?? null

const getOrganizationStatusLabel = (status) => {
  const key = ['pending', 'running', 'retry', 'succeeded', 'failed', 'cancelled'].includes(status) ? status : 'pending'
  return t(`knowledgeBase.organization_status_${key}`)
}

const getOrganizationStatusType = (status) => ({
  pending: 'info',
  running: 'warning',
  retry: 'warning',
  succeeded: 'success',
  failed: 'danger',
  cancelled: 'info'
}[status] || 'info')

const getOrganizationNumber = (value) => {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const number = Number(value)
  if (!Number.isFinite(number) || number < 0) return null
  return Math.trunc(number)
}

const getOrganizationNumberFrom = (source, keys) => {
  if (!source || typeof source !== 'object') return null
  for (const key of keys) {
    const value = getOrganizationNumber(source[key])
    if (value !== null) return value
  }
  return null
}

const getOrganizationProgress = (job) => (
  job?.organization_progress && typeof job.organization_progress === 'object'
    ? job.organization_progress
    : {}
)

const getOrganizationResult = (job) => (
  job?.result && typeof job.result === 'object' && !Array.isArray(job.result)
    ? job.result
    : {}
)

const getOrganizationProgressValue = (job, keys) => getOrganizationNumberFrom(getOrganizationProgress(job), keys) ?? 0

const getOrganizationStageProgress = (job) => {
  const completed = getOrganizationProgressValue(job, ['completed_stage_count'])
  const total = getOrganizationProgressValue(job, ['stage_count'])
  return `${completed} / ${total}`
}

const getOrganizationFragmentProgress = (job) => {
  const progress = getOrganizationProgress(job)
  const completed = getOrganizationNumberFrom(progress, ['completed_fragment_count', 'succeeded_fragment_count']) ?? 0
  const total = getOrganizationNumberFrom(progress, ['expected_fragment_count']) ?? 0
  return `${completed} / ${total}`
}

const getOrganizationProgressPercentage = (completed, total) => {
  if (!total) return 0
  return Math.min(100, Math.max(0, Math.round((completed / total) * 100)))
}

const getOrganizationStageProgressPercentage = (job) => getOrganizationProgressPercentage(
  getOrganizationProgressValue(job, ['completed_stage_count']),
  getOrganizationProgressValue(job, ['stage_count'])
)

const getOrganizationFragmentProgressPercentage = (job) => getOrganizationProgressPercentage(
  getOrganizationNumberFrom(getOrganizationProgress(job), ['completed_fragment_count', 'succeeded_fragment_count']) ?? 0,
  getOrganizationProgressValue(job, ['expected_fragment_count'])
)

const getOrganizationSnapshotId = (job) => {
  const result = getOrganizationResult(job)
  const payload = job?.payload && typeof job.payload === 'object' ? job.payload : {}
  const snapshotId = [job?.snapshot_id, result.snapshot_id, payload.snapshot_id]
    .map(getOrganizationNumber)
    .find(value => value !== null && value > 0)
  return snapshotId ?? t('knowledgeBase.organization_no_snapshot')
}

const getOrganizationFailureCount = (job) => {
  const result = getOrganizationResult(job)
  const directCount = getOrganizationNumberFrom(result, ['failure_count', 'failed_count'])
  if (directCount !== null) return directCount

  const childFailureCount = getOrganizationNumber(job?.child_failed_count) ?? 0
  const stageFailureCount = getOrganizationProgressValue(job, ['failed_stage_count'])
    + getOrganizationProgressValue(job, ['invalidated_stage_count'])
  return Math.max(childFailureCount, stageFailureCount)
}

const getOrganizationConflictCount = (job) => getOrganizationNumberFrom(
  getOrganizationResult(job),
  ['conflict_count', 'conflicts_count']
) ?? 0

const getOrganizationCountsText = (job) => [
  `${t('knowledgeBase.organization_success_count')}: ${getKnowledgeOrganizationPublishedSuccessCount(job)}`,
  `${t('knowledgeBase.organization_failure_count')}: ${getOrganizationFailureCount(job)}`,
  `${t('knowledgeBase.organization_conflict_count')}: ${getOrganizationConflictCount(job)}`
].join(' / ')

const isOrganizationJobForSelectedKnowledgeBase = (job) => Boolean(
  selectedKb.value
  && job?.knowledge_base_id === selectedKb.value.id
)

const isOrganizationJobTerminal = (job) => {
  if (!organizationTerminalStatuses.includes(job?.status)) return false
  const childJobCount = getOrganizationNumber(job?.child_job_count) ?? 0
  const childTerminalCount = getOrganizationNumber(job?.child_terminal_count) ?? 0
  return childTerminalCount >= childJobCount
}

const canCancelOrganizationJob = (job) => Boolean(
  isOrganizationJobForSelectedKnowledgeBase(job)
  && ['pending', 'running', 'retry'].includes(job?.status)
  && !job?.cancel_requested_at
)

const canRetryOrganizationJob = (job) => Boolean(
  isOrganizationJobForSelectedKnowledgeBase(job)
  && ['failed', 'cancelled'].includes(job?.status)
)

const fetchOrganizationJobs = async (notifyError = true, session = organizationPollingSession) => {
  if (
    !organizationDialogVisible.value
    || !selectedKb.value
    || !canManageManagedKnowledge(selectedKb.value)
    || session !== organizationPollingSession
  ) return

  const selectedId = selectedKb.value.id
  const taskKey = `organization-jobs-${session}`
  const token = organizationTaskManager.begin(taskKey)
  if (!token) return
  const requestSeq = organizationRequestTracker.begin()
  if (organizationTaskManager.isCurrent(token)) organizationLoading.value = true

  try {
    const res = await knowledgeBaseApi.organizationJobs(selectedId, {
      page: 1,
      size: organizationJobPageSize
    })
    const data = res.data.data || {}
    const items = Array.isArray(data.items)
      ? data.items.filter(item => item?.knowledge_base_id === selectedId)
      : []
    if (
      !organizationTaskManager.isCurrent(token)
      || !organizationRequestTracker.isCurrent(requestSeq)
      || !organizationDialogVisible.value
      || session !== organizationPollingSession
      || selectedKb.value?.id !== selectedId
    ) return
    organizationJobs.value = items
    organizationTotal.value = getOrganizationNumber(data.total) ?? items.length
  } catch (error) {
    if (
      notifyError
      && organizationTaskManager.isCurrent(token)
      && organizationRequestTracker.isCurrent(requestSeq)
      && session === organizationPollingSession
    ) {
      ElMessage.error(t('knowledgeBase.organization_fetch_failed') + error.message)
    }
  } finally {
    if (
      organizationTaskManager.isCurrent(token)
      && organizationRequestTracker.isCurrent(requestSeq)
      && session === organizationPollingSession
    ) organizationLoading.value = false
    organizationTaskManager.finish(token)
  }
}

const beginOrganizationPollingSession = () => {
  if (organizationPollTimer) {
    clearTimeout(organizationPollTimer)
    organizationPollTimer = null
  }
  organizationPollingSession += 1
  organizationRequestTracker.invalidate()
  organizationLoading.value = false
  return organizationPollingSession
}

const stopOrganizationPolling = () => {
  beginOrganizationPollingSession()
}

const scheduleOrganizationPolling = (session = organizationPollingSession) => {
  if (organizationPollTimer) {
    clearTimeout(organizationPollTimer)
    organizationPollTimer = null
  }
  if (
    !organizationDialogVisible.value
    || session !== organizationPollingSession
    || !organizationJobs.value.some(job => !isOrganizationJobTerminal(job))
  ) return

  organizationPollTimer = setTimeout(async () => {
    organizationPollTimer = null
    try {
      await fetchOrganizationJobs(false, session)
    } finally {
      if (session === organizationPollingSession) scheduleOrganizationPolling(session)
    }
  }, 2000)
}

const showOrganizationDialog = async () => {
  if (!selectedKb.value || !canManageManagedKnowledge(selectedKb.value)) return
  stopOrganizationPolling()
  organizationJobs.value = []
  organizationTotal.value = 0
  organizationDialogVisible.value = true
  const session = organizationPollingSession
  await fetchOrganizationJobs(true, session)
  scheduleOrganizationPolling(session)
}

const refreshOrganizationData = async () => {
  const session = beginOrganizationPollingSession()
  await Promise.all([
    fetchOrganizationJobs(true, session),
    fetchManagedKnowledgeItems(true, true)
  ])
  scheduleManagedKnowledgePolling()
  if (organizationDialogVisible.value) scheduleOrganizationPolling(session)
}

const submitKnowledgeOrganization = async (selectedOnly) => {
  if (!selectedKb.value || !canManageManagedKnowledge(selectedKb.value) || organizationSubmitting.value) return
  const knowledgeIds = [...managedKnowledgeSelectedIds.value]
  if (selectedOnly && !knowledgeIds.length) {
    ElMessage.warning(t('knowledgeBase.organization_select_at_least_one'))
    return
  }

  const selectedId = selectedKb.value.id
  organizationSubmitting.value = true
  try {
    await knowledgeBaseApi.organize(selectedId, selectedOnly ? { knowledge_ids: knowledgeIds } : {})
    if (selectedKb.value?.id === selectedId) selectedKb.value.organization_error = null
    ElMessage.success(t('knowledgeBase.organization_submitted'))
    await refreshOrganizationData()
  } catch (error) {
    ElMessage.error(t('knowledgeBase.organization_submit_failed') + error.message)
  } finally {
    organizationSubmitting.value = false
  }
}

const handleCancelOrganizationJob = async (job) => {
  const selectedId = selectedKb.value?.id
  const jobId = getOrganizationJobId(job)
  if (!selectedId || !jobId || !canCancelOrganizationJob(job)) return
  try {
    await ElMessageBox.confirm(
      t('knowledgeBase.organization_cancel_confirm'),
      t('knowledgeBase.prompt'),
      {
        confirmButtonText: t('knowledgeBase.organization_cancel'),
        cancelButtonText: t('knowledgeBase.cancel'),
        type: 'warning'
      }
    )
  } catch (action) {
    if (action === 'cancel' || action === 'close') return
    throw action
  }
  if (selectedKb.value?.id !== selectedId || !canCancelOrganizationJob(job)) return

  organizationActionJobId.value = jobId
  try {
    await knowledgeBaseApi.cancelOrganizationJob(selectedId, jobId)
    ElMessage.success(t('knowledgeBase.organization_cancelled'))
    await refreshOrganizationData()
  } catch (error) {
    ElMessage.error(t('knowledgeBase.organization_cancel_failed') + error.message)
  } finally {
    organizationActionJobId.value = null
  }
}

const handleRetryOrganizationJob = async (job) => {
  const selectedId = selectedKb.value?.id
  const jobId = getOrganizationJobId(job)
  if (!selectedId || !jobId || !canRetryOrganizationJob(job)) return
  try {
    await ElMessageBox.confirm(
      t('knowledgeBase.organization_retry_confirm'),
      t('knowledgeBase.prompt'),
      {
        confirmButtonText: t('knowledgeBase.organization_retry'),
        cancelButtonText: t('knowledgeBase.cancel'),
        type: 'warning'
      }
    )
  } catch (action) {
    if (action === 'cancel' || action === 'close') return
    throw action
  }
  if (selectedKb.value?.id !== selectedId || !canRetryOrganizationJob(job)) return

  organizationActionJobId.value = jobId
  try {
    await knowledgeBaseApi.retryOrganizationJob(selectedId, jobId)
    ElMessage.success(t('knowledgeBase.organization_retried'))
    await refreshOrganizationData()
  } catch (error) {
    ElMessage.error(t('knowledgeBase.organization_retry_failed') + error.message)
  } finally {
    organizationActionJobId.value = null
  }
}

const showManagedKnowledgeDialog = async (row) => {
  stopManagedKnowledgePolling()
  selectedKb.value = row
  managedKnowledgePage.value = 1
  managedKnowledgeQuery.value = ''
  managedKnowledgeItems.value = []
  managedKnowledgeTotal.value = 0
  clearManagedKnowledgeSelection()
  managedKnowledgeDialogVisible.value = true
  await fetchManagedKnowledgeItems(true, true)
  scheduleManagedKnowledgePolling()
}

const handleManagedKnowledgeSearch = async () => {
  managedKnowledgePage.value = 1
  clearManagedKnowledgeSelection()
  await fetchManagedKnowledgeItems(true, true)
  scheduleManagedKnowledgePolling()
}

const handleManagedKnowledgeSizeChange = async () => {
  managedKnowledgePage.value = 1
  clearManagedKnowledgeSelection()
  await fetchManagedKnowledgeItems(true, true)
  scheduleManagedKnowledgePolling()
}

const handleManagedKnowledgePageChange = async () => {
  clearManagedKnowledgeSelection()
  await fetchManagedKnowledgeItems(true, true)
  scheduleManagedKnowledgePolling()
}

const showManagedKnowledgeCreateDialog = () => {
  resetManagedKnowledgeForm()
  managedKnowledgeEditingId.value = null
  managedKnowledgeEditDialogVisible.value = true
}

const showManagedKnowledgeEditDialog = async (row) => {
  if (!selectedKb.value) return
  resetManagedKnowledgeForm()
  try {
    const res = await knowledgeBaseApi.managedItem(selectedKb.value.id, row.id)
    const item = res.data.data
    managedKnowledgeEditingId.value = item.id
    managedKnowledgeForm.knowledge_key = item.knowledge_key
    managedKnowledgeForm.content = item.content
    managedKnowledgeForm.expected_version = item.version
    managedKnowledgeForm.llm_maintainable = Boolean(item.llm_maintainable)
    managedKnowledgeEditDialogVisible.value = true
  } catch (error) {
    ElMessage.error(t('knowledgeBase.managed_fetch_item_failed') + error.message)
  }
}

const showManagedKnowledgeMutationFeedback = (operation, status) => {
  const feedback = getManagedKnowledgeMutationFeedback(operation, status)
  ElMessage({ type: feedback.type, message: t(`knowledgeBase.${feedback.key}`) })
}

const submitManagedKnowledge = async () => {
  if (!managedKnowledgeFormRef.value || !selectedKb.value) return
  await managedKnowledgeFormRef.value.validate(async (valid) => {
    if (!valid) return
    managedKnowledgeSubmitting.value = true
    try {
      const payload = {
        knowledge_key: managedKnowledgeForm.knowledge_key,
        content: managedKnowledgeForm.content,
        llm_maintainable: managedKnowledgeForm.llm_maintainable
      }
      let operation = 'create'
      let res
      if (managedKnowledgeEditingId.value) {
        operation = 'update'
        res = await knowledgeBaseApi.updateManagedItem(selectedKb.value.id, managedKnowledgeEditingId.value, {
          ...payload,
          expected_version: managedKnowledgeForm.expected_version,
          dedupe_key: createManagedKnowledgeDedupeKey('update')
        })
      } else {
        res = await knowledgeBaseApi.createManagedItem(selectedKb.value.id, {
          ...payload,
          dedupe_key: createManagedKnowledgeDedupeKey('create')
        })
      }
      const status = res.data.data.status
      showManagedKnowledgeMutationFeedback(operation, status)
      if (!['existing_key', 'existing_content'].includes(status)) {
        managedKnowledgeEditDialogVisible.value = false
      }
      await fetchManagedKnowledgeItems(true, true)
      scheduleManagedKnowledgePolling()
    } catch (error) {
      ElMessage.error((managedKnowledgeEditingId.value ? t('knowledgeBase.managed_update_failed') : t('knowledgeBase.managed_create_failed')) + error.message)
    } finally {
      managedKnowledgeSubmitting.value = false
    }
  })
}

const handleDeleteManagedKnowledge = async (row) => {
  if (!selectedKb.value) return
  try {
    await ElMessageBox.confirm(
      t('knowledgeBase.managed_delete_confirm', { key: row.knowledge_key }),
      t('knowledgeBase.prompt'),
      {
        confirmButtonText: t('knowledgeBase.confirm'),
        cancelButtonText: t('knowledgeBase.cancel'),
        type: 'warning'
      }
    )
  } catch (action) {
    if (action === 'cancel' || action === 'close') return
    throw action
  }
  try {
    const res = await knowledgeBaseApi.deleteManagedItem(selectedKb.value.id, row.id, {
      expected_version: row.version,
      dedupe_key: createManagedKnowledgeDedupeKey('delete')
    })
    showManagedKnowledgeMutationFeedback('delete', res.data.data.status)
    await fetchManagedKnowledgeItems(true, true)
    scheduleManagedKnowledgePolling()
  } catch (error) {
    ElMessage.error(t('knowledgeBase.managed_delete_failed') + error.message)
  }
}

const retryManagedKnowledgePublication = async (row) => {
  if (
    !selectedKb.value
    || row.publication_job_status !== 'failed'
    || !row.publication_job_id
  ) return
  try {
    const res = await knowledgeBaseApi.retryManagedItem(selectedKb.value.id, row.id, {
      expected_version: row.version,
      failed_job_id: row.publication_job_id,
      dedupe_key: createManagedKnowledgeDedupeKey('retry')
    })
    showManagedKnowledgeMutationFeedback('retry', res.data.data.status)
    await fetchManagedKnowledgeItems(true, true)
    scheduleManagedKnowledgePolling()
  } catch (error) {
    ElMessage.error(t('knowledgeBase.managed_retry_failed') + error.message)
  }
}

const showManagedKnowledgeHistory = async (row) => {
  if (!selectedKb.value) return
  managedKnowledgeHistoryKey.value = row.knowledge_key
  managedKnowledgeHistory.value = []
  managedKnowledgeHistoryDialogVisible.value = true
  managedKnowledgeHistoryLoading.value = true
  try {
    const res = await knowledgeBaseApi.managedHistory(selectedKb.value.id, row.id)
    managedKnowledgeHistory.value = res.data.data || []
  } catch (error) {
    ElMessage.error(t('knowledgeBase.managed_history_failed') + error.message)
  } finally {
    managedKnowledgeHistoryLoading.value = false
  }
}

const handleManagedKnowledgeMoreAction = (command, row) => {
  if (command === 'history') {
    showManagedKnowledgeHistory(row)
    return
  }
  if (command === 'retry') {
    retryManagedKnowledgePublication(row)
    return
  }
  if (command === 'delete') handleDeleteManagedKnowledge(row)
}

const getManagedSourceLabel = (sourceType) => t(`knowledgeBase.managed_source_${sourceType || 'system'}`)
const getManagedActorLabel = (actor) => t(`knowledgeBase.managed_actor_${actor || 'system'}`)
const getManagedOperationLabel = (operation) => t(`knowledgeBase.managed_operation_${operation || 'update'}`)

const getManagedKnowledgeStatusLabel = (row) => {
  if (row.pending_job_id || ['pending', 'running', 'retry'].includes(row.publication_job_status)) {
    return t('knowledgeBase.managed_status_processing')
  }
  if (row.publication_job_status === 'failed') return t('knowledgeBase.managed_status_failed')
  if (row.publication_job_status === 'cancelled') return t('knowledgeBase.managed_status_cancelled')
  if (row.is_recallable && row.indexed_version === row.version) return t('knowledgeBase.managed_status_ready')
  return t('knowledgeBase.managed_status_pending')
}

const formatManagedSnapshot = (snapshot) => {
  if (!snapshot || typeof snapshot !== 'object') return '-'
  return snapshot.content || snapshot.knowledge_key || '-'
}

const formatManagedSourceReference = (sourceReference) => {
  if (!sourceReference || typeof sourceReference !== 'object') return '-'
  try {
    return JSON.stringify(sourceReference)
  } catch (_error) {
    return '-'
  }
}

const resetImportForm = () => {
  if (importFormRef.value) importFormRef.value.resetFields()
  importForm.file = null
  importForm.chunk_size = 1000
  importForm.chunk_overlap = 100
  importForm.batch_size = 16
  uploadFileList.value = []
}

const showImportDialog = (row) => {
  selectedKb.value = row
  resetImportForm()
  importDialogVisible.value = true
}

const handleFileChange = (uploadFile, uploadFiles) => {
  uploadFileList.value = uploadFiles.slice(-1)
  importForm.file = uploadFile.raw
  if (importFormRef.value) importFormRef.value.validateField('file')
}

const handleFileRemove = () => {
  uploadFileList.value = []
  importForm.file = null
}

const submitImport = async () => {
  if (!importFormRef.value || !selectedKb.value) return
  if (importForm.chunk_overlap >= importForm.chunk_size) {
    ElMessage.error(t('knowledgeBase.overlap_less_than_size'))
    return
  }
  await importFormRef.value.validate(async (valid) => {
    if (!valid) return
    importing.value = true
    try {
      const formData = new FormData()
      formData.append('file', importForm.file)
      formData.append('chunk_size', importForm.chunk_size)
      formData.append('chunk_overlap', importForm.chunk_overlap)
      formData.append('batch_size', importForm.batch_size)
      await knowledgeBaseApi.importDocument(selectedKb.value.id, formData)
      ElMessage.success(t('knowledgeBase.import_success'))
      importDialogVisible.value = false
      if (documentDialogVisible.value) fetchDocuments()
    } catch (error) {
      ElMessage.error(t('knowledgeBase.import_failed') + error.message)
    } finally {
      importing.value = false
    }
  })
}

const showDocumentDialog = (row) => {
  selectedKb.value = row
  documentPage.value = 1
  documentDialogVisible.value = true
  fetchDocuments()
}

const fetchDocuments = async () => {
  if (!selectedKb.value) return
  documentLoading.value = true
  try {
    const res = await knowledgeBaseApi.documents(selectedKb.value.id, {
      page: documentPage.value,
      size: documentPageSize.value
    })
    documentList.value = res.data.data.items || []
    documentTotal.value = res.data.data.total || 0
  } catch (error) {
    ElMessage.error(t('knowledgeBase.fetch_doc_list_failed') + error.message)
  } finally {
    documentLoading.value = false
  }
}

const handleDocumentSizeChange = () => {
  documentPage.value = 1
  fetchDocuments()
}

const showContentDialog = async (row) => {
  if (!selectedKb.value) return
  try {
    const res = await knowledgeBaseApi.document(selectedKb.value.id, row.id)
    contentTitle.value = row.filename
    documentContent.value = res.data.data.content || ''
    contentDialogVisible.value = true
  } catch (error) {
    ElMessage.error(t('knowledgeBase.fetch_doc_content_failed') + error.message)
  }
}

const deleteSelectedDocument = (documentId) => knowledgeBaseApi.deleteDocument(selectedKb.value.id, documentId)

const { handleDelete: confirmDeleteDocument } = useDeleteConfirm(deleteSelectedDocument, fetchDocuments)

const handleDeleteDocument = (row) => {
  if (!selectedKb.value) return
  confirmDeleteDocument(row.id, row.filename, {
    title: t('knowledgeBase.prompt'),
    message: t('knowledgeBase.delete_doc_confirm', { filename: row.filename }),
    dangerouslyUseHTMLString: false,
    successMessage: t('knowledgeBase.delete_doc_success'),
    errorMessage: t('knowledgeBase.delete_doc_failed')
  })
}

const showQueryTestDialog = (row) => {
  selectedKb.value = row
  queryTestForm.query = ''
  queryTestForm.top_k = 5
  queryTestResults.value = []
  queryTested.value = false
  if (queryTestFormRef.value) queryTestFormRef.value.clearValidate()
  queryTestDialogVisible.value = true
}

const submitQueryTest = async () => {
  if (!queryTestFormRef.value || !selectedKb.value) return
  await queryTestFormRef.value.validate(async (valid) => {
    if (!valid) return
    queryTesting.value = true
    queryTested.value = false
    try {
      const res = await knowledgeBaseApi.queryTest(selectedKb.value.id, {
        query: queryTestForm.query,
        top_k: queryTestForm.top_k
      })
      queryTestResults.value = res.data.data.items || []
      retrievalMode.value = res.data.data.retrieval_mode || null
      rerankError.value = res.data.data.rerank_error || null
      queryTested.value = true

    } catch (error) {
      ElMessage.error(t('knowledgeBase.query_test_failed') + error.message)
    } finally {
      queryTesting.value = false
    }
  })
}

const formatDistance = (distance) => {
  if (distance === null || distance === undefined) return '-'
  return Number(distance).toFixed(4)
}

const formatScore = (score) => {
  if (score === null || score === undefined) return '-'
  return Number(score).toFixed(4)
}


const submitForm = async () => {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (valid) {
      submitting.value = true
      try {
        if (isEditing.value) {
          await knowledgeBaseApi.update(editingId.value, {
            name: form.name,
            description: form.description
          })
          ElMessage.success(t('knowledgeBase.update_success'))
        } else {
          const [embeddingChannelId, embeddingModelId] = form.embedding_model_key.split('::')
          await knowledgeBaseApi.create({
            name: form.name,
            description: form.description,
            embedding_channel_id: Number(embeddingChannelId),
            embedding_model_id: embeddingModelId
          })
          ElMessage.success(t('knowledgeBase.create_success'))
        }
        dialogVisible.value = false
        fetchData()
      } catch (error) {
        ElMessage.error((isEditing.value ? t('knowledgeBase.update_failed') : t('knowledgeBase.create_failed')) + error.message)
      } finally {
        submitting.value = false
      }
    }
  })
}

const { handleDelete: confirmDeleteKnowledgeBase } = useDeleteConfirm(knowledgeBaseApi.delete, fetchData)

const handleDelete = (row) => {
  confirmDeleteKnowledgeBase(row.id, row.name, {
    title: t('knowledgeBase.prompt'),
    message: t('knowledgeBase.delete_kb_confirm', { name: row.name }),
    dangerouslyUseHTMLString: false,
    errorMessage: t('knowledgeBase.delete_kb_failed')
  })
}

onMounted(() => {
  fetchData()
})

onBeforeUnmount(() => {
  stopMigrationPolling()
  stopManagedKnowledgePolling()
  stopOrganizationPolling()
})
</script>

<style lang="scss">
@import "@/assets/css/KnowledgeBase.scss";
</style>
