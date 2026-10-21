import assert from 'node:assert/strict'
import { Blob as NodeBlob } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

import {
  BACKEND_UNAVAILABLE_PATH,
  createSetupGuard,
  createSetupStatusState,
  HOME_PATH,
  LOGIN_PATH,
} from '../src/router/setupGuard.js'
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

const loadApi = ({ storage, location, axiosStub }) => {
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
    `${apiSource}\nglobalThis.__module = { fileApi, knowledgeBaseApi, profileApi, setupApi }`,
    context,
    { filename: 'api/index.js' }
  )

  return context.__module
}

const createFixture = ({
  respond,
  token = 'current-token',
  locale = 'zh-CN',
  trackHash = false,
  initialHash = '#/chat'
} = {}) => {
  const storage = createStorage({ token, locale })
  const hashAssignments = []
  let hash = initialHash
  const location = trackHash
    ? {
        origin: 'https://dashboard.example.test',
        get hash() { return hash },
        set hash(value) {
          hashAssignments.push(value)
          hash = value.startsWith('#') ? value : `#${value}`
        }
      }
    : {
        origin: 'https://dashboard.example.test',
        hash: initialHash
      }
  const axiosStub = createAxiosStub({ respond })
  const setupStatusState = createSetupStatusState()
  setupStatusState.setReady(false)
  const api = loadApi({ storage, location, axiosStub })

  return {
    fileApi: api.fileApi,
    knowledgeBaseApi: api.knowledgeBaseApi,
    profileApi: api.profileApi,
    setupApi: api.setupApi,
    storage,
    location,
    axiosStub,
    setupStatusState,
    hashAssignments
  }
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

test('does not redirect on a 500 response while preserving a non-JSON Blob error', async () => {
  const responseBlob = new NodeBlob(['not JSON'], { type: 'text/plain' })
  const backendError = new Error('wire unavailable')
  backendError.response = {
    status: 500,
    data: responseBlob
  }
  const fixture = createFixture({
    respond: async () => { throw backendError },
    trackHash: true
  })
  const beforeSnapshot = fixture.setupStatusState.getSnapshot()

  await assert.rejects(
    fixture.fileApi.fetchDownloadBlob('/api/v1/download?path=broken'),
    error => {
      assert.strictEqual(error, backendError)
      assert.equal(error.message, 'wire unavailable')
      assert.strictEqual(error.response.data, responseBlob)
      return true
    }
  )
  assert.equal(fixture.storage.getItem('token'), 'current-token')
  assert.equal(fixture.location.hash, '#/chat')
  assert.deepEqual(fixture.hashAssignments, [])
  assert.strictEqual(fixture.setupStatusState.getSnapshot(), beforeSnapshot)
})

test('passes knowledge-base and ordinary API 5xx failures to callers without redirecting', async () => {
  const requests = [
    ['knowledge-base', fixture => fixture.knowledgeBaseApi.managedItems(7)],
    ['profile', fixture => fixture.profileApi.list()]
  ]

  for (const status of [500, 502, 503, 504]) {
    for (const [name, request] of requests) {
      const message = `backend failure ${status}`
      const error = new Error('request failed')
      error.request = {}
      error.response = {
        status,
        data: status === 500
          ? { code: status, detail: message }
          : { code: status, message }
      }
      const fixture = createFixture({
        respond: async () => { throw error },
        trackHash: true
      })
      const beforeSnapshot = fixture.setupStatusState.getSnapshot()

      await assert.rejects(request(fixture), caught => {
        assert.strictEqual(caught, error, name)
        assert.equal(caught.message, message)
        assert.equal(caught.response.status, status)
        return true
      })

      assert.equal(fixture.location.hash, '#/chat')
      assert.deepEqual(fixture.hashAssignments, [])
      assert.strictEqual(fixture.setupStatusState.getSnapshot(), beforeSnapshot)
      assert.equal(fixture.storage.getItem('token'), 'current-token')
    }
  }
})

test('passes network and timeout errors to callers without redirecting', async () => {
  for (const code of ['ERR_NETWORK', 'ECONNABORTED', 'ETIMEDOUT']) {
    const error = new Error(`${code} failure`)
    error.code = code
    error.request = {}
    const fixture = createFixture({
      respond: async () => { throw error },
      trackHash: true
    })
    const beforeSnapshot = fixture.setupStatusState.getSnapshot()

    await assert.rejects(fixture.profileApi.list(), caught => {
      assert.strictEqual(caught, error)
      assert.equal(caught.message, `${code} failure`)
      assert.equal(caught.code, code)
      assert.strictEqual(caught.request, error.request)
      return true
    })

    assert.equal(fixture.location.hash, '#/chat')
    assert.deepEqual(fixture.hashAssignments, [])
    assert.strictEqual(fixture.setupStatusState.getSnapshot(), beforeSnapshot)
    assert.equal(fixture.storage.getItem('token'), 'current-token')
  }
})

test('does not redirect for cancellation, requestless errors, or client failures', async () => {
  const unaffectedErrors = [
    Object.assign(new Error('request canceled'), { code: 'ERR_CANCELED', request: {} }),
    new Error('local configuration failure')
  ]

  for (const error of unaffectedErrors) {
    const fixture = createFixture({
      respond: async () => { throw error },
      trackHash: true
    })

    await assert.rejects(fixture.knowledgeBaseApi.managedItems(7), caught => {
      assert.strictEqual(caught, error)
      return true
    })

    assert.equal(fixture.location.hash, '#/chat')
    assert.deepEqual(fixture.hashAssignments, [])
    assert.equal(fixture.setupStatusState.getSnapshot().phase, 'ready')
    assert.equal(fixture.storage.getItem('token'), 'current-token')
  }

  for (const status of [400, 403, 404, 409, 422, 429]) {
    const error = new Error('request failed')
    error.response = { status, data: { message: `client failure ${status}` } }
    const fixture = createFixture({
      respond: async () => { throw error },
      trackHash: true
    })

    await assert.rejects(fixture.profileApi.list(), caught => {
      assert.strictEqual(caught, error)
      assert.equal(caught.message, `client failure ${status}`)
      return true
    })

    assert.equal(fixture.location.hash, '#/chat')
    assert.deepEqual(fixture.hashAssignments, [])
    assert.equal(fixture.setupStatusState.getSnapshot().phase, 'ready')
    assert.equal(fixture.storage.getItem('token'), 'current-token')
  }
})

test('does not redirect for HTTP 200 business errors', async () => {
  const fixture = createFixture({
    respond: async () => ({ data: { code: 422, message: 'invalid business request' } }),
    trackHash: true
  })

  await assert.rejects(fixture.profileApi.list(), error => {
    assert.equal(error.message, 'invalid business request')
    return true
  })

  assert.equal(fixture.location.hash, '#/chat')
  assert.deepEqual(fixture.hashAssignments, [])
  assert.equal(fixture.setupStatusState.getSnapshot().phase, 'ready')
  assert.equal(fixture.storage.getItem('token'), 'current-token')
})

test('routes setup status failures to the unavailable page and resumes after recovery', async () => {
  const ordinaryError = new Error('managed items unavailable')
  ordinaryError.code = 'ERR_NETWORK'
  ordinaryError.request = {}
  const setupError = new Error('backend unavailable')
  setupError.code = 'ERR_NETWORK'
  setupError.request = {}
  let setupStatusCalls = 0
  const fixture = createFixture({
    respond: async requestConfig => {
      if (requestConfig.url === '/knowledge-base/managed-items/list') {
        throw ordinaryError
      }
      if (requestConfig.url === '/setup/status') {
        setupStatusCalls += 1
        if (setupStatusCalls === 1) throw setupError
        return { data: { data: { required: false } } }
      }
      return { data: null }
    },
    trackHash: true
  })
  const beforeSnapshot = fixture.setupStatusState.getSnapshot()

  await assert.rejects(fixture.knowledgeBaseApi.managedItems(7), error => {
    assert.strictEqual(error, ordinaryError)
    return true
  })
  assert.equal(fixture.location.hash, '#/chat')
  assert.deepEqual(fixture.hashAssignments, [])
  assert.strictEqual(fixture.setupStatusState.getSnapshot(), beforeSnapshot)
  assert.equal(fixture.storage.getItem('token'), 'current-token')

  const guard = createSetupGuard({
    statusRequest: () => fixture.setupApi.status(),
    getToken: () => fixture.storage.getItem('token'),
    state: fixture.setupStatusState
  })
  assert.equal(await guard('/profiles'), BACKEND_UNAVAILABLE_PATH)
  assert.equal(fixture.setupStatusState.getSnapshot().phase, 'error')
  assert.equal(fixture.setupStatusState.getSnapshot().error.message, 'backend unavailable')
  assert.equal(await guard(BACKEND_UNAVAILABLE_PATH), undefined)
  assert.equal(setupStatusCalls, 1)

  assert.equal(await guard('/profiles'), undefined)
  assert.deepEqual(fixture.setupStatusState.getSnapshot(), {
    phase: 'ready',
    required: false,
    error: null
  })
  assert.equal(await guard(BACKEND_UNAVAILABLE_PATH), HOME_PATH)

  const signedOutFixture = createFixture({
    token: null,
    respond: async () => ({ data: { data: { required: false } } })
  })
  const signedOutGuard = createSetupGuard({
    statusRequest: async () => ({ data: { data: { required: false } } }),
    getToken: () => signedOutFixture.storage.getItem('token'),
    state: signedOutFixture.setupStatusState
  })
  assert.equal(await signedOutGuard(BACKEND_UNAVAILABLE_PATH), LOGIN_PATH)
})

test('concurrent ordinary network failures do not redirect or clear the token', async () => {
  const error = new Error('backend unavailable')
  error.code = 'ERR_NETWORK'
  error.request = {}
  const fixture = createFixture({
    respond: async () => { throw error },
    trackHash: true
  })
  const beforeSnapshot = fixture.setupStatusState.getSnapshot()

  await Promise.all([
    assert.rejects(fixture.knowledgeBaseApi.managedItems(7), caught => caught === error),
    assert.rejects(fixture.profileApi.list(), caught => caught === error)
  ])

  assert.equal(fixture.location.hash, '#/chat')
  assert.deepEqual(fixture.hashAssignments, [])
  assert.equal(fixture.storage.getItem('token'), 'current-token')
  assert.strictEqual(fixture.setupStatusState.getSnapshot(), beforeSnapshot)
})

test('setup status HTTP failures are routed to the unavailable page by the setup guard', async () => {
  for (const status of [500, 503]) {
    const message = `setup status failed ${status}`
    const fixture = createFixture({
      respond: async () => {
        const error = new Error('request failed')
        error.request = {}
        error.response = { status, data: { message } }
        throw error
      },
      trackHash: true
    })
    const guard = createSetupGuard({
      statusRequest: () => fixture.setupApi.status(),
      getToken: () => fixture.storage.getItem('token'),
      state: fixture.setupStatusState
    })

    assert.equal(await guard('/profiles'), BACKEND_UNAVAILABLE_PATH)
    assert.equal(fixture.setupStatusState.getSnapshot().phase, 'error')
    assert.equal(fixture.setupStatusState.getSnapshot().error.message, message)
    assert.equal(fixture.location.hash, '#/chat')
    assert.equal(fixture.storage.getItem('token'), 'current-token')
    assert.equal(await guard(BACKEND_UNAVAILABLE_PATH), undefined)
    assert.equal(fixture.axiosStub.calls.length, 1)
  }
})
