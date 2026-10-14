import { computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { profileApi } from '../../api/index.js'
import { defaultProfileConfigs } from '../../constants/index.js'
import {
  buildOrganizationSettingsPayload,
  validateOrganizationSettings
} from '../../utils/memoryManagement.js'
import {
  buildKnowledgeBaseBindingPayload,
  filterKnowledgeBaseIdsForOwner,
  filterUserKnowledgeBasesForOwner
} from '../../utils/profileOptions.js'

export function useProfileEditor({
  t,
  userPermissions,
  channels,
  users,
  knowledgeBases,
  knowledgeBasesReady,
  showOwnerColumn,
  currentUid,
  loadProfiles,
  state,
  memory
}) {
  const {
    form,
    dialogVisible,
    dialogType,
    submitting,
    activeTab,
    allowedOperationDirInput,
    fileSendBlockedExtensionInput
  } = state

  const auditModelOptions = computed(() => {
    const options = []
    channels.value
      .filter(channel => channel.is_active !== false)
      .forEach(channel => {
        ;(channel.model_ids || [])
          .filter(model => model.usage === 'CHAT' && model.model_id)
          .forEach(model => {
            options.push({
              key: `${channel.id}::${model.model_id}`,
              channel_id: channel.id,
              model_id: model.model_id,
              label: `${channel.name} / ${model.model_id}`
            })
          })
      })
    return options
  })

  const knowledgeBaseOptions = computed(() => filterUserKnowledgeBasesForOwner(knowledgeBases.value, form.uid)
    .map(item => ({
      value: item.id,
      label: item.name,
      description: item.description || ''
    })))

  const filterFormKnowledgeBaseIds = () => {
    if (!knowledgeBasesReady.value) return
    form.knowledge_base_ids = filterKnowledgeBaseIdsForOwner(form.knowledge_base_ids, knowledgeBases.value, form.uid)
  }

  watch(() => form.uid, filterFormKnowledgeBaseIds)
  watch([knowledgeBasesReady, knowledgeBases], filterFormKnowledgeBaseIds)

  const addUniqueListValue = (targetList, rawValue, normalizeValue = value => value) => {
    const value = normalizeValue((rawValue || '').trim())
    if (!value || targetList.includes(value)) return false
    targetList.push(value)
    return true
  }

  const addAllowedOperationDir = () => {
    if (addUniqueListValue(form.configs.tool.allowed_operation_dirs, allowedOperationDirInput.value)) {
      allowedOperationDirInput.value = ''
    }
  }

  const removeAllowedOperationDir = (value) => {
    form.configs.tool.allowed_operation_dirs = form.configs.tool.allowed_operation_dirs.filter(item => item !== value)
  }

  const normalizeExtension = (value) => {
    if (!value) return ''
    return value.startsWith('.') ? value.toLowerCase() : `.${value.toLowerCase()}`
  }

  const addFileSendBlockedExtension = () => {
    if (addUniqueListValue(form.configs.tool.file_send_blocked_extensions, fileSendBlockedExtensionInput.value, normalizeExtension)) {
      fileSendBlockedExtensionInput.value = ''
    }
  }

  const removeFileSendBlockedExtension = (value) => {
    form.configs.tool.file_send_blocked_extensions = form.configs.tool.file_send_blocked_extensions.filter(item => item !== value)
  }

  const migrateToolConfig = (toolConfig) => {
    if (!toolConfig || toolConfig.tool_timeout !== undefined) return toolConfig
    if (toolConfig.tool_timeout !== undefined) {
      toolConfig.tool_timeout = toolConfig.tool_timeout
    } else if (toolConfig.tool_timeout !== undefined) {
      toolConfig.tool_timeout = toolConfig.tool_timeout
    }
    return toolConfig
  }

  const migrateSecurityConfig = (securityConfig) => {
    const normalized = { ...(securityConfig || {}) }
    if (normalized.audit_confirmation_timeout_seconds === undefined && normalized.audit_confirmation_timeout_minutes !== undefined) {
      const legacyMinutes = Number(normalized.audit_confirmation_timeout_minutes)
      if (Number.isFinite(legacyMinutes)) {
        normalized.audit_confirmation_timeout_seconds = legacyMinutes * 60
      }
    }
    delete normalized.audit_confirmation_timeout_minutes
    return normalized
  }

  const auditModelKey = computed({
    get() {
      const security = form.configs.security
      if (!security.audit_channel_id || !security.audit_model_id) return null
      return `${security.audit_channel_id}::${security.audit_model_id}`
    },
    set(key) {
      if (!key) {
        form.configs.security.audit_channel_id = null
        form.configs.security.audit_model_id = null
        return
      }

      const option = auditModelOptions.value.find(item => item.key === key)
      if (!option) return
      form.configs.security.audit_channel_id = option.channel_id
      form.configs.security.audit_model_id = option.model_id
    }
  })

  const showDialog = (type, row = null) => {
    memory.resetDialogMemoryState()
    dialogType.value = type
    activeTab.value = 'base'
    allowedOperationDirInput.value = ''
    fileSendBlockedExtensionInput.value = ''
    if (type === 'edit' && row) {
      form.id = row.id
      form.uid = row.uid || null
      form.name = row.name
      form.prompt_id = row.prompt_id
      form.knowledge_base_ids = [...(row.knowledge_base_ids || [])]
      form.memory_organization = {
        auto_organize_enabled: Boolean(row.memory_organization?.auto_organize_enabled),
        organization_channel_id: row.memory_organization?.organization_channel_id ?? null,
        organization_model_id: row.memory_organization?.organization_model_id ?? null
      }
      const base = defaultProfileConfigs()
      if (row.configs) {
        if (row.configs.tool) migrateToolConfig(row.configs.tool)
        if (row.configs.channel) {
          const p = row.configs.channel
          // 深合并渠道配置
          if (p.chat_channel) Object.assign(base.channel.chat_channel, JSON.parse(JSON.stringify(p.chat_channel)))
          if (p.context_summary_channel) {
            Object.assign(base.channel.context_summary_channel, JSON.parse(JSON.stringify(p.context_summary_channel)))
          } else {
            base.channel.context_summary_channel = JSON.parse(JSON.stringify(base.channel.chat_channel))
          }
          if (p.rerank_channel) Object.assign(base.channel.rerank_channel, JSON.parse(JSON.stringify(p.rerank_channel)))
          if (p.image_generation_channel) Object.assign(base.channel.image_generation_channel, JSON.parse(JSON.stringify(p.image_generation_channel)))
        }
        if (row.configs.security) Object.assign(base.security, migrateSecurityConfig(row.configs.security))
        if (row.configs.tool) Object.assign(base.tool, row.configs.tool)
        if (row.configs.other) Object.assign(base.other, row.configs.other)
        if (row.configs.memory) Object.assign(base.memory, JSON.parse(JSON.stringify(row.configs.memory)))
      }
      form.configs = base
      memory.initializeMemoryState(base.memory, row.memory_runtime || {})
    } else {
      form.id = null
      form.uid = (userPermissions.isSuperuser ? users.value[0]?.uid : null) || userPermissions.currentUser?.uid || currentUid.value || null
      form.name = ''
      form.prompt_id = null
      form.knowledge_base_ids = []
      form.memory_organization = {
        auto_organize_enabled: false,
        organization_channel_id: null,
        organization_model_id: null
      }
      form.configs = defaultProfileConfigs()
      memory.initializeMemoryState(form.configs.memory, {})
    }
    dialogVisible.value = true
    memory.loadMemorySettings()
  }

  const submitForm = async () => {
    if (!form.name) {
      return ElMessage.warning(t('profiles.fill_required'))
    }
    if (dialogType.value === 'create' && showOwnerColumn.value && !form.uid) {
      return ElMessage.warning(t('profiles.select_owner'))
    }

    const organizationValidationError = validateOrganizationSettings(
      form.memory_organization,
      memory.memoryOrganizationModel.value,
      memory.memoryOrganizationRequiredOutputTokens.value
    )
    if (organizationValidationError) {
      return ElMessage.warning(t(`profiles.${organizationValidationError}`, { required: memory.memoryOrganizationRequiredOutputTokens.value }))
    }

    // 清理无效规则与旧版规则级启用状态，并按后端规则排序：priority 数字越小越优先。
    // 同一 priority 内保留当前顺序，因为该顺序就是加权轮询周期内的使用顺序。
    const compareRules = (left, right) => {
      return (left.priority || 1) - (right.priority || 1)
    }

    const cleanChannel = (ch) => {
      if (ch && ch.rules) {
        ch.rules = ch.rules
          .filter(r => r.channel_id && r.model_id)
          .map(({ channel_id, model_id, priority, weight, reasoning_effort }) => ({
            channel_id,
            model_id,
            priority,
            weight,
            reasoning_effort: typeof reasoning_effort === 'string' ? reasoning_effort.trim() || null : null
          }))
          .sort(compareRules)
      }
    }
    cleanChannel(form.configs.channel.chat_channel)
    cleanChannel(form.configs.channel.context_summary_channel)
    cleanChannel(form.configs.channel.rerank_channel)
    cleanChannel(form.configs.channel.image_generation_channel)
    addAllowedOperationDir()
    addFileSendBlockedExtension()
    form.configs.security = migrateSecurityConfig(form.configs.security)

    submitting.value = true
    try {
      const payload = {
        ...(dialogType.value === 'create' ? { uid: form.uid } : {}),
        name: form.name,
        prompt_id: form.prompt_id,
        memory_organization: buildOrganizationSettingsPayload(form.memory_organization),
        configs: memory.buildConfigsForSave()
      }
      if (userPermissions.currentUser && knowledgeBasesReady.value) {
        const knowledgeBaseIds = buildKnowledgeBaseBindingPayload(form.knowledge_base_ids, knowledgeBasesReady.value)
        if (knowledgeBaseIds !== undefined) {
          payload.knowledge_base_ids = knowledgeBaseIds
        }
      }

      if (dialogType.value === 'create') {
        await profileApi.create(payload)
      } else {
        await profileApi.update(form.id, payload)
      }
      ElMessage.success(t('profiles.save_success'))
      dialogVisible.value = false
      loadProfiles()
    } catch (err) {
      ElMessage.error(err.message || t('profiles.submit_failed'))
    } finally {
      submitting.value = false
    }
  }

  return {
    auditModelOptions,
    auditModelKey,
    knowledgeBaseOptions,
    addAllowedOperationDir,
    removeAllowedOperationDir,
    addFileSendBlockedExtension,
    removeFileSendBlockedExtension,
    showDialog,
    submitForm
  }
}
