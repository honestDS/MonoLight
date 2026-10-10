import { ref, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'
import {
  canManageManagedKnowledge,
  createManagedKnowledgeDedupeKey,
  getManagedKnowledgeMutationFeedback
} from '../../utils/knowledgeBaseManagement.js'
import { createAbortableTaskManager, createLatestRequestTracker } from '../../utils/requestTaskManager.js'

export const useManagedKnowledge = ({ t, selectedKb }) => {
  const managedKnowledgeDialogVisible = ref(false)
  const managedKnowledgeLoading = ref(false)
  const managedKnowledgeItems = ref([])
  const managedKnowledgeTotal = ref(0)
  const managedKnowledgePage = ref(1)
  const managedKnowledgePageSize = ref(10)
  const managedKnowledgeQuery = ref('')
  const managedKnowledgeTableRef = ref(null)
  const managedKnowledgeSelectedIds = ref([])
  const managedKnowledgeEditDialogVisible = ref(false)
  const managedKnowledgeEditingId = ref(null)
  const managedKnowledgeFormRef = ref(null)
  const managedKnowledgeSubmitting = ref(false)
  const managedKnowledgeHistoryDialogVisible = ref(false)
  const managedKnowledgeHistoryLoading = ref(false)
  const managedKnowledgeHistory = ref([])
  const managedKnowledgeHistoryKey = ref('')
  let managedKnowledgePollTimer = null
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
    if (managedKnowledgeFormRef.value) managedKnowledgeFormRef.value.clearValidate()
    managedKnowledgeForm.knowledge_key = ''
    managedKnowledgeForm.content = ''
    managedKnowledgeForm.expected_version = null
    managedKnowledgeForm.llm_maintainable = false
  }

  const clearManagedKnowledgeSelection = () => {
    managedKnowledgeSelectedIds.value = []
    managedKnowledgeTableRef.value?.clearSelection()
  }

  const isManagedKnowledgeOrganizable = (item) => Boolean(
    item?.llm_maintainable === true
    && item?.is_recallable === true
    && !item?.pending_job_id
    && item?.indexed_version === item?.version
  )

  const handleManagedKnowledgeSelectionChange = (selection) => {
    managedKnowledgeSelectedIds.value = selection
      .filter(isManagedKnowledgeOrganizable)
      .map(item => item.id)
      .filter(id => id !== null && id !== undefined)
  }

  const managedKnowledgeListTaskKey = 'managed-knowledge-list'

  const fetchManagedKnowledgeItems = async (notifyError = true, replaceCurrent = false) => {
    if (!selectedKb.value || !canManageManagedKnowledge(selectedKb.value)) return
    const selectedId = selectedKb.value.id
    if (replaceCurrent) {
      managedKnowledgeTaskManager.cancel(managedKnowledgeListTaskKey)
      clearManagedKnowledgeSelection()
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
        !managedKnowledgeTaskManager.isCurrent(token)
        || !managedKnowledgeRequestTracker.isCurrent(requestSeq)
        || selectedKb.value?.id !== selectedId
      ) return
      managedKnowledgeItems.value = res.data.data.items || []
      managedKnowledgeTotal.value = res.data.data.total || 0
      const pageIds = new Set(managedKnowledgeItems.value.map(item => item.id))
      managedKnowledgeSelectedIds.value = managedKnowledgeSelectedIds.value.filter(id => pageIds.has(id))
    } catch (error) {
      if (token.signal.aborted || !managedKnowledgeTaskManager.isCurrent(token)) return
      if (notifyError && managedKnowledgeRequestTracker.isCurrent(requestSeq)) {
        ElMessage.error(t('knowledgeBase.managed_fetch_failed') + error.message)
      }
    } finally {
      if (managedKnowledgeTaskManager.isCurrent(token) && managedKnowledgeRequestTracker.isCurrent(requestSeq)) {
        managedKnowledgeLoading.value = false
      }
      managedKnowledgeTaskManager.finish(token)
    }
  }

  const stopManagedKnowledgePolling = () => {
    if (managedKnowledgePollTimer) {
      clearTimeout(managedKnowledgePollTimer)
      managedKnowledgePollTimer = null
    }
    managedKnowledgeTaskManager.invalidate()
    managedKnowledgeRequestTracker.invalidate()
    managedKnowledgeLoading.value = false
  }

  const scheduleManagedKnowledgePolling = () => {
    if (managedKnowledgePollTimer) {
      clearTimeout(managedKnowledgePollTimer)
      managedKnowledgePollTimer = null
    }
    if (!managedKnowledgeDialogVisible.value || !managedKnowledgeItems.value.some(item => item.pending_job_id)) return
    managedKnowledgePollTimer = setTimeout(async () => {
      try {
        await fetchManagedKnowledgeItems(false)
      } finally {
        scheduleManagedKnowledgePolling()
      }
    }, 2000)
  }

  const showManagedKnowledgeDialog = async (row) => {
    stopManagedKnowledgePolling()
    selectedKb.value = row
    managedKnowledgePage.value = 1
    managedKnowledgeQuery.value = ''
    managedKnowledgeItems.value = []
    managedKnowledgeTotal.value = 0
    clearManagedKnowledgeSelection()
    managedKnowledgeDialogVisible.value = true
    await fetchManagedKnowledgeItems(true, true)
    scheduleManagedKnowledgePolling()
  }

  const handleManagedKnowledgeSearch = async () => {
    managedKnowledgePage.value = 1
    clearManagedKnowledgeSelection()
    await fetchManagedKnowledgeItems(true, true)
    scheduleManagedKnowledgePolling()
  }

  const handleManagedKnowledgeSizeChange = async () => {
    managedKnowledgePage.value = 1
    clearManagedKnowledgeSelection()
    await fetchManagedKnowledgeItems(true, true)
    scheduleManagedKnowledgePolling()
  }

  const handleManagedKnowledgePageChange = async () => {
    clearManagedKnowledgeSelection()
    await fetchManagedKnowledgeItems(true, true)
    scheduleManagedKnowledgePolling()
  }

  const showManagedKnowledgeCreateDialog = () => {
    resetManagedKnowledgeForm()
    managedKnowledgeEditingId.value = null
    managedKnowledgeEditDialogVisible.value = true
  }

  const showManagedKnowledgeEditDialog = async (row) => {
    if (!selectedKb.value) return
    resetManagedKnowledgeForm()
    try {
      const res = await knowledgeBaseApi.managedItem(selectedKb.value.id, row.id)
      const item = res.data.data
      managedKnowledgeEditingId.value = item.id
      managedKnowledgeForm.knowledge_key = item.knowledge_key
      managedKnowledgeForm.content = item.content
      managedKnowledgeForm.expected_version = item.version
      managedKnowledgeForm.llm_maintainable = Boolean(item.llm_maintainable)
      managedKnowledgeEditDialogVisible.value = true
    } catch (error) {
      ElMessage.error(t('knowledgeBase.managed_fetch_item_failed') + error.message)
    }
  }

  const showManagedKnowledgeMutationFeedback = (operation, status) => {
    const feedback = getManagedKnowledgeMutationFeedback(operation, status)
    ElMessage({ type: feedback.type, message: t(`knowledgeBase.${feedback.key}`) })
  }

  const submitManagedKnowledge = async () => {
    if (!managedKnowledgeFormRef.value || !selectedKb.value) return
    await managedKnowledgeFormRef.value.validate(async (valid) => {
      if (!valid) return
      managedKnowledgeSubmitting.value = true
      try {
        const payload = {
          knowledge_key: managedKnowledgeForm.knowledge_key,
          content: managedKnowledgeForm.content,
          llm_maintainable: managedKnowledgeForm.llm_maintainable
        }
        let operation = 'create'
        let res
        if (managedKnowledgeEditingId.value) {
          operation = 'update'
          res = await knowledgeBaseApi.updateManagedItem(selectedKb.value.id, managedKnowledgeEditingId.value, {
            ...payload,
            expected_version: managedKnowledgeForm.expected_version,
            dedupe_key: createManagedKnowledgeDedupeKey('update')
          })
        } else {
          res = await knowledgeBaseApi.createManagedItem(selectedKb.value.id, {
            ...payload,
            dedupe_key: createManagedKnowledgeDedupeKey('create')
          })
        }
        const status = res.data.data.status
        showManagedKnowledgeMutationFeedback(operation, status)
        if (!['existing_key', 'existing_content'].includes(status)) {
          managedKnowledgeEditDialogVisible.value = false
        }
        await fetchManagedKnowledgeItems(true, true)
        scheduleManagedKnowledgePolling()
      } catch (error) {
        ElMessage.error((managedKnowledgeEditingId.value ? t('knowledgeBase.managed_update_failed') : t('knowledgeBase.managed_create_failed')) + error.message)
      } finally {
        managedKnowledgeSubmitting.value = false
      }
    })
  }

  const handleDeleteManagedKnowledge = async (row) => {
    if (!selectedKb.value) return
    try {
      await ElMessageBox.confirm(
        t('knowledgeBase.managed_delete_confirm', { key: row.knowledge_key }),
        t('knowledgeBase.prompt'),
        {
          confirmButtonText: t('knowledgeBase.confirm'),
          cancelButtonText: t('knowledgeBase.cancel'),
          type: 'warning'
        }
      )
    } catch (action) {
      if (action === 'cancel' || action === 'close') return
      throw action
    }
    try {
      const res = await knowledgeBaseApi.deleteManagedItem(selectedKb.value.id, row.id, {
        expected_version: row.version,
        dedupe_key: createManagedKnowledgeDedupeKey('delete')
      })
      showManagedKnowledgeMutationFeedback('delete', res.data.data.status)
      await fetchManagedKnowledgeItems(true, true)
      scheduleManagedKnowledgePolling()
    } catch (error) {
      ElMessage.error(t('knowledgeBase.managed_delete_failed') + error.message)
    }
  }

  const retryManagedKnowledgePublication = async (row) => {
    if (
      !selectedKb.value
      || row.publication_job_status !== 'failed'
      || !row.publication_job_id
    ) return
    try {
      const res = await knowledgeBaseApi.retryManagedItem(selectedKb.value.id, row.id, {
        expected_version: row.version,
        failed_job_id: row.publication_job_id,
        dedupe_key: createManagedKnowledgeDedupeKey('retry')
      })
      showManagedKnowledgeMutationFeedback('retry', res.data.data.status)
      await fetchManagedKnowledgeItems(true, true)
      scheduleManagedKnowledgePolling()
    } catch (error) {
      ElMessage.error(t('knowledgeBase.managed_retry_failed') + error.message)
    }
  }

  const showManagedKnowledgeHistory = async (row) => {
    if (!selectedKb.value) return
    managedKnowledgeHistoryKey.value = row.knowledge_key
    managedKnowledgeHistory.value = []
    managedKnowledgeHistoryDialogVisible.value = true
    managedKnowledgeHistoryLoading.value = true
    try {
      const res = await knowledgeBaseApi.managedHistory(selectedKb.value.id, row.id)
      managedKnowledgeHistory.value = res.data.data || []
    } catch (error) {
      ElMessage.error(t('knowledgeBase.managed_history_failed') + error.message)
    } finally {
      managedKnowledgeHistoryLoading.value = false
    }
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
    managedKnowledgeTableRef,
    managedKnowledgeSelectedIds,
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
    isManagedKnowledgeOrganizable,
    handleManagedKnowledgeSelectionChange,
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
