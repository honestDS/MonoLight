import assert from 'node:assert/strict'
import test from 'node:test'
import { loadSessionLoadingModule } from '../../support/chat/sessionListHarness.js'

test('HTTP work recovery fetches a terminal result after observing the work in progress and reuses persisted failures', async () => {
  const { hasHttpResultMessage, shouldFetchHttpWorkStatus } = await loadSessionLoadingModule()

  assert.equal(shouldFetchHttpWorkStatus({
    status: 'running',
    previousStatus: undefined,
    hasPendingRequest: false,
    historyLoaded: true,
    sessionLoading: true,
    resolved: false,
    fetching: false
  }), false)

  assert.equal(shouldFetchHttpWorkStatus({
    status: 'failed',
    previousStatus: 'running',
    hasPendingRequest: false,
    historyLoaded: true,
    sessionLoading: false,
    resolved: false,
    fetching: false
  }), true)

  const history = [
    { role: 'user', db_id: 40, content: 'question' },
    { role: 'err', db_id: 41, content: 'worker failed' }
  ]
  assert.equal(hasHttpResultMessage(history, 41), true)
  assert.equal(hasHttpResultMessage(history, '41'), true)
  assert.equal(hasHttpResultMessage(history, 42), false)
})

test('shouldFetchHttpWorkStatus uses the observed work state to decide whether to fetch', async () => {
  const { shouldFetchHttpWorkStatus } = await loadSessionLoadingModule()
  const cases = [
    {
      name: 'does not fetch an initially completed old work',
      input: {
        status: 'succeeded',
        previousStatus: undefined,
        hasPendingRequest: false,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'does not fetch before history is loaded',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: false,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'fetches a pending request that succeeded',
      input: {
        status: 'succeeded',
        previousStatus: undefined,
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'fetches a pending request that failed',
      input: {
        status: 'failed',
        previousStatus: undefined,
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'fetches an observed active work that succeeded',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: false,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'does not fetch merged work while the session is loading',
      input: {
        status: 'merged',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: true,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'fetches merged work after the session becomes idle',
      input: {
        status: 'merged',
        previousStatus: 'running',
        hasPendingRequest: false,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: true
    },
    {
      name: 'does not fetch an already resolved work',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: true,
        fetching: false
      },
      expected: false
    },
    {
      name: 'does not fetch work that is already being fetched',
      input: {
        status: 'succeeded',
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: true
      },
      expected: false
    },
    {
      name: 'does not fetch a non-terminal work',
      input: {
        status: 'running',
        previousStatus: undefined,
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    },
    {
      name: 'does not fetch a malformed status',
      input: {
        status: null,
        previousStatus: 'running',
        hasPendingRequest: true,
        historyLoaded: true,
        sessionLoading: false,
        resolved: false,
        fetching: false
      },
      expected: false
    }
  ]

  for (const { name, input, expected } of cases) {
    assert.equal(shouldFetchHttpWorkStatus(input), expected, name)
  }
})
