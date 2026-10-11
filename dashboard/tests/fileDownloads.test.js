import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createFileDownloadCache,
  resolveAuthenticatedDownloadUrl
} from '../src/utils/fileDownloads.js'

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createCacheHarness = ({ fetchBlob, createObjectUrl } = {}) => {
  const fetchCalls = []
  const createdUrls = []
  const revokedUrls = []
  let objectUrlNumber = 0

  const cache = createFileDownloadCache({
    fetchBlob: url => {
      fetchCalls.push(url)
      return fetchBlob ? fetchBlob(url) : { url }
    },
    createObjectUrl: async blob => {
      const objectUrl = createObjectUrl
        ? await createObjectUrl(blob)
        : `blob:${blob.url}:${++objectUrlNumber}`
      if (objectUrl) createdUrls.push(objectUrl)
      return objectUrl
    },
    revokeObjectUrl: objectUrl => revokedUrls.push(objectUrl)
  })

  return { cache, fetchCalls, createdUrls, revokedUrls }
}

test('resolves exact same-origin download endpoints and preserves query parameters', () => {
  const pageOrigin = 'https://dashboard.example.test'
  const apiBaseUrl = 'https://dashboard.example.test/api/v1/'
  const cases = [
    {
      url: 'https://dashboard.example.test/api/v1/download?token=download-token&path=reports%2Fdaily&session_id=session-1&lang=zh-CN',
      expected: 'https://dashboard.example.test/api/v1/download?token=download-token&path=reports%2Fdaily&session_id=session-1&lang=zh-CN'
    },
    {
      url: '/api/v1/download-sent?token=sent-token&path=reports%2Fdaily&session_id=session-2&lang=en-US',
      expected: 'https://dashboard.example.test/api/v1/download-sent?token=sent-token&path=reports%2Fdaily&session_id=session-2&lang=en-US'
    }
  ]

  for (const { url, expected } of cases) {
    assert.equal(resolveAuthenticatedDownloadUrl(url, apiBaseUrl, pageOrigin), expected)
  }
})

test('rejects other origins, protocols, URL credentials, fragments, endpoints, and encoded paths', () => {
  const pageOrigin = 'https://dashboard.example.test'
  const apiBaseUrl = 'https://dashboard.example.test/api/v1'
  const cases = [
    {
      name: 'other origin',
      url: 'https://evil.example.test/api/v1/download'
    },
    {
      name: 'protocol-relative external origin',
      url: '//evil.example.test/api/v1/download'
    },
    {
      name: 'different protocol',
      url: 'http://dashboard.example.test/api/v1/download'
    },
    {
      name: 'unsupported protocol',
      url: 'ftp://dashboard.example.test/api/v1/download'
    },
    {
      name: 'target userinfo',
      url: 'https://user:password@dashboard.example.test/api/v1/download'
    },
    {
      name: 'API userinfo',
      url: '/api/v1/download',
      apiBaseUrl: 'https://user:password@dashboard.example.test/api/v1'
    },
    {
      name: 'target hash',
      url: '/api/v1/download#fragment'
    },
    {
      name: 'API hash',
      url: '/api/v1/download',
      apiBaseUrl: 'https://dashboard.example.test/api/v1#fragment'
    },
    {
      name: 'other endpoint',
      url: '/api/v1/downloads'
    },
    {
      name: 'nested endpoint',
      url: '/api/v1/download/extra'
    },
    {
      name: 'encoded endpoint name',
      url: '/api/v1/%64ownload'
    },
    {
      name: 'encoded path traversal',
      url: '/api/v1/download%2F..%2Fadmin'
    }
  ]

  for (const { name, url, apiBaseUrl: currentApiBaseUrl = apiBaseUrl } of cases) {
    assert.throws(
      () => resolveAuthenticatedDownloadUrl(url, currentApiBaseUrl, pageOrigin),
      TypeError,
      name
    )
  }
})

test('accepts an API origin independent from the page origin', () => {
  const pageOrigin = 'https://dashboard.example.test/app'
  const apiBaseUrl = 'https://api.example.test/backend///'
  const url = 'https://api.example.test/backend/download-sent?token=api-token&lang=en'

  assert.equal(resolveAuthenticatedDownloadUrl(url, apiBaseUrl, pageOrigin), url)
})

test('does not append a login token while resolving a valid URL', () => {
  const pageOrigin = 'https://dashboard.example.test'
  const target = '/api/v1/download?path=reports%2Fdaily&session_id=session-3&lang=zh-CN'

  assert.equal(
    resolveAuthenticatedDownloadUrl(target, '/api/v1', pageOrigin),
    'https://dashboard.example.test/api/v1/download?path=reports%2Fdaily&session_id=session-3&lang=zh-CN'
  )
})

test('shares concurrent loads for one URL and caches the successful object URL', async () => {
  const response = createDeferred()
  const url = 'https://dashboard.example.test/api/v1/download?token=one'
  const { cache, fetchCalls, createdUrls } = createCacheHarness({
    fetchBlob: () => response.promise,
    createObjectUrl: blob => `object:${blob.value}`
  })

  const first = cache.load(url)
  const second = cache.load(url)
  assert.equal(fetchCalls.length, 1)

  response.resolve({ value: 'file-one' })

  assert.deepEqual(await Promise.all([first, second]), ['object:file-one', 'object:file-one'])
  assert.equal(cache.get(url), 'object:file-one')
  assert.deepEqual(createdUrls, ['object:file-one'])
  assert.equal(await cache.load(url), 'object:file-one')
  assert.equal(fetchCalls.length, 1)
})

test('removes a failed load so the next load can retry successfully', async () => {
  const firstResponse = createDeferred()
  const failure = new Error('download failed')
  let attempt = 0
  const url = 'https://dashboard.example.test/api/v1/download?token=retry'
  const { cache, fetchCalls } = createCacheHarness({
    fetchBlob: () => {
      attempt += 1
      return attempt === 1 ? firstResponse.promise : { value: 'recovered' }
    },
    createObjectUrl: blob => `object:${blob.value}`
  })

  const failed = cache.load(url)
  firstResponse.reject(failure)
  await assert.rejects(failed, error => error === failure)

  assert.equal(await cache.load(url), 'object:recovered')
  assert.equal(cache.get(url), 'object:recovered')
  assert.equal(fetchCalls.length, 2)
})

test('loads different URLs independently', async () => {
  const firstResponse = createDeferred()
  const secondResponse = createDeferred()
  const firstUrl = 'https://dashboard.example.test/api/v1/download?token=first'
  const secondUrl = 'https://dashboard.example.test/api/v1/download?token=second'
  const responses = new Map([
    [firstUrl, firstResponse],
    [secondUrl, secondResponse]
  ])
  const { cache, fetchCalls } = createCacheHarness({
    fetchBlob: url => responses.get(url).promise,
    createObjectUrl: blob => `object:${blob.value}`
  })

  const first = cache.load(firstUrl)
  const second = cache.load(secondUrl)
  assert.equal(fetchCalls.length, 2)

  firstResponse.resolve({ value: 'first' })
  assert.equal(await first, 'object:first')
  assert.equal(cache.get(firstUrl), 'object:first')
  assert.equal(cache.get(secondUrl), '')

  secondResponse.resolve({ value: 'second' })
  assert.equal(await second, 'object:second')
  assert.equal(cache.get(secondUrl), 'object:second')
})

test('clear invalidates a pending load before its late response can create or restore a URL', async () => {
  const response = createDeferred()
  const url = 'https://dashboard.example.test/api/v1/download?token=clear'
  const { cache, createdUrls } = createCacheHarness({
    fetchBlob: () => response.promise,
    createObjectUrl: blob => `object:${blob.value}`
  })

  const pending = cache.load(url)
  cache.clear()
  response.resolve({ value: 'late' })

  assert.equal(await pending, null)
  assert.equal(cache.get(url), '')
  assert.deepEqual(createdUrls, [])
})

test('clear revokes ready entries and allows a fresh load for the same URL', async () => {
  let requestNumber = 0
  const url = 'https://dashboard.example.test/api/v1/download?token=refresh'
  const { cache, fetchCalls, revokedUrls } = createCacheHarness({
    fetchBlob: () => {
      requestNumber += 1
      return { value: `version-${requestNumber}` }
    },
    createObjectUrl: blob => `object:${blob.value}`
  })

  assert.equal(await cache.load(url), 'object:version-1')
  cache.clear()
  assert.equal(cache.get(url), '')
  assert.deepEqual(revokedUrls, ['object:version-1'])

  assert.equal(await cache.load(url), 'object:version-2')
  assert.equal(cache.get(url), 'object:version-2')
  assert.deepEqual(revokedUrls, ['object:version-1'])
  assert.equal(fetchCalls.length, 2)
})

test('prune retains selected entries and revokes removed ready entries', async () => {
  const retainedUrl = 'https://dashboard.example.test/api/v1/download?token=retain'
  const removedUrl = 'https://dashboard.example.test/api/v1/download?token=remove'
  const { cache, fetchCalls, revokedUrls } = createCacheHarness({
    createObjectUrl: blob => `object:${blob.url}`
  })

  assert.equal(await cache.load(retainedUrl), `object:${retainedUrl}`)
  assert.equal(await cache.load(removedUrl), `object:${removedUrl}`)
  cache.prune([retainedUrl])

  assert.equal(cache.get(retainedUrl), `object:${retainedUrl}`)
  assert.equal(cache.get(removedUrl), '')
  assert.deepEqual(revokedUrls, [`object:${removedUrl}`])
  assert.equal(await cache.load(retainedUrl), `object:${retainedUrl}`)
  assert.equal(fetchCalls.length, 2)
})

test('prune invalidates an old pending load before a new same-URL request', async () => {
  const oldResponse = createDeferred()
  const newResponse = createDeferred()
  let requestNumber = 0
  const url = 'https://dashboard.example.test/api/v1/download?token=prune'
  const { cache, createdUrls } = createCacheHarness({
    fetchBlob: () => {
      requestNumber += 1
      return requestNumber === 1 ? oldResponse.promise : newResponse.promise
    },
    createObjectUrl: blob => `object:${blob.value}`
  })

  const stale = cache.load(url)
  cache.prune([])
  const current = cache.load(url)

  oldResponse.resolve({ value: 'old' })
  assert.equal(await stale, null)
  assert.deepEqual(createdUrls, [])

  newResponse.resolve({ value: 'new' })
  assert.equal(await current, 'object:new')
  assert.equal(cache.get(url), 'object:new')
  assert.deepEqual(createdUrls, ['object:new'])
})

test('dispose is idempotent, revokes ready entries, and blocks future loads', async () => {
  const url = 'https://dashboard.example.test/api/v1/download?token=dispose'
  const { cache, fetchCalls, revokedUrls } = createCacheHarness()

  assert.equal(await cache.load(url), `blob:${url}:1`)
  assert.equal(await cache.dispose(), null)
  assert.equal(await cache.dispose(), null)

  assert.equal(cache.get(url), '')
  assert.deepEqual(revokedUrls, [`blob:${url}:1`])
  assert.equal(await cache.load(url), null)
  assert.equal(fetchCalls.length, 1)
})

test('dispose prevents a pending response from creating or restoring a URL', async () => {
  const response = createDeferred()
  const url = 'https://dashboard.example.test/api/v1/download?token=late-dispose'
  const { cache, createdUrls } = createCacheHarness({
    fetchBlob: () => response.promise,
    createObjectUrl: blob => `object:${blob.value}`
  })

  const pending = cache.load(url)
  await cache.dispose()
  response.resolve({ value: 'late' })

  assert.equal(await pending, null)
  assert.equal(await cache.load(url), null)
  assert.equal(cache.get(url), '')
  assert.deepEqual(createdUrls, [])
})

test('empty load inputs resolve null without fetching', async () => {
  const { cache, fetchCalls } = createCacheHarness()

  assert.equal(await cache.load(''), null)
  assert.equal(await cache.load(null), null)
  assert.equal(await cache.load(undefined), null)
  assert.equal(cache.get(''), '')
  assert.deepEqual(fetchCalls, [])
})
