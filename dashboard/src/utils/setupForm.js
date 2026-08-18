import { isValidHttpProxy, normalizeHttpProxy } from './channelHttpProxy.js'

export const SETUP_PROTOCOLS = Object.freeze(['OPENAI', 'OPENAI_RESPONSES'])

function stringValue(value) {
  return typeof value === 'string' ? value : ''
}

function trimmedString(value) {
  return stringValue(value).trim()
}

function validationError(key, params = {}) {
  return { key, params }
}

function isPlainObject(value) {
  if (!value || typeof value !== 'object') {
    return false
  }

  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

export function unicodeLength(value) {
  return Array.from(stringValue(value)).length
}

export function utf8ByteLength(value) {
  return new TextEncoder().encode(stringValue(value)).length
}

export function validateSetupUsername(value) {
  const username = stringValue(value)

  if (!username) {
    return validationError('required')
  }

  const length = unicodeLength(username)
  if (length < 3 || length > 50) {
    return validationError('username_length')
  }

  if (!/^[A-Za-z0-9_-]+$/.test(username)) {
    return validationError('username_format')
  }

  return null
}

export function validateSetupPassword(value) {
  const password = stringValue(value)

  if (!password) {
    return validationError('required')
  }

  const length = unicodeLength(password)
  if (length < 8 || length > 72) {
    return validationError('password_length')
  }

  if (utf8ByteLength(password) > 72) {
    return validationError('password_bytes')
  }

  return null
}

export function validateSetupPasswordConfirmation(value, password) {
  const confirmation = stringValue(value)

  if (!confirmation) {
    return validationError('required')
  }

  if (confirmation !== password) {
    return validationError('password_mismatch')
  }

  return null
}

export function validateSetupName(value) {
  const name = trimmedString(value)

  if (!name) {
    return validationError('required')
  }

  if (unicodeLength(name) > 100) {
    return validationError('max_length', { max: 100 })
  }

  return null
}

export function validateSetupBaseUrl(value) {
  const baseUrl = trimmedString(value)

  if (!baseUrl) {
    return validationError('required')
  }

  if (unicodeLength(baseUrl) > 2048) {
    return validationError('max_length', { max: 2048 })
  }

  if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
    return validationError('url_format')
  }

  return null
}

export function validateSetupHttpProxy(value) {
  if (!isValidHttpProxy(value)) {
    return validationError('proxy_format')
  }

  return null
}

export function validateSetupApiKey(value) {
  if (typeof value !== 'string' || !value) {
    return validationError('required')
  }

  return null
}

export function validateSetupModelId(value) {
  const modelId = trimmedString(value)

  if (!modelId) {
    return validationError('required')
  }

  if (unicodeLength(modelId) > 255) {
    return validationError('max_length', { max: 255 })
  }

  return null
}

export function validateSetupProtocol(value) {
  if (!SETUP_PROTOCOLS.includes(value)) {
    return validationError('required')
  }

  return null
}

export function buildSetupRequest(form) {
  const source = form && typeof form === 'object' ? form : {}
  const admin = source.admin && typeof source.admin === 'object' ? source.admin : {}
  const channel = source.channel && typeof source.channel === 'object' ? source.channel : {}
  const profile = source.profile && typeof source.profile === 'object' ? source.profile : {}

  return {
    admin: {
      username: trimmedString(admin.username),
      password: stringValue(admin.password)
    },
    channel: {
      name: trimmedString(channel.name),
      base_url: trimmedString(channel.base_url),
      api_key: stringValue(channel.api_key),
      model_id: trimmedString(channel.model_id),
      protocol: stringValue(channel.protocol),
      http_proxy: normalizeHttpProxy(channel.http_proxy) || null,
      image_understanding: Boolean(channel.image_understanding),
      audio_understanding: Boolean(channel.audio_understanding),
      video_understanding: Boolean(channel.video_understanding),
      context_window_k: channel.context_window_k,
      temperature: channel.temperature,
      top_p: channel.top_p,
      max_tokens: channel.max_tokens,
      description: stringValue(channel.description),
      advanced_settings: isPlainObject(channel.advanced_settings)
        ? { ...channel.advanced_settings }
        : {}
    },
    profile: {
      name: trimmedString(profile.name)
    }
  }
}

export function readSetupTokenData(response) {
  const data = response?.data?.data

  if (
    typeof data?.access_token !== 'string' ||
    !data.access_token ||
    typeof data.token_type !== 'string' ||
    !data.token_type ||
    !Number.isInteger(data.profile_id) ||
    data.profile_id <= 0 ||
    !Number.isInteger(data.channel_id) ||
    data.channel_id <= 0
  ) {
    return null
  }

  return {
    access_token: data.access_token,
    token_type: data.token_type,
    profile_id: data.profile_id,
    channel_id: data.channel_id
  }
}

export function cloneSetupProfileConfigs(configs) {
  try {
    if (
      !isPlainObject(configs) ||
      !isPlainObject(configs.channel) ||
      !isPlainObject(configs.security) ||
      !isPlainObject(configs.tool) ||
      !isPlainObject(configs.other) ||
      !isPlainObject(configs.memory) ||
      !Array.isArray(configs.tool.enabled_tools) ||
      !Array.isArray(configs.tool.allowed_operation_dirs) ||
      !Array.isArray(configs.tool.file_send_blocked_extensions)
    ) {
      return null
    }

    return JSON.parse(JSON.stringify(configs))
  } catch {
    return null
  }
}

export function readSetupProfileGuideData(response, profileId) {
  try {
    if (!Number.isInteger(profileId) || profileId <= 0) {
      return null
    }

    const data = response?.data?.data
    if (!Array.isArray(data?.items) || !Array.isArray(data?.meta?.tool_options)) {
      return null
    }

    const profile = data.items.find(item => item?.id === profileId)
    const configs = cloneSetupProfileConfigs(profile?.configs)
    if (!configs) {
      return null
    }

    const toolOptions = []
    for (const option of data.meta.tool_options) {
      if (
        !isPlainObject(option) ||
        typeof option.value !== 'string' ||
        !option.value ||
        typeof option.label !== 'string' ||
        !option.label
      ) {
        return null
      }

      toolOptions.push({ value: option.value, label: option.label })
    }

    return { configs, toolOptions }
  } catch {
    return null
  }
}
