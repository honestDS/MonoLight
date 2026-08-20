<template>
  <div class="memory-view">
    <section class="settings-panel">
      <div class="section-heading">
        <div class="section-heading-content">
          <h2>{{ $t('memories.title') }}</h2>
          <p>{{ $t('memories.settings') }}</p>
          <Transition name="memory-task-transition" mode="out-in">
            <div v-if="currentMemoryTask" key="task-summary" class="memory-task-summary">
              <div class="memory-task-summary-item">
                <span>{{ $t('memories.current_task') }}</span>
                <strong>{{ operationLabel(currentMemoryTask.operation) }}<span v-if="currentMemoryTask.id"> #{{ currentMemoryTask.id }}</span></strong>
              </div>
              <div class="memory-task-summary-item memory-task-progress">
                <span>{{ $t('memories.progress') }}</span>
                <div v-if="currentMemoryTask.total > 0 && currentMemoryTask.completed !== null" class="memory-task-progress-value">
                  <el-progress :percentage="currentMemoryTask.percentage ?? 0" :show-text="false" />
                  <span>{{ currentMemoryTask.completed }} / {{ currentMemoryTask.total }}</span>
                </div>
                <el-tag v-else size="small" type="warning">{{ statusText(currentMemoryTask.status) }}</el-tag>
              </div>
            </div>
          </Transition>
        </div>
        <div class="heading-actions">
          <el-button size="small" @click="loadSettings()" :loading="settingsLoading">{{ $t('memories.refresh') }}</el-button>
          <el-button type="success" size="small" @click="organize" :loading="actionLoading === 'organize'" :disabled="organizeBlocked">{{ $t('memories.organize_now') }}</el-button>
          <el-button type="warning" size="small" @click="reindex" :loading="actionLoading === 'reindex'" :disabled="!configured">{{ $t('memories.reindex') }}</el-button>
          <el-button
            v-if="cleanupRetryId"
            type="danger"
            size="small"
            @click="retryCleanup(cleanupRetryId)"
            :loading="actionLoading === `cleanup-${cleanupRetryId}`">
            {{ $t('memories.cleanup_retry') }}
          </el-button>
          <el-button size="small" @click="settingsExpanded = !settingsExpanded" :aria-expanded="settingsExpanded">
            {{ settingsExpanded ? $t('memories.collapse_settings') : $t('memories.expand_settings') }}
          </el-button>
        </div>
      </div>

      <el-collapse-transition>
        <div v-show="settingsExpanded" class="settings-content">
          <el-alert v-if="!configured" type="info" :closable="false" show-icon :title="$t('memories.no_config')" />
          <div class="settings-grid runtime-settings-grid" v-loading="settingsLoading">
            <div class="config-block">
              <strong>{{ $t('memories.active_config') }}</strong>
              <div class="config-line"><span>{{ $t('memories.channel') }}</span><b>{{ channelName(nestedSetting('active', 'channel_id', 'active_embedding_channel_id')) }}</b></div>
              <div class="config-line"><span>{{ $t('memories.model') }}</span><b>{{ nestedSetting('active', 'model_id', 'active_embedding_model_id') }}</b></div>
              <div class="config-line"><span>{{ $t('memories.dimensions') }}</span><b>{{ nestedSetting('active', 'dimensions', 'active_embedding_dimensions') }}</b></div>
              <div class="config-line"><span>{{ $t('memories.collection') }}</span><b class="mono">{{ nestedSetting('active', 'collection', 'active_collection_name') }}</b></div>
              <div class="config-line"><span>{{ $t('memories.revision') }}</span><b>{{ nestedSetting('active', 'revision', 'active_embedding_revision') }}</b></div>
            </div>
            <div class="config-block">
              <strong>{{ $t('memories.target_config') }}</strong>
              <div class="config-line"><span>{{ $t('memories.channel') }}</span><b>{{ channelName(nestedSetting('target', 'channel_id', 'target_embedding_channel_id')) }}</b></div>
              <div class="config-line"><span>{{ $t('memories.model') }}</span><b>{{ nestedSetting('target', 'model_id', 'target_embedding_model_id') }}</b></div>
              <div class="config-line"><span>{{ $t('memories.dimensions') }}</span><b>{{ nestedSetting('target', 'dimensions', 'target_embedding_dimensions') }}</b></div>
              <div class="config-line"><span>{{ $t('memories.collection') }}</span><b class="mono">{{ nestedSetting('target', 'collection', 'target_collection_name') }}</b></div>
              <div class="config-line"><span>{{ $t('memories.migration_status') }}</span><StatusTag :status="settings.migration?.status || setting('migration_status')" :active-text="statusText(settings.migration?.status || setting('migration_status'))" :inactive-text="statusText(settings.migration?.status || setting('migration_status'))" :active-type="statusType(settings.migration?.status || setting('migration_status'))" :inactive-type="statusType(settings.migration?.status || setting('migration_status'))" /></div>
            </div>
            <div class="config-block progress-block">
              <strong>{{ $t('memories.progress') }}</strong>
              <el-progress :percentage="migrationPercentage" :status="(settings.migration?.status || setting('migration_status')) === 'failed' ? 'exception' : undefined" />
              <div class="progress-counts">
                <span>{{ $t('memories.total_count') }} {{ settings.migration?.total_count ?? setting('migration_total_count') ?? 0 }}</span>
                <span>{{ $t('memories.success_count') }} {{ settings.migration?.success_count ?? setting('migration_success_count') ?? 0 }}</span>
                <span>{{ $t('memories.failure_count') }} {{ settings.migration?.failure_count ?? setting('migration_failure_count') ?? 0 }}</span>
              </div>
              <div class="config-line"><span>{{ $t('memories.index_status') }}</span><StatusTag :status="settings.index?.status || setting('index_status')" :active-text="statusText(settings.index?.status || setting('index_status'))" :inactive-text="statusText(settings.index?.status || setting('index_status'))" :active-type="statusType(settings.index?.status || setting('index_status'))" :inactive-type="statusType(settings.index?.status || setting('index_status'))" /></div>
              <div class="config-line"><span>{{ $t('memories.cleanup_status') }}</span><StatusTag :status="settings.old_collection_cleanup?.status || setting('old_collection_cleanup_status')" :active-text="statusText(settings.old_collection_cleanup?.status || setting('old_collection_cleanup_status'))" :inactive-text="statusText(settings.old_collection_cleanup?.status || setting('old_collection_cleanup_status'))" :active-type="statusType(settings.old_collection_cleanup?.status || setting('old_collection_cleanup_status'))" :inactive-type="statusType(settings.old_collection_cleanup?.status || setting('old_collection_cleanup_status'))" /></div>
              <div class="config-line"><span>{{ $t('memories.capacity') }}</span><b>{{ settings.capacity?.active_record_count ?? setting('active_record_count') ?? 0 }} / {{ settings.capacity?.max_active_records ?? setting('max_active_records') ?? 0 }}</b></div>
            </div>
          </div>

          <div class="settings-grid organization-settings">
            <div class="config-block">
              <strong>{{ $t('memories.capacity_settings') }}</strong>
              <div class="config-line"><span>{{ $t('memories.active_record_count') }}</span><b>{{ settings.capacity?.active_record_count ?? setting('active_record_count') ?? 0 }}</b></div>
              <div class="config-line"><span>{{ $t('memories.organize_trigger_records') }}</span><b>{{ settings.capacity?.organize_trigger_records ?? 45 }} / {{ settings.capacity?.max_active_records ?? 50 }}</b></div>
              <div class="config-line"><span>{{ $t('memories.content_max_tokens') }}</span><b>{{ contentMaxTokens }}</b></div>
              <div class="config-line"><span>{{ $t('memories.capacity_status') }}</span><el-tag :type="capacityOverLimit ? 'danger' : 'success'">{{ statusText(settings.capacity?.status || 'normal') }}</el-tag></div>
              <div class="config-line"><span>{{ $t('memories.over_limit') }}</span><b>{{ capacityOverLimit ? $t('memories.yes') : $t('memories.no') }}</b></div>
            </div>
            <div class="config-block">
              <strong>{{ $t('memories.organization_jobs') }}</strong>
              <div class="config-line"><span>{{ $t('memories.current_job') }}</span><b>{{ settings.organization?.current_job_id ?? '-' }}</b></div>
              <div class="config-line"><span>{{ $t('memories.recent_job') }}</span><b>{{ settings.organization?.recent_job_id ?? '-' }}</b></div>
              <div class="config-line"><span>{{ $t('memories.recent_job_status') }}</span><StatusTag :status="settings.organization?.recent_job?.status" :active-text="statusText(settings.organization?.recent_job?.status)" :inactive-text="statusText(settings.organization?.recent_job?.status)" :active-type="statusType(settings.organization?.recent_job?.status)" :inactive-type="statusType(settings.organization?.recent_job?.status)" /></div>
              <div class="config-line"><span>{{ $t('memories.last_organized_at') }}</span><b>{{ formatTime(settings.organization?.last_run_at || settings.organization?.recent_job?.finished_at) }}</b></div>
              <div class="config-line"><span>{{ $t('memories.organization_error') }}</span><b class="text-wrap">{{ settings.organization?.error || settings.organization?.recent_job?.error || '-' }}</b></div>
              <div v-if="settings.organization?.validation_error" class="config-line"><span>{{ $t('memories.organization_validation_error') }}</span><b class="text-wrap">{{ settings.organization.validation_error }}</b></div>
              <div class="config-line"><span>{{ $t('memories.organize_blocking') }}</span><b class="text-wrap">{{ blockingText(settings.blocking?.organize) }}</b></div>
            </div>
          </div>

          <el-alert v-if="settingsError" class="settings-error" type="warning" :closable="false" show-icon>
            <template #title>{{ settingsError }}</template>
          </el-alert>
        </div>
      </el-collapse-transition>
    </section>

    <el-tabs v-model="activeTab" @tab-change="handleTabChange" class="memory-tabs">
      <el-tab-pane :label="$t('memories.memories')" name="memories">
        <div class="filter-bar">
          <el-input v-model="filters.keyword" :placeholder="$t('memories.keyword_placeholder')" clearable class="keyword-input" @keyup.enter="resetAndLoadMemories" />
          <el-select v-model="filters.memory_type" :placeholder="$t('memories.all_types')" clearable class="filter-input" @change="resetAndLoadMemories">
            <el-option :label="$t('memories.all_types')" value="" />
            <el-option v-for="type in memoryTypes" :key="type" :label="typeLabel(type)" :value="type" />
          </el-select>
          <el-select v-model="filters.sort_by" class="sort-input" @change="loadMemories">
            <el-option :label="$t('memories.updated_at')" value="updated_at" />
            <el-option :label="$t('memories.created_at')" value="created_at" />
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
        <el-table :data="memories" v-loading="memoriesLoading" class="memory-table memory-data-table">
          <el-table-column prop="id" :label="$t('memories.memory_id')" width="88" align="center" />
          <el-table-column :label="$t('memories.content_preview')" min-width="200"><template #default="{ row }"><div class="content-preview">{{ row.content || '-' }}</div></template></el-table-column>
          <el-table-column :label="$t('memories.type')" width="120" align="center"><template #default="{ row }">{{ typeLabel(row.memory_type) }}</template></el-table-column>
          <el-table-column prop="content_token_count" :label="$t('memories.token_count')" width="100" align="center" />
          <el-table-column :label="$t('memories.pinned')" width="90" align="center"><template #default="{ row }"><el-tag :type="row.pinned ? 'warning' : 'info'">{{ row.pinned ? $t('memories.pinned_yes') : $t('memories.pinned_no') }}</el-tag></template></el-table-column>
          <el-table-column :label="$t('memories.last_recalled_at')" width="170"><template #default="{ row }">{{ formatTime(row.last_recalled_at) }}</template></el-table-column>
          <el-table-column :label="$t('memories.current_status')" width="130" align="center"><template #default="{ row }"><el-tag :type="recordStatusType(row)">{{ recordStatus(row) }}</el-tag></template></el-table-column>
          <el-table-column prop="version" :label="$t('memories.version')" width="76" align="center" />
          <el-table-column :label="$t('memories.updated_at')" width="170"><template #default="{ row }">{{ formatTime(row.updated_at) }}</template></el-table-column>
          <el-table-column :label="$t('memories.actions')" width="280" fixed="right" header-align="center">
            <template #default="{ row }">
              <div class="memory-action-buttons">
                <el-button size="small" type="info" @click="showDetails(row)">{{ $t('memories.view') }}</el-button>
                <el-button size="small" type="primary" @click="openEditor(row)" :disabled="!canMutateRecord(row)">{{ $t('memories.edit') }}</el-button>
                <el-button size="small" @click="showHistory(row)">{{ $t('memories.history') }}</el-button>
                <el-button size="small" type="warning" @click="togglePin(row)" :disabled="!canPin(row)">{{ row.pinned ? $t('memories.unpin') : $t('memories.pin') }}</el-button>
                <el-button v-if="row.suppress_recall && !row.pending_mutation_job_id" size="small" type="warning" @click="resumeCurrent(row)">{{ $t('memories.resume_current') }}</el-button>
                <el-button size="small" type="danger" @click="deleteMemory(row)" :disabled="!canMutateRecord(row)">{{ $t('memories.delete') }}</el-button>
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
        <el-table :data="jobs" v-loading="jobsLoading" row-key="id" :tree-props="{ children: 'childJobs' }" :default-expand-all="false" class="memory-data-table">
          <el-table-column prop="id" :label="$t('memories.job_id')" width="90" align="center" />
          <el-table-column :label="$t('memories.operation')" width="190"><template #default="{ row }"><div class="job-tree"><el-tag v-if="row.jobLevel" size="small" type="info">{{ $t('memories.job_child') }}</el-tag><el-tag v-else-if="row.child_job_ids?.length" size="small" type="success">{{ $t('memories.job_parent') }}</el-tag><span>{{ operationLabel(row.operation) }}</span></div></template></el-table-column>
          <el-table-column prop="memory_id" :label="$t('memories.memory_id')" width="100" align="center" />
          <el-table-column :label="$t('memories.status')" width="120" align="center"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag></template></el-table-column>
          <el-table-column :label="$t('memories.snapshot_count')" width="100" align="center"><template #default="{ row }">{{ row.snapshot_count ?? '-' }}</template></el-table-column>
          <el-table-column :label="$t('memories.organization_counts')" min-width="300"><template #default="{ row }">{{ jobCountsText(row) }}</template></el-table-column>
          <el-table-column prop="attempt_count" :label="$t('memories.attempt')" width="90" align="center" />
          <el-table-column :label="$t('memories.error')" min-width="260" show-overflow-tooltip><template #default="{ row }">{{ jobError(row) }}</template></el-table-column>
          <el-table-column :label="$t('memories.created_at')" width="170"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
          <el-table-column :label="$t('memories.actions')" width="250" fixed="right" header-align="center">
            <template #default="{ row }">
              <div class="memory-action-buttons">
                <el-button v-if="canRetry(row)" size="small" type="warning" @click="retryJob(row)">{{ $t('memories.retry') }}</el-button>
                <el-button v-if="canCancel(row)" size="small" type="danger" @click="cancelJob(row)">{{ $t('memories.cancel_job') }}</el-button>
                <el-button v-if="canShowDeletedHistory(row)" size="small" @click="showDeletedHistory(row)">{{ $t('memories.history') }}</el-button>
                <el-button size="small" type="info" @click="showJob(row)">{{ $t('memories.view') }}</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <div class="table-footer"><span>{{ $t('common.total_items', { total: jobTotal }) }}</span><el-pagination v-model:current-page="jobPage" v-model:page-size="jobPageSize" :total="jobTotal" :page-sizes="[10, 20, 50, 100]" layout="total, sizes, prev, pager, next, jumper" @current-change="loadJobs" @size-change="resetAndLoadJobs" /></div>
      </el-tab-pane>

      <el-tab-pane :label="$t('memories.migrations')" name="migrations">
        <div class="filter-bar"><el-button @click="loadMigrations">{{ $t('memories.refresh') }}</el-button></div>
        <el-table :data="migrations" v-loading="migrationsLoading" class="memory-data-table">
          <el-table-column :label="$t('memories.migration_job')" width="110" align="center">
            <template #default="{ row }">{{ migrationId(row) }}</template>
          </el-table-column>
          <el-table-column :label="$t('memories.status')" width="140" align="center">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status || row.migration_status)">{{ statusText(row.status || row.migration_status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('memories.target')" min-width="250">
            <template #default="{ row }">{{ migrationTarget(row) }}</template>
          </el-table-column>
          <el-table-column :label="$t('memories.progress')" min-width="180">
            <template #default="{ row }">{{ progressText(row) }}</template>
          </el-table-column>
          <el-table-column :label="$t('memories.error')" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">{{ row.error || row.migration_error || '-' }}</template>
          </el-table-column>
          <el-table-column :label="$t('memories.actions')" width="350" fixed="right" header-align="center">
            <template #default="{ row }">
              <div class="memory-action-buttons">
                <el-button size="small" type="info" @click="showMigration(row)">{{ $t('memories.view') }}</el-button>
                <el-button v-if="canRetryMigration(row)" size="small" type="warning" @click="retryMigration(row)">{{ $t('memories.migration_retry') }}</el-button>
                <el-button v-if="canCancelMigration(row)" size="small" type="danger" @click="cancelMigration(row)">{{ $t('memories.migration_cancel') }}</el-button>
                <el-button v-if="cleanupId(row)" size="small" type="danger" @click="retryCleanup(cleanupId(row))">{{ $t('memories.cleanup_retry') }}</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <div class="table-footer"><span>{{ $t('common.total_items', { total: migrationTotal }) }}</span><el-pagination v-model:current-page="migrationPage" v-model:page-size="migrationPageSize" :total="migrationTotal" :page-sizes="[10, 20, 50]" layout="total, sizes, prev, pager, next, jumper" @current-change="loadMigrations" @size-change="resetAndLoadMigrations" /></div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="editorVisible" :title="editorMode === 'create' ? $t('memories.form_create_title') : $t('memories.form_edit_title')" width="720px" class="standard-dialog" align-center>
      <el-form :model="form" label-width="120px">
        <el-form-item :label="$t('memories.memory_key')" required><el-input v-model="form.memory_key" :placeholder="$t('memories.memory_key_placeholder')" /></el-form-item>
        <el-form-item :label="$t('memories.type')" required><el-select v-model="form.memory_type" class="full-width-input"><el-option v-for="type in memoryTypes" :key="type" :label="typeLabel(type)" :value="type" /></el-select></el-form-item>
        <el-form-item :label="$t('memories.content')" required><el-input v-model="form.content" type="textarea" :rows="9" :placeholder="$t('memories.content_placeholder')" /></el-form-item>
        <div class="token-estimate" :class="{ 'token-estimate-error': contentTooLong }"><span>{{ $t('memories.token_estimate', { count: contentTokenCount, max: contentMaxTokens }) }}</span><span v-if="contentTooLong">{{ $t('memories.token_limit_exceeded') }}</span></div>
        <el-form-item :label="$t('memories.change_evidence')"><el-input v-model="form.change_evidence" type="textarea" :rows="3" :placeholder="$t('memories.change_evidence_placeholder')" /></el-form-item>
        <el-form-item v-if="editorMode === 'edit'" :label="$t('memories.suppress_current')"><el-checkbox v-model="form.suppress_current">{{ $t('memories.suppress_current') }}</el-checkbox><div class="help-text">{{ $t('memories.suppress_hint') }}</div></el-form-item>
      </el-form>
      <template #footer><el-button @click="editorVisible = false">{{ $t('memories.cancel') }}</el-button><el-button type="primary" :loading="submitting" :disabled="contentTooLong" @click="submitMemory">{{ $t('memories.save') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="detailsVisible" :title="$t('memories.details')" width="760px" class="standard-dialog" align-center>
      <el-descriptions v-if="selectedMemory" :column="2" border><el-descriptions-item :label="$t('memories.memory_id')">{{ selectedMemory.id }}</el-descriptions-item><el-descriptions-item :label="$t('memories.version')">{{ selectedMemory.version }}</el-descriptions-item><el-descriptions-item :label="$t('memories.memory_key')">{{ selectedMemory.memory_key }}</el-descriptions-item><el-descriptions-item :label="$t('memories.type')">{{ typeLabel(selectedMemory.memory_type) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.source')">{{ sourceLabel(selectedMemory.source) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.token_count')">{{ selectedMemory.content_token_count ?? '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.pinned')">{{ selectedMemory.pinned ? $t('memories.pinned_yes') : $t('memories.pinned_no') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.last_recalled_at')">{{ formatTime(selectedMemory.last_recalled_at) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.current_status')">{{ recordStatus(selectedMemory) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.content')" :span="2"><pre class="memory-content">{{ selectedMemory.content || '-' }}</pre></el-descriptions-item><el-descriptions-item :label="$t('memories.change_evidence')" :span="2"><pre class="memory-content">{{ selectedMemory.change_evidence || '-' }}</pre></el-descriptions-item></el-descriptions>
      <template #footer><el-button @click="detailsVisible = false">{{ $t('memories.close') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="historyVisible" :title="$t('memories.history_title', { key: selectedMemory?.memory_key || '' })" width="900px" class="standard-dialog" align-center>
      <el-alert type="info" :closable="false" show-icon :title="$t('memories.deleted_history_read_only')" />
      <el-table :data="history" v-loading="historyLoading"><el-table-column prop="version" :label="$t('memories.revision_version')" width="100" align="center" /><el-table-column prop="memory_type" :label="$t('memories.type')" width="120"><template #default="{ row }">{{ typeLabel(row.memory_type) }}</template></el-table-column><el-table-column prop="content_token_count" :label="$t('memories.token_count')" width="100" align="center" /><el-table-column prop="content" :label="$t('memories.content')" min-width="350" show-overflow-tooltip /><el-table-column prop="published_at" :label="$t('memories.published_at')" width="180"><template #default="{ row }">{{ formatTime(row.published_at || row.created_at) }}</template></el-table-column></el-table>
      <el-empty v-if="!historyLoading && !history.length" :description="$t('memories.no_history')" />
    </el-dialog>

    <el-dialog v-model="jobVisible" :title="$t('memories.jobs')" width="820px" class="standard-dialog" align-center><el-descriptions v-if="selectedJob" :column="2" border><el-descriptions-item :label="$t('memories.job_id')">{{ selectedJob.id }}</el-descriptions-item><el-descriptions-item :label="$t('memories.operation')">{{ operationLabel(selectedJob.operation) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.status')">{{ statusText(selectedJob.status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.memory_id')">{{ selectedJob.memory_id || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.parent_job_id')">{{ selectedJob.parent_job_id || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.children')">{{ selectedJob.child_job_ids?.join(', ') || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.snapshot_count')">{{ selectedJob.snapshot_count ?? '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.attempt')">{{ selectedJob.attempt_count }} / {{ selectedJob.max_attempts }}</el-descriptions-item><el-descriptions-item :label="$t('memories.organization_counts')" :span="2">{{ jobCountsText(selectedJob) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.token_budget')" :span="2">{{ tokenBudgetText(selectedJob.token_budget) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.context_error')" :span="2">{{ selectedJob.context_error ? JSON.stringify(selectedJob.context_error) : '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.error')" :span="2">{{ jobError(selectedJob) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.payload')" :span="2"><pre class="memory-content">{{ JSON.stringify(selectedJob.payload || {}, null, 2) }}</pre></el-descriptions-item><el-descriptions-item :label="$t('memories.result')" :span="2"><pre class="memory-content">{{ JSON.stringify(selectedJob.result || {}, null, 2) }}</pre></el-descriptions-item></el-descriptions><template #footer><el-button @click="jobVisible = false">{{ $t('memories.close') }}</el-button></template></el-dialog>

    <el-dialog v-model="migrationVisible" :title="$t('memories.migration_detail')" width="780px" class="standard-dialog" align-center><el-descriptions v-if="selectedMigration" :column="2" border><el-descriptions-item :label="$t('memories.migration_job')">{{ migrationId(selectedMigration) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.status')">{{ statusText(selectedMigration.status || selectedMigration.migration_status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.target')" :span="2">{{ migrationTarget(selectedMigration) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.snapshot')">{{ migrationProgress(selectedMigration, 'migration_cursor') }} / {{ migrationProgress(selectedMigration, 'migration_snapshot_boundary') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.delta')">{{ migrationProgress(selectedMigration, 'migration_delta_applied_watermark') }} / {{ migrationProgress(selectedMigration, 'migration_delta_high_watermark') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.error')" :span="2">{{ selectedMigration.error || selectedMigration.migration_error || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.cleanup_status')">{{ statusText(selectedMigration.old_collection_cleanup_status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.collection')">{{ selectedMigration.old_collection_name || '-' }}</el-descriptions-item></el-descriptions><template #footer><el-button @click="migrationVisible = false">{{ $t('memories.close') }}</el-button></template></el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { channelApi, memoryApi } from '../api'
import { MEMORY_JOB_OPERATIONS, MEMORY_JOB_STATUSES, MEMORY_TYPES } from '../constants'
import StatusTag from '../components/StatusTag.vue'
import {
  buildOrganizePayload,
  createLatestRequestTracker,
  decorateMemoryJobs,
  estimateMemoryTokens,
  getCurrentMemoryTask,
  isMemoryContentTooLong,
  memoryOperationLabelKey,
  memorySourceLabelKey,
  normalizeMemorySettings
} from '../utils/memoryManagement'
import { createAbortableTaskManager } from '../utils/channelTestManager'

const { t } = useI18n()
const memoryTypes = MEMORY_TYPES
const jobStatuses = MEMORY_JOB_STATUSES
const jobOperations = MEMORY_JOB_OPERATIONS
const activeTab = ref('memories')
const settingsExpanded = ref(false)
const settings = reactive({})
const settingsLoading = ref(false)
const actionLoading = ref('')
const channels = ref([])
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
let pollingStopped = false
const pollingTaskManager = createAbortableTaskManager()
const settingsRequestTracker = createLatestRequestTracker()
const memoriesRequestTracker = createLatestRequestTracker()
const jobsRequestTracker = createLatestRequestTracker()
const migrationsRequestTracker = createLatestRequestTracker()
const historyRequestTracker = createLatestRequestTracker()
const filters = reactive({ keyword: '', memory_type: '', sort_by: 'updated_at', sort_order: 'desc' })
const jobFilters = reactive({ status: '', operation: '', memory_id: '' })
const form = reactive({ id: null, version: 0, memory_key: '', memory_type: 'fact', content: '', change_evidence: '', suppress_current: false })

const unwrap = (response) => response?.data?.data ?? response?.data ?? {}
const pageData = (response) => {
  const data = unwrap(response)
  if (Array.isArray(data)) return { items: data, total: data.length }
  return { items: data.items || [], total: Number(data.total || 0) }
}
const formatTime = (value) => value ? new Date(value).toLocaleString() : '-'
const setting = (key) => settings.store?.[key] ?? settings[key] ?? '-'
const nestedSetting = (section, key, legacyKey) => settings[section]?.[key] ?? setting(legacyKey)
const channelName = (channelId) => {
  if (channelId === null || channelId === undefined || channelId === '' || channelId === '-') return '-'
  const channel = channels.value.find(item => String(item.id) === String(channelId))
  return typeof channel?.name === 'string' && channel.name.trim() ? channel.name : '-'
}
const numericSetting = (key, fallback) => {
  const value = Number(setting(key))
  return Number.isFinite(value) ? value : fallback
}
const configured = computed(() => settings.configured !== undefined ? Boolean(settings.configured) : Boolean(setting('active_embedding_channel_id') !== '-' && setting('active_embedding_model_id') !== '-' && setting('active_collection_name') !== '-'))
const contentMaxTokens = computed(() => settings.contentMaxTokens ?? Number(settings.capacity?.content_max_tokens ?? numericSetting('content_max_tokens', 160)))
const activeRecordCount = computed(() => settings.activeRecordCount ?? Number(settings.capacity?.active_record_count ?? numericSetting('active_record_count', 0)))
const maxActiveRecords = computed(() => settings.maxActiveRecords ?? Number(settings.capacity?.max_active_records ?? numericSetting('max_active_records', 50)))
const capacityOverLimit = computed(() => ['over_limit', 'full'].includes(settings.capacity?.status) || activeRecordCount.value > maxActiveRecords.value)
const organizeBlocked = computed(() => Boolean(settings.blocking?.organize?.blocked))
const cleanupRetryId = computed(() => {
  const status = settings.old_collection_cleanup?.status ?? settings.store?.old_collection_cleanup_status
  if (status !== 'failed') return null
  return settings.old_collection_cleanup?.job_id ?? settings.store?.old_collection_cleanup_job_id ?? null
})
const migrationPercentage = computed(() => {
  const total = Number(settings.migration?.total_count ?? numericSetting('migration_total_count', 0))
  return total ? Math.min(100, Math.round(Number(settings.migration?.success_count ?? numericSetting('migration_success_count', 0)) * 100 / total)) : 0
})
const contentTokenCount = computed(() => estimateMemoryTokens(form.content))
const contentTooLong = computed(() => isMemoryContentTooLong(form.content, contentMaxTokens.value))
const settingsError = computed(() => {
  const candidates = [
    settings.migration?.error,
    settings.old_collection_cleanup?.error,
    settings.store?.migration_error,
    settings.store?.old_collection_cleanup_error,
    settings.migration_error,
    settings.old_collection_cleanup_error,
  ]
  return candidates.find(value => typeof value === 'string' && value.trim() && value.trim() !== '-') || ''
})
const currentMemoryTask = computed(() => getCurrentMemoryTask(settings))

const typeLabel = (value) => t(`memories.type_${value}`, value || '-')
const sourceLabel = (value) => {
  const labelKey = memorySourceLabelKey(value)
  return labelKey === `memories.source_${value}` ? t(labelKey) : labelKey
}
const statusText = (value) => value ? t(`memories.status_${value}`, value) : t('memories.not_available')
const operationLabel = (value) => {
  const labelKey = memoryOperationLabelKey(value)
  return labelKey === `memories.operation_${value}` ? t(labelKey) : labelKey
}
const statusType = (value) => ['succeeded', 'ready', 'confirmed', 'none', 'normal'].includes(value) ? 'success' : ['failed', 'over_limit', 'full'].includes(value) ? 'danger' : ['cancelled'].includes(value) ? 'info' : 'warning'
const recordStatus = (row) => row.deleted_at ? t('memories.deleted') : row.suppress_recall ? t('memories.suppressed') : row.pending_mutation_job_id ? t('memories.pending') : statusText(row.index_status || 'ready')
const recordStatusType = (row) => row.deleted_at ? 'danger' : row.suppress_recall ? 'warning' : row.pending_mutation_job_id ? 'warning' : statusType(row.index_status || 'ready')
const migrationId = (row) => row.job_id || row.migration_job_id || row.id || '-'
const cleanupId = (row) => row.old_collection_cleanup_job_id || row.cleanup_job_id || row.cleanup?.job_id || null
const migrationProgress = (row, key) => row[key] ?? row.progress?.[key.replace('migration_', '')] ?? 0
const migrationTarget = (row) => `${row.target_embedding_model_id || row.target?.model_id || '-'} / ${row.target_embedding_dimensions || row.target?.dimensions || '-'}D`
const progressText = (row) => `${migrationProgress(row, 'migration_success_count')} / ${migrationProgress(row, 'migration_total_count')}`
const newDedupeKey = () => `dashboard-${Date.now()}-${Math.random().toString(36).slice(2)}`
const blockingReason = (reason) => {
  const known = ['active_store_not_configured', 'organization_active', 'reindex_active', 'embedding_migration_active', 'old_collection_cleanup_active', 'organization_model_not_configured', 'organization_model_invalid']
  return known.includes(reason) ? t(`memories.blocking_${reason}`) : (reason || t('memories.not_blocked'))
}
const blockingText = (state) => state?.blocked ? t('memories.blocked_with_reason', { reason: blockingReason(state.reason), job: state.job_id || '-' }) : t('memories.not_blocked')
const canMutateRecord = (row) => !row.pending_mutation_job_id && !row.deleted_at && row.is_active !== false
const canPin = (row) => canMutateRecord(row)
const jobError = (row) => row.error || row.result?.error || (row.context_error ? JSON.stringify(row.context_error) : '-')
const jobCountsText = (row) => {
  const counts = [
    [t('memories.keep_count'), row.keep_count],
    [t('memories.update_count'), row.update_count],
    [t('memories.merge_count'), row.merge_count],
    [t('memories.conflict_count'), row.conflict_count],
    [t('memories.stale_count'), row.stale_count],
    [t('memories.skipped_count'), row.skipped_count]
  ].filter(([, value]) => value !== null && value !== undefined)
  return counts.length ? counts.map(([label, value]) => `${label}: ${value}`).join(' / ') : '-'
}
const tokenBudgetText = (budget) => budget ? [
  `${t('memories.context_window_tokens')}: ${budget.context_window_tokens ?? '-'}`,
  `${t('memories.required_input_tokens')}: ${budget.required_input_tokens ?? '-'}`,
  `${t('memories.available_input_tokens')}: ${budget.available_input_tokens ?? '-'}`,
  `${t('memories.max_output_tokens')}: ${budget.max_output_tokens ?? budget.max_tokens ?? '-'}`,
  `${t('memories.required_output_tokens')}: ${budget.required_output_tokens ?? '-'}`
].join(' / ') : '-'

const applySettings = (data) => {
  const normalizedData = normalizeMemorySettings(data)
  Object.keys(settings).forEach(key => delete settings[key])
  Object.assign(settings, normalizedData)
}

const loadSettings = async (silent = false) => {
  const token = pollingTaskManager.begin('settings')
  if (!token) return
  const requestSeq = settingsRequestTracker.begin()
  if (pollingTaskManager.isCurrent(token)) settingsLoading.value = !silent
  try {
    const data = unwrap(await memoryApi.settings({ signal: token.signal }))
    if (!pollingTaskManager.isCurrent(token) || !settingsRequestTracker.isCurrent(requestSeq)) return
    applySettings(data)
  } catch (error) {
    if (token.signal.aborted || !pollingTaskManager.isCurrent(token)) return
    if (settingsRequestTracker.isCurrent(requestSeq) && !silent) ElMessage.error(error.message || t('memories.load_failed'))
  } finally {
    if (pollingTaskManager.isCurrent(token) && settingsRequestTracker.isCurrent(requestSeq)) settingsLoading.value = false
    pollingTaskManager.finish(token)
  }
}

const loadChannels = async () => {
  try {
    const allChannels = []
    let page = 1
    let total = 0
    while (true) {
      const data = pageData(await channelApi.list({ page, size: 100 }))
      allChannels.push(...data.items)
      total = data.total
      if (!data.items.length || allChannels.length >= total || data.items.length < 100) break
      page += 1
    }
    channels.value = allChannels
  } catch (error) { ElMessage.error(error.message || t('memories.load_failed')) }
}

const loadMemories = async (silent = false) => {
  const token = pollingTaskManager.begin('memories')
  if (!token) return
  const requestSeq = memoriesRequestTracker.begin()
  if (pollingTaskManager.isCurrent(token)) memoriesLoading.value = !silent
  try {
    const data = pageData(await memoryApi.list({ page: memoryPage.value, size: memoryPageSize.value, keyword: filters.keyword || undefined, memory_type: filters.memory_type || undefined, sort_by: filters.sort_by, sort_order: filters.sort_order }, { signal: token.signal }))
    if (!pollingTaskManager.isCurrent(token) || !memoriesRequestTracker.isCurrent(requestSeq)) return
    memories.value = data.items
    memoryTotal.value = data.total
  } catch (error) {
    if (token.signal.aborted || !pollingTaskManager.isCurrent(token)) return
    if (memoriesRequestTracker.isCurrent(requestSeq) && !silent) ElMessage.error(error.message || t('memories.load_failed'))
  } finally {
    if (pollingTaskManager.isCurrent(token) && memoriesRequestTracker.isCurrent(requestSeq)) memoriesLoading.value = false
    pollingTaskManager.finish(token)
  }
}

const loadJobs = async (silent = false) => {
  const token = pollingTaskManager.begin('jobs')
  if (!token) return
  const requestSeq = jobsRequestTracker.begin()
  if (pollingTaskManager.isCurrent(token)) jobsLoading.value = !silent
  try {
    const data = pageData(await memoryApi.jobs({ page: jobPage.value, size: jobPageSize.value, status: jobFilters.status || undefined, operation: jobFilters.operation || undefined, memory_id: jobFilters.memory_id || undefined }, { signal: token.signal }))
    if (!pollingTaskManager.isCurrent(token) || !jobsRequestTracker.isCurrent(requestSeq)) return
    jobs.value = decorateMemoryJobs(data.items)
    jobTotal.value = data.total
  } catch (error) {
    if (token.signal.aborted || !pollingTaskManager.isCurrent(token)) return
    if (jobsRequestTracker.isCurrent(requestSeq) && !silent) ElMessage.error(error.message || t('memories.operation_failed'))
  } finally {
    if (pollingTaskManager.isCurrent(token) && jobsRequestTracker.isCurrent(requestSeq)) jobsLoading.value = false
    pollingTaskManager.finish(token)
  }
}

const loadMigrations = async (silent = false) => {
  const token = pollingTaskManager.begin('migrations')
  if (!token) return
  const requestSeq = migrationsRequestTracker.begin()
  if (pollingTaskManager.isCurrent(token)) migrationsLoading.value = !silent
  try {
    const data = pageData(await memoryApi.migrations({ page: migrationPage.value, size: migrationPageSize.value }, { signal: token.signal }))
    if (!pollingTaskManager.isCurrent(token) || !migrationsRequestTracker.isCurrent(requestSeq)) return
    migrations.value = data.items
    migrationTotal.value = data.total
  } catch (error) {
    if (token.signal.aborted || !pollingTaskManager.isCurrent(token)) return
    if (migrationsRequestTracker.isCurrent(requestSeq) && !silent) ElMessage.error(error.message || t('memories.operation_failed'))
  } finally {
    if (pollingTaskManager.isCurrent(token) && migrationsRequestTracker.isCurrent(requestSeq)) migrationsLoading.value = false
    pollingTaskManager.finish(token)
  }
}

const resetAndLoadMemories = () => { memoryPage.value = 1; loadMemories() }
const resetAndLoadJobs = () => { jobPage.value = 1; loadJobs() }
const resetAndLoadMigrations = () => { migrationPage.value = 1; loadMigrations() }
const handleTabChange = (tab) => { if (tab === 'jobs') loadJobs(); if (tab === 'migrations') loadMigrations() }
const refreshAll = async () => {
  const requests = [loadSettings(true), loadMemories(true)]
  if (activeTab.value === 'jobs') requests.push(loadJobs(true))
  if (activeTab.value === 'migrations') requests.push(loadMigrations(true))
  await Promise.all(requests)
}
const scheduleRefresh = () => {
  if (pollingStopped) return
  pollTimer.value = window.setTimeout(async () => {
    pollTimer.value = null
    if (pollingStopped) return
    try {
      await refreshAll()
    } finally {
      if (!pollingStopped) scheduleRefresh()
    }
  }, 5000)
}

const organize = async () => {
  actionLoading.value = 'organize'
  try { await memoryApi.organize(buildOrganizePayload(newDedupeKey())); ElMessage.info(t('memories.organize_submitted')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } finally { actionLoading.value = '' }
}

const resetForm = () => Object.assign(form, { id: null, version: 0, memory_key: '', memory_type: 'fact', content: '', change_evidence: '', suppress_current: false })
const openEditor = (row = null) => { editorMode.value = row ? 'edit' : 'create'; resetForm(); if (row) Object.assign(form, { id: row.id, version: row.version, memory_key: row.memory_key || '', memory_type: row.memory_type || 'fact', content: row.content || '', change_evidence: row.change_evidence || '', suppress_current: false }); editorVisible.value = true }
const submitMemory = async () => {
  if (!form.memory_key.trim() || !form.content.trim()) return ElMessage.warning(t('memories.required'))
  if (contentTooLong.value) return ElMessage.warning(t('memories.token_limit_exceeded'))
  submitting.value = true
  try {
    const payload = { dedupe_key: newDedupeKey(), content: form.content, memory_key: form.memory_key, memory_type: form.memory_type, change_evidence: form.change_evidence || null }
    if (editorMode.value === 'create') await memoryApi.create(payload)
    else await memoryApi.update({ ...payload, memory_id: form.id, expected_version: form.version, suppress_current: form.suppress_current })
    ElMessage.info(t('memories.accepted_processing')); editorVisible.value = false; refreshAll()
  } catch (error) { ElMessage.error(error.message || t('memories.save_failed')) } finally { submitting.value = false }
}
const showDetails = async (row) => { try { selectedMemory.value = unwrap(await memoryApi.get(row.id)) || row; detailsVisible.value = true } catch (error) { ElMessage.error(error.message || t('memories.load_failed')) } }
const deleteMemory = async (row) => {
  try {
    await ElMessageBox.confirm(t('memories.delete_confirm'), t('common.warning'), { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') })
    await memoryApi.delete({ memory_id: row.id, expected_version: row.version, dedupe_key: newDedupeKey() }); ElMessage.info(t('memories.delete_success')); refreshAll()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) }
}
const togglePin = async (row) => { try { if (row.pinned) await memoryApi.unpin(row.id); else await memoryApi.pin(row.id); ElMessage.info(t('memories.operation_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const loadHistory = async (memoryId, memory) => {
  const requestSeq = historyRequestTracker.begin()
  selectedMemory.value = memory
  history.value = []
  historyVisible.value = true
  historyLoading.value = true
  try {
    const data = pageData(await memoryApi.history(memoryId, { page: 1, size: 100 }))
    if (!historyRequestTracker.isCurrent(requestSeq)) return
    history.value = data.items
  } catch (error) {
    if (historyRequestTracker.isCurrent(requestSeq)) ElMessage.error(error.message || t('memories.operation_failed'))
  } finally {
    if (historyRequestTracker.isCurrent(requestSeq)) historyLoading.value = false
  }
}
const showHistory = (row) => loadHistory(row.id, row)
const isRecordSnapshot = (value) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length > 0
const deletedRecordSnapshot = (row) => { const resultSnapshot = row?.result?.record_snapshot; if (isRecordSnapshot(resultSnapshot)) return resultSnapshot; const payloadSnapshot = row?.payload?.record_snapshot; return isRecordSnapshot(payloadSnapshot) ? payloadSnapshot : null }
const canShowDeletedHistory = (row) => row.operation === 'delete_cleanup' && Boolean(row.memory_id && deletedRecordSnapshot(row))
const showDeletedHistory = (row) => { const snapshot = deletedRecordSnapshot(row); if (!snapshot || !row.memory_id) return; loadHistory(row.memory_id, { ...snapshot, id: row.memory_id, memory_key: snapshot.memory_key || '', version: snapshot.version }) }
const resumeCurrent = async (row) => { try { await memoryApi.resumeCurrent(row.id, { expected_version: row.version }); ElMessage.info(t('memories.operation_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const canRetry = (row) => { if (row.operation === 'restore') return false; return row.operation === 'delete_cleanup' ? row.status === 'failed' : ['failed', 'cancelled'].includes(row.status) }
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

onMounted(async () => {
  await Promise.all([loadSettings(), loadChannels(), loadMemories()])
  if (!pollingStopped) scheduleRefresh()
})
onBeforeUnmount(() => {
  pollingStopped = true
  if (pollTimer.value) window.clearTimeout(pollTimer.value)
  pollingTaskManager.invalidate()
  settingsRequestTracker.invalidate()
  memoriesRequestTracker.invalidate()
  jobsRequestTracker.invalidate()
  migrationsRequestTracker.invalidate()
  historyRequestTracker.invalidate()
})
</script>

<style lang="scss">
@import "@/assets/css/MemoriesView.scss";
</style>
