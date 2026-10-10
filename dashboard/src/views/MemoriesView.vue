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

      <div
        class="memory-runtime-body"
        :aria-busy="settingsLoading"
        :class="{ 'is-loading': settingsLoading && !settingsLoaded }"
        v-loading="settingsLoading && !settingsLoaded"
        :element-loading-text="$t('memories.runtime_status_loading')">
        <el-alert v-if="!runtimeOwnerUid" type="info" :closable="false" show-icon :title="$t('memories.select_owner_for_action')" />
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

        <el-descriptions v-if="runtimeOwnerUid && !settingsLoadError && ['organize', 'reindex'].includes(runtimeDialogAction)" :column="1" border>
          <el-descriptions-item :label="$t('memories.active_record_count')">{{ activeRecordCount }}</el-descriptions-item>
          <el-descriptions-item :label="$t('memories.index_status')"><StatusTag :status="settings.index?.status || setting('index_status')" :active-text="statusText(settings.index?.status || setting('index_status'))" :inactive-text="statusText(settings.index?.status || setting('index_status'))" :active-type="statusType(settings.index?.status || setting('index_status'))" :inactive-type="statusType(settings.index?.status || setting('index_status'))" /></el-descriptions-item>
        </el-descriptions>
        <el-alert v-if="runtimeBlockingMessage" type="warning" :closable="false" show-icon :title="runtimeBlockingMessage" />

        <div v-if="runtimeOwnerUid && !settingsLoadError && runtimeDialogAction === 'status'" class="settings-content">
          <el-alert v-if="settingsLoaded && !configured" type="info" :closable="false" show-icon :title="$t('memories.no_config')" />
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
import StatusTag from '../components/StatusTag.vue'
import { useMemoriesView } from '../composables/memories/useMemoriesView.js'

const {
  memoryTypes,
  jobStatuses,
  jobOperations,
  activeTab,
  isSuperuser,
  owners,
  ownersLoading,
  memoryScopeReady,
  ownerFilter,
  memories,
  memoriesLoading,
  memoryPage,
  memoryPageSize,
  memoryTotal,
  jobs,
  jobsLoading,
  jobPage,
  jobPageSize,
  jobTotal,
  migrations,
  migrationsLoading,
  migrationPage,
  migrationPageSize,
  migrationTotal,
  filters,
  jobFilters,
  formatTime,
  channelName,
  ownerLabel,
  typeLabel,
  sourceLabel,
  statusText,
  operationLabel,
  statusType,
  loadMemories,
  loadJobs,
  loadMigrations,
  resetAndLoadMemories,
  resetAndLoadJobs,
  resetAndLoadMigrations,
  handleTabChange,
  handleOwnerChange,
  settings,
  runtimeDialogVisible,
  runtimeDialogAction,
  runtimeOwnerFilter,
  settingsLoaded,
  settingsLoadError,
  settingsLoading,
  actionLoading,
  setting,
  nestedSetting,
  runtimeOwnerUid,
  runtimeDialogTitle,
  configured,
  contentMaxTokens,
  activeRecordCount,
  capacityOverLimit,
  organizeBlocked,
  reindexBlocked,
  cleanupRetryId,
  migrationPercentage,
  settingsError,
  currentMemoryTask,
  blockingText,
  runtimeBlockingMessage,
  loadSettings,
  openRuntimeDialog,
  closeRuntimeDialog,
  handleRuntimeOwnerChange,
  submitRuntimeOperation,
  retryCleanup,
  editorVisible,
  editorMode,
  submitting,
  detailsVisible,
  selectedMemory,
  historyVisible,
  selectedHistoryMemory,
  historyLoading,
  history,
  jobVisible,
  selectedJob,
  migrationVisible,
  selectedMigration,
  form,
  contentTokenCount,
  contentTooLong,
  recordStatus,
  recordStatusType,
  migrationId,
  cleanupId,
  migrationProgress,
  migrationTarget,
  progressText,
  canMutateRecord,
  canPin,
  jobError,
  jobCountsText,
  tokenBudgetText,
  openEditor,
  editSelectedMemory,
  submitMemory,
  showDetails,
  togglePin,
  handleMemoryMoreAction,
  canShowDeletedHistory,
  showDeletedHistory,
  resumeCurrent,
  canRetry,
  canCancel,
  retryJob,
  cancelJob,
  showJob,
  canRetryMigration,
  canCancelMigration,
  retryMigration,
  cancelMigration,
  showMigration
} = useMemoriesView()
</script>

<style lang="scss">
@import "@/assets/css/MemoriesView.scss";
</style>
