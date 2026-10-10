import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'
import {
  canManageManagedKnowledge,
  getKnowledgeOrganizationPublishedSuccessCount
} from '../../utils/knowledgeBaseManagement.js'
import { createAbortableTaskManager, createLatestRequestTracker } from '../../utils/requestTaskManager.js'

export function useKnowledgeOrganization ({
  t,
  selectedKb,
  managedKnowledgeSelectedIds,
  fetchManagedKnowledgeItems,
  scheduleManagedKnowledgePolling
}) {
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

  const latestOrganizationJob = computed(() => organizationJobs.value[0] || null)

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

  return {
    organizationDialogVisible,
    organizationLoading,
    organizationSubmitting,
    organizationActionJobId,
    organizationJobs,
    organizationTotal,
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
    submitKnowledgeOrganization,
    handleCancelOrganizationJob,
    handleRetryOrganizationJob,
    stopOrganizationPolling,
    fetchOrganizationJobs
  }
}
