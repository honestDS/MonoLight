import { h, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { profileApi, promptApi, systemApi, adminApi, knowledgeBaseApi } from '../../api/index.js'
import { defaultProfileConfigs } from '../../constants/index.js'
import { SUPPORT_LOCALES } from '../../i18n/index.js'
import { useUserPermissionsStore } from '../useUserPermissions.js'
import { useProfileMemory } from './useProfileMemory.js'
import { useProfileEditor } from './useProfileEditor.js'

export function useProfilesView() {
  const { t } = useI18n()
  const userPermissions = useUserPermissionsStore()

  const configPermissions = ref({ security: false })
  const profiles = ref([])
  const users = ref([])
  const channels = ref([])
  const prompts = ref([])
  const knowledgeBases = ref([])
  const knowledgeBasesLoading = ref(false)
  const knowledgeBasesReady = ref(false)
  const knowledgeBasesUnavailable = ref(false)
  const toolOptions = ref([])
  const showOwnerColumn = ref(false)
  const currentUid = ref(null)
  const loading = ref(false)
  const total = ref(0)
  const currentPage = ref(1)
  const pageSize = ref(10)
  const filters = reactive({
    ownerUsername: ''
  })
  const dialogVisible = ref(false)
  const settingsDialogVisible = ref(false)
  const dialogType = ref('create')
  const submitting = ref(false)
  const settingsSubmitting = ref(false)
  const activeTab = ref('base')
  const allowedOperationDirInput = ref('')
  const fileSendBlockedExtensionInput = ref('')
  const localeOptions = SUPPORT_LOCALES
  const contextSummaryThresholdOptions = [50, 60, 70, 80, 90]

  watch(() => userPermissions.currentUser, () => {
    configPermissions.value = { security: false }
  })

  const systemSettings = reactive({
    log_locale: 'zh',
    log_redaction_enabled: false,
    temp_dir_max_size_mb: 1024,
    llm_request_max_size_mb: 32,
    audit_retention_days: 90,
    audit_report_email: '',
    session_reply_max_concurrency: 4
  })

  const form = reactive({
    id: null,
    uid: null,
    name: '',
    prompt_id: null,
    knowledge_base_ids: [],
    memory_organization: {
      auto_organize_enabled: false,
      organization_channel_id: null,
      organization_model_id: null
    },
    configs: defaultProfileConfigs()
  })

  const canSetDefaultProfile = (row) => Boolean(
    row
    && (userPermissions.isSuperuser
      || (userPermissions.currentUser?.uid && row.uid === userPermissions.currentUser.uid))
  )

  const loadProfiles = async () => {
    loading.value = true
    try {
      const params = {
        page: currentPage.value,
        size: pageSize.value
      }
      if (userPermissions.isSuperuser && filters.ownerUsername.trim()) {
        params.owner_username = filters.ownerUsername.trim()
      }
      const requestedUser = userPermissions.currentUser
      const res = await profileApi.list(params)
      if (userPermissions.currentUser !== requestedUser) return
      profiles.value = res.data.data.items || []
      total.value = res.data.data.total || 0
      toolOptions.value = res.data.data.meta?.tool_options || []
      showOwnerColumn.value = Boolean(res.data.data.meta?.show_owner)
      currentUid.value = res.data.data.meta?.current_uid || null
      configPermissions.value = {
        security: false,
        ...(res.data.data.meta?.config_permissions || {})
      }
      if (showOwnerColumn.value) fetchUsers()
    } catch (err) {
      ElMessage.error(err.message || t('profiles.load_failed'))
    } finally {
      loading.value = false
    }
  }

  const confirmProfileDeleteImpact = async (data) => {
    const messages = [t('profiles.delete_impact_intro', { name: data?.profile?.name || '' })]
    const sessions = data?.sessions || {}
    if (sessions.count) {
      messages.push(t('profiles.delete_impact_sessions', {
        count: sessions.count,
        messages: sessions.message_count || 0
      }))
      for (const session of sessions.items || []) {
        messages.push(t('profiles.delete_impact_session_item', {
          name: session.title || session.session_id
        }))
      }
      if (sessions.omitted_count) {
        messages.push(t('profiles.delete_impact_more', { count: sessions.omitted_count }))
      }
    }

    const managedKnowledgeBase = data?.managed_knowledge_base
    if (managedKnowledgeBase?.count) {
      const item = managedKnowledgeBase.items?.[0]
      messages.push(t('profiles.delete_impact_managed_kb', {
        name: item?.name || '',
        count: item?.knowledge_count || 0
      }))
    }
    if (data?.scheduled_tasks?.count) {
      messages.push(t('profiles.delete_impact_scheduled_tasks', { count: data.scheduled_tasks.count }))
      for (const item of data.scheduled_tasks.items || []) {
        messages.push(t('profiles.delete_impact_scheduled_task_item', { name: item.name }))
      }
      if (data.scheduled_tasks.omitted_count) {
        messages.push(t('profiles.delete_impact_more', { count: data.scheduled_tasks.omitted_count }))
      }
    }
    if (data?.message_platforms?.count) {
      messages.push(t('profiles.delete_impact_message_platforms', { count: data.message_platforms.count }))
      for (const item of data.message_platforms.items || []) {
        messages.push(t('profiles.delete_impact_message_platform_item', { name: item.name }))
      }
      if (data.message_platforms.omitted_count) {
        messages.push(t('profiles.delete_impact_more', { count: data.message_platforms.omitted_count }))
      }
    }
    if (data?.user_knowledge_base_bindings?.count) {
      messages.push(t('profiles.delete_impact_user_kbs', { count: data.user_knowledge_base_bindings.count }))
      for (const item of data.user_knowledge_base_bindings.items || []) {
        messages.push(t('profiles.delete_impact_user_kb_item', { name: item.name }))
      }
      if (data.user_knowledge_base_bindings.omitted_count) {
        messages.push(t('profiles.delete_impact_more', { count: data.user_knowledge_base_bindings.omitted_count }))
      }
    }

    try {
      await ElMessageBox.confirm(
        h('div', messages.map((message, index) => h('div', { key: index }, message))),
        t('profiles.delete_impact_title'),
        {
          type: 'warning',
          confirmButtonText: t('profiles.delete_impact_confirm'),
          cancelButtonText: t('common.cancel'),
          showCancelButton: true
        }
      )
      return true
    } catch (action) {
      return false
    }
  }

  const handleDelete = async (id) => {
    try {
      let response = await profileApi.delete(id)
      while (response.data?.data?.requires_confirmation) {
        const impact = response.data.data
        if (!await confirmProfileDeleteImpact(impact)) return
        response = await profileApi.delete(id, {
          confirm_impact: true,
          impact_token: impact.impact_token
        })
      }
      ElMessage.success(t('common.delete_success'))
      await loadProfiles()
    } catch (err) {
      ElMessage.error(err.message || t('common.delete_failed'))
    }
  }

  const fetchPrompts = async () => {
    try {
      const res = await promptApi.list({ page: 1, size: 1000 })
      prompts.value = res.data.data.items || []
    } catch (err) {
      ElMessage.error(err.message || t('profiles.load_prompts_failed'))
    }
  }

  const loadSystemSettings = async () => {
    if (!userPermissions.isSuperuser) return false
    try {
      const settingsRes = await systemApi.settings()
      if (!userPermissions.isSuperuser) return false
      Object.assign(systemSettings, settingsRes.data.data || {})
      return true
    } catch (err) {
      ElMessage.error(err.message || t('profiles.load_settings_failed'))
      return false
    }
  }

  const saveSystemSettings = async () => {
    if (!userPermissions.isSuperuser) return
    settingsSubmitting.value = true
    try {
      const res = await systemApi.updateSettings({ ...systemSettings })
      if (!userPermissions.isSuperuser) return
      Object.assign(systemSettings, res.data.data || {})
      settingsDialogVisible.value = false
      ElMessage.success(t('profiles.system_settings_saved'))
    } catch (err) {
      ElMessage.error(err.message || t('profiles.save_settings_failed'))
    } finally {
      settingsSubmitting.value = false
    }
  }

  const showSystemSettingsDialog = async () => {
    if (!userPermissions.isSuperuser) return
    if (await loadSystemSettings() && userPermissions.isSuperuser) {
      settingsDialogVisible.value = true
    }
  }

  const fetchChannels = async () => {
    try {
      const res = await profileApi.modelOptions({ page: 1, size: 1000 })
      channels.value = res.data.data.items || []
    } catch (err) {
      ElMessage.error(err.message || t('profiles.load_channels_failed'))
    }
  }

  const fetchKnowledgeBases = async () => {
    if (!userPermissions.currentUser) return
    knowledgeBasesLoading.value = true
    knowledgeBasesReady.value = false
    knowledgeBasesUnavailable.value = false
    try {
      const res = await knowledgeBaseApi.list({ page: 1, size: 1000 })
      knowledgeBases.value = res.data.data.items || []
      knowledgeBasesReady.value = true
    } catch (err) {
      knowledgeBasesUnavailable.value = true
    } finally {
      knowledgeBasesLoading.value = false
    }
  }

  const fetchUsers = async () => {
    if (!showOwnerColumn.value) return
    try {
      const res = await adminApi.userList({ page: 1, size: 1000 })
      users.value = res.data.data.items || []
    } catch (err) {
      users.value = []
    }
  }

  const handleRefresh = () => {
    currentPage.value = 1
    loadProfiles()
    fetchChannels()
    fetchPrompts()
    loadSystemSettings()
    fetchKnowledgeBases()
    fetchUsers()
  }

  const handleSizeChange = () => {
    currentPage.value = 1
    loadProfiles()
  }

  const handleSearch = () => {
    currentPage.value = 1
    loadProfiles()
  }

  const resetFilters = () => {
    filters.ownerUsername = ''
    currentPage.value = 1
    loadProfiles()
  }

  const handleSetDefault = async (id) => {
    try {
      const res = await profileApi.setDefault(id)
      ElMessage.success(res.data.message || t('profiles.set_default_success'))
      loadProfiles()
    } catch (err) {
      ElMessage.error(err.message || t('profiles.set_default_failed'))
    }
  }

  const memory = useProfileMemory({ t, channels, dialogVisible, dialogType, form, loadProfiles })
  const editor = useProfileEditor({
    t,
    userPermissions,
    configPermissions,
    channels,
    users,
    knowledgeBases,
    knowledgeBasesReady,
    showOwnerColumn,
    currentUid,
    loadProfiles,
    state: {
      form,
      dialogVisible,
      dialogType,
      submitting,
      activeTab,
      allowedOperationDirInput,
      fileSendBlockedExtensionInput
    },
    memory
  })

  onMounted(() => {
    loadProfiles()
    fetchChannels()
    fetchPrompts()
    loadSystemSettings()
    fetchKnowledgeBases()
    fetchUsers()
  })

  return {
    userPermissions,
    configPermissions,
    profiles,
    users,
    channels,
    prompts,
    knowledgeBases,
    knowledgeBasesLoading,
    knowledgeBasesReady,
    knowledgeBasesUnavailable,
    toolOptions,
    showOwnerColumn,
    currentUid,
    loading,
    total,
    currentPage,
    pageSize,
    filters,
    dialogVisible,
    settingsDialogVisible,
    dialogType,
    submitting,
    settingsSubmitting,
    activeTab,
    allowedOperationDirInput,
    fileSendBlockedExtensionInput,
    localeOptions,
    contextSummaryThresholdOptions,
    systemSettings,
    form,
    loadProfiles,
    fetchKnowledgeBases,
    loadSystemSettings,
    handleDelete,
    saveSystemSettings,
    showSystemSettingsDialog,
    handleRefresh,
    handleSizeChange,
    handleSearch,
    resetFilters,
    canSetDefaultProfile,
    handleSetDefault,
    ...memory,
    ...editor
  }
}
