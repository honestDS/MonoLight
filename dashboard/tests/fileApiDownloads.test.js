import assert from 'node:assert/strict'
import { Blob as NodeBlob } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

import { truncateErrorMessage } from '../src/utils/errorMessage.js'
import { resolveAuthenticatedDownloadUrl } from '../src/utils/fileDownloads.js'
import {
  resolveApiBaseUrl,
  resolveWebSocketBaseUrl
} from '../src/utils/runtimeEndpoints.js'

const stripImports = source => {
  const keptLines = []
  let inImport = false

  for (const line of source.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const apiSource = stripImports(readFileSync(
  new URL('../src/api/index.js', import.meta.url),
  'utf8'
))
  .replace(/\bexport\s+const\b/g, 'const')
  .replace(/\bexport\s+default\s+request\b/g, '')

const createStorage = entries => {
  const values = new Map(Object.entries(entries))
  const removedKeys = []

  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => {
      removedKeys.push(key)
      values.delete(key)
    },
    removedKeys
  }
}

const createAxiosStub = ({ respond = async () => ({ data: new NodeBlob(['download']) }) } = {}) => {
  const requestInterceptors = []
  const responseInterceptors = []
  const calls = []
  const createInterceptorManager = handlers => ({
    use: (onFulfilled, onRejected) => {
      handlers.push({ onFulfilled, onRejected })
      return handlers.length - 1
    }
  })

  const request = {
    interceptors: {
      request: createInterceptorManager(requestInterceptors),
      response: createInterceptorManager(responseInterceptors)
    },
    get: (url, config = {}) => {
      let chain = Promise.resolve({
        ...config,
        url,
        method: 'get',
        headers: { ...(config.headers || {}) }
      })

      for (const { onFulfilled, onRejected } of requestInterceptors) {
        chain = chain.then(onFulfilled, onRejected)
      }

      chain = chain.then(async requestConfig => {
        calls.push(requestConfig)
        return respond(requestConfig)
      })

      for (const { onFulfilled, onRejected } of responseInterceptors) {
        chain = chain.then(onFulfilled, onRejected)
      }

      return chain
    },
    post: async () => ({ data: null })
  }

  return {
    axios: {
      create: () => request,
      get: async () => ({ data: null })
    },
    calls,
    requestInterceptors,
    responseInterceptors
  }
}

const loadFileApi = ({ storage, location, axiosStub }) => {
  const context = vm.createContext({
    axios: axiosStub.axios,
    Blob: NodeBlob,
    FormData: class FormDataStub {},
    URL,
    i18n: {
      global: {
        t: key => ({
          'common.network_request_failed': 'network request failed',
          'common.unknown_error': 'unknown error'
        }[key] || key)
      }
    },
    localStorage: storage,
    process: {
      env: {
        VUE_APP_API_BASE_URL: '/api/v1',
        VUE_APP_WS_BASE_URL: ''
      }
    },
    window: { location },
    resolveAuthenticatedDownloadUrl,
    resolveApiBaseUrl,
    resolveWebSocketBaseUrl,
    truncateErrorMessage
  })

  vm.runInContext(
    `${apiSource}\nglobalThis.__module = { fileApi }`,
    context,
    { filename: 'api/index.js' }
  )

  return context.__module.fileApi
}

const createFixture = ({ respond, token = 'current-token', locale = 'zh-CN' } = {}) => {
  const storage = createStorage({ token, locale })
  const location = {
    origin: 'https://dashboard.example.test',
    hash: '#/chat'
  }
  const axiosStub = createAxiosStub({ respond })
  const fileApi = loadFileApi({ storage, location, axiosStub })

  return { fileApi, storage, location, axiosStub }
}

test('encodes download parameters and authenticates both same-origin download endpoints', async () => {
  const fixture = createFixture({
    respond: async requestConfig => ({
      data: new NodeBlob([`payload:${requestConfig.url}`], { type: 'application/octet-stream' })
    })
  })
  const path = 'reports/日报?.txt'
  const sessionId = 'session id/2'
  const downloadUrl = fixture.fileApi.getDownloadUrl(path, sessionId)
  const parsedUrl = new URL(downloadUrl)

  assert.equal(parsedUrl.origin, fixture.location.origin)
  assert.equal(parsedUrl.pathname, '/api/v1/download')
  assert.equal(parsedUrl.searchParams.get('path'), path)
  assert.equal(parsedUrl.searchParams.get('session_id'), sessionId)
  assert.equal(parsedUrl.searchParams.get('lang'), 'zh-CN')
  assert.equal(parsedUrl.searchParams.has('token'), false)

  const downloadBlob = await fixture.fileApi.fetchDownloadBlob(downloadUrl)
  const sentBlob = await fixture.fileApi.fetchDownloadBlob(
    '/api/v1/download-sent?path=reports%2Fsent&lang=zh-CN'
  )

  assert.ok(downloadBlob instanceof NodeBlob)
  assert.ok(sentBlob instanceof NodeBlob)
  assert.equal(fixture.axiosStub.requestInterceptors.length, 1)
  assert.equal(fixture.axiosStub.responseInterceptors.length, 1)
  assert.deepEqual(
    fixture.axiosStub.calls.map(requestConfig => ({
      url: requestConfig.url,
      responseType: requestConfig.responseType,
      authorization: requestConfig.headers.Authorization,
      language: requestConfig.headers['Accept-Language']
    })),
    [
      {
        url: downloadUrl,
        responseType: 'blob',
        authorization: 'Bearer current-token',
        language: 'zh-CN'
      },
      {
        url: 'https://dashboard.example.test/api/v1/download-sent?path=reports%2Fsent&lang=zh-CN',
        responseType: 'blob',
        authorization: 'Bearer current-token',
        language: 'zh-CN'
      }
    ]
  )
})

test('rejects external origins, other interfaces, and unsupported protocols before sending credentials', async () => {
  const fixture = createFixture()
  const invalidUrls = [
    'https://evil.example.test/api/v1/download?path=secret',
    '/api/v1/not-download?path=secret',
    'ftp://dashboard.example.test/api/v1/download?path=secret'
  ]

  for (const url of invalidUrls) {
    await assert.rejects(
      fixture.fileApi.fetchDownloadBlob(url),
      error => error.message === 'network request failed'
    )
  }

  assert.equal(fixture.axiosStub.calls.length, 0)
  assert.equal(fixture.storage.getItem('token'), 'current-token')
})

test('parses Blob JSON message and detail errors, clears 401 tokens, and redirects to login', async () => {
  const responses = [
    { status: 400, body: { message: 'download message error' }, errorMessage: 'request failed' },
    { status: 400, body: { detail: 'download detail error' }, errorMessage: 'request failed' },
    { status: 401, body: { detail: 'session expired' }, errorMessage: 'unauthorized' }
  ]
  const fixture = createFixture({
    respond: async () => {
      const response = responses.shift()
      const error = new Error(response.errorMessage)
      error.response = {
        status: response.status,
        data: new NodeBlob([JSON.stringify(response.body)], { type: 'application/json' })
      }
      throw error
    }
  })
  const downloadUrl = 'https://dashboard.example.test/api/v1/download?path=error'

  await assert.rejects(fixture.fileApi.fetchDownloadBlob(downloadUrl), error => {
    assert.equal(error.message, 'download message error')
    assert.equal(error.response.data.message, 'download message error')
    return true
  })
  await assert.rejects(fixture.fileApi.fetchDownloadBlob(downloadUrl), error => {
    assert.equal(error.message, 'download detail error')
    assert.equal(error.response.data.detail, 'download detail error')
    return true
  })
  await assert.rejects(fixture.fileApi.fetchDownloadBlob(downloadUrl), error => {
    assert.equal(error.message, 'session expired')
    assert.equal(error.response.data.detail, 'session expired')
    return true
  })

  assert.equal(fixture.storage.getItem('token'), null)
  assert.deepEqual(fixture.storage.removedKeys, ['token'])
  assert.equal(fixture.location.hash, '/login')
})

test('falls back to the transport error when a Blob response is not JSON', async () => {
  const fixture = createFixture({
    respond: async () => {
      const error = new Error('wire unavailable')
      error.response = {
        status: 500,
        data: new NodeBlob(['not JSON'], { type: 'text/plain' })
      }
      throw error
    }
  })

  await assert.rejects(
    fixture.fileApi.fetchDownloadBlob('/api/v1/download?path=broken'),
    error => {
      assert.equal(error.message, 'wire unavailable')
      assert.ok(error.response.data instanceof NodeBlob)
      return true
    }
  )
  assert.equal(fixture.storage.getItem('token'), 'current-token')
  assert.equal(fixture.location.hash, '#/chat')
})
