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
      protocol: stringValue(channel.protocol)
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
    !data.token_type
  ) {
    return null
  }

  return {
    access_token: data.access_token,
    token_type: data.token_type
  }
}
