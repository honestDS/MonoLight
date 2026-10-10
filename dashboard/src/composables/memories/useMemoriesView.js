import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { adminApi, channelApi, memoryApi } from '../../api/index.js'
import { MEMORY_JOB_OPERATIONS, MEMORY_JOB_STATUSES, MEMORY_TYPES } from '../../constants/index.js'
import {
  createLatestRequestTracker,
  decorateMemoryJobs,
  memoryOperationLabelKey,
  memorySourceLabelKey
} from '../../utils/memoryManagement.js'
import { createAbortableTaskManager } from '../../utils/channelTestManager.js'
import { useMemoryRecords } from './useMemoryRecords.js'
import { useMemoryRuntime } from './useMemoryRuntime.js'

export function useMemoriesView() {
  const { t } = useI18n()
  const memoryTypes = MEMORY_TYPES
  const jobStatuses = MEMORY_JOB_STATUSES
  const jobOperations = MEMORY_JOB_OPERATIONS
  const activeTab = ref('memories')

  const isSuperuser = ref(false)
  const currentUid = ref(null)
  const currentUsername = ref('')
  const owners = ref([])
  const ownersLoading = ref(false)
  const ownersLoaded = ref(false)
  const memoryScopeReady = ref(false)
  const ownerFilter = ref('')
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

  const pollTimer = ref(null)
  let pollingStopped = false
  const pollingTaskManager = createAbortableTaskManager()
  const memoriesRequestTracker = createLatestRequestTracker()
  const jobsRequestTracker = createLatestRequestTracker()
  const migrationsRequestTracker = createLatestRequestTracker()
  const filters = reactive({ keyword: '', memory_type: '', sort_by: 'updated_at', sort_order: 'desc' })
  const jobFilters = reactive({ status: '', operation: '', memory_id: '' })

  const unwrap = response => response?.data?.data ?? response?.data ?? {}
  const pageData = response => {
    const data = unwrap(response)
    if (Array.isArray(data)) return { items: data, total: data.length, meta: null }
    return { items: data.items || [], total: Number(data.total || 0), meta: data.meta ?? null }
  }
  const formatTime = value => value ? new Date(value).toLocaleString() : '-'
  const channelName = channelId => {
    if (channelId === null || channelId === undefined || channelId === '' || channelId === '-') return '-'
    const channel = channels.value.find(item => String(item.id) === String(channelId))
    return typeof channel?.name === 'string' && channel.name.trim() ? channel.name : '-'
  }
  const ownerLabel = uid => {
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
  const typeLabel = value => t(`memories.type_${value}`, value || '-')
  const sourceLabel = value => {
    const labelKey = memorySourceLabelKey(value)
    return labelKey === `memories.source_${value}` ? t(labelKey) : labelKey
  }
  const statusText = value => value ? t(`memories.status_${value}`, value) : t('memories.not_available')
  const operationLabel = value => {
    const labelKey = memoryOperationLabelKey(value)
    return labelKey === `memories.operation_${value}` ? t(labelKey) : labelKey
  }
  const statusType = value => ['succeeded', 'ready', 'confirmed', 'none', 'normal'].includes(value)
    ? 'success'
    : ['failed', 'over_limit', 'full'].includes(value)
      ? 'danger'
      : ['cancelled'].includes(value) ? 'info' : 'warning'
  const newDedupeKey = () => `dashboard-${Date.now()}-${Math.random().toString(36).slice(2)}`

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
      const data = pageData(await memoryApi.list({
        page: memoryPage.value,
        size: memoryPageSize.value,
        keyword: filters.keyword || undefined,
        memory_type: filters.memory_type || undefined,
        sort_by: filters.sort_by,
        sort_order: filters.sort_order,
        uid: isSuperuser.value ? ownerFilter.value || undefined : undefined
      }, { signal: token.signal }))
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
      const data = pageData(await memoryApi.jobs({
        page: jobPage.value,
        size: jobPageSize.value,
        status: jobFilters.status || undefined,
        operation: jobFilters.operation || undefined,
        memory_id: jobFilters.memory_id || undefined,
        uid: isSuperuser.value ? ownerFilter.value || undefined : undefined
      }, { signal: token.signal }))
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
      const data = pageData(await memoryApi.migrations({
        page: migrationPage.value,
        size: migrationPageSize.value,
        uid: isSuperuser.value ? ownerFilter.value || undefined : undefined
      }, { signal: token.signal }))
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
  const handleTabChange = tab => { if (tab === 'jobs') loadJobs(); if (tab === 'migrations') loadMigrations() }
  const refreshAll = async () => {
    const requests = [loadMemories(true)]
    if (runtime.runtimeDialogVisible.value) requests.push(runtime.loadSettings(true))
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
    resetOwnerState()
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

  const { invalidateRequests: invalidateRuntimeRequests, ...runtime } = useMemoryRuntime({
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
    isPollingStopped: () => pollingStopped,
    refreshAll,
    unwrap,
    operationLabel,
    newDedupeKey,
    canOpenRuntimeDialog: () => ![
      records.editorVisible,
      records.detailsVisible,
      records.historyVisible,
      records.jobVisible,
      records.migrationVisible
    ].some(visible => visible.value),
    invalidateDetails: () => invalidateDetails()
  })

  const { invalidateDetails, invalidateRequests: invalidateRecordRequests, resetOwnerState, ...records } = useMemoryRecords({
    t,
    isSuperuser,
    currentUid,
    ownerFilter,
    memoryScopeReady,
    contentMaxTokens: runtime.contentMaxTokens,
    refreshAll,
    isPollingStopped: () => pollingStopped,
    unwrap,
    pageData,
    statusText,
    statusType,
    newDedupeKey
  })

  onMounted(async () => {
    await Promise.all([loadMemories(), loadChannels()])
    if (pollingStopped) return
    if (!pollingStopped) scheduleRefresh()
  })

  onBeforeUnmount(() => {
    pollingStopped = true
    if (pollTimer.value) window.clearTimeout(pollTimer.value)
    pollingTaskManager.invalidate()
    memoriesRequestTracker.invalidate()
    jobsRequestTracker.invalidate()
    migrationsRequestTracker.invalidate()
    invalidateRuntimeRequests()
    invalidateRecordRequests()
  })

  return {
    memoryTypes,
    jobStatuses,
    jobOperations,
    activeTab,
    isSuperuser,
    currentUid,
    currentUsername,
    owners,
    ownersLoading,
    ownersLoaded,
    memoryScopeReady,
    ownerFilter,
    channels,
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
    newDedupeKey,
    loadOwners,
    loadChannels,
    loadMemories,
    loadJobs,
    loadMigrations,
    resetAndLoadMemories,
    resetAndLoadJobs,
    resetAndLoadMigrations,
    handleTabChange,
    handleOwnerChange,
    refreshAll,
    scheduleRefresh,
    ...runtime,
    ...records
  }
}
