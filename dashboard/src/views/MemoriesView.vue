<template>
  <div class="memory-view">
    <section class="settings-panel">
      <div class="section-heading">
        <div>
          <h2>{{ $t('memories.title') }}</h2>
          <p>{{ $t('memories.settings') }}</p>
        </div>
        <div class="heading-actions">
          <el-button size="small" @click="loadSettings()" :loading="settingsLoading">{{ $t('memories.refresh') }}</el-button>
          <el-button type="warning" size="small" @click="reindex" :loading="actionLoading === 'reindex'" :disabled="!configured">{{ $t('memories.reindex') }}</el-button>
          <el-button
            v-if="cleanupRetryId"
            type="danger"
            size="small"
            @click="retryCleanup(cleanupRetryId)"
            :loading="actionLoading === `cleanup-${cleanupRetryId}`">
            {{ $t('memories.cleanup_retry') }}
          </el-button>
        </div>
      </div>

      <el-alert v-if="!configured" type="info" :closable="false" show-icon :title="$t('memories.no_config')" />
      <div class="settings-grid" v-loading="settingsLoading">
        <div class="config-block">
          <strong>{{ $t('memories.active_config') }}</strong>
          <div class="config-line"><span>{{ $t('memories.channel') }}</span><b>{{ setting('active_embedding_channel_id') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.model') }}</span><b>{{ setting('active_embedding_model_id') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.dimensions') }}</span><b>{{ setting('active_embedding_dimensions') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.collection') }}</span><b class="mono">{{ setting('active_collection_name') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.revision') }}</span><b>{{ setting('active_embedding_revision') }}</b></div>
        </div>
        <div class="config-block">
          <strong>{{ $t('memories.target_config') }}</strong>
          <div class="config-line"><span>{{ $t('memories.channel') }}</span><b>{{ setting('target_embedding_channel_id') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.model') }}</span><b>{{ setting('target_embedding_model_id') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.dimensions') }}</span><b>{{ setting('target_embedding_dimensions') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.collection') }}</span><b class="mono">{{ setting('target_collection_name') }}</b></div>
          <div class="config-line"><span>{{ $t('memories.migration_status') }}</span><StatusTag :status="settings.migration_status" :active-text="statusText(settings.migration_status)" :inactive-text="statusText(settings.migration_status)" :active-type="statusType(settings.migration_status)" :inactive-type="statusType(settings.migration_status)" /></div>
        </div>
        <div class="config-block progress-block">
          <strong>{{ $t('memories.progress') }}</strong>
          <el-progress :percentage="migrationPercentage" :status="settings.migration_status === 'failed' ? 'exception' : undefined" />
          <div class="progress-counts">
            <span>{{ $t('memories.total_count') }} {{ settings.migration_total_count || 0 }}</span>
            <span>{{ $t('memories.success_count') }} {{ settings.migration_success_count || 0 }}</span>
            <span>{{ $t('memories.failure_count') }} {{ settings.migration_failure_count || 0 }}</span>
          </div>
          <div class="config-line"><span>{{ $t('memories.index_status') }}</span><StatusTag :status="settings.index_status" :active-text="statusText(settings.index_status)" :inactive-text="statusText(settings.index_status)" :active-type="statusType(settings.index_status)" :inactive-type="statusType(settings.index_status)" /></div>
          <div class="config-line"><span>{{ $t('memories.cleanup_status') }}</span><StatusTag :status="settings.old_collection_cleanup_status" :active-text="statusText(settings.old_collection_cleanup_status)" :inactive-text="statusText(settings.old_collection_cleanup_status)" :active-type="statusType(settings.old_collection_cleanup_status)" :inactive-type="statusType(settings.old_collection_cleanup_status)" /></div>
          <div class="config-line"><span>{{ $t('memories.capacity') }}</span><b>{{ settings.max_active_records || 0 }}</b></div>
        </div>
      </div>
      <el-alert v-if="settings.migration_error || settings.old_collection_cleanup_error" class="settings-error" type="warning" :closable="false" show-icon>
        <template #title>{{ settings.migration_error || settings.old_collection_cleanup_error }}</template>
      </el-alert>
    </section>

    <el-tabs v-model="activeTab" @tab-change="handleTabChange" class="memory-tabs">
      <el-tab-pane :label="$t('memories.memories')" name="memories">
        <div class="filter-bar">
          <el-input v-model="filters.keyword" :placeholder="$t('memories.keyword_placeholder')" clearable class="keyword-input" @keyup.enter="resetAndLoadMemories" />
          <el-select v-model="filters.memory_type" :placeholder="$t('memories.all_types')" clearable class="filter-input" @change="resetAndLoadMemories">
            <el-option :label="$t('memories.all_types')" value="" />
            <el-option v-for="type in memoryTypes" :key="type" :label="typeLabel(type)" :value="type" />
          </el-select>
          <el-input v-model="filters.scope" :placeholder="$t('memories.scope_placeholder')" clearable class="filter-input" @keyup.enter="resetAndLoadMemories" />
          <el-select v-model="filters.sort_by" class="sort-input" @change="loadMemories">
            <el-option :label="$t('memories.updated_at')" value="updated_at" />
            <el-option :label="$t('memories.created_at')" value="created_at" />
            <el-option :label="$t('memories.importance')" value="importance" />
            <el-option :label="$t('memories.version')" value="version" />
          </el-select>
          <el-select v-model="filters.sort_order" class="order-input" @change="loadMemories">
            <el-option :label="$t('memories.descending')" value="desc" />
            <el-option :label="$t('memories.ascending')" value="asc" />
          </el-select>
          <el-button type="primary" @click="resetAndLoadMemories">{{ $t('common.confirm') }}</el-button>
          <el-button @click="openEditor()">{{ $t('memories.create') }}</el-button>
          <el-button @click="loadMemories">{{ $t('memories.refresh') }}</el-button>
        </div>
        <el-table :data="memories" v-loading="memoriesLoading" border stripe class="memory-table">
          <el-table-column prop="id" :label="$t('memories.memory_id')" width="88" align="center" />
          <el-table-column prop="memory_key" :label="$t('memories.memory_key')" min-width="190" show-overflow-tooltip />
          <el-table-column :label="$t('memories.content_preview')" min-width="300">
            <template #default="{ row }"><div class="content-preview">{{ row.content || '-' }}</div></template>
          </el-table-column>
          <el-table-column :label="$t('memories.type')" width="120" align="center"><template #default="{ row }">{{ typeLabel(row.memory_type) }}</template></el-table-column>
          <el-table-column prop="importance" :label="$t('memories.importance')" width="90" align="center" />
          <el-table-column prop="scope" :label="$t('memories.scope')" width="130" show-overflow-tooltip />
          <el-table-column :label="$t('memories.current_status')" width="130" align="center"><template #default="{ row }"><el-tag :type="recordStatusType(row)">{{ recordStatus(row) }}</el-tag></template></el-table-column>
          <el-table-column prop="version" :label="$t('memories.version')" width="76" align="center" />
          <el-table-column :label="$t('memories.updated_at')" width="170"><template #default="{ row }">{{ formatTime(row.updated_at) }}</template></el-table-column>
          <el-table-column :label="$t('memories.actions')" width="300" fixed="right" align="center">
            <template #default="{ row }">
              <div class="action-buttons">
                <el-button size="small" type="info" @click="showDetails(row)">{{ $t('memories.view') }}</el-button>
                <el-button size="small" type="primary" @click="openEditor(row)" :disabled="Boolean(row.pending_mutation_job_id || row.deleted_at || row.is_active === false)">{{ $t('memories.edit') }}</el-button>
                <el-button size="small" @click="showHistory(row)">{{ $t('memories.history') }}</el-button>
                <el-button v-if="row.suppress_recall && !row.pending_mutation_job_id" size="small" type="warning" @click="resumeCurrent(row)">{{ $t('memories.resume_current') }}</el-button>
                <el-button size="small" type="danger" @click="deleteMemory(row)">{{ $t('memories.delete') }}</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <div class="table-footer"><span>{{ $t('common.total_items', { total: memoryTotal }) }}</span><el-pagination v-model:current-page="memoryPage" v-model:page-size="memoryPageSize" :total="memoryTotal" :page-sizes="[10, 20, 50, 100]" layout="total, sizes, prev, pager, next, jumper" @current-change="loadMemories" @size-change="resetAndLoadMemories" /></div>
      </el-tab-pane>

      <el-tab-pane :label="$t('memories.jobs')" name="jobs">
        <div class="filter-bar">
          <el-select v-model="jobFilters.status" :placeholder="$t('memories.status')" clearable class="filter-input" @change="resetAndLoadJobs"><el-option v-for="status in jobStatuses" :key="status" :label="statusText(status)" :value="status" /></el-select>
          <el-select v-model="jobFilters.operation" :placeholder="$t('memories.operation')" clearable class="operation-input" @change="resetAndLoadJobs"><el-option v-for="operation in jobOperations" :key="operation" :label="operationLabel(operation)" :value="operation" /></el-select>
          <el-input v-model="jobFilters.memory_id" :placeholder="$t('memories.memory_id')" clearable class="small-input" @keyup.enter="resetAndLoadJobs" />
          <el-button type="primary" @click="resetAndLoadJobs">{{ $t('common.confirm') }}</el-button><el-button @click="loadJobs">{{ $t('memories.refresh') }}</el-button>
        </div>
        <el-table :data="jobs" v-loading="jobsLoading" border stripe>
          <el-table-column prop="id" :label="$t('memories.job_id')" width="90" align="center" /><el-table-column prop="operation" :label="$t('memories.operation')" width="170"><template #default="{ row }">{{ operationLabel(row.operation) }}</template></el-table-column><el-table-column prop="memory_id" :label="$t('memories.memory_id')" width="100" align="center" /><el-table-column :label="$t('memories.status')" width="120" align="center"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag></template></el-table-column><el-table-column prop="attempt_count" :label="$t('memories.attempt')" width="90" align="center" /><el-table-column :label="$t('memories.error')" min-width="260" show-overflow-tooltip><template #default="{ row }">{{ row.error || row.result?.error || '-' }}</template></el-table-column><el-table-column :label="$t('memories.created_at')" width="170"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column><el-table-column :label="$t('memories.actions')" width="190" fixed="right" align="center"><template #default="{ row }"><div class="action-buttons"><el-button v-if="canRetry(row)" size="small" type="warning" @click="retryJob(row)">{{ $t('memories.retry') }}</el-button><el-button v-if="canCancel(row)" size="small" type="danger" @click="cancelJob(row)">{{ $t('memories.cancel_job') }}</el-button><el-button size="small" type="info" @click="showJob(row)">{{ $t('memories.view') }}</el-button></div></template></el-table-column>
        </el-table>
        <div class="table-footer"><span>{{ $t('common.total_items', { total: jobTotal }) }}</span><el-pagination v-model:current-page="jobPage" v-model:page-size="jobPageSize" :total="jobTotal" :page-sizes="[10, 20, 50, 100]" layout="total, sizes, prev, pager, next, jumper" @current-change="loadJobs" @size-change="resetAndLoadJobs" /></div>
      </el-tab-pane>

      <el-tab-pane :label="$t('memories.migrations')" name="migrations">
        <div class="filter-bar"><el-button @click="loadMigrations">{{ $t('memories.refresh') }}</el-button></div>
        <el-table :data="migrations" v-loading="migrationsLoading" border stripe>
          <el-table-column :label="$t('memories.migration_job')" width="110" align="center"><template #default="{ row }">{{ migrationId(row) }}</template></el-table-column><el-table-column :label="$t('memories.status')" width="140" align="center"><template #default="{ row }"><el-tag :type="statusType(row.status || row.migration_status)">{{ statusText(row.status || row.migration_status) }}</el-tag></template></el-table-column><el-table-column :label="$t('memories.target')" min-width="250"><template #default="{ row }">{{ migrationTarget(row) }}</template></el-table-column><el-table-column :label="$t('memories.progress')" min-width="180"><template #default="{ row }">{{ progressText(row) }}</template></el-table-column><el-table-column :label="$t('memories.error')" min-width="220" show-overflow-tooltip><template #default="{ row }">{{ row.error || row.migration_error || '-' }}</template></el-table-column><el-table-column :label="$t('memories.actions')" width="350" fixed="right" align="center"><template #default="{ row }"><div class="action-buttons"><el-button size="small" type="info" @click="showMigration(row)">{{ $t('memories.view') }}</el-button><el-button v-if="canRetryMigration(row)" size="small" type="warning" @click="retryMigration(row)">{{ $t('memories.migration_retry') }}</el-button><el-button v-if="canCancelMigration(row)" size="small" type="danger" @click="cancelMigration(row)">{{ $t('memories.migration_cancel') }}</el-button><el-button v-if="cleanupId(row)" size="small" type="danger" @click="retryCleanup(cleanupId(row))">{{ $t('memories.cleanup_retry') }}</el-button></div></template></el-table-column>
        </el-table>
        <div class="table-footer"><span>{{ $t('common.total_items', { total: migrationTotal }) }}</span><el-pagination v-model:current-page="migrationPage" v-model:page-size="migrationPageSize" :total="migrationTotal" :page-sizes="[10, 20, 50]" layout="total, sizes, prev, pager, next, jumper" @current-change="loadMigrations" @size-change="resetAndLoadMigrations" /></div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="editorVisible" :title="editorMode === 'create' ? $t('memories.form_create_title') : $t('memories.form_edit_title')" width="720px" class="standard-dialog" align-center>
      <el-form :model="form" label-width="120px">
        <el-form-item :label="$t('memories.memory_key')" required><el-input v-model="form.memory_key" :placeholder="$t('memories.memory_key_placeholder')" /></el-form-item>
        <el-form-item :label="$t('memories.type')" required><el-select v-model="form.memory_type" class="full-width-input"><el-option v-for="type in memoryTypes" :key="type" :label="typeLabel(type)" :value="type" /></el-select></el-form-item>
        <el-form-item :label="$t('memories.importance')" required><el-input-number v-model="form.importance" :min="0" :max="10" class="full-width-input" /><div class="help-text">{{ $t('memories.importance_hint') }}</div></el-form-item>
        <el-form-item :label="$t('memories.scope')"><el-input v-model="form.scope" :placeholder="$t('memories.scope_placeholder')" /></el-form-item>
        <el-form-item :label="$t('memories.content')" required><el-input v-model="form.content" type="textarea" :rows="9" :placeholder="$t('memories.content_placeholder')" /></el-form-item>
        <el-form-item :label="$t('memories.change_evidence')"><el-input v-model="form.change_evidence" type="textarea" :rows="3" :placeholder="$t('memories.change_evidence_placeholder')" /></el-form-item>
        <el-form-item v-if="editorMode === 'edit'" :label="$t('memories.suppress_current')"><el-checkbox v-model="form.suppress_current">{{ $t('memories.suppress_current') }}</el-checkbox><div class="help-text">{{ $t('memories.suppress_hint') }}</div></el-form-item>
      </el-form>
      <template #footer><el-button @click="editorVisible = false">{{ $t('memories.cancel') }}</el-button><el-button type="primary" :loading="submitting" @click="submitMemory">{{ $t('memories.save') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="detailsVisible" :title="$t('memories.details')" width="760px" class="standard-dialog" align-center>
      <el-descriptions v-if="selectedMemory" :column="2" border><el-descriptions-item :label="$t('memories.memory_id')">{{ selectedMemory.id }}</el-descriptions-item><el-descriptions-item :label="$t('memories.version')">{{ selectedMemory.version }}</el-descriptions-item><el-descriptions-item :label="$t('memories.memory_key')">{{ selectedMemory.memory_key }}</el-descriptions-item><el-descriptions-item :label="$t('memories.type')">{{ typeLabel(selectedMemory.memory_type) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.importance')">{{ selectedMemory.importance }}</el-descriptions-item><el-descriptions-item :label="$t('memories.scope')">{{ selectedMemory.scope || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.source')">{{ sourceLabel(selectedMemory.source) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.current_status')">{{ recordStatus(selectedMemory) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.content')" :span="2"><pre class="memory-content">{{ selectedMemory.content || '-' }}</pre></el-descriptions-item><el-descriptions-item :label="$t('memories.change_evidence')" :span="2"><pre class="memory-content">{{ selectedMemory.change_evidence || '-' }}</pre></el-descriptions-item></el-descriptions>
      <template #footer><el-button @click="detailsVisible = false">{{ $t('memories.close') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="historyVisible" :title="$t('memories.history_title', { key: selectedMemory?.memory_key || '' })" width="900px" class="standard-dialog" align-center>
      <el-table :data="history" v-loading="historyLoading" border stripe><el-table-column prop="version" :label="$t('memories.revision_version')" width="100" align="center" /><el-table-column prop="memory_type" :label="$t('memories.type')" width="120"><template #default="{ row }">{{ typeLabel(row.memory_type) }}</template></el-table-column><el-table-column prop="content" :label="$t('memories.content')" min-width="350" show-overflow-tooltip /><el-table-column prop="published_at" :label="$t('memories.published_at')" width="180"><template #default="{ row }">{{ formatTime(row.published_at || row.created_at) }}</template></el-table-column><el-table-column :label="$t('memories.actions')" width="150" align="center"><template #default="{ row }"><el-button size="small" type="warning" @click="restoreRevision(row)">{{ $t('memories.restore') }}</el-button></template></el-table-column></el-table>
      <el-empty v-if="!historyLoading && !history.length" :description="$t('memories.no_history')" />
    </el-dialog>

    <el-dialog v-model="jobVisible" :title="$t('memories.jobs')" width="720px" class="standard-dialog" align-center><el-descriptions v-if="selectedJob" :column="2" border><el-descriptions-item :label="$t('memories.job_id')">{{ selectedJob.id }}</el-descriptions-item><el-descriptions-item :label="$t('memories.operation')">{{ operationLabel(selectedJob.operation) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.status')">{{ statusText(selectedJob.status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.memory_id')">{{ selectedJob.memory_id || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.attempt')">{{ selectedJob.attempt_count }} / {{ selectedJob.max_attempts }}</el-descriptions-item><el-descriptions-item :label="$t('memories.error')">{{ selectedJob.error || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.content')" :span="2"><pre class="memory-content">{{ JSON.stringify(selectedJob.result || selectedJob.payload || {}, null, 2) }}</pre></el-descriptions-item></el-descriptions><template #footer><el-button @click="jobVisible = false">{{ $t('memories.close') }}</el-button></template></el-dialog>

    <el-dialog v-model="migrationVisible" :title="$t('memories.migration_detail')" width="780px" class="standard-dialog" align-center><el-descriptions v-if="selectedMigration" :column="2" border><el-descriptions-item :label="$t('memories.migration_job')">{{ migrationId(selectedMigration) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.status')">{{ statusText(selectedMigration.status || selectedMigration.migration_status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.target')" :span="2">{{ migrationTarget(selectedMigration) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.snapshot')">{{ migrationProgress(selectedMigration, 'migration_cursor') }} / {{ migrationProgress(selectedMigration, 'migration_snapshot_boundary') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.delta')">{{ migrationProgress(selectedMigration, 'migration_delta_applied_watermark') }} / {{ migrationProgress(selectedMigration, 'migration_delta_high_watermark') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.error')" :span="2">{{ selectedMigration.error || selectedMigration.migration_error || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.cleanup_status')">{{ statusText(selectedMigration.old_collection_cleanup_status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.collection')">{{ selectedMigration.old_collection_name || '-' }}</el-descriptions-item></el-descriptions><template #footer><el-button @click="migrationVisible = false">{{ $t('memories.close') }}</el-button></template></el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { memoryApi } from '../api'
import { MEMORY_JOB_OPERATIONS, MEMORY_JOB_STATUSES, MEMORY_TYPES } from '../constants'
import StatusTag from '../components/StatusTag.vue'

const { t } = useI18n()
const memoryTypes = MEMORY_TYPES
const jobStatuses = MEMORY_JOB_STATUSES
const jobOperations = MEMORY_JOB_OPERATIONS
const activeTab = ref('memories')
const settings = reactive({})
const settingsLoading = ref(false)
const actionLoading = ref('')
const memories = ref([])
const memoriesLoading = ref(false)
const memoryPage = ref(1)
const memoryPageSize = ref(20)
const memoryTotal = ref(0)
const jobs = ref([])
const jobsLoading = ref(false)
const jobPage = ref(1)
const jobPageSize = ref(20)
const jobTotal = ref(0)
const migrations = ref([])
const migrationsLoading = ref(false)
const migrationPage = ref(1)
const migrationPageSize = ref(20)
const migrationTotal = ref(0)
const editorVisible = ref(false)
const editorMode = ref('create')
const submitting = ref(false)
const detailsVisible = ref(false)
const selectedMemory = ref(null)
const historyVisible = ref(false)
const historyLoading = ref(false)
const history = ref([])
const jobVisible = ref(false)
const selectedJob = ref(null)
const migrationVisible = ref(false)
const selectedMigration = ref(null)
const pollTimer = ref(null)
const filters = reactive({ keyword: '', memory_type: '', scope: '', sort_by: 'updated_at', sort_order: 'desc' })
const jobFilters = reactive({ status: '', operation: '', memory_id: '' })
const form = reactive({ id: null, version: 0, memory_key: '', memory_type: 'fact', importance: 0, scope: '', content: '', change_evidence: '', suppress_current: false })

const unwrap = (response) => response?.data?.data ?? response?.data ?? {}
const pageData = (response) => {
  const data = unwrap(response)
  if (Array.isArray(data)) return { items: data, total: data.length }
  return { items: data.items || [], total: Number(data.total || 0) }
}
const formatTime = (value) => value ? new Date(value).toLocaleString() : '-'
const setting = (key) => settings[key] ?? '-'
const configured = computed(() => Boolean(settings.active_embedding_channel_id && settings.active_embedding_model_id && settings.active_collection_name))
const cleanupRetryId = computed(() => settings.old_collection_cleanup_status === 'failed' ? settings.old_collection_cleanup_job_id : null)
const migrationPercentage = computed(() => {
  const total = Number(settings.migration_total_count || 0)
  return total ? Math.min(100, Math.round(Number(settings.migration_success_count || 0) * 100 / total)) : 0
})
const typeLabel = (value) => t(`memories.type_${value}`, value || '-')
const sourceLabel = (value) => t(`memories.source_${value}`, value || '-')
const statusText = (value) => value ? t(`memories.status_${value}`, value) : t('memories.not_available')
const operationLabel = (value) => value ? t(`memories.operation_${value}`, value) : '-'
const statusType = (value) => ['succeeded', 'ready', 'confirmed', 'none'].includes(value) ? 'success' : ['failed'].includes(value) ? 'danger' : ['cancelled'].includes(value) ? 'info' : 'warning'
const recordStatus = (row) => row.deleted_at ? t('memories.deleted') : row.suppress_recall ? t('memories.suppressed') : row.pending_mutation_job_id ? t('memories.pending') : statusText(row.index_status || 'ready')
const recordStatusType = (row) => row.deleted_at ? 'danger' : row.suppress_recall ? 'warning' : row.pending_mutation_job_id ? 'warning' : statusType(row.index_status || 'ready')
const migrationId = (row) => row.job_id || row.migration_job_id || row.id || '-'
const cleanupId = (row) => row.old_collection_cleanup_job_id || row.cleanup_job_id || row.cleanup?.job_id || null
const migrationProgress = (row, key) => row[key] ?? row.progress?.[key.replace('migration_', '')] ?? 0
const migrationTarget = (row) => `${row.target_embedding_model_id || row.target?.model_id || '-'} / ${row.target_embedding_dimensions || row.target?.dimensions || '-'}D`
const progressText = (row) => `${migrationProgress(row, 'migration_success_count')} / ${migrationProgress(row, 'migration_total_count')}`
const newDedupeKey = () => `dashboard-${Date.now()}-${Math.random().toString(36).slice(2)}`

const loadSettings = async (silent = false) => {
  settingsLoading.value = !silent
  try {
    const data = unwrap(await memoryApi.settings())
    Object.keys(settings).forEach(key => delete settings[key])
    Object.assign(settings, data.store || data)
  } catch (error) {
    if (!silent) ElMessage.error(error.message || t('memories.load_failed'))
  } finally { settingsLoading.value = false }
}
const loadMemories = async (silent = false) => {
  memoriesLoading.value = !silent
  try {
    const data = pageData(await memoryApi.list({ page: memoryPage.value, size: memoryPageSize.value, keyword: filters.keyword || undefined, memory_type: filters.memory_type || undefined, scope: filters.scope || undefined, sort_by: filters.sort_by, sort_order: filters.sort_order }))
    memories.value = data.items
    memoryTotal.value = data.total
  } catch (error) {
    if (!silent) ElMessage.error(error.message || t('memories.load_failed'))
  } finally { memoriesLoading.value = false }
}
const loadJobs = async (silent = false) => {
  jobsLoading.value = !silent
  try {
    const data = pageData(await memoryApi.jobs({ page: jobPage.value, size: jobPageSize.value, status: jobFilters.status || undefined, operation: jobFilters.operation || undefined, memory_id: jobFilters.memory_id || undefined }))
    jobs.value = data.items
    jobTotal.value = data.total
  } catch (error) {
    if (!silent) ElMessage.error(error.message || t('memories.operation_failed'))
  } finally { jobsLoading.value = false }
}
const loadMigrations = async (silent = false) => {
  migrationsLoading.value = !silent
  try {
    const data = pageData(await memoryApi.migrations({ page: migrationPage.value, size: migrationPageSize.value }))
    migrations.value = data.items
    migrationTotal.value = data.total
  } catch (error) {
    if (!silent) ElMessage.error(error.message || t('memories.operation_failed'))
  } finally { migrationsLoading.value = false }
}
const resetAndLoadMemories = () => { memoryPage.value = 1; loadMemories() }
const resetAndLoadJobs = () => { jobPage.value = 1; loadJobs() }
const resetAndLoadMigrations = () => { migrationPage.value = 1; loadMigrations() }
const handleTabChange = (tab) => { if (tab === 'jobs') loadJobs(); if (tab === 'migrations') loadMigrations() }
const refreshAll = () => { loadSettings(true); loadMemories(true); loadJobs(true); if (activeTab.value === 'migrations') loadMigrations(true) }

const resetForm = () => Object.assign(form, { id: null, version: 0, memory_key: '', memory_type: 'fact', importance: 0, scope: '', content: '', change_evidence: '', suppress_current: false })
const openEditor = (row = null) => {
  editorMode.value = row ? 'edit' : 'create'
  resetForm()
  if (row) Object.assign(form, { id: row.id, version: row.version, memory_key: row.memory_key || '', memory_type: row.memory_type || 'fact', importance: row.importance ?? 0, scope: row.scope || '', content: row.content || '', change_evidence: row.change_evidence || '', suppress_current: false })
  editorVisible.value = true
}
const submitMemory = async () => {
  if (!form.memory_key.trim() || !form.content.trim()) return ElMessage.warning(t('memories.required'))
  if (!Number.isInteger(form.importance) || form.importance < 0 || form.importance > 10) return ElMessage.warning(t('memories.invalid_importance'))
  submitting.value = true
  try {
    const payload = { dedupe_key: newDedupeKey(), content: form.content, memory_key: form.memory_key, memory_type: form.memory_type, importance: form.importance, scope: form.scope || null, change_evidence: form.change_evidence || null }
    if (editorMode.value === 'create') await memoryApi.create(payload)
    else await memoryApi.update({ ...payload, memory_id: form.id, expected_version: form.version, suppress_current: form.suppress_current })
    ElMessage.info(t('memories.accepted_processing'))
    editorVisible.value = false
    refreshAll()
  } catch (error) { ElMessage.error(error.message || t('memories.save_failed')) } finally { submitting.value = false }
}
const showDetails = async (row) => { try { selectedMemory.value = unwrap(await memoryApi.get(row.id)) || row; detailsVisible.value = true } catch (error) { ElMessage.error(error.message || t('memories.load_failed')) } }
const deleteMemory = async (row) => {
  try {
    await ElMessageBox.confirm(t('memories.delete_confirm'), t('common.warning'), { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') })
    await memoryApi.delete({ memory_id: row.id, expected_version: row.version, dedupe_key: newDedupeKey() })
    ElMessage.info(t('memories.delete_success'))
    refreshAll()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) }
}
const showHistory = async (row) => {
  selectedMemory.value = row
  historyVisible.value = true
  historyLoading.value = true
  try { history.value = pageData(await memoryApi.history(row.id, { page: 1, size: 100 })).items } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } finally { historyLoading.value = false }
}
const restoreRevision = async (revision) => {
  try {
    await ElMessageBox.confirm(t('memories.restore_confirm'), t('common.warning'), { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') })
    await memoryApi.restore(selectedMemory.value.id, { revision_version: revision.version, expected_version: selectedMemory.value.version, dedupe_key: newDedupeKey() })
    ElMessage.info(t('memories.restore_success')); historyVisible.value = false; refreshAll()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) }
}
const resumeCurrent = async (row) => { try { await memoryApi.resumeCurrent(row.id, { expected_version: row.version }); ElMessage.info(t('memories.operation_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const canRetry = (row) => ['failed', 'cancelled'].includes(row.status) && row.operation !== 'delete_cleanup'
const canCancel = (row) => !['succeeded', 'failed', 'cancelled'].includes(row.status) && row.operation !== 'delete_cleanup'
const retryJob = async (row) => { try { await ElMessageBox.confirm(t('memories.retry_confirm'), t('common.warning'), { type: 'warning' }); await memoryApi.retryJob(row.id); ElMessage.info(t('memories.retry_success')); refreshAll() } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) } }
const cancelJob = async (row) => { try { await ElMessageBox.confirm(t('memories.cancel_confirm'), t('common.warning'), { type: 'warning' }); await memoryApi.cancelJob(row.id); ElMessage.info(t('memories.cancel_success')); refreshAll() } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) } }
const showJob = async (row) => { try { selectedJob.value = unwrap(await memoryApi.job(row.id)) || row; jobVisible.value = true } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const reindex = async () => { actionLoading.value = 'reindex'; try { await memoryApi.reindex({ dedupe_key: newDedupeKey() }); ElMessage.info(t('memories.accepted_processing')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } finally { actionLoading.value = '' } }
const canRetryMigration = (row) => ['failed', 'cancelled'].includes(row.status || row.migration_status)
const canCancelMigration = (row) => ['preparing', 'building', 'catching_up', 'validating'].includes(row.status || row.migration_status)
const retryMigration = async (row) => { try { await memoryApi.retryMigration(migrationId(row)); ElMessage.info(t('memories.migration_retry_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const cancelMigration = async (row) => { try { await memoryApi.cancelMigration(migrationId(row)); ElMessage.info(t('memories.migration_cancel_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const retryCleanup = async (id) => { actionLoading.value = `cleanup-${id}`; try { await memoryApi.retryCleanup(id); ElMessage.info(t('memories.retry_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } finally { actionLoading.value = '' } }
const showMigration = async (row) => { try { selectedMigration.value = unwrap(await memoryApi.migration(migrationId(row))) || row; migrationVisible.value = true } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }

onMounted(() => { loadSettings(); loadMemories(); pollTimer.value = window.setInterval(refreshAll, 5000) })
onBeforeUnmount(() => { if (pollTimer.value) window.clearInterval(pollTimer.value) })
</script>

<style lang="scss">
@import "@/assets/css/common.scss";

.memory-view { min-width: 0; }
.settings-panel { padding: 20px; margin-bottom: 18px; background: #fff; border: 1px solid var(--color-border-light); }
.section-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.section-heading h2 { margin: 0 0 4px; font-size: 22px; color: var(--color-text-main); }
.section-heading p { margin: 0; color: var(--color-text-secondary); }
.heading-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.settings-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-top: 16px; }
.config-block { min-width: 0; padding: 16px; background: #f8fafc; border: 1px solid #e5e7eb; }
.config-block strong { display: block; margin-bottom: 12px; color: var(--color-text-main); }
.config-line { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 28px; color: var(--color-text-secondary); font-size: 13px; }
.config-line b { min-width: 0; color: var(--color-text-main); font-weight: 500; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: Consolas, monospace; font-size: 12px; }
.progress-counts { display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0; color: var(--color-text-secondary); font-size: 12px; }
.settings-error { margin-top: 16px; }
.memory-tabs { background: #fff; padding: 0 20px 20px; border: 1px solid var(--color-border-light); }
.filter-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 16px 0; }
.keyword-input { width: 240px; }.filter-input { width: 150px; }.sort-input { width: 130px; }.order-input { width: 110px; }.operation-input { width: 190px; }.small-input { width: 120px; }
.memory-table { width: 100%; }.content-preview { overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; line-height: 1.45; }
.table-footer { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding-top: 16px; color: var(--color-text-secondary); font-size: 13px; }
.memory-content { max-height: 360px; margin: 0; overflow: auto; white-space: pre-wrap; word-break: break-word; font: inherit; line-height: 1.55; }
.action-buttons { flex-wrap: wrap; gap: 4px; }.action-buttons .el-button { margin-left: 0; }
@media (max-width: 1100px) { .settings-grid { grid-template-columns: 1fr 1fr; } }
@media (max-width: 760px) { .settings-grid { grid-template-columns: 1fr; }.section-heading { flex-direction: column; }.heading-actions { justify-content: flex-start; }.filter-bar > * { width: 100% !important; }.table-footer { align-items: flex-start; flex-direction: column; overflow-x: auto; } }
</style>
