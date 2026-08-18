export const customHeadersTemplate = {
  'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
  accept: 'application/json, text/plain, */*',
  'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
  'cache-control': 'no-cache'
}

export const customHeadersPlaceholder = JSON.stringify({ 'user-agent': customHeadersTemplate['user-agent'] })

const httpHeaderNamePattern = /^[!#$%&'*+.^_\x60|~0-9A-Za-z-]+$/
const reservedCustomHeaderNames = new Set([
  'authorization',
  'content-type',
  'content-length',
  'host',
  'connection',
  'transfer-encoding',
  'proxy-authorization',
  'proxy-connection'
])

const isPlainJsonObject = (value) => (
  value !== null &&
  typeof value === 'object' &&
  !Array.isArray(value) &&
  Object.getPrototypeOf(value) === Object.prototype
)

export const ensureAdvancedSettings = (entry) => {
  if (!isPlainJsonObject(entry.advanced_settings)) {
    entry.advanced_settings = {}
  }
}

const getCustomHeaders = (settings) => {
  if (!isPlainJsonObject(settings)) return {}
  if (isPlainJsonObject(settings.custom_headers)) return settings.custom_headers
  if (settings.custom_headers === undefined && typeof settings.user_agent === 'string') {
    return { 'user-agent': settings.user_agent }
  }
  return {}
}

export const formatAdvancedSettings = (settings) => {
  const customHeaders = getCustomHeaders(settings)
  if (Object.keys(customHeaders).length === 0) return ''
  return JSON.stringify(customHeaders, null, 2)
}

export const parseAdvancedSettingsDraft = (draft, translate) => {
  const text = typeof draft === 'string' ? draft.trim() : ''
  if (!text) return { value: {}, error: '' }

  let settings
  try {
    settings = JSON.parse(text)
  } catch {
    return { value: null, error: translate('channels.custom_headers_json_object_error') }
  }

  if (!isPlainJsonObject(settings)) {
    return { value: null, error: translate('channels.custom_headers_json_object_error') }
  }

  const headerEntries = Object.entries(settings)
  if (headerEntries.length > 32) {
    return { value: null, error: translate('channels.custom_headers_max_items_error') }
  }

  const normalizedHeaders = {}
  const headerNames = new Set()
  for (const [headerName, headerValue] of headerEntries) {
    if (!httpHeaderNamePattern.test(headerName)) {
      return { value: null, error: translate('channels.custom_headers_name_error') }
    }

    const normalizedName = headerName.toLowerCase()
    if (headerNames.has(normalizedName)) {
      return { value: null, error: translate('channels.custom_headers_name_error') }
    }
    if (reservedCustomHeaderNames.has(normalizedName)) {
      return {
        value: null,
        error: translate('channels.custom_headers_reserved_header_error', { header: normalizedName })
      }
    }
    if (typeof headerValue !== 'string') {
      return { value: null, error: translate('channels.custom_headers_value_error') }
    }

    const normalizedValue = headerValue.trim()
    if (!normalizedValue || normalizedValue.length > 4096 || /[^\x20-\x7E]/.test(normalizedValue)) {
      return { value: null, error: translate('channels.custom_headers_value_error') }
    }
    Object.defineProperty(normalizedHeaders, normalizedName, {
      configurable: true,
      enumerable: true,
      value: normalizedValue,
      writable: true
    })
    headerNames.add(normalizedName)
  }

  return { value: normalizedHeaders, error: '' }
}

export const mergeCustomHeaders = (entry, customHeaders) => {
  const advancedSettings = isPlainJsonObject(entry.advanced_settings)
    ? { ...entry.advanced_settings }
    : {}
  delete advancedSettings.user_agent
  if (Object.keys(customHeaders).length === 0) {
    delete advancedSettings.custom_headers
  } else {
    advancedSettings.custom_headers = { ...customHeaders }
  }
  entry.advanced_settings = advancedSettings
  return advancedSettings
}
