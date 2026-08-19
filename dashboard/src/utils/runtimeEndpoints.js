const getConfiguredBaseUrl = configuredBaseUrl =>
  typeof configuredBaseUrl === 'string' ? configuredBaseUrl.trim() : ''

const withoutTrailingSlash = url => {
  const hash = url.hash
  const search = url.search

  url.hash = ''
  url.search = ''

  return `${url.toString().replace(/\/+$/, '')}${search}${hash}`
}

export const resolveApiBaseUrl = (configuredBaseUrl, pageOrigin) => {
  const target = getConfiguredBaseUrl(configuredBaseUrl) || '/api/v1'

  return withoutTrailingSlash(new URL(target, pageOrigin))
}

export const resolveWebSocketBaseUrl = (configuredBaseUrl, apiBaseUrl, pageOrigin) => {
  const configuredUrl = getConfiguredBaseUrl(configuredBaseUrl)
  const target = configuredUrl || new URL(apiBaseUrl, pageOrigin).origin
  const url = new URL(target, pageOrigin)

  if (url.protocol === 'http:') {
    url.protocol = 'ws:'
  } else if (url.protocol === 'https:') {
    url.protocol = 'wss:'
  }

  return withoutTrailingSlash(url)
}
