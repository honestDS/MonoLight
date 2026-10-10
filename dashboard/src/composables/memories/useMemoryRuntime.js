import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { memoryApi } from '../../api/index.js'
import {
  buildOrganizePayload,
  getCurrentMemoryTask,
  normalizeMemorySettings
} from '../../utils/memoryManagement.js'
import { createLatestRequestTracker } from '../../utils/requestTaskManager.js'

export function useMemoryRuntime({
  t,
  isSuperuser,
  currentUid,
  ownerFilter,
  memoryScopeReady,
  ownersLoaded,
  ownersLoading,
  loadOwners,
  ownerLabel,
  pollingTaskManager,
  isPollingStopped,
  refreshAll,
  canOpenRuntimeDialog,
  invalidateDetails,
  unwrap,
  operationLabel,
  newDedupeKey
}) {
  const settings = reactive({})
  const runtimeDialogVisible = ref(false)
  const runtimeDialogAction = ref('status')
  const runtimeOwnerFilter = ref('')
  const settingsLoaded = ref(false)
  const settingsLoadError = ref('')
  const settingsLoading = ref(false)
  const actionLoading = ref('')
  const settingsRequestTracker = createLatestRequestTracker()
  const runtimeActionRequestTracker = createLatestRequestTracker()

  const setting = (key) => settings.store?.[key] ?? settings[key] ?? '-'
  const nestedSetting = (section, key, legacyKey) => settings[section]?.[key] ?? setting(legacyKey)
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
    if (!runtimeDialogVisible.value || !runtimeOwnerUid.value || isPollingStopped() || actionLoading.value) return
    const token = pollingTaskManager.begin('settings')
    if (!token) return
    const requestSeq = settingsRequestTracker.begin()
    const ownerUid = runtimeOwnerUid.value
    if (pollingTaskManager.isCurrent(token)) settingsLoading.value = !silent || !settingsLoaded.value
    try {
      const data = unwrap(await memoryApi.settings({ signal: token.signal, params: { uid: ownerUid } }))
      if (!pollingTaskManager.isCurrent(token) || !settingsRequestTracker.isCurrent(requestSeq) || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || isPollingStopped()) return
      applySettings(data)
      settingsLoaded.value = true
      settingsLoadError.value = ''
    } catch (error) {
      if (token.signal.aborted || !pollingTaskManager.isCurrent(token) || !settingsRequestTracker.isCurrent(requestSeq) || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || isPollingStopped()) return
      Object.keys(settings).forEach(key => delete settings[key])
      settingsLoaded.value = false
      settingsLoadError.value = error?.message || t('memories.runtime_status_load_failed')
    } finally {
      if (pollingTaskManager.isCurrent(token) && settingsRequestTracker.isCurrent(requestSeq)) settingsLoading.value = false
      pollingTaskManager.finish(token)
    }
  }

  const openRuntimeDialog = (action) => {
    if (!['organize', 'reindex', 'status'].includes(action) || isPollingStopped() || !memoryScopeReady.value || actionLoading.value || runtimeDialogVisible.value || !canOpenRuntimeDialog()) return
    invalidateDetails()
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
    if (!runtimeDialogVisible.value || actionLoading.value || isPollingStopped()) return
    runtimeOwnerFilter.value = isSuperuser.value
      ? (typeof uid === 'string' ? uid.trim() : uid || '')
      : (typeof currentUid.value === 'string' ? currentUid.value.trim() : currentUid.value || '')
    resetRuntimeSettings()
    loadSettings()
  }

  const submitRuntimeOperation = async (operation) => {
    const blocked = operation === 'organize' ? organizeBlocked.value : operation === 'reindex' ? reindexBlocked.value : true
    if (!['organize', 'reindex'].includes(operation) || isPollingStopped() || !runtimeDialogVisible.value || runtimeDialogAction.value !== operation || !memoryScopeReady.value || blocked) return
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
      if (!runtimeActionRequestTracker.isCurrent(requestSeq) || isPollingStopped() || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || runtimeDialogAction.value !== operation) return
      ElMessage.info(t('memories.runtime_operation_submitted', { owner, operation: operationLabel(operation) }))
      actionLoading.value = ''
      closeRuntimeDialog()
      refreshAll()
    } catch (error) {
      if (!runtimeActionRequestTracker.isCurrent(requestSeq) || isPollingStopped() || !runtimeDialogVisible.value || runtimeOwnerUid.value !== ownerUid || runtimeDialogAction.value !== operation) return
      failed = true
      ElMessage.error(error.message || t('memories.operation_failed'))
    } finally {
      if (!runtimeActionRequestTracker.isCurrent(requestSeq)) return
      actionLoading.value = ''
      if (failed && !isPollingStopped() && runtimeDialogVisible.value && runtimeOwnerUid.value === ownerUid && runtimeDialogAction.value === operation) loadSettings()
    }
  }

  const retryCleanup = async (id) => {
    if (!id || actionLoading.value || isPollingStopped()) return
    pollingTaskManager.cancel('settings')
    settingsRequestTracker.invalidate()
    const requestSeq = runtimeActionRequestTracker.begin()
    actionLoading.value = `cleanup-${id}`
    try {
      await memoryApi.retryCleanup(id)
      if (!runtimeActionRequestTracker.isCurrent(requestSeq) || isPollingStopped()) return
      ElMessage.info(t('memories.retry_success'))
    } catch (error) {
      if (!runtimeActionRequestTracker.isCurrent(requestSeq) || isPollingStopped()) return
      ElMessage.error(error.message || t('memories.operation_failed'))
    } finally {
      if (!runtimeActionRequestTracker.isCurrent(requestSeq) || isPollingStopped()) return
      actionLoading.value = ''
      loadSettings()
      refreshAll()
    }
  }

  const invalidateRequests = () => {
    settingsRequestTracker.invalidate()
    runtimeActionRequestTracker.invalidate()
  }

  return {
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
    maxActiveRecords,
    capacityOverLimit,
    organizeBlocked,
    reindexBlocked,
    cleanupRetryId,
    migrationPercentage,
    settingsError,
    currentMemoryTask,
    blockingReason,
    blockingText,
    runtimeBlockingMessage,
    loadSettings,
    openRuntimeDialog,
    closeRuntimeDialog,
    handleRuntimeOwnerChange,
    submitRuntimeOperation,
    retryCleanup,
    invalidateRequests
  }
}
