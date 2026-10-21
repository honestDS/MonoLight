import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'
import {
  canManageManagedKnowledge,
  getKnowledgeOrganizationPublishedSuccessCount
} from '../../utils/knowledgeBaseManagement.js'
import { createAbortableTaskManager, createLatestRequestTracker } from '../../utils/requestTaskManager.js'

export function useKnowledgeOrganization ({
  t,
  selectedKb,
  scope,
  fetchManagedKnowledgeItems,
  scheduleManagedKnowledgePolling
}) {
  const organizationDialogVisible = ref(false)
  const organizationLoading = ref(false)
  const organizationSubmitting = ref(false)
  const organizationActionJobId = ref(null)
  const organizationJobs = ref([])
  const organizationTotal = ref(0)
  const organizationJobPage = ref(1)
  const organizationJobPageSize = 20
  let organizationPollTimer = null
  let organizationPollingSession = 0
  let organizationActionGeneration = 0
  let organizationSubmitGeneration = 0
  const organizationTaskManager = createAbortableTaskManager()
  const organizationRequestTracker = createLatestRequestTracker()

  const latestOrganizationJob = ref(null)
  const organizationHistoryTotal = computed(() => Math.max(0, organizationTotal.value - 1))
  const organizationHistoryJobs = computed(() => {
    if (!latestOrganizationJob.value) return organizationJobs.value
    const latestJobId = getOrganizationJobId(latestOrganizationJob.value)
    return organizationJobs.value.filter(job => getOrganizationJobId(job) !== latestJobId)
  })

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

  const isOrganizationScopeCurrent = (scopeGuard, selectedId) => Boolean(
    selectedId !== null
    && selectedId !== undefined
    && scopeGuard()
    && selectedKb.value?.id === selectedId
  )

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
    && job?.can_retry === true
  )

  const fetchOrganizationJobs = async (
    notifyError = true,
    session = organizationPollingSession,
    page = organizationJobPage.value
  ) => {
    const scopeGuard = scope.capture()
    const selectedId = selectedKb.value?.id
    if (
      !isOrganizationScopeCurrent(scopeGuard, selectedId)
      || !organizationDialogVisible.value
      || !selectedKb.value
      || !canManageManagedKnowledge(selectedKb.value)
      || session !== organizationPollingSession
    ) return

    const taskKey = `organization-jobs-${session}`
    const token = organizationTaskManager.begin(taskKey)
    if (!token) return
    const requestSeq = organizationRequestTracker.begin()
    if (organizationTaskManager.isCurrent(token)) organizationLoading.value = true

    try {
      const requests = [knowledgeBaseApi.organizationJobs(selectedId, {
        page,
        size: organizationJobPageSize
      })]
      if (page > 1) {
        requests.push(knowledgeBaseApi.organizationJobs(selectedId, {
          page: 1,
          size: 1
        }))
      }
      const responses = await Promise.all(requests)
      const data = responses[0].data.data || {}
      const items = Array.isArray(data.items)
        ? data.items.filter(item => item?.knowledge_base_id === selectedId)
        : []
      const latestData = page === 1 ? data : responses[1].data.data || {}
      const latestItems = Array.isArray(latestData.items)
        ? latestData.items.filter(item => item?.knowledge_base_id === selectedId)
        : []
      const latestJob = latestItems[0] || null
      if (
        !organizationTaskManager.isCurrent(token)
        || !organizationRequestTracker.isCurrent(requestSeq)
        || !isOrganizationScopeCurrent(scopeGuard, selectedId)
        || !organizationDialogVisible.value
        || session !== organizationPollingSession
      ) return
      organizationJobPage.value = page
      organizationJobs.value = items
      organizationTotal.value = getOrganizationNumber(data.total) ?? items.length
      latestOrganizationJob.value = latestJob
    } catch (error) {
      if (
        notifyError
        && organizationTaskManager.isCurrent(token)
        && organizationRequestTracker.isCurrent(requestSeq)
        && isOrganizationScopeCurrent(scopeGuard, selectedId)
        && session === organizationPollingSession
      ) {
        ElMessage.error(t('knowledgeBase.organization_fetch_failed') + error.message)
      }
    } finally {
      if (
        organizationTaskManager.isCurrent(token)
        && organizationRequestTracker.isCurrent(requestSeq)
        && isOrganizationScopeCurrent(scopeGuard, selectedId)
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
    organizationTaskManager.invalidate()
    organizationRequestTracker.invalidate()
    organizationLoading.value = false
    return organizationPollingSession
  }

  const stopOrganizationPolling = () => {
    beginOrganizationPollingSession()
  }

  const scheduleOrganizationPolling = (session = organizationPollingSession) => {
    if (session !== organizationPollingSession) return
    if (organizationPollTimer) {
      clearTimeout(organizationPollTimer)
      organizationPollTimer = null
    }
    const scopeGuard = scope.capture()
    const selectedId = selectedKb.value?.id
    if (
      !isOrganizationScopeCurrent(scopeGuard, selectedId)
      || !organizationDialogVisible.value
      || ![latestOrganizationJob.value, ...organizationJobs.value]
        .filter(Boolean)
        .some(job => !isOrganizationJobTerminal(job))
    ) return

    const timer = setTimeout(async () => {
      if (
        session !== organizationPollingSession
        || !isOrganizationScopeCurrent(scopeGuard, selectedId)
      ) return
      if (organizationPollTimer === timer) organizationPollTimer = null
      try {
        await fetchOrganizationJobs(false, session)
      } finally {
        if (
          session === organizationPollingSession
          && isOrganizationScopeCurrent(scopeGuard, selectedId)
        ) scheduleOrganizationPolling(session)
      }
    }, 2000)
    organizationPollTimer = timer
  }

  const showOrganizationDialog = async () => {
    const scopeGuard = scope.capture()
    const selectedId = selectedKb.value?.id
    if (
      !isOrganizationScopeCurrent(scopeGuard, selectedId)
      || !selectedKb.value
      || !canManageManagedKnowledge(selectedKb.value)
    ) return
    stopOrganizationPolling()
    organizationJobs.value = []
    organizationTotal.value = 0
    latestOrganizationJob.value = null
    organizationJobPage.value = 1
    organizationDialogVisible.value = true
    const session = organizationPollingSession
    await fetchOrganizationJobs(true, session)
    if (
      isOrganizationScopeCurrent(scopeGuard, selectedId)
      && session === organizationPollingSession
    ) scheduleOrganizationPolling(session)
  }

  const refreshOrganizationData = async (scopeGuard, selectedId) => {
    if (!isOrganizationScopeCurrent(scopeGuard, selectedId)) return
    const session = beginOrganizationPollingSession()
    await Promise.all([
      fetchOrganizationJobs(true, session),
      fetchManagedKnowledgeItems(true, true)
    ])
    if (!isOrganizationScopeCurrent(scopeGuard, selectedId)) return
    scheduleManagedKnowledgePolling()
    if (
      organizationDialogVisible.value
      && session === organizationPollingSession
    ) scheduleOrganizationPolling(session)
  }

  const handleOrganizationPageChange = async (page) => {
    const maxPage = Math.max(1, Math.ceil(organizationTotal.value / organizationJobPageSize))
    if (!Number.isInteger(page) || page < 1 || page > maxPage || page === organizationJobPage.value) return

    const session = beginOrganizationPollingSession()
    await fetchOrganizationJobs(true, session, page)
    if (session === organizationPollingSession) scheduleOrganizationPolling(session)
  }

  const submitKnowledgeOrganization = async () => {
    const scopeGuard = scope.capture()
    const selectedId = selectedKb.value?.id
    if (
      !isOrganizationScopeCurrent(scopeGuard, selectedId)
      || !selectedKb.value
      || !canManageManagedKnowledge(selectedKb.value)
      || organizationSubmitting.value
    ) return

    const generation = ++organizationSubmitGeneration
    const isCurrent = () => (
      generation === organizationSubmitGeneration
      && isOrganizationScopeCurrent(scopeGuard, selectedId)
    )
    organizationSubmitting.value = true
    try {
      await knowledgeBaseApi.organize(selectedId, {})
      if (!isCurrent()) return
      if (selectedKb.value?.id === selectedId) selectedKb.value.organization_error = null
      ElMessage.success(t('knowledgeBase.organization_submitted'))
      await refreshOrganizationData(scopeGuard, selectedId)
      if (!isCurrent()) return
    } catch (error) {
      if (isCurrent()) ElMessage.error(t('knowledgeBase.organization_submit_failed') + error.message)
    } finally {
      if (isCurrent()) organizationSubmitting.value = false
    }
  }

  const handleCancelOrganizationJob = async (job) => {
    const scopeGuard = scope.capture()
    const selectedId = selectedKb.value?.id
    const jobId = getOrganizationJobId(job)
    const isCurrent = () => isOrganizationScopeCurrent(scopeGuard, selectedId)
    if (!isCurrent() || !selectedId || !jobId || !canCancelOrganizationJob(job)) return
    const confirmed = await scope.confirm(
      t('knowledgeBase.organization_cancel_confirm'),
      t('knowledgeBase.prompt'),
      {
        confirmButtonText: t('knowledgeBase.organization_cancel'),
        cancelButtonText: t('knowledgeBase.cancel'),
        type: 'warning'
      }
    )
    if (!isCurrent() || !confirmed || !canCancelOrganizationJob(job)) return

    const generation = ++organizationActionGeneration
    const isCurrentAction = () => (
      generation === organizationActionGeneration
      && isCurrent()
    )
    organizationActionJobId.value = jobId
    try {
      await knowledgeBaseApi.cancelOrganizationJob(selectedId, jobId)
      if (!isCurrentAction()) return
      ElMessage.success(t('knowledgeBase.organization_cancelled'))
      await refreshOrganizationData(scopeGuard, selectedId)
      if (!isCurrentAction()) return
    } catch (error) {
      if (isCurrentAction()) ElMessage.error(t('knowledgeBase.organization_cancel_failed') + error.message)
    } finally {
      if (isCurrentAction()) organizationActionJobId.value = null
    }
  }

  const handleRetryOrganizationJob = async (job) => {
    const scopeGuard = scope.capture()
    const selectedId = selectedKb.value?.id
    const jobId = getOrganizationJobId(job)
    const isCurrent = () => isOrganizationScopeCurrent(scopeGuard, selectedId)
    if (!isCurrent() || !selectedId || !jobId || !canRetryOrganizationJob(job)) return
    const confirmed = await scope.confirm(
      t('knowledgeBase.organization_retry_confirm'),
      t('knowledgeBase.prompt'),
      {
        confirmButtonText: t('knowledgeBase.organization_retry'),
        cancelButtonText: t('knowledgeBase.cancel'),
        type: 'warning'
      }
    )
    if (!isCurrent() || !confirmed || !canRetryOrganizationJob(job)) return

    const generation = ++organizationActionGeneration
    const isCurrentAction = () => (
      generation === organizationActionGeneration
      && isCurrent()
    )
    organizationActionJobId.value = jobId
    try {
      await knowledgeBaseApi.retryOrganizationJob(selectedId, jobId)
      if (!isCurrentAction()) return
      ElMessage.success(t('knowledgeBase.organization_retried'))
      await refreshOrganizationData(scopeGuard, selectedId)
      if (!isCurrentAction()) return
    } catch (error) {
      if (isCurrentAction()) ElMessage.error(t('knowledgeBase.organization_retry_failed') + error.message)
    } finally {
      if (isCurrentAction()) organizationActionJobId.value = null
    }
  }

  const resetOrganizationState = () => {
    stopOrganizationPolling()
    organizationActionGeneration += 1
    organizationSubmitGeneration += 1
    organizationDialogVisible.value = false
    organizationJobs.value = []
    organizationTotal.value = 0
    latestOrganizationJob.value = null
    organizationJobPage.value = 1
    organizationLoading.value = false
    organizationSubmitting.value = false
    organizationActionJobId.value = null
  }

  return {
    organizationDialogVisible,
    organizationLoading,
    organizationSubmitting,
    organizationActionJobId,
    organizationJobs,
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
    fetchOrganizationJobs,
    resetOrganizationState
  }
}
