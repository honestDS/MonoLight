import { computed, ref } from 'vue'

import { SESSION_MAX_TURNS_UPPER_BOUND } from '../../constants/index.js'

export const createSessionAgentSettings = ({ sessionManager, currentSession }) => {
  const goalModeDefault = ref(true)
  const maxTurnsDefault = ref(5)

  const currentSessionGoalMode = computed({
    get: () => {
      if (!sessionManager.currentSessionId.value) return goalModeDefault.value
      return currentSession.value?.goal_mode ?? true
    },
    set: (goalMode) => {
      const enabled = Boolean(goalMode)
      const sessionId = sessionManager.currentSessionId.value
      if (!sessionId) {
        goalModeDefault.value = enabled
        return
      }

      const sessionIndex = sessionManager.sessions.value.findIndex(session => session.session_id === sessionId)
      if (sessionIndex !== -1) {
        sessionManager.sessions.value[sessionIndex] = {
          ...sessionManager.sessions.value[sessionIndex],
          goal_mode: enabled
        }
      }
    }
  })

  const currentSessionMaxTurns = computed({
    get: () => {
      if (!sessionManager.currentSessionId.value) return maxTurnsDefault.value
      return currentSession.value?.max_turns ?? 5
    },
    set: (maxTurns) => {
      if (typeof maxTurns !== 'number' || !Number.isInteger(maxTurns) || maxTurns < 1 || maxTurns > SESSION_MAX_TURNS_UPPER_BOUND) return

      const sessionId = sessionManager.currentSessionId.value
      if (!sessionId) {
        maxTurnsDefault.value = maxTurns
        return
      }

      const sessionIndex = sessionManager.sessions.value.findIndex(session => session.session_id === sessionId)
      if (sessionIndex !== -1) {
        sessionManager.sessions.value[sessionIndex] = {
          ...sessionManager.sessions.value[sessionIndex],
          max_turns: maxTurns
        }
      }
    }
  })

  return {
    goalModeDefault,
    maxTurnsDefault,
    currentSessionGoalMode,
    currentSessionMaxTurns
  }
}

const isValidMaxTurns = (value) => (
  typeof value === 'number' &&
  Number.isInteger(value) &&
  value >= 1 &&
  value <= SESSION_MAX_TURNS_UPPER_BOUND
)

export const createSessionAgentSettingUpdater = ({
  currentSessionId,
  sessions,
  goalModeDefault,
  maxTurnsDefault,
  agentSettingSubmitting,
  loading,
  updateSessionSetting,
  reportError,
  translate
}) => {
  const updateSessionAgentSetting = async (field, value) => {
    if (agentSettingSubmitting.value || loading.value) return

    if (field === 'max_turns' && !isValidMaxTurns(value)) {
      reportError(translate('chat.max_turns_invalid'))
      return
    }

    agentSettingSubmitting.value = true
    const sessionId = currentSessionId.value
    try {
      if (!sessionId) {
        if (field === 'goal_mode') goalModeDefault.value = value
        if (field === 'max_turns') maxTurnsDefault.value = value
        return
      }

      await updateSessionSetting(sessionId, { [field]: value })
      const session = sessions.value.find(item => item.session_id === sessionId)
      if (session) session[field] = value
    } catch (error) {
      reportError(error.message || translate('chat.setting_failed'))
    } finally {
      agentSettingSubmitting.value = false
    }
  }

  const updateSessionGoalMode = (value) => updateSessionAgentSetting('goal_mode', value)
  const updateSessionMaxTurns = (value) => updateSessionAgentSetting('max_turns', value)

  return {
    updateSessionAgentSetting,
    updateSessionGoalMode,
    updateSessionMaxTurns
  }
}
