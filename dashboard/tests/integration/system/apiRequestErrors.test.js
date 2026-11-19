import assert from 'node:assert/strict'
import test from 'node:test'

import { createFixture } from './apiHarness.js'

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
