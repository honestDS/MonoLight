import { defaultModelEntry } from '../constants/index.js'
import { isValidHttpProxy, normalizeHttpProxy } from './channelHttpProxy.js'
import { getModelReasoningEfforts } from './channelModelMetadata.js'

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
  if (typeof value !== 'string' || !value || !value.trim()) {
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

function serializeSetupModel(model) {
  const source = model && typeof model === 'object' ? model : {}

  return {
    model_id: trimmedString(source.model_id),
    protocol: stringValue(source.protocol),
    image_understanding: Boolean(source.image_understanding),
    audio_understanding: Boolean(source.audio_understanding),
    video_understanding: Boolean(source.video_understanding),
    context_window_k: source.context_window_k,
    temperature: source.temperature,
    top_p: source.top_p,
    reasoning_efforts: getModelReasoningEfforts(source),
    max_tokens: source.max_tokens,
    description: stringValue(source.description),
    advanced_settings: isPlainObject(source.advanced_settings)
      ? { ...source.advanced_settings }
      : {}
  }
}

export function addSetupDetectedModels(entries, values) {
  if (!Array.isArray(entries) || !Array.isArray(values)) {
    return []
  }

  const existingIds = new Set()
  for (const entry of entries) {
    if (entry && typeof entry === 'object') {
      const modelId = trimmedString(entry.model_id)
      if (modelId) {
        existingIds.add(modelId)
      }
    }
  }

  const touchedEntries = []
  for (const value of values) {
    if (typeof value !== 'string') {
      continue
    }

    const modelId = value.trim()
    if (!modelId || existingIds.has(modelId)) {
      continue
    }

    const emptyEntry = entries.find(
      entry => entry && typeof entry === 'object' && !trimmedString(entry.model_id)
    )
    const entry = emptyEntry || defaultModelEntry()

    if (!emptyEntry) {
      entries.push(entry)
    }

    entry.model_id = modelId
    existingIds.add(modelId)
    touchedEntries.push(entry)
  }

  return touchedEntries
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
      http_proxy: normalizeHttpProxy(channel.http_proxy) || null,
      model_ids: Array.isArray(channel.model_ids)
        ? channel.model_ids.map(serializeSetupModel)
        : []
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

export function readSetupProfileGuideData(profileResponse, promptResponse, profileId) {
  try {
    if (!Number.isInteger(profileId) || profileId <= 0) {
      return null
    }

    const profileData = profileResponse?.data?.data
    if (
      !Array.isArray(profileData?.items) ||
      !Array.isArray(profileData?.meta?.tool_options)
    ) {
      return null
    }

    const profile = profileData.items.find(item => item?.id === profileId)
    if (!Number.isInteger(profile?.prompt_id) || profile.prompt_id <= 0) {
      return null
    }

    const promptItems = promptResponse?.data?.data?.items
    if (!Array.isArray(promptItems)) {
      return null
    }

    const prompt = promptItems.find(item => item?.id === profile.prompt_id)
    if (
      !isPlainObject(prompt) ||
      !Number.isInteger(prompt.id) ||
      prompt.id <= 0 ||
      prompt.id !== profile.prompt_id ||
      typeof prompt.name !== 'string' ||
      !prompt.name ||
      typeof prompt.content !== 'string'
    ) {
      return null
    }

    const configs = cloneSetupProfileConfigs(profile?.configs)
    if (!configs) {
      return null
    }

    const toolOptions = []
    for (const option of profileData.meta.tool_options) {
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

    return {
      configs,
      prompt: {
        id: prompt.id,
        name: prompt.name,
        content: prompt.content
      },
      toolOptions
    }
  } catch {
    return null
  }
}
