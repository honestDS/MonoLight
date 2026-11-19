import assert from 'node:assert/strict'
import { Blob as NodeBlob } from 'node:buffer'
import test from 'node:test'

import { createFixture } from '../system/apiHarness.js'

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

test('returns null only for unavailable download 404 Blobs without changing auth or redirect state', async () => {
  const errors = [
    {
      status: 404,
      body: { data: { reason: 'file_unavailable' }, message: 'file unavailable' },
      errorMessage: 'download unavailable'
    },
    {
      status: 404,
      body: { data: {}, message: 'download file missing' },
      errorMessage: 'download file missing'
    },
    {
      status: 403,
      body: { data: { reason: 'file_unavailable' }, message: 'download forbidden' },
      errorMessage: 'download forbidden'
    }
  ].map(({ status, body, errorMessage }) => {
    const error = new Error(errorMessage)
    error.response = {
      status,
      data: new NodeBlob([JSON.stringify(body)], { type: 'application/json' })
    }
    return error
  })
  const [, missingReasonError, forbiddenError] = errors
  const fixture = createFixture({
    respond: async () => { throw errors.shift() },
    trackHash: true
  })
  const beforeSnapshot = fixture.setupStatusState.getSnapshot()
  const downloadUrl = '/api/v1/download-sent?token=unavailable'

  assert.equal(await fixture.fileApi.fetchDownloadBlob(downloadUrl), null)
  assert.equal(fixture.storage.getItem('token'), 'current-token')
  assert.deepEqual(fixture.storage.removedKeys, [])
  assert.equal(fixture.location.hash, '#/chat')
  assert.deepEqual(fixture.hashAssignments, [])
  assert.strictEqual(fixture.setupStatusState.getSnapshot(), beforeSnapshot)

  await assert.rejects(fixture.fileApi.fetchDownloadBlob(downloadUrl), error => {
    assert.strictEqual(error, missingReasonError)
    assert.equal(error.message, 'download file missing')
    assert.equal(error.response.status, 404)
    assert.equal(error.response.data.message, 'download file missing')
    assert.equal(Object.hasOwn(error.response.data.data, 'reason'), false)
    return true
  })
  await assert.rejects(fixture.fileApi.fetchDownloadBlob(downloadUrl), error => {
    assert.strictEqual(error, forbiddenError)
    assert.equal(error.message, 'download forbidden')
    assert.equal(error.response.status, 403)
    assert.equal(error.response.data.message, 'download forbidden')
    assert.equal(error.response.data.data.reason, 'file_unavailable')
    return true
  })

  assert.equal(fixture.storage.getItem('token'), 'current-token')
  assert.equal(fixture.location.hash, '#/chat')
  assert.deepEqual(fixture.hashAssignments, [])
  assert.strictEqual(fixture.setupStatusState.getSnapshot(), beforeSnapshot)
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
