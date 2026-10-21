<template>
  <div class="view-container">
    <el-alert
      v-if="repairRequiredCount > 0"
      type="warning"
      show-icon
      :closable="false"
      class="mb-15"
      :title="$t('knowledgeBase.repair_warning_title', { count: repairRequiredCount })"
      :description="$t('knowledgeBase.repair_warning_description')"
    />
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
      <template #actions>
        <el-button type="primary" @click="showDialog">
          {{ $t('knowledgeBase.create_kb') }}
        </el-button>
        <el-button type="warning" :disabled="repairSubmitting || migrationSubmitting" @click="showRepairDialog">
          {{ $t('knowledgeBase.repair') }}
        </el-button>
        <el-button @click="handleRefresh">
          {{ $t('knowledgeBase.refresh') }}
        </el-button>
      </template>
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
      <el-table-column :resizable="false" :label="$t('knowledgeBase.index_status')" width="110" align="center">
        <template #default="{ row }">
          <el-tag :type="row.index_status === 'failed' ? 'danger' : row.index_status === 'ready' ? 'success' : 'warning'">{{ $t(`knowledgeBase.index_status_${row.index_status || 'pending'}`) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="created_at" :label="$t('knowledgeBase.created_at')" width="180" sortable>
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>

      <el-table-column :resizable="false" :label="$t('knowledgeBase.actions')" width="300" align="center" fixed="right">
        <template #default="{ row }">
          <div class="action-buttons knowledge-base-action-buttons">
            <el-button type="success" size="small" @click="openEntries(row)">{{ $t('knowledgeBase.entries') }}</el-button>
            <el-button type="warning" size="small" @click="showQueryTestDialog(row)">{{ $t('knowledgeBase.test') }}</el-button>
            <el-dropdown trigger="click" popper-class="knowledge-base-more-dropdown" @command="handleKnowledgeBaseMoreAction($event, row)">
              <el-button size="small">{{ $t('knowledgeBase.more') }}</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="embedding_status">{{ $t('knowledgeBase.embedding_status') }}</el-dropdown-item>
                  <el-dropdown-item command="repair" :disabled="repairSubmitting || migrationSubmitting || !canRepairKnowledgeBase(row)">
                    {{ $t('knowledgeBase.repair') }}
                  </el-dropdown-item>
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

    <KnowledgeBaseRepairDialog
      :visible="repairDialogVisible"
      :is-superuser="isSuperuser"
      v-model:owner-uid="repairOwnerUid"
      :owners="repairOwners"
      :loading="repairOwnersLoading"
      :error="repairOwnersError"
      :submitting="repairSubmitting"
      @close="closeRepairDialog"
      @submit="submitUserRepair"
      @refresh="fetchRepairOwners()"
    />

    <el-dialog
      :title="$t('knowledgeBase.embedding_migration_title', { name: selectedKb?.name || '' })"
      v-model="migrationDialogVisible"
      width="680px"
      class="standard-dialog"
      center
      align-center
      @closed="closeMigrationDialog"
    >
      <el-alert
        v-if="migrationStateError"
        type="error"
        :title="migrationStateError"
        :closable="false"
        class="mb-15"
      />
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
        <el-form-item :label="$t('knowledgeBase.index_status')">
          <el-tag :type="selectedKb?.index_status === 'failed' ? 'danger' : selectedKb?.index_status === 'ready' ? 'success' : 'warning'">{{ $t(`knowledgeBase.index_status_${selectedKb?.index_status || 'pending'}`) }}</el-tag>
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
            :disabled="migrationStateLoading || !!migrationStateError || repairSubmitting || migrationSubmitting || !canStartKnowledgeBaseMigration(selectedKb)"
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
        <el-button @click="closeMigrationDialog" size="default">{{ $t('knowledgeBase.close') }}</el-button>
        <el-button
          v-if="selectedKb?.knowledge_base_type === 'user'"
          type="primary"
          :loading="migrationSubmitting"
          :disabled="migrationStateLoading || !!migrationStateError || repairSubmitting || migrationSubmitting || !migrationTargetKey || !canStartKnowledgeBaseMigration(selectedKb)"
          @click="submitEmbeddingMigration"
        >
          {{ $t('knowledgeBase.start_migration') }}
        </el-button>
      </template>
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
import KnowledgeBaseRepairDialog from '@/components/KnowledgeBaseRepairDialog.vue'
import { useKnowledgeBaseView } from '@/composables/knowledgeBase/useKnowledgeBaseView.js'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const routePage = typeof route.query.page === 'string' ? Number(route.query.page) : NaN
const routeSize = typeof route.query.size === 'string' ? Number(route.query.size) : NaN

const {
  tableData,
  loading,
  total,
  repairRequiredCount,
  currentPage,
  pageSize,
  showDialog,
  handleRefresh,
  fetchData,
  handleSizeChange,
  canManageManagedKnowledge,
  repairDialogVisible,
  repairOwnerUid,
  repairOwners,
  repairOwnersLoading,
  repairOwnersError,
  repairSubmitting,
  showRepairDialog,
  fetchRepairOwners,
  closeRepairDialog,
  submitUserRepair,
  canRepairKnowledgeBase,
  getKnowledgeBaseOwnerLabel,
  getKnowledgeBaseProfileLabel,
  getEmbeddingModelName,
  getTargetEmbeddingModelName,
  getMigrationStatusLabel,
  getCleanupStatusLabel,
  formatTime,
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
  closeMigrationDialog,
  migrationStateLoading,
  migrationStateError,
  managedMigrationTerminalHintKey,
  migrationTargetKey,
  canStartKnowledgeBaseMigration,
  migrationProgress,
  migrationSubmitting,
  submitEmbeddingMigration,
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

currentPage.value = Number.isSafeInteger(routePage) && routePage > 0 ? routePage : 1
pageSize.value = [10, 20, 50, 100].includes(routeSize) ? routeSize : 20

const openEntries = (row) => router.push({
  path: `/knowledge-base/${row.id}`,
  query: { page: currentPage.value, size: pageSize.value }
})
</script>

<style lang="scss">
@import "@/assets/css/KnowledgeBase.scss";
</style>
