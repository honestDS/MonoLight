<script setup>
import { onBeforeUnmount, toRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useKnowledgeOrganization } from '@/composables/knowledgeBase/useKnowledgeOrganization.js'

const props = defineProps({
  knowledgeBase: {
    type: Object,
    required: true
  },
  scope: {
    type: Object,
    required: true
  },
  refreshEntries: {
    type: Function,
    required: true
  },
  schedulePolling: {
    type: Function,
    required: true
  }
})

const { t } = useI18n()
const selectedKb = toRef(props, 'knowledgeBase')

const {
  organizationDialogVisible,
  organizationLoading,
  organizationSubmitting,
  organizationActionJobId,
  organizationTotal,
  organizationJobPage,
  organizationJobPageSize,
  organizationHistoryJobs,
  organizationHistoryTotal,
  latestOrganizationJob,
  getOrganizationJobId,
  getOrganizationStatusLabel,
  getOrganizationStatusType,
  getOrganizationStageProgress,
  getOrganizationStageProgressPercentage,
  getOrganizationFragmentProgress,
  getOrganizationFragmentProgressPercentage,
  getOrganizationSnapshotId,
  getOrganizationCountsText,
  canCancelOrganizationJob,
  canRetryOrganizationJob,
  showOrganizationDialog,
  handleOrganizationPageChange,
  submitKnowledgeOrganization,
  handleCancelOrganizationJob,
  handleRetryOrganizationJob,
  stopOrganizationPolling,
  resetOrganizationState
} = useKnowledgeOrganization({
  t,
  selectedKb,
  scope: props.scope,
  fetchManagedKnowledgeItems: (...args) => props.refreshEntries(...args),
  scheduleManagedKnowledgePolling: () => props.schedulePolling()
})

watch(() => props.knowledgeBase?.id, resetOrganizationState, { flush: 'sync' })
onBeforeUnmount(resetOrganizationState)

defineExpose({ open: showOrganizationDialog })
</script>

<template>
  <el-dialog
    :title="t('knowledgeBase.organization_title', { name: selectedKb?.name || '' })"
    v-model="organizationDialogVisible"
    width="1020px"
    class="standard-dialog organization-dialog dialog-with-scroll-body"
    center
    align-center
    @closed="stopOrganizationPolling"
  >
    <div class="organization-toolbar">
      <p class="organization-intro">{{ t('knowledgeBase.organization_intro') }}</p>
      <el-button
        type="primary"
        :loading="organizationSubmitting"
        @click="submitKnowledgeOrganization"
      >
        {{ t('knowledgeBase.organization_start') }}
      </el-button>
    </div>

    <section v-if="latestOrganizationJob" class="organization-latest">
      <div class="organization-latest__header">
        <div class="organization-latest__heading">
          <h3>{{ t('knowledgeBase.organization_latest') }}</h3>
          <div class="organization-latest__meta">
            <span>{{ t('knowledgeBase.organization_job_id') }}: {{ getOrganizationJobId(latestOrganizationJob) || '-' }}</span>
            <span>{{ t('knowledgeBase.organization_snapshot') }}: {{ getOrganizationSnapshotId(latestOrganizationJob) }}</span>
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
            {{ t('knowledgeBase.organization_cancel') }}
          </el-button>
          <el-button
            v-if="canRetryOrganizationJob(latestOrganizationJob)"
            type="primary"
            size="small"
            :loading="organizationActionJobId === getOrganizationJobId(latestOrganizationJob)"
            @click="handleRetryOrganizationJob(latestOrganizationJob)"
          >
            {{ t('knowledgeBase.organization_retry') }}
          </el-button>
        </div>
      </div>

      <div v-if="['pending', 'running', 'retry'].includes(latestOrganizationJob.status)" class="organization-progress-grid">
        <div class="organization-progress-item">
          <div class="organization-progress-label">
            <span>{{ t('knowledgeBase.organization_stage_progress') }}</span>
            <span>{{ getOrganizationStageProgress(latestOrganizationJob) }}</span>
          </div>
          <el-progress :percentage="getOrganizationStageProgressPercentage(latestOrganizationJob)" :show-text="false" />
        </div>
        <div class="organization-progress-item">
          <div class="organization-progress-label">
            <span>{{ t('knowledgeBase.organization_fragment_progress') }}</span>
            <span>{{ getOrganizationFragmentProgress(latestOrganizationJob) }}</span>
          </div>
          <el-progress :percentage="getOrganizationFragmentProgressPercentage(latestOrganizationJob)" :show-text="false" />
        </div>
      </div>

      <div class="organization-latest__counts">
        <span>{{ t('knowledgeBase.organization_counts') }}</span>
        <span>{{ getOrganizationCountsText(latestOrganizationJob) }}</span>
      </div>

      <div v-if="latestOrganizationJob.status === 'failed'" class="organization-job-error" role="alert">
        {{ latestOrganizationJob.error || t('knowledgeBase.organization_error') }}
      </div>
    </section>

    <el-empty
      v-if="!organizationLoading && !latestOrganizationJob"
      :description="t('knowledgeBase.organization_no_jobs')"
    />
    <details v-if="organizationDialogVisible && organizationHistoryTotal > 0" class="organization-history">
      <summary class="organization-history__summary">
        <span class="organization-history__title">{{ t('knowledgeBase.organization_history') }}</span>
        <el-tag class="organization-history__count" size="small" type="info" effect="plain">{{ organizationHistoryTotal }}</el-tag>
        <el-icon class="organization-history__icon" aria-hidden="true"><ArrowRight /></el-icon>
      </summary>
      <div class="organization-history__body">
        <el-table
          :data="organizationHistoryJobs"
          v-loading="organizationLoading"
          :row-key="getOrganizationJobId"
          max-height="240"
          class="organization-job-table"
        >
          <el-table-column type="expand" width="76" :label="t('knowledgeBase.organization_details')">
            <template #default="{ row }">
              <div class="organization-history-details">
                <div>{{ t('knowledgeBase.organization_snapshot') }}: {{ getOrganizationSnapshotId(row) }}</div>
                <div>{{ t('knowledgeBase.organization_stage_progress') }}: {{ getOrganizationStageProgress(row) }}</div>
                <div>{{ t('knowledgeBase.organization_fragment_progress') }}: {{ getOrganizationFragmentProgress(row) }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="t('knowledgeBase.organization_job_id')" width="90" align="center">
            <template #default="{ row }">{{ getOrganizationJobId(row) || '-' }}</template>
          </el-table-column>
          <el-table-column :label="t('knowledgeBase.organization_status')" width="148" align="center">
            <template #default="{ row }">
              <el-tag :type="getOrganizationStatusType(row.status)">
                {{ getOrganizationStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('knowledgeBase.organization_counts')" min-width="220">
            <template #default="{ row }">
              <div class="organization-history__result">
                <div>{{ getOrganizationCountsText(row) }}</div>
                <div v-if="row.status === 'failed'" class="organization-history__error">{{ row.error || t('knowledgeBase.organization_error') }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="t('knowledgeBase.actions')" width="200" align="center" fixed="right">
            <template #default="{ row }">
              <div class="action-buttons">
                <el-button
                  v-if="canCancelOrganizationJob(row)"
                  type="warning"
                  size="small"
                  :loading="organizationActionJobId === getOrganizationJobId(row)"
                  @click="handleCancelOrganizationJob(row)"
                >
                  {{ t('knowledgeBase.organization_cancel') }}
                </el-button>
                <el-button
                  v-if="canRetryOrganizationJob(row)"
                  type="primary"
                  size="small"
                  :loading="organizationActionJobId === getOrganizationJobId(row)"
                  @click="handleRetryOrganizationJob(row)"
                >
                  {{ t('knowledgeBase.organization_retry') }}
                </el-button>
                <span v-if="!canCancelOrganizationJob(row) && !canRetryOrganizationJob(row)">-</span>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="organizationTotal > organizationJobPageSize" class="organization-history__pagination">
          <el-pagination
            :current-page="organizationJobPage"
            :page-size="organizationJobPageSize"
            :total="organizationTotal"
            layout="prev, pager, next"
            :disabled="organizationLoading"
            @current-change="handleOrganizationPageChange"
          />
        </div>
      </div>
    </details>
  </el-dialog>
</template>

<style lang="scss">
@import '@/assets/css/KnowledgeBase.scss';
</style>
