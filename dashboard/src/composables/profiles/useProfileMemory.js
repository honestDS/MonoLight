import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { profileApi } from '../../api/index.js'
import {
  createLatestRequestTracker,
  getOrganizationModelOptions,
  normalizeMemorySettings
} from '../../utils/memoryManagement.js'

export function useProfileMemory({ t, channels, dialogVisible, dialogType, form, loadProfiles }) {
  const memoryEmbeddingTargetKey = ref('')
  const memoryPreview = ref(null)
  const memoryEmbeddingPreviewing = ref(false)
  const memoryEmbeddingDialogVisible = ref(false)
  const memoryConfirmationChecked = ref(false)
  const memoryEmbeddingConfirming = ref(false)
  const memoryRuntime = ref({})
  const persistedMemoryConfig = ref({})
  const memorySettingsLoading = ref(false)
  const memorySettingsReady = ref(false)
  const memorySettingsUnavailable = ref(false)
  const memoryStorageConfigured = ref(true)
  const memoryOrganizationRequiredOutputTokens = ref(0)
  const memorySettingsRequestTracker = createLatestRequestTracker()
  let memoryEmbeddingRequestGeneration = 0

  const activeMemoryEmbeddingMigrationStatuses = new Set([
    'preparing',
    'building',
    'catching_up',
    'validating',
    'switching'
  ])

  const memoryEmbeddingOptions = computed(() => channels.value
    .filter(channel => channel.is_active !== false)
    .flatMap(channel => (channel.model_ids || [])
      .filter(model => model.usage === 'EMBEDDING' && model.model_id && model.is_enabled !== false)
      .map(model => ({
        key: `${channel.id}::${model.model_id}`,
        channel_id: channel.id,
        model_id: model.model_id,
        label: `${channel.name} / ${model.model_id}`
      }))))

  const formatMemorySelection = (selection) => {
    if (!selection?.channel_id || !selection?.model_id) return t('profiles.memory_embedding_not_configured')
    const channel = channels.value.find(item => String(item.id) === String(selection.channel_id))
    return channel ? `${channel.name} / ${selection.model_id}` : `${selection.channel_id} / ${selection.model_id}`
  }

  const memoryOrganizationModelOptions = computed(() => getOrganizationModelOptions(channels.value))
  const memoryOrganizationModelKey = computed({
    get() {
      const organization = form.memory_organization
      if (!organization.organization_channel_id || !organization.organization_model_id) return null
      return `${organization.organization_channel_id}::${organization.organization_model_id}`
    },
    set(key) {
      if (!key) {
        form.memory_organization.organization_channel_id = null
        form.memory_organization.organization_model_id = null
        return
      }
      const option = memoryOrganizationModelOptions.value.find(item => item.key === key)
      if (!option) return
      form.memory_organization.organization_channel_id = option.channel_id
      form.memory_organization.organization_model_id = option.model_id
    }
  })
  const memoryOrganizationModel = computed(() => (
    memoryOrganizationModelOptions.value.find(item => item.key === memoryOrganizationModelKey.value) || null
  ))

  const currentMemoryEmbedding = computed(() => ({
    channel_id: memoryRuntime.value.embedding_channel_id ?? form.configs.memory?.embedding_channel_id,
    model_id: memoryRuntime.value.embedding_model_id ?? form.configs.memory?.embedding_model_id,
    dimensions: memoryRuntime.value.embedding_dimensions
  }))

  const memoryEmbeddingCurrentLabel = computed(() => formatMemorySelection(currentMemoryEmbedding.value))

  const memoryEmbeddingConfigured = computed(() => Boolean(
    memoryRuntime.value.embedding_channel_id && memoryRuntime.value.embedding_model_id
  ))

  const memoryEmbeddingTargetLabel = computed(() => {
    const channelId = memoryRuntime.value.target_embedding_channel_id
    const modelId = memoryRuntime.value.target_embedding_model_id
    if (!channelId || !modelId) return ''
    return formatMemorySelection({ channel_id: channelId, model_id: modelId })
  })

  const memoryEmbeddingMigrationActive = computed(() => activeMemoryEmbeddingMigrationStatuses.has(
    memoryRuntime.value.migration_status
  ))

  const memoryEmbeddingMigrationStatusText = computed(() => {
    const status = memoryRuntime.value.migration_status
    return status ? t(`memories.status_${status}`, status) : ''
  })

  const memoryEmbeddingMigrationStatusType = computed(() => {
    const status = memoryRuntime.value.migration_status
    if (status === 'succeeded') return 'success'
    if (status === 'failed') return 'danger'
    if (status === 'cancelled') return 'info'
    return 'warning'
  })

  const memoryPreviewRequiresMigration = computed(() => {
    const preview = memoryPreview.value
    const current = preview?.current_active
    if (!preview || preview.is_initial_selection || !current) return false
    return current.channel_id !== preview.channel_id
      || current.model_id !== preview.model_id
      || current.dimensions !== (preview.actual_dimensions || preview.dimensions)
  })

  const normalizeMemoryOrganizationSelection = () => {
    const key = memoryOrganizationModelKey.value
    if (!key) return
    if (!memoryOrganizationModelOptions.value.some(item => item.key === key)) {
      memoryOrganizationModelKey.value = null
    }
  }

  watch(
    memoryOrganizationModelOptions,
    () => {
      if (memorySettingsLoading.value && !memoryOrganizationModelOptions.value.length) return
      normalizeMemoryOrganizationSelection()
    },
    { deep: true }
  )

  watch(memoryEmbeddingTargetKey, () => {
    memoryEmbeddingRequestGeneration += 1
    memoryEmbeddingPreviewing.value = false
    memoryEmbeddingConfirming.value = false
    memoryPreview.value = null
    memoryConfirmationChecked.value = false
  })

  const invalidateMemoryEmbeddingRequests = () => {
    memoryEmbeddingRequestGeneration += 1
  }

  const isCurrentMemoryEmbeddingRequest = (requestGeneration, profileId, targetKey) => (
    requestGeneration === memoryEmbeddingRequestGeneration
    && dialogVisible.value
    && memoryEmbeddingDialogVisible.value
    && dialogType.value === 'edit'
    && String(form.id) === String(profileId)
    && memoryEmbeddingTargetKey.value === targetKey
  )

  const resetDialogMemoryState = () => {
    invalidateMemoryEmbeddingRequests()
    memorySettingsLoading.value = false
    memorySettingsReady.value = false
    memorySettingsUnavailable.value = false
    memoryStorageConfigured.value = true
    memoryOrganizationRequiredOutputTokens.value = 0
    memoryEmbeddingPreviewing.value = false
    memoryEmbeddingConfirming.value = false
    memoryPreview.value = null
    memoryEmbeddingDialogVisible.value = false
    memoryConfirmationChecked.value = false
  }

  const initializeMemoryState = (memoryConfig, runtime = {}) => {
    persistedMemoryConfig.value = JSON.parse(JSON.stringify(memoryConfig))
    memoryRuntime.value = runtime
    memoryEmbeddingTargetKey.value = ''
  }

  const loadMemorySettings = async () => {
    const requestSeq = memorySettingsRequestTracker.begin()
    memorySettingsLoading.value = true
    memorySettingsReady.value = false
    memorySettingsUnavailable.value = false
    memoryOrganizationRequiredOutputTokens.value = 0
    memoryStorageConfigured.value = false
    const params = form.uid ? { uid: form.uid } : {}
    try {
      const res = await profileApi.memorySettings(params)
      if (!memorySettingsRequestTracker.isCurrent(requestSeq)) return
      const normalized = normalizeMemorySettings(res.data.data || {})
      form.memory_organization = { ...normalized.organizationForm }
      memoryOrganizationRequiredOutputTokens.value = normalized.requiredOutputTokens
      memoryStorageConfigured.value = normalized.configured !== undefined
        ? Boolean(normalized.configured)
        : Boolean(normalized.active_embedding_channel_id && normalized.active_embedding_model_id)
      memorySettingsReady.value = true
    } catch (err) {
      if (memorySettingsRequestTracker.isCurrent(requestSeq)) {
        memorySettingsUnavailable.value = true
        ElMessage.error(err.message || t('profiles.load_memory_settings_failed'))
      }
    } finally {
      if (memorySettingsRequestTracker.isCurrent(requestSeq)) memorySettingsLoading.value = false
    }
  }

  const previewMemoryEmbedding = async () => {
    if (dialogType.value !== 'edit' || !form.id) return
    const target = memoryEmbeddingOptions.value.find(item => item.key === memoryEmbeddingTargetKey.value)
    if (!target) return ElMessage.warning(t('profiles.memory_embedding_target_placeholder'))

    const profileId = form.id
    const targetKey = target.key
    const requestGeneration = ++memoryEmbeddingRequestGeneration
    memoryPreview.value = null
    memoryConfirmationChecked.value = false
    memoryEmbeddingPreviewing.value = true
    try {
      const res = await profileApi.memoryEmbeddingPreview({
        profile_id: profileId,
        embedding_channel_id: target.channel_id,
        embedding_model_id: target.model_id
      })
      if (!isCurrentMemoryEmbeddingRequest(requestGeneration, profileId, targetKey)) return
      memoryPreview.value = res.data.data || null
      memoryConfirmationChecked.value = false
    } catch (err) {
      if (!isCurrentMemoryEmbeddingRequest(requestGeneration, profileId, targetKey)) return
      ElMessage.error(err.message || t('profiles.submit_failed'))
    } finally {
      if (requestGeneration === memoryEmbeddingRequestGeneration) memoryEmbeddingPreviewing.value = false
    }
  }

  const openMemoryEmbeddingDialog = () => {
    if (dialogType.value !== 'edit' || !form.id || memoryEmbeddingMigrationActive.value) return
    invalidateMemoryEmbeddingRequests()
    memoryEmbeddingTargetKey.value = ''
    memoryPreview.value = null
    memoryConfirmationChecked.value = false
    memoryEmbeddingPreviewing.value = false
    memoryEmbeddingConfirming.value = false
    memoryEmbeddingDialogVisible.value = true
  }

  const closeMemoryEmbeddingDialog = () => {
    invalidateMemoryEmbeddingRequests()
    memoryEmbeddingDialogVisible.value = false
    memoryEmbeddingTargetKey.value = ''
    memoryConfirmationChecked.value = false
    memoryPreview.value = null
    memoryEmbeddingPreviewing.value = false
    memoryEmbeddingConfirming.value = false
  }

  const confirmMemoryEmbedding = async () => {
    if (!memoryPreview.value || !memoryConfirmationChecked.value || !form.id) return
    if (!memoryPreview.value.is_initial_selection && !memoryPreviewRequiresMigration.value) return
    const target = memoryEmbeddingOptions.value.find(item => item.key === memoryEmbeddingTargetKey.value)
    if (!target) return

    const profileId = form.id
    const targetKey = target.key
    const isInitialSelection = Boolean(memoryPreview.value.is_initial_selection)
    const memory = {
      ...persistedMemoryConfig.value,
      embedding_channel_id: target.channel_id,
      embedding_model_id: target.model_id
    }
    const embeddingSelectionSignature = memoryPreview.value.embedding_selection_signature
    const requestGeneration = ++memoryEmbeddingRequestGeneration
    memoryEmbeddingConfirming.value = true
    try {
      const res = await profileApi.memoryEmbeddingConfirm({
        profile_id: profileId,
        memory,
        embedding_selection_signature: embeddingSelectionSignature
      })
      if (!isCurrentMemoryEmbeddingRequest(requestGeneration, profileId, targetKey)
        || memoryPreview.value?.embedding_selection_signature !== embeddingSelectionSignature) return
      const confirmed = res.data.data || {}
      if (confirmed.configs?.memory) {
        persistedMemoryConfig.value = JSON.parse(JSON.stringify(confirmed.configs.memory))
        form.configs.memory = {
          ...form.configs.memory,
          embedding_channel_id: confirmed.configs.memory.embedding_channel_id,
          embedding_model_id: confirmed.configs.memory.embedding_model_id
        }
      }
      memoryRuntime.value = confirmed.memory_runtime || memoryRuntime.value
      memoryStorageConfigured.value = Boolean(
        memoryRuntime.value.embedding_channel_id && memoryRuntime.value.embedding_model_id
      )
      closeMemoryEmbeddingDialog()
      ElMessage.success(t(isInitialSelection
        ? 'profiles.memory_embedding_enable_success'
        : 'profiles.memory_embedding_migration_started'))
      loadProfiles()
    } catch (err) {
      if (!isCurrentMemoryEmbeddingRequest(requestGeneration, profileId, targetKey)
        || memoryPreview.value?.embedding_selection_signature !== embeddingSelectionSignature) return
      ElMessage.error(err.message || t('profiles.submit_failed'))
    } finally {
      if (requestGeneration === memoryEmbeddingRequestGeneration) memoryEmbeddingConfirming.value = false
    }
  }

  const handleMemoryOwnerChange = (uid) => {
    if (!dialogVisible.value || dialogType.value !== 'create') return
    form.uid = uid || null
    form.memory_organization = {
      auto_organize_enabled: false,
      organization_channel_id: null,
      organization_model_id: null
    }
    loadMemorySettings()
  }

  const handleDialogVisibilityChange = (visible) => {
    if (visible) return
    closeMemoryEmbeddingDialog()
    memorySettingsRequestTracker.invalidate()
    memorySettingsLoading.value = false
    memorySettingsReady.value = false
    memorySettingsUnavailable.value = false
  }

  const buildConfigsForSave = () => {
    const configs = JSON.parse(JSON.stringify(form.configs))
    const active = currentMemoryEmbedding.value
    configs.memory.embedding_channel_id = active.channel_id || null
    configs.memory.embedding_model_id = active.model_id || null
    return configs
  }

  return {
    memoryEmbeddingTargetKey,
    memoryPreview,
    memoryEmbeddingPreviewing,
    memoryEmbeddingDialogVisible,
    memoryConfirmationChecked,
    memoryEmbeddingConfirming,
    memorySettingsLoading,
    memorySettingsReady,
    memorySettingsUnavailable,
    memoryStorageConfigured,
    memoryOrganizationRequiredOutputTokens,
    memoryEmbeddingOptions,
    memoryOrganizationModelOptions,
    memoryOrganizationModelKey,
    memoryOrganizationModel,
    memoryEmbeddingCurrentLabel,
    memoryEmbeddingConfigured,
    memoryEmbeddingTargetLabel,
    memoryEmbeddingMigrationActive,
    memoryEmbeddingMigrationStatusText,
    memoryEmbeddingMigrationStatusType,
    memoryPreviewRequiresMigration,
    loadMemorySettings,
    previewMemoryEmbedding,
    openMemoryEmbeddingDialog,
    closeMemoryEmbeddingDialog,
    confirmMemoryEmbedding,
    handleMemoryOwnerChange,
    handleDialogVisibilityChange,
    buildConfigsForSave,
    resetDialogMemoryState,
    initializeMemoryState
  }
}
