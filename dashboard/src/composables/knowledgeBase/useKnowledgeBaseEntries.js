import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { knowledgeBaseApi } from '../../api/index.js'
import {
  createManagedKnowledgeDedupeKey,
  getManagedKnowledgeMutationFeedback
} from '../../utils/knowledgeBaseManagement.js'
import { createKnowledgeBaseScope } from '../../utils/knowledgeBaseScope.js'
import { createAbortableTaskManager, createLatestRequestTracker } from '../../utils/requestTaskManager.js'
import { useUserPermissionsStore } from '../useUserPermissions.js'

const LIST_TASK_KEY = 'knowledge-base-entries-list'

export function useKnowledgeBaseEntries(knowledgeBaseId) {
  const { t } = useI18n()
  const userPermissions = useUserPermissionsStore()
  const currentUser = computed(() => userPermissions.currentUser)
  const isSuperuser = computed(() => userPermissions.isSuperuser === true)
  const selectedKb = ref(null)
  const isManaged = computed(() => selectedKb.value?.knowledge_base_type === 'llm_managed')
  const loading = ref(false)
  const items = ref([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(10)
  const query = ref('')
  const entryActionId = ref(null)
  const detailVisible = ref(false)
  const detailLoading = ref(false)
  const detailTitle = ref('')
  const detailContent = ref('')
  const detailError = ref(null)
  const historyVisible = ref(false)
  const historyLoading = ref(false)
  const historyTitle = ref('')
  const historyItems = ref([])
  const historyError = ref(null)

  let disposed = false
  let metadataRequestId = 0
  let detailRequestId = 0
  let historyRequestId = 0
  let actionGeneration = 0
  let pollGeneration = 0
  let pollTimer = null

  const scope = createKnowledgeBaseScope({
    isActive: () => !disposed && currentUser.value?.uid != null,
    confirm: (...args) => ElMessageBox.confirm(...args),
    closeConfirmations: () => ElMessageBox.close()
  })
  const listTaskManager = createAbortableTaskManager()
  const listRequestTracker = createLatestRequestTracker()

  const getValidKnowledgeBaseId = () => {
    const id = knowledgeBaseId.value
    return Number.isSafeInteger(id) && id > 0 ? id : null
  }

  const getCurrentUid = () => currentUser.value?.uid ?? null

  const isCurrentSession = (guard, kbId, uid, role, requireSelected = true) => (
    !disposed &&
    guard() &&
    getValidKnowledgeBaseId() === kbId &&
    getCurrentUid() === uid &&
    isSuperuser.value === role &&
    (!requireSelected || selectedKb.value?.id === kbId)
  )

  const clearPollingTimer = () => {
    if (pollTimer !== null) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  const stopPolling = () => {
    pollGeneration += 1
    clearPollingTimer()
  }

  const clearDetail = () => {
    detailRequestId += 1
    detailVisible.value = false
    detailLoading.value = false
    detailTitle.value = ''
    detailContent.value = ''
    detailError.value = null
  }

  const clearHistory = () => {
    historyRequestId += 1
    historyVisible.value = false
    historyLoading.value = false
    historyTitle.value = ''
    historyItems.value = []
    historyError.value = null
  }

  const resetPrivateState = () => {
    scope.invalidate()
    stopPolling()
    listTaskManager.invalidate()
    listRequestTracker.invalidate()
    metadataRequestId += 1
    detailRequestId += 1
    historyRequestId += 1
    actionGeneration += 1
    selectedKb.value = null
    loading.value = false
    items.value = []
    total.value = 0
    page.value = 1
    pageSize.value = 10
    query.value = ''
    entryActionId.value = null
    detailVisible.value = false
    detailLoading.value = false
    detailTitle.value = ''
    detailContent.value = ''
    detailError.value = null
    historyVisible.value = false
    historyLoading.value = false
    historyTitle.value = ''
    historyItems.value = []
    historyError.value = null
  }

  const loadSelectedKnowledgeBase = async (kbId, uid, role, guard) => {
    const requestId = ++metadataRequestId
    const isCurrent = () => (
      requestId === metadataRequestId &&
      isCurrentSession(guard, kbId, uid, role, false)
    )
    loading.value = true

    try {
      const res = await knowledgeBaseApi.get(kbId)
      if (!isCurrent()) return
      selectedKb.value = res.data.data
    } catch (requestError) {
      if (isCurrent()) {
        ElMessage.error(t('knowledgeBase.entries_load_failed', {
          message: requestError?.message || ''
        }))
        loading.value = false
      }
      return
    }

    if (isCurrent()) await fetchEntries(true, true)
  }

  const hasPendingPublication = row => (
    row?.pending_job_id != null ||
    ['pending', 'running', 'retry'].includes(row?.publication_job_status)
  )

  const schedulePolling = () => {
    clearPollingTimer()
    const kb = selectedKb.value
    const kbId = kb?.id
    const uid = getCurrentUid()
    const role = isSuperuser.value
    const guard = scope.capture()
    if (
      disposed ||
      !guard() ||
      !isManaged.value ||
      !Number.isSafeInteger(kbId) ||
      !items.value.some(hasPendingPublication)
    ) return

    const generation = pollGeneration
    let timer
    timer = setTimeout(async () => {
      if (
        pollTimer !== timer ||
        generation !== pollGeneration ||
        !isCurrentSession(guard, kbId, uid, role)
      ) return
      pollTimer = null
      await refresh(false)
    }, 2000)
    pollTimer = timer
  }

  const runFetchEntries = async (notifyError, replaceCurrent) => {
    stopPolling()
    const guard = scope.capture()
    const kb = selectedKb.value
    const kbId = kb?.id
    const uid = getCurrentUid()
    const role = isSuperuser.value
    if (!kb || !Number.isSafeInteger(kbId) || !isCurrentSession(guard, kbId, uid, role)) return

    const requestId = listRequestTracker.begin()
    if (replaceCurrent || listTaskManager.isRunning(LIST_TASK_KEY)) {
      listTaskManager.cancel(LIST_TASK_KEY)
    }
    const token = listTaskManager.begin(LIST_TASK_KEY)
    if (!token) return
    const isCurrent = () => (
      listRequestTracker.isCurrent(requestId) &&
      listTaskManager.isCurrent(token) &&
      isCurrentSession(guard, kbId, uid, role)
    )
    loading.value = true

    try {
      const res = isManaged.value
        ? await knowledgeBaseApi.managedItems(kbId, {
          page: page.value,
          size: pageSize.value,
          query: query.value
        }, { signal: token.signal })
        : await knowledgeBaseApi.documents(kbId, {
          page: page.value,
          size: pageSize.value
        })
      if (!isCurrent()) return
      const data = res?.data?.data || {}
      items.value = data.items || []
      total.value = data.total || 0
    } catch (requestError) {
      if (isCurrent() && !token.signal.aborted) {
        if (notifyError) ElMessage.error(t('knowledgeBase.entries_load_failed', {
          message: requestError?.message || ''
        }))
      }
    } finally {
      if (isCurrent()) {
        loading.value = false
        schedulePolling()
      }
      listTaskManager.finish(token)
    }
  }

  const fetchEntries = (notifyError = true, replaceCurrent = false) => (
    runFetchEntries(notifyError, replaceCurrent)
  )

  const refresh = async (notifyError = true) => {
    stopPolling()
    const kbId = getValidKnowledgeBaseId()
    const uid = getCurrentUid()
    const role = isSuperuser.value
    const guard = scope.capture()
    if (kbId === null || uid === null || !guard()) return

    const requestId = ++metadataRequestId
    const isCurrent = () => (
      requestId === metadataRequestId &&
      isCurrentSession(guard, kbId, uid, role, false)
    )
    listTaskManager.cancel(LIST_TASK_KEY)
    listRequestTracker.invalidate()
    loading.value = true

    try {
      const res = await knowledgeBaseApi.get(kbId)
      if (!isCurrent()) return
      selectedKb.value = res.data.data
    } catch (requestError) {
      if (isCurrent()) {
        loading.value = false
        if (notifyError) ElMessage.error(t('knowledgeBase.entries_load_failed', {
          message: requestError?.message || ''
        }))
      }
      return
    }

    if (isCurrent()) await fetchEntries(notifyError, true)
  }

  const handleSearch = () => {
    page.value = 1
    return fetchEntries(true, true)
  }

  const handlePageChange = () => fetchEntries(true, true)

  const handleSizeChange = () => {
    page.value = 1
    return fetchEntries(true, true)
  }

  const getEntryName = row => (isManaged.value ? row?.knowledge_key : row?.filename) || ''

  const getEntryStatus = (row) => {
    const indexStatus = selectedKb.value?.index_status
    let status
    if (indexStatus === 'failed') status = 'index_unavailable'
    else if (isManaged.value && hasPendingPublication(row)) status = 'processing'
    else if (isManaged.value && row?.publication_job_status === 'failed') status = 'failed'
    else if (isManaged.value && row?.publication_job_status === 'cancelled') status = 'cancelled'
    else if (['reindexing', 'pending'].includes(indexStatus)) status = 'indexing'
    else if (!isManaged.value) status = indexStatus === 'ready' ? 'ready' : 'pending'
    else if (row?.is_recallable === true && row?.indexed_version === row?.version) status = 'ready'
    else status = 'pending'
    return t(`knowledgeBase.entries_status_${status}`)
  }

  const getEntrySourceLabel = row => (
    isManaged.value
      ? t(`knowledgeBase.managed_source_${row?.source_type || 'system'}`)
      : t('knowledgeBase.entries_source_manual')
  )

  const viewEntry = async (row) => {
    const kb = selectedKb.value
    const kbId = kb?.id
    const uid = getCurrentUid()
    const role = isSuperuser.value
    const guard = scope.capture()
    if (!row || row.id == null || !Number.isSafeInteger(kbId) || !isCurrentSession(guard, kbId, uid, role)) return

    const requestId = ++detailRequestId
    const managed = isManaged.value
    const isCurrent = () => (
      requestId === detailRequestId &&
      isCurrentSession(guard, kbId, uid, role)
    )
    detailVisible.value = true
    detailLoading.value = true
    detailTitle.value = getEntryName(row)
    detailContent.value = ''
    detailError.value = null

    try {
      const res = managed
        ? await knowledgeBaseApi.managedItem(kbId, row.id)
        : await knowledgeBaseApi.document(kbId, row.id)
      if (!isCurrent()) return
      const content = res?.data?.data?.content
      detailContent.value = typeof content === 'string' ? content : ''
    } catch (requestError) {
      if (isCurrent()) {
        const key = managed ? 'managed_fetch_item_failed' : 'fetch_doc_content_failed'
        detailError.value = t(`knowledgeBase.${key}`) + (requestError?.message || '')
      }
    } finally {
      if (isCurrent()) detailLoading.value = false
    }
  }

  const closeDetail = () => clearDetail()

  const showHistory = async (row) => {
    const kb = selectedKb.value
    const kbId = kb?.id
    const uid = getCurrentUid()
    const role = isSuperuser.value
    const guard = scope.capture()
    if (!isManaged.value || !row || row.id == null || !Number.isSafeInteger(kbId) || !isCurrentSession(guard, kbId, uid, role)) return

    const requestId = ++historyRequestId
    const isCurrent = () => (
      requestId === historyRequestId &&
      isCurrentSession(guard, kbId, uid, role)
    )
    historyVisible.value = true
    historyLoading.value = true
    historyTitle.value = t('knowledgeBase.managed_history_title', { key: row.knowledge_key || '' })
    historyItems.value = []
    historyError.value = null

    try {
      const res = await knowledgeBaseApi.managedHistory(kbId, row.id)
      if (!isCurrent()) return
      const data = res?.data?.data
      historyItems.value = Array.isArray(data) ? data : []
    } catch (requestError) {
      if (isCurrent()) {
        historyError.value = t('knowledgeBase.managed_history_failed') + (requestError?.message || '')
      }
    } finally {
      if (isCurrent()) historyLoading.value = false
    }
  }

  const closeHistory = () => clearHistory()

  const isCurrentAction = (guard, kbId, uid, role, generation, rowId) => (
    generation === actionGeneration &&
    entryActionId.value === rowId &&
    isCurrentSession(guard, kbId, uid, role)
  )

  const showManagedMutationFeedback = (operation, status) => {
    const feedback = getManagedKnowledgeMutationFeedback(operation, status)
    ElMessage({ type: feedback.type, message: t(`knowledgeBase.${feedback.key}`) })
  }

  const deleteEntry = async (row) => {
    const kb = selectedKb.value
    const kbId = kb?.id
    const uid = getCurrentUid()
    const role = isSuperuser.value
    const guard = scope.capture()
    if (
      entryActionId.value !== null ||
      !row ||
      row.id == null ||
      !Number.isSafeInteger(kbId) ||
      !isCurrentSession(guard, kbId, uid, role)
    ) return

    const managed = isManaged.value
    const generation = ++actionGeneration
    const rowId = row.id
    entryActionId.value = rowId
    const isCurrent = () => isCurrentAction(guard, kbId, uid, role, generation, rowId)

    try {
      const confirmed = await scope.confirm(
        managed
          ? t('knowledgeBase.managed_delete_confirm', { key: row.knowledge_key })
          : t('knowledgeBase.delete_doc_confirm', { filename: row.filename }),
        t('knowledgeBase.prompt'),
        {
          confirmButtonText: managed ? t('knowledgeBase.confirm') : t('common.confirm'),
          cancelButtonText: managed ? t('knowledgeBase.cancel') : t('common.cancel'),
          type: 'warning',
          dangerouslyUseHTMLString: false
        }
      )
      if (!confirmed || !isCurrent()) return

      if (managed) {
        const res = await knowledgeBaseApi.deleteManagedItem(kbId, rowId, {
          expected_version: row.version,
          dedupe_key: createManagedKnowledgeDedupeKey('delete')
        })
        if (!isCurrent()) return
        showManagedMutationFeedback('delete', res?.data?.data?.status)
      } else {
        await knowledgeBaseApi.deleteDocument(kbId, rowId)
        if (!isCurrent()) return
        ElMessage.success(t('knowledgeBase.delete_doc_success'))
      }

      if (!isCurrent()) return
      if (
        page.value > 1 &&
        items.value.length === 1 &&
        total.value <= (page.value - 1) * pageSize.value + 1
      ) page.value -= 1
      await fetchEntries(true, true)
    } catch (requestError) {
      if (isCurrent()) {
        ElMessage.error(managed
          ? t('knowledgeBase.managed_delete_failed') + (requestError?.message || '')
          : requestError?.message || t('knowledgeBase.delete_doc_failed'))
      }
    } finally {
      if (isCurrent()) entryActionId.value = null
    }
  }

  const retryEntry = async (row) => {
    const kb = selectedKb.value
    const kbId = kb?.id
    const uid = getCurrentUid()
    const role = isSuperuser.value
    const guard = scope.capture()
    if (
      !isManaged.value ||
      entryActionId.value !== null ||
      !row ||
      row.id == null ||
      row.publication_job_status !== 'failed' ||
      row.publication_job_id == null ||
      !Number.isSafeInteger(kbId) ||
      !isCurrentSession(guard, kbId, uid, role)
    ) return

    const generation = ++actionGeneration
    const rowId = row.id
    entryActionId.value = rowId
    const isCurrent = () => isCurrentAction(guard, kbId, uid, role, generation, rowId)

    try {
      const res = await knowledgeBaseApi.retryManagedItem(kbId, rowId, {
        expected_version: row.version,
        failed_job_id: row.publication_job_id,
        dedupe_key: createManagedKnowledgeDedupeKey('retry')
      })
      if (!isCurrent()) return
      showManagedMutationFeedback('retry', res?.data?.data?.status)
      await fetchEntries(true, true)
    } catch (requestError) {
      if (isCurrent()) {
        ElMessage.error(t('knowledgeBase.managed_retry_failed') + (requestError?.message || ''))
      }
    } finally {
      if (isCurrent()) entryActionId.value = null
    }
  }

  watch(
    [knowledgeBaseId, () => currentUser.value?.uid ?? null, isSuperuser],
    ([routeKbId, uid, role]) => {
      resetPrivateState()
      if (uid == null || !Number.isSafeInteger(routeKbId) || routeKbId <= 0 || disposed) return
      const guard = scope.capture()
      void loadSelectedKnowledgeBase(routeKbId, uid, role, guard)
    },
    { immediate: true, flush: 'sync' }
  )

  onBeforeUnmount(() => {
    disposed = true
    resetPrivateState()
  })

  return {
    selectedKb,
    isManaged,
    loading,
    items,
    total,
    page,
    pageSize,
    query,
    entryActionId,
    scope,
    refresh,
    fetchEntries,
    schedulePolling,
    handleSearch,
    handlePageChange,
    handleSizeChange,
    getEntryName,
    getEntryStatus,
    getEntrySourceLabel,
    viewEntry,
    closeDetail,
    detailVisible,
    detailLoading,
    detailTitle,
    detailContent,
    detailError,
    showHistory,
    closeHistory,
    historyVisible,
    historyLoading,
    historyTitle,
    historyItems,
    historyError,
    deleteEntry,
    retryEntry
  }
}
