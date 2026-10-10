<template>
  <div class="view-container memory-view">
    <section class="settings-panel">
      <div class="section-heading">
        <div class="section-heading-content">
          <h2>{{ $t('memories.title') }}</h2>
          <p>{{ $t('memories.page_description') }}</p>
        </div>
        <div class="heading-actions">
          <el-button type="primary" @click="openRuntimeDialog('organize')" :disabled="!memoryScopeReady || Boolean(actionLoading)">{{ $t('memories.organize_now') }}</el-button>
          <el-button @click="openRuntimeDialog('reindex')" :disabled="!memoryScopeReady || Boolean(actionLoading)">{{ $t('memories.reindex') }}</el-button>
          <el-button @click="openRuntimeDialog('status')" :disabled="!memoryScopeReady || Boolean(actionLoading)">{{ $t('memories.view_status') }}</el-button>
        </div>
      </div>
    </section>

    <div v-if="isSuperuser" class="memory-owner-filter">
      <span>{{ $t('memories.owner_filter') }}</span>
      <el-select
        v-model="ownerFilter"
        class="filter-input"
        filterable
        :loading="ownersLoading"
        :placeholder="$t('memories.owner_user')"
        :aria-label="$t('memories.owner_filter')"
        @change="handleOwnerChange">
        <el-option :label="$t('memories.all_users')" value="" />
        <el-option v-for="owner in owners" :key="owner.uid" :label="owner.username" :value="owner.uid" />
      </el-select>
    </div>

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
          <el-button @click="openEditor()" :disabled="!memoryScopeReady">{{ $t('memories.create') }}</el-button>
          <el-button @click="loadMemories">{{ $t('memories.refresh') }}</el-button>
        </div>
        <el-table :data="memories" v-loading="memoriesLoading" class="memory-table memory-data-table">
          <el-table-column prop="id" :label="$t('memories.memory_id')" width="88" align="center" />
          <el-table-column :label="$t('memories.owner_user')" min-width="130" show-overflow-tooltip><template #default="{ row }">{{ ownerLabel(row.owner_uid) }}</template></el-table-column>
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
                <el-button size="small" type="warning" @click="togglePin(row)" :disabled="!canPin(row)">{{ row.pinned ? $t('memories.unpin') : $t('memories.pin') }}</el-button>
                <el-button v-if="row.suppress_recall && !row.pending_mutation_job_id" size="small" type="warning" @click="resumeCurrent(row)">{{ $t('memories.resume_current') }}</el-button>
                <el-dropdown trigger="click" popper-class="memory-more-dropdown" @command="handleMemoryMoreAction($event, row)">
                  <el-button size="small">{{ $t('memories.more') }}</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="history">{{ $t('memories.history') }}</el-dropdown-item>
                      <el-dropdown-item command="delete" class="danger-dropdown-item" :disabled="!canMutateRecord(row)">{{ $t('memories.delete') }}</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
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
          <el-table-column :label="$t('memories.owner_user')" min-width="130" show-overflow-tooltip><template #default="{ row }">{{ ownerLabel(row.owner_uid) }}</template></el-table-column>
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
          <el-table-column :label="$t('memories.owner_user')" min-width="130" show-overflow-tooltip><template #default="{ row }">{{ ownerLabel(row.owner_uid) }}</template></el-table-column>
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

    <el-dialog
      :model-value="runtimeDialogVisible"
      @update:model-value="closeRuntimeDialog"
      :title="runtimeDialogTitle"
      :width="runtimeDialogAction === 'status' ? '1000px' : '560px'"
      class="standard-dialog dialog-with-scroll-body memory-runtime-dialog"
      align-center
      :close-on-click-modal="!actionLoading"
      :close-on-press-escape="!actionLoading"
      :show-close="!actionLoading">
      <el-form label-width="120px">
        <el-form-item :label="$t('memories.owner_user')">
          <el-select
            v-if="isSuperuser"
            :model-value="runtimeOwnerFilter"
            filterable
            clearable
            :loading="ownersLoading"
            :disabled="Boolean(actionLoading)"
            :placeholder="$t('memories.select_owner')"
            class="full-width-input"
            :aria-label="$t('memories.owner_user')"
            @change="handleRuntimeOwnerChange">
            <el-option v-for="owner in owners" :key="owner.uid" :label="owner.username" :value="owner.uid" />
          </el-select>
          <span v-else>{{ ownerLabel(runtimeOwnerUid) }}</span>
        </el-form-item>
      </el-form>

      <div class="memory-runtime-body" :aria-busy="settingsLoading">
        <el-alert v-if="!runtimeOwnerUid" type="info" :closable="false" show-icon :title="$t('memories.select_owner_for_action')" />
        <div v-if="settingsLoading && !settingsLoaded">
          <el-skeleton :animated="false" :rows="6" />
          <p class="help-text">{{ $t('memories.runtime_status_loading') }}</p>
        </div>
        <el-alert v-if="settingsLoadError" type="warning" :closable="false" show-icon>
          <template #title>{{ settingsLoadError }}</template>
        </el-alert>
        <p v-if="runtimeDialogAction === 'organize' && runtimeOwnerUid" class="help-text">{{ $t('memories.organize_scope', { owner: ownerLabel(runtimeOwnerUid) }) }}</p>
        <p v-if="runtimeDialogAction === 'reindex' && runtimeOwnerUid" class="help-text">{{ $t('memories.reindex_scope', { owner: ownerLabel(runtimeOwnerUid) }) }}</p>

        <div v-if="settingsLoaded && currentMemoryTask" class="memory-task-summary">
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

        <el-descriptions v-if="settingsLoaded && ['organize', 'reindex'].includes(runtimeDialogAction)" :column="1" border>
          <el-descriptions-item :label="$t('memories.active_record_count')">{{ activeRecordCount }}</el-descriptions-item>
          <el-descriptions-item :label="$t('memories.index_status')"><StatusTag :status="settings.index?.status || setting('index_status')" :active-text="statusText(settings.index?.status || setting('index_status'))" :inactive-text="statusText(settings.index?.status || setting('index_status'))" :active-type="statusType(settings.index?.status || setting('index_status'))" :inactive-type="statusType(settings.index?.status || setting('index_status'))" /></el-descriptions-item>
        </el-descriptions>
        <el-alert v-if="runtimeBlockingMessage" type="warning" :closable="false" show-icon :title="runtimeBlockingMessage" />

        <div v-if="settingsLoaded && runtimeDialogAction === 'status'" class="settings-content">
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
      </div>
      <template #footer>
        <el-button @click="loadSettings()" :loading="settingsLoading" :disabled="!runtimeOwnerUid || Boolean(actionLoading)">{{ $t('memories.refresh') }}</el-button>
        <el-button
          v-if="runtimeDialogAction === 'status' && settingsLoaded && cleanupRetryId"
          type="danger"
          @click="retryCleanup(cleanupRetryId)"
          :loading="actionLoading === `cleanup-${cleanupRetryId}`"
          :disabled="!settingsLoaded || settingsLoading || Boolean(actionLoading)">
          {{ $t('memories.cleanup_retry') }}
        </el-button>
        <el-button @click="closeRuntimeDialog" :disabled="Boolean(actionLoading)">{{ runtimeDialogAction === 'status' ? $t('memories.close') : $t('memories.cancel') }}</el-button>
        <el-button
          v-if="runtimeDialogAction !== 'status'"
          type="primary"
          :loading="actionLoading === runtimeDialogAction"
          :disabled="runtimeDialogAction === 'organize' ? organizeBlocked : reindexBlocked"
          @click="submitRuntimeOperation(runtimeDialogAction)">
          {{ $t(runtimeDialogAction === 'organize' ? 'memories.confirm_organize' : 'memories.confirm_reindex') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editorVisible" :title="editorMode === 'create' ? $t('memories.form_create_title') : $t('memories.form_edit_title')" width="720px" class="standard-dialog" align-center>
      <el-form :model="form" label-width="120px">
        <el-form-item :label="$t('memories.owner_user')">
          <el-select v-if="isSuperuser && editorMode === 'create'" v-model="form.owner_uid" filterable clearable :loading="ownersLoading" :placeholder="$t('memories.select_owner')" class="full-width-input">
            <el-option v-for="owner in owners" :key="owner.uid" :label="owner.username" :value="owner.uid" />
          </el-select>
          <span v-else>{{ ownerLabel(form.owner_uid) }}</span>
        </el-form-item>
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
      <el-descriptions v-if="selectedMemory" :column="2" border><el-descriptions-item :label="$t('memories.memory_id')">{{ selectedMemory.id }}</el-descriptions-item><el-descriptions-item :label="$t('memories.owner_user')">{{ ownerLabel(selectedMemory.owner_uid) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.version')">{{ selectedMemory.version }}</el-descriptions-item><el-descriptions-item :label="$t('memories.memory_key')">{{ selectedMemory.memory_key }}</el-descriptions-item><el-descriptions-item :label="$t('memories.type')">{{ typeLabel(selectedMemory.memory_type) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.source')">{{ sourceLabel(selectedMemory.source) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.token_count')">{{ selectedMemory.content_token_count ?? '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.pinned')">{{ selectedMemory.pinned ? $t('memories.pinned_yes') : $t('memories.pinned_no') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.last_recalled_at')">{{ formatTime(selectedMemory.last_recalled_at) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.current_status')">{{ recordStatus(selectedMemory) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.content')" :span="2"><pre class="memory-content">{{ selectedMemory.content || '-' }}</pre></el-descriptions-item><el-descriptions-item :label="$t('memories.change_evidence')" :span="2"><pre class="memory-content">{{ selectedMemory.change_evidence || '-' }}</pre></el-descriptions-item></el-descriptions>
      <template #footer>
        <el-button @click="detailsVisible = false">{{ $t('memories.close') }}</el-button>
        <el-button type="primary" @click="editSelectedMemory" :disabled="!selectedMemory || !canMutateRecord(selectedMemory)">{{ $t('memories.edit') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="historyVisible" :title="$t('memories.history_title', { key: selectedHistoryMemory?.memory_key || '' })" width="900px" class="standard-dialog" align-center>
      <p class="help-text">{{ $t('memories.owner_user') }}: {{ ownerLabel(selectedHistoryMemory?.owner_uid) }}</p>
      <el-alert type="info" :closable="false" show-icon :title="$t('memories.deleted_history_read_only')" />
      <el-table :data="history" v-loading="historyLoading"><el-table-column prop="version" :label="$t('memories.revision_version')" width="100" align="center" /><el-table-column prop="memory_type" :label="$t('memories.type')" width="120"><template #default="{ row }">{{ typeLabel(row.memory_type) }}</template></el-table-column><el-table-column prop="content_token_count" :label="$t('memories.token_count')" width="100" align="center" /><el-table-column prop="content" :label="$t('memories.content')" min-width="350" show-overflow-tooltip /><el-table-column prop="published_at" :label="$t('memories.published_at')" width="180"><template #default="{ row }">{{ formatTime(row.published_at || row.created_at) }}</template></el-table-column></el-table>
      <el-empty v-if="!historyLoading && !history.length" :description="$t('memories.no_history')" />
    </el-dialog>

    <el-dialog v-model="jobVisible" :title="$t('memories.jobs')" width="820px" class="standard-dialog" align-center><el-descriptions v-if="selectedJob" :column="2" border><el-descriptions-item :label="$t('memories.job_id')">{{ selectedJob.id }}</el-descriptions-item><el-descriptions-item :label="$t('memories.owner_user')">{{ ownerLabel(selectedJob.owner_uid) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.operation')">{{ operationLabel(selectedJob.operation) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.status')">{{ statusText(selectedJob.status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.memory_id')">{{ selectedJob.memory_id || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.parent_job_id')">{{ selectedJob.parent_job_id || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.children')">{{ selectedJob.child_job_ids?.join(', ') || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.snapshot_count')">{{ selectedJob.snapshot_count ?? '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.attempt')">{{ selectedJob.attempt_count }} / {{ selectedJob.max_attempts }}</el-descriptions-item><el-descriptions-item :label="$t('memories.organization_counts')" :span="2">{{ jobCountsText(selectedJob) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.token_budget')" :span="2">{{ tokenBudgetText(selectedJob.token_budget) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.context_error')" :span="2">{{ selectedJob.context_error ? JSON.stringify(selectedJob.context_error) : '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.error')" :span="2">{{ jobError(selectedJob) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.payload')" :span="2"><pre class="memory-content">{{ JSON.stringify(selectedJob.payload || {}, null, 2) }}</pre></el-descriptions-item><el-descriptions-item :label="$t('memories.result')" :span="2"><pre class="memory-content">{{ JSON.stringify(selectedJob.result || {}, null, 2) }}</pre></el-descriptions-item></el-descriptions><template #footer><el-button @click="jobVisible = false">{{ $t('memories.close') }}</el-button></template></el-dialog>

    <el-dialog v-model="migrationVisible" :title="$t('memories.migration_detail')" width="780px" class="standard-dialog" align-center><el-descriptions v-if="selectedMigration" :column="2" border><el-descriptions-item :label="$t('memories.migration_job')">{{ migrationId(selectedMigration) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.owner_user')">{{ ownerLabel(selectedMigration.owner_uid) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.status')">{{ statusText(selectedMigration.status || selectedMigration.migration_status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.target')" :span="2">{{ migrationTarget(selectedMigration) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.snapshot')">{{ migrationProgress(selectedMigration, 'migration_cursor') }} / {{ migrationProgress(selectedMigration, 'migration_snapshot_boundary') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.delta')">{{ migrationProgress(selectedMigration, 'migration_delta_applied_watermark') }} / {{ migrationProgress(selectedMigration, 'migration_delta_high_watermark') }}</el-descriptions-item><el-descriptions-item :label="$t('memories.error')" :span="2">{{ selectedMigration.error || selectedMigration.migration_error || '-' }}</el-descriptions-item><el-descriptions-item :label="$t('memories.cleanup_status')">{{ statusText(selectedMigration.old_collection_cleanup_status) }}</el-descriptions-item><el-descriptions-item :label="$t('memories.collection')">{{ selectedMigration.old_collection_name || '-' }}</el-descriptions-item></el-descriptions><template #footer><el-button @click="migrationVisible = false">{{ $t('memories.close') }}</el-button></template></el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { adminApi, channelApi, memoryApi } from '../api'
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
const settings = reactive({})
const runtimeDialogVisible = ref(false)
const runtimeDialogAction = ref('status')
const runtimeOwnerFilter = ref('')
const settingsLoaded = ref(false)
const settingsLoadError = ref('')
const isSuperuser = ref(false)
const currentUid = ref(null)
const currentUsername = ref('')
const owners = ref([])
const ownersLoading = ref(false)
const ownersLoaded = ref(false)
const memoryScopeReady = ref(false)
const ownerFilter = ref('')
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
const selectedHistoryMemory = ref(null)
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
const runtimeActionRequestTracker = createLatestRequestTracker()
const memoriesRequestTracker = createLatestRequestTracker()
const jobsRequestTracker = createLatestRequestTracker()
const migrationsRequestTracker = createLatestRequestTracker()
const historyRequestTracker = createLatestRequestTracker()
const detailsRequestTracker = createLatestRequestTracker()
const jobDetailsRequestTracker = createLatestRequestTracker()
const migrationDetailsRequestTracker = createLatestRequestTracker()
const editorRequestTracker = createLatestRequestTracker()
const filters = reactive({ keyword: '', memory_type: '', sort_by: 'updated_at', sort_order: 'desc' })
const jobFilters = reactive({ status: '', operation: '', memory_id: '' })
const form = reactive({ id: null, version: 0, owner_uid: '', memory_key: '', memory_type: 'fact', content: '', change_evidence: '', suppress_current: false })

const unwrap = (response) => response?.data?.data ?? response?.data ?? {}
const pageData = (response) => {
  const data = unwrap(response)
  if (Array.isArray(data)) return { items: data, total: data.length, meta: null }
  return { items: data.items || [], total: Number(data.total || 0), meta: data.meta ?? null }
}
const formatTime = (value) => value ? new Date(value).toLocaleString() : '-'
const setting = (key) => settings.store?.[key] ?? settings[key] ?? '-'
const nestedSetting = (section, key, legacyKey) => settings[section]?.[key] ?? setting(legacyKey)
const channelName = (channelId) => {
  if (channelId === null || channelId === undefined || channelId === '' || channelId === '-') return '-'
  const channel = channels.value.find(item => String(item.id) === String(channelId))
  return typeof channel?.name === 'string' && channel.name.trim() ? channel.name : '-'
}
const ownerLabel = (uid) => {
  if (typeof uid !== 'string' || !uid.trim()) return t('memories.owner_unknown')
  const normalizedUid = uid.trim()
  const owner = owners.value.find(item => String(item.uid) === normalizedUid)
  const ownerUsername = typeof owner?.username === 'string' ? owner.username.trim() : ''
  if (ownerUsername) return ownerUsername
  if (String(currentUid.value || '') === normalizedUid) {
    const currentUsernameValue = typeof currentUsername.value === 'string' ? currentUsername.value.trim() : ''
    if (currentUsernameValue) return currentUsernameValue
  }
  return normalizedUid
}
const runtimeOwnerUid = computed(() => {
  const ownerUid = isSuperuser.value ? runtimeOwnerFilter.value : currentUid.value
  if (typeof ownerUid === 'string') return ownerUid.trim() || null
  return ownerUid ?? null
})
const runtimeDialogTitle = computed(() => {
  if (runtimeDialogAction.value === 'organize') return t('memories.organize_now')
  if (runtimeDialogAction.value === 'reindex') return t('memories.reindex')
  return t('memories.view_status')
})
const numericSetting = (key, fallback) => {
  const value = Number(setting(key))
  return Number.isFinite(value) ? value : fallback
}
const configured = computed(() => settings.configured !== undefined ? Boolean(settings.configured) : Boolean(setting('active_embedding_channel_id') !== '-' && setting('active_embedding_model_id') !== '-' && setting('active_collection_name') !== '-'))
const contentMaxTokens = computed(() => settings.contentMaxTokens ?? Number(settings.capacity?.content_max_tokens ?? numericSetting('content_max_tokens', 160)))
const activeRecordCount = computed(() => settings.activeRecordCount ?? Number(settings.capacity?.active_record_count ?? numericSetting('active_record_count', 0)))
const maxActiveRecords = computed(() => settings.maxActiveRecords ?? Number(settings.capacity?.max_active_records ?? numericSetting('max_active_records', 50)))
const capacityOverLimit = computed(() => ['over_limit', 'full'].includes(settings.capacity?.status) || activeRecordCount.value > maxActiveRecords.value)
const organizeBlocked = computed(() => !runtimeDialogVisible.value || !memoryScopeReady.value || !settingsLoaded.value || settingsLoading.value || !runtimeOwnerUid.value || !configured.value || Boolean(actionLoading.value) || Boolean(settings.blocking?.organize?.blocked))
const reindexBlocked = computed(() => !runtimeDialogVisible.value || !memoryScopeReady.value || !settingsLoaded.value || settingsLoading.value || !runtimeOwnerUid.value || !configured.value || Boolean(actionLoading.value) || Boolean(settings.blocking?.maintenance?.blocked))
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
const runtimeBlockingMessage = computed(() => {
  if (!runtimeDialogVisible.value || !settingsLoaded.value || runtimeDialogAction.value === 'status') return ''
  if (!configured.value) return t('memories.no_config')
  const blocking = runtimeDialogAction.value === 'organize'
    ? settings.blocking?.organize
    : runtimeDialogAction.value === 'reindex'
      ? settings.blocking?.maintenance
      : null
  return blocking?.blocked ? blockingText(blocking) : ''
})
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

const resetRuntimeSettings = () => {
  pollingTaskManager.cancel('settings')
  settingsRequestTracker.invalidate()
  Object.keys(settings).forEach(key => delete settings[key])
  settingsLoaded.value = false
  settingsLoading.value = false
  settingsLoadError.value = ''
}

const loadSettings = async (silent = false) => {
  if (!runtimeDialogVisible.value || !runtimeOwnerUid.value || pollingStopped || actionLoading.value) return
  const token = pollingTaskManager.begin('settings')
  if (!token) return
  const requestSeq = settingsRequestTracker.begin()
  const ownerUid = runtimeOwnerUid.value
  if (pollingTaskManager.isCurrent(token)) settingsLoading.value = !silent || !settingsLoaded.value
  try {
    const data = unwrap(await memoryApi.settings({ signal: token.signal, params: { uid: ownerUid } }))
    if (!pollingTaskManager.isCurrent(token) || !settingsRequestTracker.isCurrent(requestSeq) || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || pollingStopped) return
    applySettings(data)
    settingsLoaded.value = true
    settingsLoadError.value = ''
  } catch (error) {
    if (token.signal.aborted || !pollingTaskManager.isCurrent(token) || !settingsRequestTracker.isCurrent(requestSeq) || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || pollingStopped) return
    Object.keys(settings).forEach(key => delete settings[key])
    settingsLoaded.value = false
    settingsLoadError.value = error?.message || t('memories.runtime_status_load_failed')
  } finally {
    if (pollingTaskManager.isCurrent(token) && settingsRequestTracker.isCurrent(requestSeq)) settingsLoading.value = false
    pollingTaskManager.finish(token)
  }
}

const openRuntimeDialog = (action) => {
  if (!['organize', 'reindex', 'status'].includes(action) || pollingStopped || !memoryScopeReady.value || actionLoading.value || runtimeDialogVisible.value || editorVisible.value || detailsVisible.value || historyVisible.value || jobVisible.value || migrationVisible.value) return
  detailsRequestTracker.invalidate()
  jobDetailsRequestTracker.invalidate()
  migrationDetailsRequestTracker.invalidate()
  historyRequestTracker.invalidate()
  resetRuntimeSettings()
  runtimeDialogAction.value = action
  runtimeOwnerFilter.value = isSuperuser.value
    ? (typeof ownerFilter.value === 'string' ? ownerFilter.value.trim() : ownerFilter.value || '')
    : (typeof currentUid.value === 'string' ? currentUid.value.trim() : currentUid.value || '')
  runtimeDialogVisible.value = true
  if (isSuperuser.value && !ownersLoaded.value && !ownersLoading.value) loadOwners()
  loadSettings()
}

const closeRuntimeDialog = () => {
  if (actionLoading.value) return
  runtimeActionRequestTracker.invalidate()
  runtimeDialogVisible.value = false
  runtimeOwnerFilter.value = ''
  resetRuntimeSettings()
}

const handleRuntimeOwnerChange = (uid) => {
  if (!runtimeDialogVisible.value || actionLoading.value || pollingStopped) return
  runtimeOwnerFilter.value = isSuperuser.value
    ? (typeof uid === 'string' ? uid.trim() : uid || '')
    : (typeof currentUid.value === 'string' ? currentUid.value.trim() : currentUid.value || '')
  resetRuntimeSettings()
  loadSettings()
}

const loadOwners = async () => {
  if (!isSuperuser.value || ownersLoading.value) return
  const token = pollingTaskManager.begin('owners')
  if (!token) return
  if (pollingTaskManager.isCurrent(token)) ownersLoading.value = true
  try {
    const allOwners = []
    let page = 1
    let total = 0
    while (true) {
      if (!pollingTaskManager.isCurrent(token) || !isSuperuser.value) return
      const data = pageData(await adminApi.userList({ page, size: 100 }))
      if (!pollingTaskManager.isCurrent(token) || !isSuperuser.value) return
      allOwners.push(...data.items)
      total = data.total
      if (!data.items.length || allOwners.length >= total || data.items.length < 100) break
      page += 1
    }
    if (!pollingTaskManager.isCurrent(token) || !isSuperuser.value) return
    owners.value = allOwners
    ownersLoaded.value = true
  } catch (error) {
    if (token.signal.aborted || !pollingTaskManager.isCurrent(token) || !isSuperuser.value) return
    ElMessage.error(error.message || t('memories.load_failed'))
  } finally {
    if (pollingTaskManager.isCurrent(token)) ownersLoading.value = false
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
    const data = pageData(await memoryApi.list({ page: memoryPage.value, size: memoryPageSize.value, keyword: filters.keyword || undefined, memory_type: filters.memory_type || undefined, sort_by: filters.sort_by, sort_order: filters.sort_order, uid: isSuperuser.value ? ownerFilter.value || undefined : undefined }, { signal: token.signal }))
    if (!pollingTaskManager.isCurrent(token) || !memoriesRequestTracker.isCurrent(requestSeq)) return
    const meta = data.meta || {}
    isSuperuser.value = Boolean(meta.is_superuser)
    currentUid.value = meta.current_uid ?? null
    currentUsername.value = meta.current_username ?? ''
    memoryScopeReady.value = true
    if (isSuperuser.value) {
      if (!ownersLoaded.value && !ownersLoading.value) loadOwners()
    } else {
      owners.value = []
      ownerFilter.value = ''
      ownersLoaded.value = false
    }
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
    const data = pageData(await memoryApi.jobs({ page: jobPage.value, size: jobPageSize.value, status: jobFilters.status || undefined, operation: jobFilters.operation || undefined, memory_id: jobFilters.memory_id || undefined, uid: isSuperuser.value ? ownerFilter.value || undefined : undefined }, { signal: token.signal }))
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
    const data = pageData(await memoryApi.migrations({ page: migrationPage.value, size: migrationPageSize.value, uid: isSuperuser.value ? ownerFilter.value || undefined : undefined }, { signal: token.signal }))
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
  const requests = [loadMemories(true)]
  if (runtimeDialogVisible.value) requests.push(loadSettings(true))
  if (activeTab.value === 'jobs') requests.push(loadJobs(true))
  if (activeTab.value === 'migrations') requests.push(loadMigrations(true))
  await Promise.all(requests)
}
const handleOwnerChange = () => {
  pollingTaskManager.cancel('memories')
  pollingTaskManager.cancel('jobs')
  pollingTaskManager.cancel('migrations')
  memoriesRequestTracker.invalidate()
  jobsRequestTracker.invalidate()
  migrationsRequestTracker.invalidate()
  historyRequestTracker.invalidate()
  detailsRequestTracker.invalidate()
  jobDetailsRequestTracker.invalidate()
  migrationDetailsRequestTracker.invalidate()
  editorRequestTracker.invalidate()
  editorVisible.value = false
  detailsVisible.value = false
  historyVisible.value = false
  jobVisible.value = false
  migrationVisible.value = false
  selectedMemory.value = null
  selectedJob.value = null
  selectedMigration.value = null
  selectedHistoryMemory.value = null
  history.value = []
  memories.value = []
  memoryTotal.value = 0
  jobs.value = []
  jobTotal.value = 0
  migrations.value = []
  migrationTotal.value = 0
  memoryPage.value = 1
  jobPage.value = 1
  migrationPage.value = 1
  memoriesLoading.value = false
  jobsLoading.value = false
  migrationsLoading.value = false
  historyLoading.value = false
  submitting.value = false
  refreshAll()
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

const submitRuntimeOperation = async (operation) => {
  const blocked = operation === 'organize' ? organizeBlocked.value : operation === 'reindex' ? reindexBlocked.value : true
  if (!['organize', 'reindex'].includes(operation) || pollingStopped || !runtimeDialogVisible.value || runtimeDialogAction.value !== operation || !memoryScopeReady.value || blocked) return
  const ownerUid = runtimeOwnerUid.value
  const owner = ownerLabel(ownerUid)
  const dedupeKey = newDedupeKey()
  pollingTaskManager.cancel('settings')
  settingsRequestTracker.invalidate()
  const requestSeq = runtimeActionRequestTracker.begin()
  actionLoading.value = operation
  let failed = false
  try {
    if (operation === 'organize') await memoryApi.organize(buildOrganizePayload(dedupeKey), { params: { uid: ownerUid } })
    else await memoryApi.reindex({ dedupe_key: dedupeKey }, { params: { uid: ownerUid } })
    if (!runtimeActionRequestTracker.isCurrent(requestSeq) || pollingStopped || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || runtimeDialogAction.value !== operation) return
    ElMessage.info(t('memories.runtime_operation_submitted', { owner, operation: operationLabel(operation) }))
    actionLoading.value = ''
    closeRuntimeDialog()
    refreshAll()
  } catch (error) {
    if (!runtimeActionRequestTracker.isCurrent(requestSeq) || pollingStopped || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || runtimeDialogAction.value !== operation) return
    failed = true
    ElMessage.error(error.message || t('memories.operation_failed'))
  } finally {
    if (!runtimeActionRequestTracker.isCurrent(requestSeq)) return
    actionLoading.value = ''
    if (failed && !pollingStopped && runtimeDialogVisible.value && runtimeOwnerUid.value === ownerUid && runtimeDialogAction.value === operation) loadSettings()
  }
}
const resetForm = () => Object.assign(form, { id: null, version: 0, owner_uid: '', memory_key: '', memory_type: 'fact', content: '', change_evidence: '', suppress_current: false })
const openEditor = (row = null) => {
  editorRequestTracker.invalidate()
  submitting.value = false
  editorMode.value = row ? 'edit' : 'create'
  resetForm()
  if (row) {
    Object.assign(form, { id: row.id, version: row.version, owner_uid: row.owner_uid, memory_key: row.memory_key || '', memory_type: row.memory_type || 'fact', content: row.content || '', change_evidence: row.change_evidence || '', suppress_current: false })
  } else {
    form.owner_uid = isSuperuser.value ? (ownerFilter.value || currentUid.value || '') : (currentUid.value || '')
  }
  editorVisible.value = true
}
const editSelectedMemory = () => {
  const row = selectedMemory.value
  if (!detailsVisible.value || !row || !canMutateRecord(row)) return
  detailsRequestTracker.invalidate()
  detailsVisible.value = false
  openEditor(row)
}
const submitMemory = async () => {
  if (submitting.value || !editorVisible.value || !memoryScopeReady.value || pollingStopped) return
  if (editorMode.value === 'create' && isSuperuser.value && !form.owner_uid) return ElMessage.warning(t('memories.select_owner'))
  if (!form.memory_key.trim() || !form.content.trim()) return ElMessage.warning(t('memories.required'))
  if (contentTooLong.value) return ElMessage.warning(t('memories.token_limit_exceeded'))
  const requestSeq = editorRequestTracker.begin()
  submitting.value = true
  try {
    const payload = { dedupe_key: newDedupeKey(), content: form.content, memory_key: form.memory_key, memory_type: form.memory_type, change_evidence: form.change_evidence || null }
    if (editorMode.value === 'create') await memoryApi.create(payload, { params: { uid: isSuperuser.value ? form.owner_uid : currentUid.value } })
    else await memoryApi.update({ ...payload, memory_id: form.id, expected_version: form.version, suppress_current: form.suppress_current })
    if (editorRequestTracker.isCurrent(requestSeq) && editorVisible.value) {
      ElMessage.info(t('memories.accepted_processing')); editorVisible.value = false; refreshAll()
    }
  } catch (error) {
    if (editorRequestTracker.isCurrent(requestSeq) && editorVisible.value) ElMessage.error(error.message || t('memories.save_failed'))
  } finally {
    if (editorRequestTracker.isCurrent(requestSeq)) submitting.value = false
  }
}
const showDetails = async (row) => {
  const requestSeq = detailsRequestTracker.begin()
  try {
    const selected = unwrap(await memoryApi.get(row.id)) || row
    if (!detailsRequestTracker.isCurrent(requestSeq)) return
    selectedMemory.value = selected
    detailsVisible.value = true
  } catch (error) {
    if (detailsRequestTracker.isCurrent(requestSeq)) ElMessage.error(error.message || t('memories.load_failed'))
  }
}
const deleteMemory = async (row) => {
  try {
    await ElMessageBox.confirm(t('memories.delete_confirm'), t('common.warning'), { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') })
    await memoryApi.delete({ memory_id: row.id, expected_version: row.version, dedupe_key: newDedupeKey() }); ElMessage.info(t('memories.delete_success')); refreshAll()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) }
}
const togglePin = async (row) => { try { if (row.pinned) await memoryApi.unpin(row.id); else await memoryApi.pin(row.id); ElMessage.info(t('memories.operation_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const loadHistory = async (memoryId, memory) => {
  const requestSeq = historyRequestTracker.begin()
  selectedHistoryMemory.value = memory
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
const handleMemoryMoreAction = (command, row) => {
  if (command === 'history') {
    showHistory(row)
    return
  }
  if (command === 'delete' && canMutateRecord(row)) deleteMemory(row)
}
const isRecordSnapshot = (value) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length > 0
const deletedRecordSnapshot = (row) => { const resultSnapshot = row?.result?.record_snapshot; if (isRecordSnapshot(resultSnapshot)) return resultSnapshot; const payloadSnapshot = row?.payload?.record_snapshot; return isRecordSnapshot(payloadSnapshot) ? payloadSnapshot : null }
const canShowDeletedHistory = (row) => row.operation === 'delete_cleanup' && Boolean(row.memory_id && deletedRecordSnapshot(row))
const showDeletedHistory = (row) => { const snapshot = deletedRecordSnapshot(row); if (!snapshot || !row.memory_id) return; loadHistory(row.memory_id, { ...snapshot, id: row.memory_id, memory_key: snapshot.memory_key || '', version: snapshot.version, owner_uid: row.owner_uid }) }
const resumeCurrent = async (row) => { try { await memoryApi.resumeCurrent(row.id, { expected_version: row.version }); ElMessage.info(t('memories.operation_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const canRetry = (row) => { if (row.operation === 'restore') return false; return row.operation === 'delete_cleanup' ? row.status === 'failed' : ['failed', 'cancelled'].includes(row.status) }
const canCancel = (row) => !['succeeded', 'failed', 'cancelled'].includes(row.status) && row.operation !== 'delete_cleanup'
const retryJob = async (row) => { try { await ElMessageBox.confirm(t('memories.retry_confirm'), t('common.warning'), { type: 'warning' }); await memoryApi.retryJob(row.id); ElMessage.info(t('memories.retry_success')); refreshAll() } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) } }
const cancelJob = async (row) => { try { await ElMessageBox.confirm(t('memories.cancel_confirm'), t('common.warning'), { type: 'warning' }); await memoryApi.cancelJob(row.id); ElMessage.info(t('memories.cancel_success')); refreshAll() } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed')) } }
const showJob = async (row) => {
  const requestSeq = jobDetailsRequestTracker.begin()
  try {
    const selected = unwrap(await memoryApi.job(row.id)) || row
    if (!jobDetailsRequestTracker.isCurrent(requestSeq)) return
    selectedJob.value = selected
    jobVisible.value = true
  } catch (error) {
    if (jobDetailsRequestTracker.isCurrent(requestSeq)) ElMessage.error(error.message || t('memories.operation_failed'))
  }
}
const canRetryMigration = (row) => ['failed', 'cancelled'].includes(row.status || row.migration_status)
const canCancelMigration = (row) => ['preparing', 'building', 'catching_up', 'validating'].includes(row.status || row.migration_status)
const retryMigration = async (row) => { try { await memoryApi.retryMigration(migrationId(row)); ElMessage.info(t('memories.migration_retry_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const cancelMigration = async (row) => { try { await memoryApi.cancelMigration(migrationId(row)); ElMessage.info(t('memories.migration_cancel_success')); refreshAll() } catch (error) { ElMessage.error(error.message || t('memories.operation_failed')) } }
const retryCleanup = async (id) => {
  if (!id || actionLoading.value || pollingStopped) return
  pollingTaskManager.cancel('settings')
  settingsRequestTracker.invalidate()
  const requestSeq = runtimeActionRequestTracker.begin()
  actionLoading.value = `cleanup-${id}`
  try {
    await memoryApi.retryCleanup(id)
    if (!runtimeActionRequestTracker.isCurrent(requestSeq) || pollingStopped) return
    ElMessage.info(t('memories.retry_success'))
  } catch (error) {
    if (!runtimeActionRequestTracker.isCurrent(requestSeq) || pollingStopped) return
    ElMessage.error(error.message || t('memories.operation_failed'))
  } finally {
    if (!runtimeActionRequestTracker.isCurrent(requestSeq) || pollingStopped) return
    actionLoading.value = ''
    loadSettings()
    refreshAll()
  }
}
const showMigration = async (row) => {
  const requestSeq = migrationDetailsRequestTracker.begin()
  try {
    const selected = unwrap(await memoryApi.migration(migrationId(row))) || row
    if (!migrationDetailsRequestTracker.isCurrent(requestSeq)) return
    selectedMigration.value = selected
    migrationVisible.value = true
  } catch (error) {
    if (migrationDetailsRequestTracker.isCurrent(requestSeq)) ElMessage.error(error.message || t('memories.operation_failed'))
  }
}

onMounted(async () => {
  await Promise.all([loadMemories(), loadChannels()])
  if (pollingStopped) return
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
  detailsRequestTracker.invalidate()
  jobDetailsRequestTracker.invalidate()
  migrationDetailsRequestTracker.invalidate()
  editorRequestTracker.invalidate()
  runtimeActionRequestTracker.invalidate()
})
</script>

<style lang="scss">
@import "@/assets/css/MemoriesView.scss";
</style>
