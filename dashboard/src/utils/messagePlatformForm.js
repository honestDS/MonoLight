import { SESSION_MAX_TURNS_UPPER_BOUND } from '../constants/index.js'

const createDefaultConfig = () => ({
  api_timeout_ms: 15000,
  long_poll_timeout_ms: 30000,
  poll_interval_ms: 1000,
  merge_single_poll_messages: true,
  stream_llm_request: false,
  session_settings: {
    enable_markdown: false,
    goal_mode: true,
    max_turns: 5,
    show_tool_calls: true,
    show_reasoning: true
  }
})

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)

export const createMessagePlatformForm = ({ uid = '', isSuperuser = false } = {}) => ({
  name: '',
  platform_type: 'WEIXIN_OPENCLAW',
  language: 'zh',
  use_stream_dispatch: false,
  is_enabled: true,
  uid: isSuperuser ? '' : uid,
  profile_id: null,
  config: createDefaultConfig(),
  state: {}
})

export const loadMessagePlatformForm = (row, options = {}) => {
  const source = isObject(row) ? row : {}
  const { uid = '', isSuperuser = false } = options || {}
  const form = createMessagePlatformForm({ uid, isSuperuser })
  const rowConfig = isObject(source.config) ? source.config : {}
  const rowSessionSettings = isObject(rowConfig.session_settings) ? rowConfig.session_settings : {}
  const useStreamDispatch = Boolean(source.use_stream_dispatch)

  const config = {
    ...form.config,
    ...rowConfig,
    session_settings: {
      ...form.config.session_settings,
      ...rowSessionSettings
    }
  }

  if (rowSessionSettings.show_tool_calls === undefined) {
    config.session_settings.show_tool_calls = useStreamDispatch
  }

  const profileId = source.profile_id

  return {
    ...form,
    name: source.name ?? form.name,
    platform_type: source.platform_type ?? form.platform_type,
    language: source.language ?? form.language,
    use_stream_dispatch: useStreamDispatch,
    is_enabled: source.is_enabled ?? form.is_enabled,
    uid: isSuperuser ? source.uid || '' : uid,
    profile_id: profileId === null || profileId === undefined || profileId === '' ? null : profileId,
    config,
    state: {}
  }
}

export const buildMessagePlatformPayload = (form, { includeUid = false } = {}) => {
  const defaultForm = createMessagePlatformForm()
  const config = isObject(form?.config) ? form.config : {}
  const sessionSettings = isObject(config.session_settings) ? config.session_settings : {}

  return {
    name: form.name.trim(),
    platform_type: form.platform_type,
    language: form.language,
    use_stream_dispatch: form.use_stream_dispatch,
    is_enabled: form.is_enabled,
    ...(includeUid ? { uid: form.uid || null } : {}),
    profile_id: form.profile_id ?? null,
    config: {
      api_timeout_ms: config.api_timeout_ms ?? defaultForm.config.api_timeout_ms,
      long_poll_timeout_ms: config.long_poll_timeout_ms ?? defaultForm.config.long_poll_timeout_ms,
      poll_interval_ms: config.poll_interval_ms ?? defaultForm.config.poll_interval_ms,
      merge_single_poll_messages: config.merge_single_poll_messages ?? defaultForm.config.merge_single_poll_messages,
      stream_llm_request: config.stream_llm_request ?? defaultForm.config.stream_llm_request,
      session_settings: {
        enable_markdown: sessionSettings.enable_markdown ?? defaultForm.config.session_settings.enable_markdown,
        goal_mode: sessionSettings.goal_mode ?? defaultForm.config.session_settings.goal_mode,
        max_turns: sessionSettings.max_turns ?? defaultForm.config.session_settings.max_turns,
        show_tool_calls: sessionSettings.show_tool_calls ?? defaultForm.config.session_settings.show_tool_calls,
        show_reasoning: sessionSettings.show_reasoning ?? defaultForm.config.session_settings.show_reasoning
      }
    }
  }
}

export const isValidMessagePlatformMaxTurns = (value) => (
  Number.isInteger(value) && value >= 1 && value <= SESSION_MAX_TURNS_UPPER_BOUND
)
