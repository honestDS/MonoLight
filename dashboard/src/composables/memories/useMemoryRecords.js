import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { memoryApi } from '../../api/index.js'
import {
  createLatestRequestTracker,
  estimateMemoryTokens,
  isMemoryContentTooLong
} from '../../utils/memoryManagement.js'

export function useMemoryRecords({
  t,
  isSuperuser,
  currentUid,
  ownerFilter,
  memoryScopeReady,
  contentMaxTokens,
  refreshAll,
  isPollingStopped,
  unwrap,
  pageData,
  statusText,
  statusType,
  newDedupeKey
}) {
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
  const form = reactive({
    id: null,
    version: 0,
    owner_uid: '',
    memory_key: '',
    memory_type: 'fact',
    content: '',
    change_evidence: '',
    suppress_current: false
  })

  const historyRequestTracker = createLatestRequestTracker()
  const detailsRequestTracker = createLatestRequestTracker()
  const jobDetailsRequestTracker = createLatestRequestTracker()
  const migrationDetailsRequestTracker = createLatestRequestTracker()
  const editorRequestTracker = createLatestRequestTracker()

  const contentTokenCount = computed(() => estimateMemoryTokens(form.content))
  const contentTooLong = computed(() => isMemoryContentTooLong(form.content, contentMaxTokens.value))
  const recordStatus = row => row.deleted_at
    ? t('memories.deleted')
    : row.suppress_recall
      ? t('memories.suppressed')
      : row.pending_mutation_job_id
        ? t('memories.pending')
        : statusText(row.index_status || 'ready')
  const recordStatusType = row => row.deleted_at
    ? 'danger'
    : row.suppress_recall
      ? 'warning'
      : row.pending_mutation_job_id
        ? 'warning'
        : statusType(row.index_status || 'ready')
  const migrationId = row => row.job_id || row.migration_job_id || row.id || '-'
  const cleanupId = row => row.old_collection_cleanup_job_id || row.cleanup_job_id || row.cleanup?.job_id || null
  const migrationProgress = (row, key) => row[key] ?? row.progress?.[key.replace('migration_', '')] ?? 0
  const migrationTarget = row => `${row.target_embedding_model_id || row.target?.model_id || '-'} / ${row.target_embedding_dimensions || row.target?.dimensions || '-'}D`
  const progressText = row => `${migrationProgress(row, 'migration_success_count')} / ${migrationProgress(row, 'migration_total_count')}`
  const canMutateRecord = row => !row.pending_mutation_job_id && !row.deleted_at && row.is_active !== false
  const canPin = row => canMutateRecord(row)
  const jobError = row => row.error || row.result?.error || (row.context_error ? JSON.stringify(row.context_error) : '-')
  const jobCountsText = row => {
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
  const tokenBudgetText = budget => budget ? [
    `${t('memories.context_window_tokens')}: ${budget.context_window_tokens ?? '-'}`,
    `${t('memories.required_input_tokens')}: ${budget.required_input_tokens ?? '-'}`,
    `${t('memories.available_input_tokens')}: ${budget.available_input_tokens ?? '-'}`,
    `${t('memories.max_output_tokens')}: ${budget.max_output_tokens ?? budget.max_tokens ?? '-'}`,
    `${t('memories.required_output_tokens')}: ${budget.required_output_tokens ?? '-'}`
  ].join(' / ') : '-'

  const invalidateDetails = () => {
    historyRequestTracker.invalidate()
    detailsRequestTracker.invalidate()
    jobDetailsRequestTracker.invalidate()
    migrationDetailsRequestTracker.invalidate()
  }
  const invalidateRequests = () => {
    invalidateDetails()
    editorRequestTracker.invalidate()
  }
  const resetOwnerState = () => {
    invalidateRequests()
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
    historyLoading.value = false
    submitting.value = false
  }

  const resetForm = () => Object.assign(form, {
    id: null,
    version: 0,
    owner_uid: '',
    memory_key: '',
    memory_type: 'fact',
    content: '',
    change_evidence: '',
    suppress_current: false
  })
  const openEditor = (row = null) => {
    editorRequestTracker.invalidate()
    submitting.value = false
    editorMode.value = row ? 'edit' : 'create'
    resetForm()
    if (row) {
      Object.assign(form, {
        id: row.id,
        version: row.version,
        owner_uid: row.owner_uid,
        memory_key: row.memory_key || '',
        memory_type: row.memory_type || 'fact',
        content: row.content || '',
        change_evidence: row.change_evidence || '',
        suppress_current: false
      })
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
    if (submitting.value || !editorVisible.value || !memoryScopeReady.value || isPollingStopped()) return
    if (editorMode.value === 'create' && isSuperuser.value && !form.owner_uid) return ElMessage.warning(t('memories.select_owner'))
    if (!form.memory_key.trim() || !form.content.trim()) return ElMessage.warning(t('memories.required'))
    if (contentTooLong.value) return ElMessage.warning(t('memories.token_limit_exceeded'))
    const requestSeq = editorRequestTracker.begin()
    submitting.value = true
    try {
      const payload = {
        dedupe_key: newDedupeKey(),
        content: form.content,
        memory_key: form.memory_key,
        memory_type: form.memory_type,
        change_evidence: form.change_evidence || null
      }
      if (editorMode.value === 'create') {
        await memoryApi.create(payload, { params: { uid: isSuperuser.value ? form.owner_uid : currentUid.value } })
      } else {
        await memoryApi.update({
          ...payload,
          memory_id: form.id,
          expected_version: form.version,
          suppress_current: form.suppress_current
        })
      }
      if (editorRequestTracker.isCurrent(requestSeq) && editorVisible.value) {
        ElMessage.info(t('memories.accepted_processing'))
        editorVisible.value = false
        refreshAll()
      }
    } catch (error) {
      if (editorRequestTracker.isCurrent(requestSeq) && editorVisible.value) ElMessage.error(error.message || t('memories.save_failed'))
    } finally {
      if (editorRequestTracker.isCurrent(requestSeq)) submitting.value = false
    }
  }
  const showDetails = async row => {
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
  const deleteMemory = async row => {
    try {
      await ElMessageBox.confirm(t('memories.delete_confirm'), t('common.warning'), {
        type: 'warning',
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel')
      })
      await memoryApi.delete({ memory_id: row.id, expected_version: row.version, dedupe_key: newDedupeKey() })
      ElMessage.info(t('memories.delete_success'))
      refreshAll()
    } catch (error) {
      if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed'))
    }
  }
  const togglePin = async row => {
    try {
      if (row.pinned) await memoryApi.unpin(row.id)
      else await memoryApi.pin(row.id)
      ElMessage.info(t('memories.operation_success'))
      refreshAll()
    } catch (error) {
      ElMessage.error(error.message || t('memories.operation_failed'))
    }
  }
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
  const showHistory = row => loadHistory(row.id, row)
  const handleMemoryMoreAction = (command, row) => {
    if (command === 'history') {
      showHistory(row)
      return
    }
    if (command === 'delete' && canMutateRecord(row)) deleteMemory(row)
  }
  const isRecordSnapshot = value => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length > 0
  const deletedRecordSnapshot = row => {
    const resultSnapshot = row?.result?.record_snapshot
    if (isRecordSnapshot(resultSnapshot)) return resultSnapshot
    const payloadSnapshot = row?.payload?.record_snapshot
    return isRecordSnapshot(payloadSnapshot) ? payloadSnapshot : null
  }
  const canShowDeletedHistory = row => row.operation === 'delete_cleanup' && Boolean(row.memory_id && deletedRecordSnapshot(row))
  const showDeletedHistory = row => {
    const snapshot = deletedRecordSnapshot(row)
    if (!snapshot || !row.memory_id) return
    loadHistory(row.memory_id, {
      ...snapshot,
      id: row.memory_id,
      memory_key: snapshot.memory_key || '',
      version: snapshot.version,
      owner_uid: row.owner_uid
    })
  }
  const resumeCurrent = async row => {
    try {
      await memoryApi.resumeCurrent(row.id, { expected_version: row.version })
      ElMessage.info(t('memories.operation_success'))
      refreshAll()
    } catch (error) {
      ElMessage.error(error.message || t('memories.operation_failed'))
    }
  }
  const canRetry = row => {
    if (row.operation === 'restore') return false
    return row.operation === 'delete_cleanup' ? row.status === 'failed' : ['failed', 'cancelled'].includes(row.status)
  }
  const canCancel = row => !['succeeded', 'failed', 'cancelled'].includes(row.status) && row.operation !== 'delete_cleanup'
  const retryJob = async row => {
    try {
      await ElMessageBox.confirm(t('memories.retry_confirm'), t('common.warning'), { type: 'warning' })
      await memoryApi.retryJob(row.id)
      ElMessage.info(t('memories.retry_success'))
      refreshAll()
    } catch (error) {
      if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed'))
    }
  }
  const cancelJob = async row => {
    try {
      await ElMessageBox.confirm(t('memories.cancel_confirm'), t('common.warning'), { type: 'warning' })
      await memoryApi.cancelJob(row.id)
      ElMessage.info(t('memories.cancel_success'))
      refreshAll()
    } catch (error) {
      if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || t('memories.operation_failed'))
    }
  }
  const showJob = async row => {
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
  const canRetryMigration = row => ['failed', 'cancelled'].includes(row.status || row.migration_status)
  const canCancelMigration = row => ['preparing', 'building', 'catching_up', 'validating'].includes(row.status || row.migration_status)
  const retryMigration = async row => {
    try {
      await memoryApi.retryMigration(migrationId(row))
      ElMessage.info(t('memories.migration_retry_success'))
      refreshAll()
    } catch (error) {
      ElMessage.error(error.message || t('memories.operation_failed'))
    }
  }
  const cancelMigration = async row => {
    try {
      await memoryApi.cancelMigration(migrationId(row))
      ElMessage.info(t('memories.migration_cancel_success'))
      refreshAll()
    } catch (error) {
      ElMessage.error(error.message || t('memories.operation_failed'))
    }
  }
  const showMigration = async row => {
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

  return {
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
    deleteMemory,
    togglePin,
    showHistory,
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
    showMigration,
    invalidateDetails,
    invalidateRequests,
    resetOwnerState
  }
}
