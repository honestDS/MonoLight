import { computed, ref } from 'vue'

import { createSessionAgentSettingUpdater } from './sessionAgentSettings.js'
import {
  filterProfilesByUid,
  resolveProfileOwnerUid,
  resolveSessionProfileDisplayId,
  resolveSessionProfilePlaceholder
} from '../../utils/profileOptions'

export function useChatViewSettings({ chat, currentUid, api, profileApi, notify, translate }) {
  const t = translate
  const {
    enableMarkdownDefault,
    sessions,
    currentSessionId,
    currentSession,
    newSessionProfileOverrideId,
    isCurrentSessionReadOnly,
    currentSessionShowToolCalls,
    currentSessionShowReasoning,
    goalModeDefault,
    maxTurnsDefault,
    loading,
    transportMode,
    modeSettingSubmitting,
    transportModeChangeBlocked,
    reloadCurrentSessionHistory,
    setTransportMode
  } = chat

  const profiles = ref([])
  const profilesLoading = ref(false)
  const profileSettingSubmitting = ref(false)
  const toolOutputSettingSubmitting = ref(false)
  const agentSettingSubmitting = ref(false)
  const reasoningSettingSubmitting = ref(false)

  const currentSessionEnableMarkdown = computed({
    get() {
      if (!currentSessionId.value) return enableMarkdownDefault.value
      const session = sessions.value.find(s => s.session_id === currentSessionId.value)
      return session ? session.enable_markdown : false
    },
    set(val) {
      if (!currentSessionId.value) {
        enableMarkdownDefault.value = val
        return
      }
      const session = sessions.value.find(s => s.session_id === currentSessionId.value)
      if (session) {
        session.enable_markdown = val
      }
    }
  })

  const toggleMarkdown = async (val) => {
    if (isCurrentSessionReadOnly.value) {
      notify.warning(t('chat.external_session_read_only'))
      return
    }

    currentSessionEnableMarkdown.value = val

    if (!currentSessionId.value) {
      return
    }

    try {
      await api.updateSessionSetting(currentSessionId.value, { enable_markdown: val })
    } catch (error) {
      notify.error(error.message || t('chat.setting_failed'))
      currentSessionEnableMarkdown.value = !val
    }
  }

  const isWsModeComputed = computed(() => transportMode.value === 'ws')

  const currentSessionProfileDisplayId = computed(() => resolveSessionProfileDisplayId(
    currentSession.value,
    newSessionProfileOverrideId.value
  ))
  const currentSessionProfileOptions = computed(() => filterProfilesByUid(
    profiles.value,
    resolveProfileOwnerUid(currentSession.value, currentUid.value)
  ))
  const currentSessionProfilePlaceholder = computed(() => resolveSessionProfilePlaceholder(
    currentSessionProfileOptions.value,
    isCurrentSessionReadOnly.value,
    t('chat.default_profile_suffix'),
    t('chat.inherited_profile')
  ))

  const {
    updateSessionGoalMode,
    updateSessionMaxTurns
  } = createSessionAgentSettingUpdater({
    currentSessionId,
    sessions,
    goalModeDefault,
    maxTurnsDefault,
    agentSettingSubmitting,
    loading,
    updateSessionSetting: (sessionId, payload) => api.updateSessionSetting(sessionId, payload),
    reportError: message => notify.error(message),
    translate: t
  })

  const loadProfiles = async () => {
    profilesLoading.value = true
    try {
      const res = await profileApi.list({ page: 1, size: 1000 })
      profiles.value = res.data.data.items || []
      currentUid.value = res.data.data.meta?.current_uid || null
    } catch (error) {
      notify.error(t('chat.load_profiles_failed'))
    } finally {
      profilesLoading.value = false
    }
  }

  const updateSessionProfileOverride = async (profileId) => {
    const sessionId = currentSessionId.value
    if (!sessionId) {
      newSessionProfileOverrideId.value = profileId ?? null
      return
    }
    if (profileSettingSubmitting.value) return

    const previousProfileOverrideId = currentSession.value?.profile_override_id ?? null
    profileSettingSubmitting.value = true
    try {
      await api.updateSessionSetting(sessionId, { profile_override_id: profileId ?? null })
      const session = sessions.value.find(item => item.session_id === sessionId)
      if (session) session.profile_override_id = profileId ?? null
      notify.success(t('chat.profile_setting_saved'))
    } catch (error) {
      const session = sessions.value.find(item => item.session_id === sessionId)
      if (session) session.profile_override_id = previousProfileOverrideId
      notify.error(t('chat.setting_failed'))
    } finally {
      profileSettingSubmitting.value = false
    }
  }

  const updateSessionShowToolCalls = async (showToolCalls) => {
    if (toolOutputSettingSubmitting.value || loading.value) return

    const sessionId = currentSessionId.value
    const previousValue = currentSessionShowToolCalls.value
    currentSessionShowToolCalls.value = showToolCalls
    if (!sessionId) return

    toolOutputSettingSubmitting.value = true
    try {
      await api.updateSessionSetting(sessionId, { show_tool_calls: showToolCalls })
      await reloadCurrentSessionHistory()
    } catch (error) {
      currentSessionShowToolCalls.value = previousValue
      notify.error(error.message || t('chat.setting_failed'))
    } finally {
      toolOutputSettingSubmitting.value = false
    }
  }

  const updateSessionShowReasoning = async (showReasoning) => {
    if (reasoningSettingSubmitting.value) return

    const sessionId = currentSessionId.value
    const previousValue = currentSessionShowReasoning.value
    currentSessionShowReasoning.value = showReasoning
    if (!sessionId) return

    reasoningSettingSubmitting.value = true
    try {
      await api.updateSessionSetting(sessionId, { show_reasoning: showReasoning })
    } catch (error) {
      currentSessionShowReasoning.value = previousValue
      notify.error(error.message || t('chat.setting_failed'))
    } finally {
      reasoningSettingSubmitting.value = false
    }
  }

  const handleModeChange = async (val) => {
    if (modeSettingSubmitting.value) return

    if (isCurrentSessionReadOnly.value) {
      notify.warning(t('chat.external_session_read_only'))
      return
    }
    if (transportModeChangeBlocked.value) {
      notify.warning(t('chat.transport_change_blocked'))
      return
    }

    const mode = val ? 'ws' : 'http'
    await setTransportMode(mode)
  }

  const formatSessionSource = (source) => {
    if (source === 'http') return t('chat.session_source_http')
    if (source === 'ws') return t('chat.session_source_ws')
    return source
  }

  return {
    profiles,
    profilesLoading,
    profileSettingSubmitting,
    toolOutputSettingSubmitting,
    agentSettingSubmitting,
    reasoningSettingSubmitting,
    currentSessionEnableMarkdown,
    toggleMarkdown,
    isWsModeComputed,
    currentSessionProfileDisplayId,
    currentSessionProfileOptions,
    currentSessionProfilePlaceholder,
    updateSessionGoalMode,
    updateSessionMaxTurns,
    loadProfiles,
    updateSessionProfileOverride,
    updateSessionShowToolCalls,
    updateSessionShowReasoning,
    handleModeChange,
    formatSessionSource
  }
}
