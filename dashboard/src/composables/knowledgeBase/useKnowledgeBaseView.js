import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { adminApi, knowledgeBaseApi } from '../../api/index.js'
import { formatTime } from '../../utils/index.js'
import {
  canStartKnowledgeBaseMigration,
  getKnowledgeBaseMigrationProgress,
  getManagedKnowledgeBaseMigrationTerminalHintKey,
  isKnowledgeBaseMigrationActive
} from '../../utils/knowledgeBaseMigration.js'
import {
  canManageManagedKnowledge,
  getKnowledgeBaseProfileLabel,
  normalizeKnowledgeBase
} from '../../utils/knowledgeBaseManagement.js'
import { createKnowledgeBaseScope } from '../../utils/knowledgeBaseScope.js'
import { useUserPermissionsStore } from '../useUserPermissions.js'
import { useKnowledgeBaseQueryTest } from './useKnowledgeBaseQueryTest.js'

export function useKnowledgeBaseView() {
  const { t } = useI18n()
  const userPermissions = useUserPermissionsStore()
  const isSuperuser = computed(() => userPermissions.isSuperuser)

  const tableData = ref([])
  const loading = ref(false)
  const total = ref(0)
  const repairRequiredCount = ref(0)
  const currentPage = ref(1)
  const pageSize = ref(20)
  const dialogVisible = ref(false)
  const submitting = ref(false)
  const isEditing = ref(false)
  const editingId = ref(null)
  const embeddingModels = ref([])
  const formRef = ref(null)
  const selectedKb = ref(null)
  const ownerUsers = ref([])
  const ownerUsersLoading = ref(false)
  const ownerUsersError = ref(null)
  let ownerUsersRequestVersion = 0
  let formGeneration = 0
  const migrationDialogVisible = ref(false)
  const migrationTargetKey = ref('')
  const migrationSubmitting = ref(false)
  const migrationStateLoading = ref(false)
  const migrationStateError = ref(null)
  let migrationPollTimer = null
  let migrationDialogGeneration = 0
  let migrationDialogSession = null
  const repairDialogVisible = ref(false)
  const repairOwnerUid = ref(null)
  const repairOwners = ref([])
  const repairOwnersLoading = ref(false)
  const repairOwnersError = ref(null)
  const repairSubmitting = ref(false)
  let repairGeneration = 0
  let repairOwnersRequestVersion = 0
  let repairSession = null
  let repairDedupe = null
  let fetchDataRequestVersion = 0
  let knowledgeBaseViewDisposed = false

  const scope = createKnowledgeBaseScope({
    isActive: () => (
      !knowledgeBaseViewDisposed &&
      (userPermissions.currentUser?.uid ?? null) !== null
    ),
    confirm: (...args) => ElMessageBox.confirm(...args),
    closeConfirmations: () => ElMessageBox.close()
  })

  const form = reactive({
    name: '',
    description: '',
    embedding_model_key: '',
    username: '',
    uid: null
  })

  const rules = computed(() => ({
    name: [{ required: true, message: t('knowledgeBase.input_kb_name'), trigger: 'blur' }],
    embedding_model_key: [{ required: !isEditing.value, message: t('knowledgeBase.select_embedding_model_err'), trigger: 'change' }],
    uid: [{ required: !isEditing.value && isSuperuser.value, message: t('knowledgeBase.owner_user_required'), trigger: 'change' }]
  }))

  const embeddingModelOptions = computed(() => embeddingModels.value.map(item => ({
    ...item,
    key: `${item.channel_id}::${item.model_id}`,
    label: `${item.channel_name} / ${item.model_id}${item.embedding_dimensions ? ` (${item.embedding_dimensions})` : ''}`
  })))

  const migrationProgress = computed(() => getKnowledgeBaseMigrationProgress(selectedKb.value))
  const managedMigrationTerminalHintKey = computed(() => getManagedKnowledgeBaseMigrationTerminalHintKey(selectedKb.value))

  const queryTest = useKnowledgeBaseQueryTest({ t, selectedKb, scope })

  const fetchData = async () => {
    const scopeGuard = scope.capture()
    if (!scopeGuard()) return
    const requestVersion = ++fetchDataRequestVersion
    const uid = userPermissions.currentUser?.uid ?? null
    const requestIsSuperuser = isSuperuser.value
    const isRequestCurrent = () => (
      scopeGuard() &&
      !knowledgeBaseViewDisposed &&
      requestVersion === fetchDataRequestVersion &&
      (userPermissions.currentUser?.uid ?? null) === uid &&
      isSuperuser.value === requestIsSuperuser
    )
    loading.value = true
    try {
      const res = await knowledgeBaseApi.list({
        page: currentPage.value,
        size: pageSize.value
      })
      if (!isRequestCurrent()) return
      const {
        items,
        total: totalCount,
        embedding_models: embeddingModelItems,
        repair_required_count: repairRequiredCountValue
      } = res.data.data
      tableData.value = (items || []).map(normalizeKnowledgeBase)
      total.value = totalCount || 0
      embeddingModels.value = embeddingModelItems || []
      repairRequiredCount.value = Number.isFinite(repairRequiredCountValue) && repairRequiredCountValue >= 0
        ? repairRequiredCountValue
        : 0
    } catch (error) {
      if (isRequestCurrent()) {
        const responseCodes = [error?.response?.status, error?.response?.data?.code]
        if (responseCodes.some(code => [401, 403, '401', '403'].includes(code))) {
          repairRequiredCount.value = 0
        }
        ElMessage.error(t('knowledgeBase.fetch_list_failed') + error.message)
      }
    } finally {
      if (isRequestCurrent()) loading.value = false
    }
  }

  const handleRefresh = () => {
    if (!scope.capture()()) return
    currentPage.value = 1
    fetchData()
  }

  const handleSizeChange = () => {
    if (!scope.capture()()) return
    currentPage.value = 1
    fetchData()
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

  const getKnowledgeBaseOwnerLabel = (row) => row?.username || t('knowledgeBase.owner_unknown')

  const canRepairKnowledgeBase = (row) => (
    row?.id !== null &&
    row?.id !== undefined &&
    ['user', 'llm_managed'].includes(row.knowledge_base_type) &&
    !isKnowledgeBaseMigrationActive(row.migration_status) &&
    !['pending', 'running', 'failed'].includes(row.old_collection_cleanup_status)
  )

  const clearRepairDedupe = (scope) => {
    if (!scope || repairDedupe?.scope === scope) repairDedupe = null
  }

  const getRepairDedupeKey = (scope) => {
    if (repairDedupe?.scope !== scope) {
      repairDedupe = {
        scope,
        key: `knowledge-base-repair:${Date.now()}:${Math.random().toString(36).slice(2)}`
      }
    }
    return repairDedupe.key
  }

  const isCurrentRepairSession = (generation, session) => (
    !knowledgeBaseViewDisposed &&
    !!session &&
    generation === repairGeneration &&
    repairSession === session &&
    (userPermissions.currentUser?.uid ?? null) === session.uid &&
    isSuperuser.value === session.isSuperuser
  )

  watch(repairOwnerUid, (uid, previousUid) => {
    if (uid !== previousUid) clearRepairDedupe()
  })

  const fetchRepairOwners = async (
    generation = repairGeneration,
    session = repairSession
  ) => {
    if (
      !isCurrentRepairSession(generation, session) ||
      !repairDialogVisible.value ||
      !session.isSuperuser
    ) return

    const requestVersion = ++repairOwnersRequestVersion
    const isCurrent = () => (
      requestVersion === repairOwnersRequestVersion &&
      isCurrentRepairSession(generation, session) &&
      repairDialogVisible.value
    )
    repairOwnersLoading.value = true
    repairOwnersError.value = null
    const users = []
    let page = 1

    try {
      while (isCurrent()) {
        const res = await adminApi.userList({ page, size: 100 })
        if (!isCurrent()) return
        const { items = [], total = 0 } = res.data.data
        users.push(...items)
        if (items.length === 0 || users.length >= total) break
        page += 1
      }

      if (!isCurrent()) return
      repairOwners.value = users
      if (
        repairOwnerUid.value === session.uid &&
        !users.some(item => item.uid === session.uid)
      ) repairOwnerUid.value = null
    } catch (error) {
      if (isCurrent()) {
        repairOwnersError.value = t('knowledgeBase.fetch_owner_users_failed', {
          message: error?.message || ''
        })
      }
    } finally {
      if (isCurrent()) repairOwnersLoading.value = false
    }
  }

  const showRepairDialog = () => {
    if (!scope.capture()() || repairSubmitting.value || migrationSubmitting.value) return

    clearRepairDedupe()
    const generation = ++repairGeneration
    const session = {
      uid: userPermissions.currentUser?.uid ?? null,
      isSuperuser: isSuperuser.value
    }
    repairSession = session
    repairDialogVisible.value = true
    repairOwnerUid.value = session.uid
    repairOwners.value = []
    repairOwnersLoading.value = false
    repairOwnersError.value = null

    if (session.isSuperuser) return fetchRepairOwners(generation, session)
    if (session.uid !== null) repairOwners.value = [userPermissions.currentUser]
  }

  const closeRepairDialog = () => {
    if (repairSubmitting.value) return
    repairGeneration += 1
    repairSession = null
    repairOwnersRequestVersion += 1
    repairDialogVisible.value = false
    repairOwnerUid.value = null
    repairOwners.value = []
    repairOwnersLoading.value = false
    repairOwnersError.value = null
    clearRepairDedupe()
  }

  const submitUserRepair = async () => {
    const session = repairSession
    const generation = repairGeneration
    const uid = repairOwnerUid.value
    if (
      !repairDialogVisible.value ||
      repairSubmitting.value ||
      migrationSubmitting.value ||
      repairOwnersLoading.value ||
      repairOwnersError.value ||
      !isCurrentRepairSession(generation, session) ||
      uid === null ||
      uid === undefined ||
      (session.isSuperuser
        ? !repairOwners.value.some(item => item.uid === uid)
        : uid !== session.uid)
    ) return

    const scope = `user:${uid}`
    const isCurrent = () => (
      isCurrentRepairSession(generation, session) &&
      repairDialogVisible.value &&
      repairOwnerUid.value === uid
    )
    const isOperationCurrent = () => isCurrentRepairSession(generation, session)
    repairSubmitting.value = true
    try {
      const res = await knowledgeBaseApi.repair(
        { dedupe_key: getRepairDedupeKey(scope) },
        { uid }
      )
      if (!isCurrent()) return

      const items = res?.data?.data?.items || []
      clearRepairDedupe(scope)
      repairDialogVisible.value = false
      repairOwnersRequestVersion += 1
      repairOwnerUid.value = null
      repairOwners.value = []
      repairOwnersLoading.value = false
      repairOwnersError.value = null
      if (items.length === 0) {
        ElMessage.warning(t('knowledgeBase.repair_no_knowledge_bases'))
      } else {
        ElMessage.success(t('knowledgeBase.repair_submitted', { count: items.length }))
      }
      await fetchData()
      if (!isOperationCurrent()) return
    } catch (error) {
      if (isCurrent()) {
        ElMessage.error(t('knowledgeBase.repair_failed', { message: error?.message || '' }))
      }
    } finally {
      if (isOperationCurrent()) repairSubmitting.value = false
    }
  }

  const repairKnowledgeBase = async (row) => {
    if (
      !scope.capture()() ||
      !canRepairKnowledgeBase(row) ||
      repairSubmitting.value ||
      migrationSubmitting.value ||
      knowledgeBaseViewDisposed
    ) return

    const generation = ++repairGeneration
    const session = {
      uid: userPermissions.currentUser?.uid ?? null,
      isSuperuser: isSuperuser.value
    }
    repairSession = session
    const isCurrent = () => isCurrentRepairSession(generation, session)
    const repairScope = `knowledge_base:${row.id}`
    const name = row.name
    const username = getKnowledgeBaseOwnerLabel(row)
    repairSubmitting.value = true

    try {
      const confirmed = await scope.confirm(
        t('knowledgeBase.repair_confirm', { name, username, id: row.id }),
        t('knowledgeBase.repair_confirm_title'),
        {
          confirmButtonText: t('knowledgeBase.repair'),
          cancelButtonText: t('knowledgeBase.cancel'),
          type: 'warning',
          dangerouslyUseHTMLString: false
        }
      )
      if (!confirmed || !isCurrent()) return

      const res = await knowledgeBaseApi.repair(
        { dedupe_key: getRepairDedupeKey(repairScope) },
        { kb_id: row.id }
      )
      if (!isCurrent()) return

      clearRepairDedupe(repairScope)
      await fetchData()
      if (!isCurrent()) return
      const repairedItem = res?.data?.data?.items?.[0]
      await showMigrationDialog(normalizeKnowledgeBase({
        ...row,
        ...(repairedItem || {}),
        id: row.id
      }))
    } catch (error) {
      if (isCurrent()) {
        ElMessage.error(t('knowledgeBase.repair_failed', { message: error?.message || '' }))
      }
    } finally {
      if (isCurrent()) repairSubmitting.value = false
    }
  }

  const stopMigrationPolling = () => {
    if (migrationPollTimer) {
      clearTimeout(migrationPollTimer)
      migrationPollTimer = null
    }
  }

  const isCurrentMigrationRequest = (generation, selectedId, session) => (
    !knowledgeBaseViewDisposed &&
    !!session &&
    migrationDialogVisible.value &&
    generation === migrationDialogGeneration &&
    migrationDialogSession === session &&
    selectedKb.value?.id === selectedId &&
    (userPermissions.currentUser?.uid ?? null) === session.uid &&
    isSuperuser.value === session.isSuperuser
  )

  const shouldPollMigration = () => {
    if (!selectedKb.value) return false
    return (
      isKnowledgeBaseMigrationActive(selectedKb.value.migration_status) ||
      ['pending', 'running'].includes(selectedKb.value.old_collection_cleanup_status)
    )
  }

  const refreshMigrationState = async (
    generation = migrationDialogGeneration,
    selectedId = selectedKb.value?.id,
    session = migrationDialogSession
  ) => {
    if (!isCurrentMigrationRequest(generation, selectedId, session)) return null

    migrationStateLoading.value = true
    migrationStateError.value = null
    try {
      const res = await knowledgeBaseApi.get(selectedId)
      if (!isCurrentMigrationRequest(generation, selectedId, session)) return null

      const refreshed = normalizeKnowledgeBase(res.data.data)
      const previous = selectedKb.value
      const indexStatusChanged = previous?.index_status !== refreshed.index_status
      const migrationFinished = (
        isKnowledgeBaseMigrationActive(previous?.migration_status) &&
        !isKnowledgeBaseMigrationActive(refreshed.migration_status)
      )
      selectedKb.value = refreshed
      const index = tableData.value.findIndex(item => item.id === selectedId)
      if (index >= 0) tableData.value.splice(index, 1, refreshed)
      if (
        (indexStatusChanged || migrationFinished) &&
        isCurrentMigrationRequest(generation, selectedId, session)
      ) await fetchData()
      return refreshed
    } catch (error) {
      if (isCurrentMigrationRequest(generation, selectedId, session)) {
        migrationStateError.value = t('knowledgeBase.fetch_migration_status_failed') + (error?.message || '')
      }
      throw error
    } finally {
      if (isCurrentMigrationRequest(generation, selectedId, session)) migrationStateLoading.value = false
    }
  }

  const scheduleMigrationPolling = (
    generation = migrationDialogGeneration,
    selectedId = selectedKb.value?.id,
    session = migrationDialogSession
  ) => {
    stopMigrationPolling()
    if (
      !isCurrentMigrationRequest(generation, selectedId, session) ||
      (!shouldPollMigration() && !migrationStateError.value)
    ) return
    migrationPollTimer = setTimeout(async () => {
      migrationPollTimer = null
      if (!isCurrentMigrationRequest(generation, selectedId, session)) return
      try {
        await refreshMigrationState(generation, selectedId, session)
      } catch {}
      if (isCurrentMigrationRequest(generation, selectedId, session)) {
        scheduleMigrationPolling(generation, selectedId, session)
      }
    }, 2000)
  }

  const showMigrationDialog = async (row) => {
    if (!scope.capture()()) return
    stopMigrationPolling()
    const generation = ++migrationDialogGeneration
    const session = {
      uid: userPermissions.currentUser?.uid ?? null,
      isSuperuser: isSuperuser.value
    }
    migrationDialogSession = session
    migrationSubmitting.value = false
    selectedKb.value = row
    migrationTargetKey.value = row.target_embedding_channel_id && row.target_embedding_model_id
      ? `${row.target_embedding_channel_id}::${row.target_embedding_model_id}`
      : ''
    migrationDialogVisible.value = true
    migrationStateLoading.value = false
    migrationStateError.value = null
    try {
      await refreshMigrationState(generation, row.id, session)
    } catch {}
    if (isCurrentMigrationRequest(generation, row.id, session)) {
      scheduleMigrationPolling(generation, row.id, session)
    }
  }

  const closeMigrationDialog = () => {
    migrationDialogVisible.value = false
    migrationDialogGeneration += 1
    migrationDialogSession = null
    stopMigrationPolling()
    migrationSubmitting.value = false
    migrationStateLoading.value = false
    migrationStateError.value = null
  }

  const submitEmbeddingMigration = async () => {
    const generation = migrationDialogGeneration
    const selectedId = selectedKb.value?.id
    const session = migrationDialogSession
    const isCurrent = () => isCurrentMigrationRequest(generation, selectedId, session)
    if (
      !isCurrent() ||
      repairSubmitting.value ||
      migrationSubmitting.value ||
      !selectedKb.value ||
      !canStartKnowledgeBaseMigration(selectedKb.value)
    ) return
    const target = embeddingModelOptions.value.find(item => item.key === migrationTargetKey.value)
    if (!target) {
      ElMessage.error(t('knowledgeBase.select_embedding_model_err'))
      return
    }
    const confirmed = await scope.confirm(
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
    if (!confirmed) return

    if (
      !isCurrent() ||
      repairSubmitting.value ||
      migrationSubmitting.value ||
      !selectedKb.value ||
      !canStartKnowledgeBaseMigration(selectedKb.value)
    ) return
    migrationSubmitting.value = true
    try {
      const res = await knowledgeBaseApi.migrateEmbedding(selectedId, {
        embedding_channel_id: target.channel_id,
        embedding_model_id: target.model_id
      })
      if (!isCurrent()) return
      const refreshed = normalizeKnowledgeBase(res.data.data)
      selectedKb.value = refreshed
      const index = tableData.value.findIndex(item => item.id === refreshed.id)
      if (index >= 0) tableData.value.splice(index, 1, refreshed)
      ElMessage.success(t('knowledgeBase.embedding_migration_submitted'))
      scheduleMigrationPolling()
    } catch (error) {
      if (isCurrent()) {
        ElMessage.error(t('knowledgeBase.embedding_migration_failed') + error.message)
      }
    } finally {
      if (isCurrent()) migrationSubmitting.value = false
    }
  }

  const handleKnowledgeBaseMoreAction = (command, row) => {
    if (!scope.capture()()) return
    if (command === 'repair') return repairKnowledgeBase(row)
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

  const fetchOwnerUsers = async () => {
    const scopeGuard = scope.capture()
    const currentUserUid = userPermissions.currentUser?.uid ?? null
    const requestIsSuperuser = isSuperuser.value
    if (!scopeGuard() || !requestIsSuperuser || isEditing.value) return
    const requestVersion = ++ownerUsersRequestVersion
    const isCurrent = () => (
      scopeGuard() &&
      requestVersion === ownerUsersRequestVersion &&
      isSuperuser.value === requestIsSuperuser &&
      !isEditing.value &&
      (userPermissions.currentUser?.uid ?? null) === currentUserUid
    )

    ownerUsersLoading.value = true
    ownerUsersError.value = null
    const users = []
    let page = 1

    try {
      while (isCurrent()) {
        const res = await adminApi.userList({ page, size: 100 })
        if (!isCurrent()) return
        const { items = [], total = 0 } = res.data.data
        users.push(...items)
        if (items.length === 0 || users.length >= total) break
        page += 1
      }

      if (!isCurrent()) return
      ownerUsers.value = users
      if (!users.some(item => item.uid === form.uid)) form.uid = null
    } catch (error) {
      if (isCurrent()) {
        ownerUsersError.value = t('knowledgeBase.fetch_owner_users_failed', { message: error.message })
      }
    } finally {
      if (isCurrent()) ownerUsersLoading.value = false
    }
  }

  const showDialog = () => {
    if (!scope.capture()()) return
    resetFormFields()
    isEditing.value = false
    editingId.value = null
    dialogVisible.value = true
    return fetchOwnerUsers()
  }

  const showEditDialog = (row) => {
    if (!scope.capture()() || !row) return
    resetFormFields()
    isEditing.value = true
    editingId.value = row.id
    form.name = row.name
    form.description = row.description || ''
    form.embedding_model_key = `${row.embedding_channel_id}::${row.embedding_model_id}`
    form.username = row.username || ''
    form.uid = row.uid ?? null
    dialogVisible.value = true
  }

  const resetFormFields = (initializeOwner = true) => {
    formGeneration += 1
    submitting.value = false
    ownerUsersRequestVersion += 1
    ownerUsersLoading.value = false
    ownerUsersError.value = null
    if (formRef.value) formRef.value.resetFields()
    form.name = ''
    form.description = ''
    form.embedding_model_key = ''
    if (initializeOwner) {
      form.username = userPermissions.currentUser?.username || ''
      form.uid = isSuperuser.value ? userPermissions.currentUser?.uid || null : null
      ownerUsers.value = isSuperuser.value && userPermissions.currentUser?.uid && userPermissions.currentUser?.username
        ? [{ uid: userPermissions.currentUser.uid, username: userPermissions.currentUser.username }]
        : []
    } else {
      form.username = ''
      form.uid = null
      ownerUsers.value = []
    }
  }

  const resetKnowledgeBaseState = () => {
    scope.invalidate()
    fetchDataRequestVersion += 1
    tableData.value = []
    total.value = 0
    embeddingModels.value = []
    selectedKb.value = null
    currentPage.value = 1
    pageSize.value = 20
    repairRequiredCount.value = 0
    loading.value = false

    resetFormFields(false)
    editingId.value = null
    isEditing.value = false
    dialogVisible.value = false

    repairGeneration += 1
    repairSession = null
    repairOwnersRequestVersion += 1
    repairDialogVisible.value = false
    repairOwnerUid.value = null
    repairOwners.value = []
    repairOwnersLoading.value = false
    repairOwnersError.value = null
    repairSubmitting.value = false
    clearRepairDedupe()

    closeMigrationDialog()
    migrationTargetKey.value = ''

    queryTest.resetQueryTestState()
  }

  watch(
    [
      () => userPermissions.currentUser?.uid ?? null,
      () => isSuperuser.value
    ],
    ([uid]) => {
      resetKnowledgeBaseState()
      if (uid !== null && !knowledgeBaseViewDisposed) fetchData()
    },
    { flush: 'sync' }
  )

  const formatDistance = (distance) => {
    if (distance === null || distance === undefined) return '-'
    return Number(distance).toFixed(4)
  }

  const formatScore = (score) => {
    if (score === null || score === undefined) return '-'
    return Number(score).toFixed(4)
  }

  const submitForm = async () => {
    const scopeGuard = scope.capture()
    const generation = formGeneration
    const requestIsEditing = isEditing.value
    const requestEditingId = editingId.value
    const requestIsSuperuser = isSuperuser.value
    const formRefForRequest = formRef.value
    if (
      !scopeGuard() ||
      !dialogVisible.value ||
      submitting.value ||
      !formRefForRequest ||
      (!requestIsEditing && requestIsSuperuser && (ownerUsersLoading.value || ownerUsersError.value))
    ) return

    const isCurrent = () => scopeGuard() && generation === formGeneration
    submitting.value = true
    try {
      let valid
      try {
        valid = await formRefForRequest.validate()
      } catch {
        return
      }
      if (
        !valid ||
        !isCurrent() ||
        !dialogVisible.value ||
        !submitting.value ||
        isSuperuser.value !== requestIsSuperuser ||
        (!requestIsEditing && requestIsSuperuser && (ownerUsersLoading.value || ownerUsersError.value))
      ) return

      try {
        if (requestIsEditing) {
          await knowledgeBaseApi.update(requestEditingId, {
            name: form.name,
            description: form.description
          })
          if (!isCurrent()) return
          ElMessage.success(t('knowledgeBase.update_success'))
        } else {
          const [embeddingChannelId, embeddingModelId] = form.embedding_model_key.split('::')
          await knowledgeBaseApi.create({
            name: form.name,
            description: form.description,
            embedding_channel_id: Number(embeddingChannelId),
            embedding_model_id: embeddingModelId,
            ...(requestIsSuperuser ? { uid: form.uid } : {})
          })
          if (!isCurrent()) return
          ElMessage.success(t('knowledgeBase.create_success'))
        }
        if (!isCurrent()) return
        dialogVisible.value = false
        fetchData()
      } catch (error) {
        if (isCurrent()) {
          ElMessage.error((requestIsEditing ? t('knowledgeBase.update_failed') : t('knowledgeBase.create_failed')) + error.message)
        }
      }
    } finally {
      if (isCurrent()) submitting.value = false
    }
  }

  const handleDelete = async (row) => {
    const scopeGuard = scope.capture()
    if (!scopeGuard() || !row) return
    const id = row.id
    const name = row.name
    const username = getKnowledgeBaseOwnerLabel(row)
    const confirmed = await scope.confirm(
      t('knowledgeBase.delete_kb_confirm', { name, username, id }),
      t('knowledgeBase.prompt'),
      {
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
        type: 'warning',
        dangerouslyUseHTMLString: false
      }
    )
    if (!scopeGuard() || !confirmed) return

    try {
      await knowledgeBaseApi.delete(id)
    } catch (error) {
      if (scopeGuard()) ElMessage.error(error?.message || t('knowledgeBase.delete_kb_failed'))
      return
    }

    if (!scopeGuard()) return
    ElMessage.success(t('common.delete_success'))
    await fetchData()
  }

  onMounted(() => {
    fetchData()
  })

  onBeforeUnmount(() => {
    knowledgeBaseViewDisposed = true
    resetKnowledgeBaseState()
  })

  return {
    tableData,
    loading,
    total,
    repairRequiredCount,
    currentPage,
    pageSize,
    dialogVisible,
    submitting,
    isEditing,
    editingId,
    embeddingModels,
    formRef,
    selectedKb,
    ownerUsers,
    ownerUsersLoading,
    ownerUsersError,
    form,
    rules,
    embeddingModelOptions,
    fetchData,
    handleRefresh,
    handleSizeChange,
    getKnowledgeBaseProfileLabel,
    getKnowledgeBaseOwnerLabel,
    getEmbeddingModelName,
    getTargetEmbeddingModelName,
    migrationDialogVisible,
    migrationTargetKey,
    migrationSubmitting,
    migrationStateLoading,
    migrationStateError,
    migrationProgress,
    managedMigrationTerminalHintKey,
    repairDialogVisible,
    repairOwnerUid,
    repairOwners,
    repairOwnersLoading,
    repairOwnersError,
    repairSubmitting,
    getMigrationStatusLabel,
    getCleanupStatusLabel,
    stopMigrationPolling,
    refreshMigrationState,
    showMigrationDialog,
    closeMigrationDialog,
    submitEmbeddingMigration,
    canRepairKnowledgeBase,
    showRepairDialog,
    fetchRepairOwners,
    closeRepairDialog,
    submitUserRepair,
    repairKnowledgeBase,
    handleKnowledgeBaseMoreAction,
    showDialog,
    showEditDialog,
    resetFormFields,
    fetchOwnerUsers,
    ...queryTest,
    formatDistance,
    formatScore,
    submitForm,
    handleDelete,
    isSuperuser,
    formatTime,
    canStartKnowledgeBaseMigration,
    canManageManagedKnowledge,
  }
}
