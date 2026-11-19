import assert from 'node:assert/strict'
import test from 'node:test'

import { createFixture } from './apiHarness.js'
import {
  BACKEND_UNAVAILABLE_PATH,
  createSetupGuard,
  HOME_PATH,
  LOGIN_PATH
} from '../../../src/router/setupGuard.js'

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
