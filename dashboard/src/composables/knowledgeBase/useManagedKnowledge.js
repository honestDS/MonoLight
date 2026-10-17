import { ref, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'
import {
  canManageManagedKnowledge,
  createManagedKnowledgeDedupeKey,
  getManagedKnowledgeMutationFeedback
} from '../../utils/knowledgeBaseManagement.js'
import { createAbortableTaskManager, createLatestRequestTracker } from '../../utils/requestTaskManager.js'

export const useManagedKnowledge = ({ t, selectedKb, scope }) => {
  const managedKnowledgeDialogVisible = ref(false)
  const managedKnowledgeLoading = ref(false)
  const managedKnowledgeItems = ref([])
  const managedKnowledgeTotal = ref(0)
  const managedKnowledgePage = ref(1)
  const managedKnowledgePageSize = ref(10)
  const managedKnowledgeQuery = ref('')
  const managedKnowledgeEditDialogVisible = ref(false)
  const managedKnowledgeEditingId = ref(null)
  const managedKnowledgeFormRef = ref(null)
  const managedKnowledgeSubmitting = ref(false)
  const managedKnowledgeHistoryDialogVisible = ref(false)
  const managedKnowledgeHistoryLoading = ref(false)
  const managedKnowledgeHistory = ref([])
  const managedKnowledgeHistoryKey = ref('')
  let managedKnowledgePollTimer = null
  let managedKnowledgePollSession = 0
  let managedKnowledgeDetailRequestSeq = 0
  let managedKnowledgeHistoryRequestSeq = 0
  let managedKnowledgeFormGeneration = 0
  const managedKnowledgeTaskManager = createAbortableTaskManager()
  const managedKnowledgeRequestTracker = createLatestRequestTracker()

  const managedKnowledgeForm = reactive({
    knowledge_key: '',
    content: '',
    expected_version: null,
    llm_maintainable: false
  })

  const managedKnowledgeRules = computed(() => ({
    knowledge_key: [{ required: true, message: t('knowledgeBase.managed_key_required'), trigger: 'blur' }],
    content: [{ required: true, message: t('knowledgeBase.managed_content_required'), trigger: 'blur' }]
  }))

  const resetManagedKnowledgeForm = () => {
    managedKnowledgeFormGeneration += 1
    managedKnowledgeDetailRequestSeq += 1
    managedKnowledgeSubmitting.value = false
    if (managedKnowledgeFormRef.value) managedKnowledgeFormRef.value.clearValidate()
    managedKnowledgeForm.knowledge_key = ''
    managedKnowledgeForm.content = ''
    managedKnowledgeForm.expected_version = null
    managedKnowledgeForm.llm_maintainable = false
  }

  const managedKnowledgeListTaskKey = 'managed-knowledge-list'

  const isManagedKnowledgeCurrent = (isScopeCurrent, selectedId) => (
    selectedId !== null
    && selectedId !== undefined
    && isScopeCurrent()
    && selectedKb.value?.id === selectedId
  )

  const isManagedKnowledgePollingCurrent = (isScopeCurrent, session, selectedId) => (
    isManagedKnowledgeCurrent(isScopeCurrent, selectedId)
    && managedKnowledgePollSession === session
  )

  const fetchManagedKnowledgeItems = async (notifyError = true, replaceCurrent = false) => {
    const isScopeCurrent = scope.capture()
    if (!isScopeCurrent() || !selectedKb.value || !canManageManagedKnowledge(selectedKb.value)) return
    const selectedId = selectedKb.value.id
    const isCurrent = () => isManagedKnowledgeCurrent(isScopeCurrent, selectedId)
    if (replaceCurrent) {
      managedKnowledgeTaskManager.cancel(managedKnowledgeListTaskKey)
    }
    const token = managedKnowledgeTaskManager.begin(managedKnowledgeListTaskKey)
    if (!token) return
    const requestSeq = managedKnowledgeRequestTracker.begin()
    if (managedKnowledgeTaskManager.isCurrent(token)) managedKnowledgeLoading.value = true
    try {
      const res = await knowledgeBaseApi.managedItems(selectedId, {
        page: managedKnowledgePage.value,
        size: managedKnowledgePageSize.value,
        query: managedKnowledgeQuery.value
      }, { signal: token.signal })
      if (
        !isCurrent()
        || !managedKnowledgeTaskManager.isCurrent(token)
        || !managedKnowledgeRequestTracker.isCurrent(requestSeq)
      ) return
      managedKnowledgeItems.value = res.data.data.items || []
      managedKnowledgeTotal.value = res.data.data.total || 0
    } catch (error) {
      if (
        !isCurrent()
        || token.signal.aborted
        || !managedKnowledgeTaskManager.isCurrent(token)
        || !managedKnowledgeRequestTracker.isCurrent(requestSeq)
      ) return
      if (notifyError && managedKnowledgeRequestTracker.isCurrent(requestSeq)) {
        ElMessage.error(t('knowledgeBase.managed_fetch_failed') + error.message)
      }
    } finally {
      if (
        isCurrent()
        && managedKnowledgeTaskManager.isCurrent(token)
        && managedKnowledgeRequestTracker.isCurrent(requestSeq)
      ) {
        managedKnowledgeLoading.value = false
      }
      managedKnowledgeTaskManager.finish(token)
    }
  }

  const stopManagedKnowledgePolling = () => {
    managedKnowledgePollSession += 1
    if (managedKnowledgePollTimer) {
      clearTimeout(managedKnowledgePollTimer)
      managedKnowledgePollTimer = null
    }
    managedKnowledgeTaskManager.invalidate()
    managedKnowledgeRequestTracker.invalidate()
    managedKnowledgeLoading.value = false
  }

  const scheduleManagedKnowledgePolling = (
    capturedScopeCurrent = scope.capture(),
    session = managedKnowledgePollSession,
    selectedId = selectedKb.value?.id
  ) => {
    if (!isManagedKnowledgePollingCurrent(capturedScopeCurrent, session, selectedId)) return
    if (managedKnowledgePollTimer) {
      clearTimeout(managedKnowledgePollTimer)
      managedKnowledgePollTimer = null
    }
    if (!managedKnowledgeDialogVisible.value || !managedKnowledgeItems.value.some(item => item.pending_job_id)) return
    let timer
    timer = setTimeout(async () => {
      if (
        !isManagedKnowledgePollingCurrent(capturedScopeCurrent, session, selectedId)
        || managedKnowledgePollTimer !== timer
      ) return
      managedKnowledgePollTimer = null
      try {
        await fetchManagedKnowledgeItems(false)
      } finally {
        if (isManagedKnowledgePollingCurrent(capturedScopeCurrent, session, selectedId)) {
          scheduleManagedKnowledgePolling(capturedScopeCurrent, session, selectedId)
        }
      }
    }, 2000)
    managedKnowledgePollTimer = timer
  }

  const showManagedKnowledgeDialog = async (row) => {
    const isScopeCurrent = scope.capture()
    if (!isScopeCurrent() || !row?.id) return
    stopManagedKnowledgePolling()
    const session = managedKnowledgePollSession
    selectedKb.value = row
    const selectedId = row.id
    if (!isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) return
    managedKnowledgePage.value = 1
    managedKnowledgeQuery.value = ''
    managedKnowledgeItems.value = []
    managedKnowledgeTotal.value = 0
    managedKnowledgeDialogVisible.value = true
    await fetchManagedKnowledgeItems(true, true)
    if (isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) {
      scheduleManagedKnowledgePolling(isScopeCurrent, session, selectedId)
    }
  }

  const handleManagedKnowledgeSearch = async () => {
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const session = managedKnowledgePollSession
    if (!isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) return
    managedKnowledgePage.value = 1
    await fetchManagedKnowledgeItems(true, true)
    if (isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) {
      scheduleManagedKnowledgePolling(isScopeCurrent, session, selectedId)
    }
  }

  const handleManagedKnowledgeSizeChange = async () => {
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const session = managedKnowledgePollSession
    if (!isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) return
    managedKnowledgePage.value = 1
    await fetchManagedKnowledgeItems(true, true)
    if (isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) {
      scheduleManagedKnowledgePolling(isScopeCurrent, session, selectedId)
    }
  }

  const handleManagedKnowledgePageChange = async () => {
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const session = managedKnowledgePollSession
    if (!isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) return
    await fetchManagedKnowledgeItems(true, true)
    if (isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) {
      scheduleManagedKnowledgePolling(isScopeCurrent, session, selectedId)
    }
  }

  const showManagedKnowledgeCreateDialog = () => {
    const isScopeCurrent = scope.capture()
    if (!isScopeCurrent() || !selectedKb.value) return
    resetManagedKnowledgeForm()
    managedKnowledgeEditingId.value = null
    managedKnowledgeEditDialogVisible.value = true
  }

  const showManagedKnowledgeEditDialog = async (row) => {
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const itemId = row?.id
    if (!isManagedKnowledgeCurrent(isScopeCurrent, selectedId) || !itemId) return
    resetManagedKnowledgeForm()
    managedKnowledgeEditingId.value = null
    const formGeneration = managedKnowledgeFormGeneration
    const requestSeq = ++managedKnowledgeDetailRequestSeq
    const isCurrent = () => (
      isManagedKnowledgeCurrent(isScopeCurrent, selectedId)
      && managedKnowledgeDetailRequestSeq === requestSeq
      && managedKnowledgeFormGeneration === formGeneration
    )
    try {
      const res = await knowledgeBaseApi.managedItem(selectedId, itemId)
      if (!isCurrent()) return
      const item = res.data.data
      managedKnowledgeEditingId.value = item.id
      managedKnowledgeForm.knowledge_key = item.knowledge_key
      managedKnowledgeForm.content = item.content
      managedKnowledgeForm.expected_version = item.version
      managedKnowledgeForm.llm_maintainable = Boolean(item.llm_maintainable)
      managedKnowledgeEditDialogVisible.value = true
    } catch (error) {
      if (isCurrent()) ElMessage.error(t('knowledgeBase.managed_fetch_item_failed') + error.message)
    }
  }

  const showManagedKnowledgeMutationFeedback = (operation, status) => {
    const feedback = getManagedKnowledgeMutationFeedback(operation, status)
    ElMessage({ type: feedback.type, message: t(`knowledgeBase.${feedback.key}`) })
  }

  const submitManagedKnowledge = async () => {
    const formRef = managedKnowledgeFormRef.value
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const editingId = managedKnowledgeEditingId.value
    const formGeneration = managedKnowledgeFormGeneration
    const session = managedKnowledgePollSession
    if (!formRef || !isManagedKnowledgeCurrent(isScopeCurrent, selectedId) || managedKnowledgeSubmitting.value) return
    const isCurrent = () => (
      isManagedKnowledgeCurrent(isScopeCurrent, selectedId)
      && managedKnowledgeFormGeneration === formGeneration
    )
    managedKnowledgeSubmitting.value = true

    let valid
    try {
      valid = await formRef.validate()
    } catch (_error) {
      if (isCurrent()) managedKnowledgeSubmitting.value = false
      return
    }
    if (!isCurrent()) return
    if (!valid) {
      managedKnowledgeSubmitting.value = false
      return
    }

    const payload = {
      knowledge_key: managedKnowledgeForm.knowledge_key,
      content: managedKnowledgeForm.content,
      llm_maintainable: managedKnowledgeForm.llm_maintainable
    }
    const operation = editingId ? 'update' : 'create'
    try {
      let res
      if (editingId) {
        res = await knowledgeBaseApi.updateManagedItem(selectedId, editingId, {
          ...payload,
          expected_version: managedKnowledgeForm.expected_version,
          dedupe_key: createManagedKnowledgeDedupeKey('update')
        })
      } else {
        res = await knowledgeBaseApi.createManagedItem(selectedId, {
          ...payload,
          dedupe_key: createManagedKnowledgeDedupeKey('create')
        })
      }
      if (!isCurrent()) return
      const status = res.data.data.status
      showManagedKnowledgeMutationFeedback(operation, status)
      if (!['existing_key', 'existing_content'].includes(status)) {
        managedKnowledgeEditDialogVisible.value = false
      }
      await fetchManagedKnowledgeItems(true, true)
      if (isCurrent() && isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) {
        scheduleManagedKnowledgePolling(isScopeCurrent, session, selectedId)
      }
    } catch (error) {
      if (isCurrent()) {
        const failureKey = editingId ? 'knowledgeBase.managed_update_failed' : 'knowledgeBase.managed_create_failed'
        ElMessage.error(t(failureKey) + error.message)
      }
    } finally {
      if (isCurrent()) managedKnowledgeSubmitting.value = false
    }
  }

  const handleDeleteManagedKnowledge = async (row) => {
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const itemId = row?.id
    const session = managedKnowledgePollSession
    if (!isManagedKnowledgeCurrent(isScopeCurrent, selectedId) || !itemId) return
    const key = row.knowledge_key
    const version = row.version
    const confirmed = await scope.confirm(
      t('knowledgeBase.managed_delete_confirm', { key }),
      t('knowledgeBase.prompt'),
      {
        confirmButtonText: t('knowledgeBase.confirm'),
        cancelButtonText: t('knowledgeBase.cancel'),
        type: 'warning'
      }
    )
    if (!isManagedKnowledgeCurrent(isScopeCurrent, selectedId) || !confirmed) return
    try {
      const res = await knowledgeBaseApi.deleteManagedItem(selectedId, itemId, {
        expected_version: version,
        dedupe_key: createManagedKnowledgeDedupeKey('delete')
      })
      if (!isManagedKnowledgeCurrent(isScopeCurrent, selectedId)) return
      showManagedKnowledgeMutationFeedback('delete', res.data.data.status)
      await fetchManagedKnowledgeItems(true, true)
      if (isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) {
        scheduleManagedKnowledgePolling(isScopeCurrent, session, selectedId)
      }
    } catch (error) {
      if (isManagedKnowledgeCurrent(isScopeCurrent, selectedId)) {
        ElMessage.error(t('knowledgeBase.managed_delete_failed') + error.message)
      }
    }
  }

  const retryManagedKnowledgePublication = async (row) => {
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const itemId = row?.id
    const session = managedKnowledgePollSession
    if (
      !isManagedKnowledgeCurrent(isScopeCurrent, selectedId)
      || !itemId
      || row.publication_job_status !== 'failed'
      || !row.publication_job_id
    ) return
    const version = row.version
    const failedJobId = row.publication_job_id
    try {
      const res = await knowledgeBaseApi.retryManagedItem(selectedId, itemId, {
        expected_version: version,
        failed_job_id: failedJobId,
        dedupe_key: createManagedKnowledgeDedupeKey('retry')
      })
      if (!isManagedKnowledgeCurrent(isScopeCurrent, selectedId)) return
      showManagedKnowledgeMutationFeedback('retry', res.data.data.status)
      await fetchManagedKnowledgeItems(true, true)
      if (isManagedKnowledgePollingCurrent(isScopeCurrent, session, selectedId)) {
        scheduleManagedKnowledgePolling(isScopeCurrent, session, selectedId)
      }
    } catch (error) {
      if (isManagedKnowledgeCurrent(isScopeCurrent, selectedId)) {
        ElMessage.error(t('knowledgeBase.managed_retry_failed') + error.message)
      }
    }
  }

  const showManagedKnowledgeHistory = async (row) => {
    const isScopeCurrent = scope.capture()
    const selectedId = selectedKb.value?.id
    const itemId = row?.id
    if (!isManagedKnowledgeCurrent(isScopeCurrent, selectedId) || !itemId) return
    const requestSeq = ++managedKnowledgeHistoryRequestSeq
    const isCurrent = () => (
      isManagedKnowledgeCurrent(isScopeCurrent, selectedId)
      && managedKnowledgeHistoryRequestSeq === requestSeq
    )
    managedKnowledgeHistoryKey.value = row.knowledge_key
    managedKnowledgeHistory.value = []
    managedKnowledgeHistoryDialogVisible.value = true
    managedKnowledgeHistoryLoading.value = true
    try {
      const res = await knowledgeBaseApi.managedHistory(selectedId, itemId)
      if (!isCurrent()) return
      managedKnowledgeHistory.value = res.data.data || []
    } catch (error) {
      if (isCurrent()) ElMessage.error(t('knowledgeBase.managed_history_failed') + error.message)
    } finally {
      if (isCurrent()) managedKnowledgeHistoryLoading.value = false
    }
  }

  const resetManagedKnowledgeState = () => {
    stopManagedKnowledgePolling()
    managedKnowledgeHistoryRequestSeq += 1
    managedKnowledgeDialogVisible.value = false
    managedKnowledgeEditDialogVisible.value = false
    managedKnowledgeHistoryDialogVisible.value = false
    managedKnowledgeItems.value = []
    managedKnowledgeTotal.value = 0
    managedKnowledgePage.value = 1
    managedKnowledgePageSize.value = 10
    managedKnowledgeQuery.value = ''
    managedKnowledgeEditingId.value = null
    managedKnowledgeHistoryKey.value = ''
    managedKnowledgeHistory.value = []
    managedKnowledgeHistoryLoading.value = false
    resetManagedKnowledgeForm()
  }

  const handleManagedKnowledgeMoreAction = (command, row) => {
    if (command === 'history') {
      showManagedKnowledgeHistory(row)
      return
    }
    if (command === 'retry') {
      retryManagedKnowledgePublication(row)
      return
    }
    if (command === 'delete') handleDeleteManagedKnowledge(row)
  }

  const getManagedSourceLabel = (sourceType) => t(`knowledgeBase.managed_source_${sourceType || 'system'}`)
  const getManagedActorLabel = (actor) => t(`knowledgeBase.managed_actor_${actor || 'system'}`)
  const getManagedOperationLabel = (operation) => t(`knowledgeBase.managed_operation_${operation || 'update'}`)

  const getManagedKnowledgeStatusLabel = (row) => {
    if (row.pending_job_id || ['pending', 'running', 'retry'].includes(row.publication_job_status)) {
      return t('knowledgeBase.managed_status_processing')
    }
    if (row.publication_job_status === 'failed') return t('knowledgeBase.managed_status_failed')
    if (row.publication_job_status === 'cancelled') return t('knowledgeBase.managed_status_cancelled')
    if (row.is_recallable && row.indexed_version === row.version) return t('knowledgeBase.managed_status_ready')
    return t('knowledgeBase.managed_status_pending')
  }

  const formatManagedSnapshot = (snapshot) => {
    if (!snapshot || typeof snapshot !== 'object') return '-'
    return snapshot.content || snapshot.knowledge_key || '-'
  }

  const formatManagedSourceReference = (sourceReference) => {
    if (!sourceReference || typeof sourceReference !== 'object') return '-'
    try {
      return JSON.stringify(sourceReference)
    } catch (_error) {
      return '-'
    }
  }

  return {
    managedKnowledgeDialogVisible,
    managedKnowledgeLoading,
    managedKnowledgeItems,
    managedKnowledgeTotal,
    managedKnowledgePage,
    managedKnowledgePageSize,
    managedKnowledgeQuery,
    managedKnowledgeEditDialogVisible,
    managedKnowledgeEditingId,
    managedKnowledgeFormRef,
    managedKnowledgeSubmitting,
    managedKnowledgeHistoryDialogVisible,
    managedKnowledgeHistoryLoading,
    managedKnowledgeHistory,
    managedKnowledgeHistoryKey,
    managedKnowledgeForm,
    managedKnowledgeRules,
    resetManagedKnowledgeState,
    fetchManagedKnowledgeItems,
    scheduleManagedKnowledgePolling,
    stopManagedKnowledgePolling,
    showManagedKnowledgeDialog,
    handleManagedKnowledgeSearch,
    handleManagedKnowledgeSizeChange,
    handleManagedKnowledgePageChange,
    showManagedKnowledgeCreateDialog,
    showManagedKnowledgeEditDialog,
    submitManagedKnowledge,
    handleDeleteManagedKnowledge,
    retryManagedKnowledgePublication,
    showManagedKnowledgeHistory,
    handleManagedKnowledgeMoreAction,
    getManagedSourceLabel,
    getManagedActorLabel,
    getManagedOperationLabel,
    getManagedKnowledgeStatusLabel,
    formatManagedSnapshot,
    formatManagedSourceReference
  }
}
