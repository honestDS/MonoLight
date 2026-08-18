const httpProxyPattern = /^http:\/\/(?:(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+:(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+@)?(?:\[[^\]\s/@?#]+\]|[^:\/\s@?#]+):(\d+)\/?$/

const normalizeHttpProxy = (value) => typeof value === 'string' ? value.trim() : ''

const isValidHttpProxy = (value) => {
  const proxy = typeof value === 'string' ? value : ''
  if (!proxy.trim()) return true

  const match = proxy.match(httpProxyPattern)
  if (/\s/.test(proxy) || !match) return false

  try {
    const url = new URL(proxy)
    const port = Number(match[1])
    return url.protocol === 'http:' && Boolean(url.hostname) &&
      Number.isInteger(port) && port >= 1 && port <= 65535 &&
      Boolean(url.username) === Boolean(url.password) && url.pathname === '/' &&
      !url.search && !url.hash
  } catch {
    return false
  }
}

export { normalizeHttpProxy, isValidHttpProxy }
