import { computed, onScopeDispose, ref, watch } from 'vue'
import { normalizeReasoningEfforts } from '../../utils/channelModelMetadata.js'

export function useSessionReasoning({
  currentSessionId,
  profileOverrideId,
  currentSessionReasoningEffort,
  sessions,
  loading,
  api,
  onError
}) {
  const id = currentSessionId
  const profileId = profileOverrideId
  const serverReasoningOptions = ref([])
  const reasoningDefaults = ref([])
  const reasoningOptionsLoading = ref(false)
  const reasoningOptionsFailed = ref(false)
  const reasoningEffortSubmitting = ref(false)
  let version = 0
  let active = true

  const reasoningOptions = computed(() => normalizeReasoningEfforts([
    ...serverReasoningOptions.value,
    currentSessionReasoningEffort.value
  ]))

  const loadReasoningOptions = async () => {
    if (!active) return

    const capturedId = id.value
    const capturedProfileId = profileId.value
    const requestVersion = ++version
    const hasSessionId = capturedId !== null && capturedId !== undefined && capturedId !== ''
    const hasProfileId = typeof capturedProfileId === 'string'
      ? capturedProfileId.trim() !== ''
      : capturedProfileId !== null && capturedProfileId !== undefined && capturedProfileId !== ''

    serverReasoningOptions.value = []
    reasoningDefaults.value = []
    reasoningOptionsFailed.value = false

    reasoningOptionsLoading.value = true
    const isCurrentRequest = () => (
      active &&
      requestVersion === version &&
      Object.is(id.value, capturedId) &&
      Object.is(profileId.value, capturedProfileId)
    )

    try {
      const response = await api.sessionReasoningOptions(
        hasSessionId
          ? { session_id: capturedId }
          : hasProfileId
            ? { profile_override_id: capturedProfileId }
            : {}
      )

      if (!isCurrentRequest()) return

      const payload = response?.data?.data || {}
      const options = payload.options
      const defaults = payload.defaults

      serverReasoningOptions.value = normalizeReasoningEfforts(options)
      reasoningDefaults.value = Array.isArray(defaults)
        ? defaults.reduce((normalizedDefaults, value) => {
          if (value === null) {
            if (!normalizedDefaults.includes(null)) normalizedDefaults.push(null)
            return normalizedDefaults
          }

          if (typeof value !== 'string') return normalizedDefaults
          const normalizedValue = value.trim()
          if (normalizedValue && !normalizedDefaults.includes(normalizedValue)) {
            normalizedDefaults.push(normalizedValue)
          }
          return normalizedDefaults
        }, [])
        : []
    } catch (error) {
      if (!isCurrentRequest()) return
      reasoningOptionsFailed.value = true
      if (typeof onError === 'function') onError(error, 'chat.reasoning_effort_load_failed')
    } finally {
      if (isCurrentRequest()) reasoningOptionsLoading.value = false
    }
  }

  const updateSessionReasoningEffort = async value => {
    if (!active || loading.value || reasoningEffortSubmitting.value) return

    let nextValue
    if (value === null) {
      nextValue = null
    } else if (typeof value === 'string') {
      nextValue = value.trim()
      if (!nextValue || Array.from(nextValue).length > 64) {
        if (typeof onError === 'function') onError(null, 'chat.reasoning_effort_invalid')
        return
      }
    } else {
      if (typeof onError === 'function') onError(null, 'chat.reasoning_effort_invalid')
      return
    }

    const currentValue = currentSessionReasoningEffort.value
    const normalizedCurrentValue = typeof currentValue === 'string' ? currentValue.trim() : currentValue
    if (Object.is(normalizedCurrentValue, nextValue)) return

    const capturedId = id.value
    const hasSessionId = capturedId !== null && capturedId !== undefined && capturedId !== ''
    if (!hasSessionId) {
      currentSessionReasoningEffort.value = nextValue
      return
    }

    reasoningEffortSubmitting.value = true
    try {
      await api.updateSessionSetting(capturedId, { reasoning_effort: nextValue })
      if (active && Array.isArray(sessions.value)) {
        const row = sessions.value.find(session => session && Object.is(session.session_id, capturedId))
        if (row) row.reasoning_effort = nextValue
      }
    } catch (error) {
      if (active && Object.is(id.value, capturedId) && typeof onError === 'function') {
        onError(error, 'chat.setting_failed')
      }
    } finally {
      reasoningEffortSubmitting.value = false
    }
  }

  watch([id, profileId], () => {
    void loadReasoningOptions()
  }, { immediate: true, flush: 'sync' })

  onScopeDispose(() => {
    active = false
    version += 1
  })

  return {
    reasoningOptions,
    reasoningDefaults,
    reasoningOptionsLoading,
    reasoningOptionsFailed,
    reasoningEffortSubmitting,
    loadReasoningOptions,
    updateSessionReasoningEffort
  }
}
