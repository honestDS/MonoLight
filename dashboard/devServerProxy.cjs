const dotenv = require('dotenv')
const { isIP } = require('node:net')

function loadEnvironment(envPath) {
  const result = dotenv.config({ path: envPath })

  if (result.error && result.error.code !== 'ENOENT') {
    throw result.error
  }

  return result
}

function resolveBackendTarget(environment = process.env) {
  const configuredHost = typeof environment.APP_HOST === 'string' ? environment.APP_HOST.trim() : environment.APP_HOST
  const host = configuredHost === undefined || configuredHost === '' ? '0.0.0.0' : configuredHost
  const hostVersion = typeof host === 'string' ? isIP(host) : 0

  if (hostVersion === 0) {
    throw new Error('APP_HOST must be an IPv4 or IPv6 literal')
  }

  if (hostVersion === 6 && host.includes('%')) {
    throw new Error('APP_HOST must not include an IPv6 scope identifier')
  }

  if (hostVersion === 6 && new URL(`http://[${host}]`).hostname === '[::]') {
    throw new Error('APP_HOST must not be an unspecified IPv6 address')
  }

  const targetHost = host === '0.0.0.0' ? '127.0.0.1' : host
  const formattedHost = hostVersion === 6 ? `[${targetHost}]` : targetHost
  const configuredPort = typeof environment.APP_PORT === 'string' ? environment.APP_PORT.trim() : environment.APP_PORT
  const port = configuredPort === undefined || configuredPort === '' ? '8000' : configuredPort

  if (typeof port !== 'string' || !/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
    throw new Error('APP_PORT must be a decimal integer between 1 and 65535')
  }

  return `http://${formattedHost}:${port}`
}

module.exports = {
  loadEnvironment,
  resolveBackendTarget,
}
