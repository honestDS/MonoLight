import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { knowledgeBaseApi } from '../../api/index.js'
import { formatTime } from '../../utils/index.js'
import {
  canStartKnowledgeBaseMigration,
  getKnowledgeBaseMigrationProgress,
  getManagedKnowledgeBaseMigrationTerminalHintKey,
  isKnowledgeBaseMigrationActive
} from '../../utils/knowledgeBaseMigration.js'
import {
  canManageKnowledgeBaseDocuments,
  canManageManagedKnowledge,
  getKnowledgeBaseProfileIds,
  normalizeKnowledgeBase
} from '../../utils/knowledgeBaseManagement.js'
import { useDeleteConfirm } from '../useDeleteConfirm.js'
import { useKnowledgeBaseDocuments } from './useKnowledgeBaseDocuments.js'
import { useManagedKnowledge } from './useManagedKnowledge.js'
import { useKnowledgeOrganization } from './useKnowledgeOrganization.js'

export function useKnowledgeBaseView() {
  const { t } = useI18n()

  const tableData = ref([])
  const loading = ref(false)
  const total = ref(0)
  const currentPage = ref(1)
  const pageSize = ref(20)
  const dialogVisible = ref(false)
  const submitting = ref(false)
  const isEditing = ref(false)
  const editingId = ref(null)
  const embeddingModels = ref([])
  const formRef = ref(null)
  const selectedKb = ref(null)
  const migrationDialogVisible = ref(false)
  const migrationTargetKey = ref('')
  const migrationSubmitting = ref(false)
  let migrationPollTimer = null
  const queryTestDialogVisible = ref(false)
  const queryTesting = ref(false)
  const queryTested = ref(false)
  const queryTestFormRef = ref(null)
  const queryTestResults = ref([])
  const retrievalMode = ref(null)
  const rerankError = ref(null)

  const form = reactive({
    name: '',
    description: '',
    embedding_model_key: ''
  })

  const rules = computed(() => ({
    name: [{ required: true, message: t('knowledgeBase.input_kb_name'), trigger: 'blur' }],
    embedding_model_key: [{ required: !isEditing.value, message: t('knowledgeBase.select_embedding_model_err'), trigger: 'change' }]
  }))

  const embeddingModelOptions = computed(() => embeddingModels.value.map(item => ({
    ...item,
    key: `${item.channel_id}::${item.model_id}`,
    label: `${item.channel_name} / ${item.model_id}${item.embedding_dimensions ? ` (${item.embedding_dimensions})` : ''}`
  })))

  const migrationProgress = computed(() => getKnowledgeBaseMigrationProgress(selectedKb.value))
  const managedMigrationTerminalHintKey = computed(() => getManagedKnowledgeBaseMigrationTerminalHintKey(selectedKb.value))

  const queryTestForm = reactive({
    query: '',
    top_k: 5
  })

  const queryTestRules = computed(() => ({
    query: [{ required: true, message: t('knowledgeBase.input_query_err'), trigger: 'blur' }],
    top_k: [{ required: true, message: t('knowledgeBase.set_top_k'), trigger: 'blur' }]
  }))

  const documents = useKnowledgeBaseDocuments({ t, selectedKb })
  const managedKnowledge = useManagedKnowledge({ t, selectedKb })
  const organization = useKnowledgeOrganization({
    t,
    selectedKb,
    managedKnowledgeSelectedIds: managedKnowledge.managedKnowledgeSelectedIds,
    fetchManagedKnowledgeItems: managedKnowledge.fetchManagedKnowledgeItems,
    scheduleManagedKnowledgePolling: managedKnowledge.scheduleManagedKnowledgePolling
  })

  const fetchData = async () => {
    loading.value = true
    try {
      const res = await knowledgeBaseApi.list({
        page: currentPage.value,
        size: pageSize.value
      })
      const { items, total: totalCount, embedding_models: embeddingModelItems } = res.data.data
      tableData.value = (items || []).map(normalizeKnowledgeBase)
      total.value = totalCount || 0
      embeddingModels.value = embeddingModelItems || []
    } catch (error) {
      ElMessage.error(t('knowledgeBase.fetch_list_failed') + error.message)
    } finally {
      loading.value = false
    }
  }

  const handleRefresh = () => {
    currentPage.value = 1
    fetchData()
  }

  const handleSizeChange = () => {
    currentPage.value = 1
    fetchData()
  }

  const getKnowledgeBaseProfileLabel = (row) => {
    const profileIds = getKnowledgeBaseProfileIds(row)
    return profileIds.length ? profileIds.map(id => `#${id}`).join(', ') : '-'
  }

  const getEmbeddingModelName = (row) => {
    if (!row) return '-'
    const channelId = row.active_embedding_channel_id || row.embedding_channel_id
    const modelId = row.active_embedding_model_id || row.embedding_model_id
    const option = embeddingModelOptions.value.find(item => item.channel_id === channelId && item.model_id === modelId)
    return option?.label || modelId || '-'
  }

  const getTargetEmbeddingModelName = (row) => {
    if (!row?.target_embedding_channel_id || !row?.target_embedding_model_id) return '-'
    const option = embeddingModelOptions.value.find(
      item => item.channel_id === row.target_embedding_channel_id && item.model_id === row.target_embedding_model_id
    )
    const name = option ? `${option.channel_name} / ${option.model_id}` : row.target_embedding_model_id
    return row.target_embedding_dimensions ? `${name} (${row.target_embedding_dimensions})` : name
  }

  const getMigrationStatusLabel = (status) => {
    const key = status || 'idle'
    return t(`knowledgeBase.migration_status_${key}`)
  }

  const getCleanupStatusLabel = (status) => {
    const key = status || 'none'
    return t(`knowledgeBase.cleanup_status_${key}`)
  }

  const stopMigrationPolling = () => {
    if (migrationPollTimer) {
      clearTimeout(migrationPollTimer)
      migrationPollTimer = null
    }
  }

  const shouldPollMigration = () => {
    if (!selectedKb.value) return false
    return (
      isKnowledgeBaseMigrationActive(selectedKb.value.migration_status) ||
      ['pending', 'running'].includes(selectedKb.value.old_collection_cleanup_status)
    )
  }

  const refreshMigrationState = async () => {
    if (!selectedKb.value) return
    const selectedId = selectedKb.value.id
    const res = await knowledgeBaseApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    const { items, total: totalCount, embedding_models: embeddingModelItems } = res.data.data
    tableData.value = (items || []).map(normalizeKnowledgeBase)
    total.value = totalCount || 0
    embeddingModels.value = embeddingModelItems || []
    const refreshed = tableData.value.find(item => item.id === selectedId)
    if (refreshed) selectedKb.value = refreshed
  }

  const scheduleMigrationPolling = () => {
    stopMigrationPolling()
    if (!migrationDialogVisible.value || !shouldPollMigration()) return
    migrationPollTimer = setTimeout(async () => {
      try {
        await refreshMigrationState()
      } catch (error) {
        ElMessage.error(t('knowledgeBase.fetch_migration_status_failed') + error.message)
      } finally {
        scheduleMigrationPolling()
      }
    }, 2000)
  }

  const showMigrationDialog = (row) => {
    selectedKb.value = row
    migrationTargetKey.value = row.target_embedding_channel_id && row.target_embedding_model_id
      ? `${row.target_embedding_channel_id}::${row.target_embedding_model_id}`
      : ''
    migrationDialogVisible.value = true
    scheduleMigrationPolling()
  }

  const submitEmbeddingMigration = async () => {
    if (!selectedKb.value || !canStartKnowledgeBaseMigration(selectedKb.value)) return
    const target = embeddingModelOptions.value.find(item => item.key === migrationTargetKey.value)
    if (!target) {
      ElMessage.error(t('knowledgeBase.select_embedding_model_err'))
      return
    }
    try {
      await ElMessageBox.confirm(
        t('knowledgeBase.embedding_migration_confirm', {
          name: selectedKb.value.name,
          target: target.label
        }),
        t('knowledgeBase.embedding_migration_confirm_title'),
        {
          confirmButtonText: t('knowledgeBase.start_migration'),
          cancelButtonText: t('knowledgeBase.cancel'),
          type: 'warning'
        }
      )
    } catch (action) {
      if (action === 'cancel' || action === 'close') return
      throw action
    }

    migrationSubmitting.value = true
    try {
      const res = await knowledgeBaseApi.migrateEmbedding(selectedKb.value.id, {
        embedding_channel_id: target.channel_id,
        embedding_model_id: target.model_id
      })
      const refreshed = normalizeKnowledgeBase(res.data.data)
      selectedKb.value = refreshed
      const index = tableData.value.findIndex(item => item.id === refreshed.id)
      if (index >= 0) tableData.value.splice(index, 1, refreshed)
      ElMessage.success(t('knowledgeBase.embedding_migration_submitted'))
      scheduleMigrationPolling()
    } catch (error) {
      ElMessage.error(t('knowledgeBase.embedding_migration_failed') + error.message)
    } finally {
      migrationSubmitting.value = false
    }
  }

  const handleKnowledgeBaseMoreAction = (command, row) => {
    if (command === 'import_document') {
      documents.showImportDialog(row)
      return
    }
    if (command === 'embedding_status') {
      showMigrationDialog(row)
      return
    }
    if (command === 'edit') {
      showEditDialog(row)
      return
    }
    if (command === 'delete') handleDelete(row)
  }

  const showDialog = () => {
    resetFormFields()
    isEditing.value = false
    editingId.value = null
    dialogVisible.value = true
  }

  const showEditDialog = (row) => {
    resetFormFields()
    isEditing.value = true
    editingId.value = row.id
    form.name = row.name
    form.description = row.description || ''
    form.embedding_model_key = `${row.embedding_channel_id}::${row.embedding_model_id}`
    dialogVisible.value = true
  }

  const resetFormFields = () => {
    if (formRef.value) formRef.value.resetFields()
    form.name = ''
    form.description = ''
    form.embedding_model_key = ''
  }

  const showQueryTestDialog = (row) => {
    selectedKb.value = row
    queryTestForm.query = ''
    queryTestForm.top_k = 5
    queryTestResults.value = []
    queryTested.value = false
    if (queryTestFormRef.value) queryTestFormRef.value.clearValidate()
    queryTestDialogVisible.value = true
  }

  const submitQueryTest = async () => {
    if (!queryTestFormRef.value || !selectedKb.value) return
    await queryTestFormRef.value.validate(async (valid) => {
      if (!valid) return
      queryTesting.value = true
      queryTested.value = false
      try {
        const res = await knowledgeBaseApi.queryTest(selectedKb.value.id, {
          query: queryTestForm.query,
          top_k: queryTestForm.top_k
        })
        queryTestResults.value = res.data.data.items || []
        retrievalMode.value = res.data.data.retrieval_mode || null
        rerankError.value = res.data.data.rerank_error || null
        queryTested.value = true
      } catch (error) {
        ElMessage.error(t('knowledgeBase.query_test_failed') + error.message)
      } finally {
        queryTesting.value = false
      }
    })
  }

  const formatDistance = (distance) => {
    if (distance === null || distance === undefined) return '-'
    return Number(distance).toFixed(4)
  }

  const formatScore = (score) => {
    if (score === null || score === undefined) return '-'
    return Number(score).toFixed(4)
  }

  const submitForm = async () => {
    if (!formRef.value) return
    await formRef.value.validate(async (valid) => {
      if (valid) {
        submitting.value = true
        try {
          if (isEditing.value) {
            await knowledgeBaseApi.update(editingId.value, {
              name: form.name,
              description: form.description
            })
            ElMessage.success(t('knowledgeBase.update_success'))
          } else {
            const [embeddingChannelId, embeddingModelId] = form.embedding_model_key.split('::')
            await knowledgeBaseApi.create({
              name: form.name,
              description: form.description,
              embedding_channel_id: Number(embeddingChannelId),
              embedding_model_id: embeddingModelId
            })
            ElMessage.success(t('knowledgeBase.create_success'))
          }
          dialogVisible.value = false
          fetchData()
        } catch (error) {
          ElMessage.error((isEditing.value ? t('knowledgeBase.update_failed') : t('knowledgeBase.create_failed')) + error.message)
        } finally {
          submitting.value = false
        }
      }
    })
  }

  const { handleDelete: confirmDeleteKnowledgeBase } = useDeleteConfirm(knowledgeBaseApi.delete, fetchData)

  const handleDelete = (row) => {
    confirmDeleteKnowledgeBase(row.id, row.name, {
      title: t('knowledgeBase.prompt'),
      message: t('knowledgeBase.delete_kb_confirm', { name: row.name }),
      dangerouslyUseHTMLString: false,
      errorMessage: t('knowledgeBase.delete_kb_failed')
    })
  }

  onMounted(() => {
    fetchData()
  })

  onBeforeUnmount(() => {
    stopMigrationPolling()
    managedKnowledge.stopManagedKnowledgePolling()
    organization.stopOrganizationPolling()
  })

  return {
    tableData,
    loading,
    total,
    currentPage,
    pageSize,
    dialogVisible,
    submitting,
    isEditing,
    editingId,
    embeddingModels,
    formRef,
    selectedKb,
    form,
    rules,
    embeddingModelOptions,
    fetchData,
    handleRefresh,
    handleSizeChange,
    getKnowledgeBaseProfileLabel,
    getEmbeddingModelName,
    getTargetEmbeddingModelName,
    migrationDialogVisible,
    migrationTargetKey,
    migrationSubmitting,
    migrationProgress,
    managedMigrationTerminalHintKey,
    getMigrationStatusLabel,
    getCleanupStatusLabel,
    stopMigrationPolling,
    showMigrationDialog,
    submitEmbeddingMigration,
    handleKnowledgeBaseMoreAction,
    showDialog,
    showEditDialog,
    resetFormFields,
    queryTestDialogVisible,
    queryTesting,
    queryTested,
    queryTestFormRef,
    queryTestResults,
    retrievalMode,
    rerankError,
    queryTestForm,
    queryTestRules,
    showQueryTestDialog,
    submitQueryTest,
    formatDistance,
    formatScore,
    submitForm,
    handleDelete,
    formatTime,
    canStartKnowledgeBaseMigration,
    canManageKnowledgeBaseDocuments,
    canManageManagedKnowledge,
    ...documents,
    ...managedKnowledge,
    ...organization
  }
}
