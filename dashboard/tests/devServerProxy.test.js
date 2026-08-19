import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import test from 'node:test'

const require = createRequire(import.meta.url)
const { resolveBackendTarget } = require('../devServerProxy.cjs')

test('uses the default local backend target', () => {
  assert.equal(resolveBackendTarget({}), 'http://127.0.0.1:8000')
})

test('normalizes the wildcard host and trims configured ports', () => {
  assert.equal(
    resolveBackendTarget({ APP_HOST: '0.0.0.0', APP_PORT: ' 9123 ' }),
    'http://127.0.0.1:9123'
  )
})

test('uses custom IPv4 hosts and brackets bare IPv6 hosts', () => {
  assert.equal(
    resolveBackendTarget({ APP_HOST: '192.0.2.10', APP_PORT: '9124' }),
    'http://192.0.2.10:9124'
  )
  assert.equal(
    resolveBackendTarget({ APP_HOST: '2001:db8::1', APP_PORT: '9125' }),
    'http://[2001:db8::1]:9125'
  )
})

test('rejects invalid backend hosts', () => {
  for (const host of [
    'backend.example.test',
    'http://127.0.0.1:8000',
    '127.0.0.1:8000',
    '::',
    '0:0:0:0:0:0:0:0',
    'fe80::1%eth0',
  ]) {
    assert.throws(() => resolveBackendTarget({ APP_HOST: host }), /APP_HOST/)
  }
})

test('rejects invalid backend ports', () => {
  for (const port of ['0x1f40', '8000.0', '1e3', '0', '65536']) {
    assert.throws(
      () => resolveBackendTarget({ APP_PORT: port }),
      /APP_PORT must be a decimal integer between 1 and 65535/
    )
  }
})

test('configures the API proxy from the normalized backend target', () => {
  const configPath = require.resolve('../vue.config.js')
  const hadAppHost = Object.prototype.hasOwnProperty.call(process.env, 'APP_HOST')
  const hadAppPort = Object.prototype.hasOwnProperty.call(process.env, 'APP_PORT')
  const appHost = process.env.APP_HOST
  const appPort = process.env.APP_PORT
  let config

  process.env.APP_HOST = '0.0.0.0'
  process.env.APP_PORT = '9123'

  try {
    delete require.cache[configPath]
    config = require('../vue.config.js')
  } finally {
    if (hadAppHost) {
      process.env.APP_HOST = appHost
    } else {
      delete process.env.APP_HOST
    }

    if (hadAppPort) {
      process.env.APP_PORT = appPort
    } else {
      delete process.env.APP_PORT
    }
  }

  const apiProxy = config.devServer.proxy['/api']

  assert.equal(config.productionSourceMap, false)
  assert.equal(apiProxy.target, 'http://127.0.0.1:9123')
  assert.equal(apiProxy.changeOrigin, true)
  assert.equal(apiProxy.ws, true)
  assert.equal(apiProxy.xfwd, true)
})
