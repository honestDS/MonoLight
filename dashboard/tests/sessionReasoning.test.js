import assert from 'node:assert/strict'
import test from 'node:test'
import { computed, effectScope, ref } from 'vue'
import { useSessionReasoning } from '../src/composables/chat/useSessionReasoning.js'

const flushMicrotasks = async () => {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve()
  }
}

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const optionResponse = (options = [], defaults = []) => ({
  data: { data: { options, defaults } }
})

const hasSessionId = value => value !== null && value !== undefined && value !== ''

const createHarness = (t, {
  currentSessionId: initialSessionId = null,
  profileOverrideId: initialProfileOverrideId = null,
  sessions: initialSessions = [],
  draftReasoningEffort: initialDraftReasoningEffort = null,
  optionsBehavior = async () => optionResponse(),
  saveBehavior = async () => undefined
} = {}) => {
  const currentSessionId = ref(initialSessionId)
  const profileOverrideId = ref(initialProfileOverrideId)
  const sessions = ref(initialSessions)
  const loading = ref(false)
  const draftReasoningEffort = ref(initialDraftReasoningEffort)
  const optionCalls = []
  const saveCalls = []
  const errors = []
  const behavior = {
    options: optionsBehavior,
    save: saveBehavior
  }

  const currentSessionReasoningEffort = computed({
    get() {
      if (!hasSessionId(currentSessionId.value)) return draftReasoningEffort.value
      const row = sessions.value.find(session => (
        session && Object.is(session.session_id, currentSessionId.value)
      ))
      return row?.reasoning_effort ?? null
    },
    set(reasoningEffort) {
      if (reasoningEffort !== null && typeof reasoningEffort !== 'string') return
      const normalizedReasoningEffort = typeof reasoningEffort === 'string'
        ? reasoningEffort.trim() || null
        : null
      const sessionId = currentSessionId.value
      if (!sessionId) {
        draftReasoningEffort.value = normalizedReasoningEffort
        return
      }

      const sessionIndex = sessions.value.findIndex(session => session.session_id === sessionId)
      if (sessionIndex !== -1) {
        sessions.value[sessionIndex] = {
          ...sessions.value[sessionIndex],
          reasoning_effort: normalizedReasoningEffort
        }
      }
    }
  })

  const api = {
    sessionReasoningOptions: params => {
      optionCalls.push(params)
      return behavior.options(params, optionCalls.length - 1)
    },
    updateSessionSetting: (sessionId, payload) => {
      saveCalls.push({ sessionId, payload })
      return behavior.save(sessionId, payload, saveCalls.length - 1)
    }
  }

  const scope = effectScope()
  const state = scope.run(() => useSessionReasoning({
    currentSessionId,
    profileOverrideId,
    currentSessionReasoningEffort,
    sessions,
    loading,
    api,
    onError: (error, key) => errors.push({ error, key })
  }))
  t.after(() => scope.stop())

  return {
    ...state,
    behavior,
    currentSessionId,
    currentSessionReasoningEffort,
    draftReasoningEffort,
    errors,
    loading,
    optionCalls,
    profileOverrideId,
    saveCalls,
    sessions,
    stop: () => scope.stop()
  }
}

test('loads new-session and session-specific options without selecting a default', async t => {
  const harness = createHarness(t, {
    optionsBehavior: async () => optionResponse(
      [' low ', 'low', '', '   ', 'medium', ' medium ', 3],
      [null, ' high ', 'high', '', '  ', 7, null]
    )
  })

  assert.deepEqual(harness.optionCalls, [{}])
  await flushMicrotasks()
  assert.deepEqual(harness.reasoningOptions.value, ['low', 'medium'])
  assert.deepEqual(harness.reasoningDefaults.value, [null, 'high'])
  assert.equal(harness.currentSessionReasoningEffort.value, null)
  assert.equal(harness.draftReasoningEffort.value, null)

  harness.profileOverrideId.value = 'profile-1'
  await flushMicrotasks()
  assert.deepEqual(harness.optionCalls.at(-1), { profile_override_id: 'profile-1' })

  harness.sessions.value = [{ session_id: 'A', reasoning_effort: null }]
  harness.currentSessionId.value = 'A'
  await flushMicrotasks()
  assert.deepEqual(harness.optionCalls.at(-1), { session_id: 'A' })
})

test('keeps the current reasoning override available as a candidate', async t => {
  const harness = createHarness(t, {
    currentSessionId: 'A',
    sessions: [{ session_id: 'A', reasoning_effort: ' custom ' }],
    optionsBehavior: async () => optionResponse([' low '])
  })

  await flushMicrotasks()
  assert.equal(harness.currentSessionReasoningEffort.value, ' custom ')
  assert.deepEqual(harness.reasoningOptions.value, ['low', 'custom'])
})

test('ignores stale option requests when the current session changes', async t => {
  const pending = []
  const harness = createHarness(t, {
    currentSessionId: 'A',
    sessions: [
      { session_id: 'A', reasoning_effort: null },
      { session_id: 'B', reasoning_effort: null }
    ],
    optionsBehavior: () => {
      const request = deferred()
      pending.push(request)
      return request.promise
    }
  })

  harness.currentSessionId.value = 'B'
  assert.equal(pending.length, 2)
  assert.deepEqual(harness.reasoningOptions.value, [])
  assert.equal(harness.reasoningOptionsLoading.value, true)

  pending[1].resolve(optionResponse([' b '], ['b-default']))
  await flushMicrotasks()
  assert.deepEqual(harness.reasoningOptions.value, ['b'])
  assert.deepEqual(harness.reasoningDefaults.value, ['b-default'])
  assert.equal(harness.reasoningOptionsLoading.value, false)

  pending[0].resolve(optionResponse(['a'], ['a-default']))
  await flushMicrotasks()
  assert.deepEqual(harness.reasoningOptions.value, ['b'])
  assert.deepEqual(harness.reasoningDefaults.value, ['b-default'])
  assert.equal(harness.reasoningOptionsFailed.value, false)
  assert.equal(harness.reasoningOptionsLoading.value, false)

  harness.currentSessionId.value = 'A'
  harness.currentSessionId.value = 'B'
  assert.equal(pending.length, 4)
  pending[3].resolve(optionResponse(['b-new']))
  await flushMicrotasks()
  pending[2].reject(new Error('stale session request'))
  await flushMicrotasks()
  assert.deepEqual(harness.reasoningOptions.value, ['b-new'])
  assert.equal(harness.reasoningOptionsFailed.value, false)
  assert.equal(harness.reasoningOptionsLoading.value, false)
  assert.deepEqual(harness.errors, [])
})

test('ignores stale reloads in one context and clears candidates on profile changes', async t => {
  const pending = []
  const harness = createHarness(t, {
    currentSessionId: 'A',
    sessions: [{ session_id: 'A', reasoning_effort: null }],
    optionsBehavior: () => {
      const request = deferred()
      pending.push(request)
      return request.promise
    }
  })

  const firstRequest = pending[0]
  const reload = harness.loadReasoningOptions()
  const latestRequest = pending[1]
  latestRequest.resolve(optionResponse(['latest']))
  await reload
  assert.deepEqual(harness.reasoningOptions.value, ['latest'])

  firstRequest.resolve(optionResponse(['old'], ['old-default']))
  await flushMicrotasks()
  assert.deepEqual(harness.reasoningOptions.value, ['latest'])
  assert.deepEqual(harness.reasoningDefaults.value, [])
  assert.equal(harness.reasoningOptionsFailed.value, false)

  harness.profileOverrideId.value = 'profile-2'
  assert.deepEqual(harness.reasoningOptions.value, [])
  assert.deepEqual(harness.reasoningDefaults.value, [])
  assert.equal(harness.reasoningOptionsLoading.value, true)
  assert.deepEqual(harness.optionCalls.at(-1), { session_id: 'A' })
  assert.equal(pending.length, 3)

  pending[2].resolve(optionResponse(['profile']))
  await flushMicrotasks()
  assert.deepEqual(harness.reasoningOptions.value, ['profile'])
  assert.equal(harness.reasoningOptionsLoading.value, false)
})

test('reports option load failures, retries cleanly, and tolerates malformed payloads', async t => {
  const loadError = new Error('options unavailable')
  const responses = [
    Promise.reject(loadError),
    optionResponse('not-an-array', { malformed: true }),
    undefined
  ]
  const harness = createHarness(t, {
    optionsBehavior: () => responses.shift()
  })

  await flushMicrotasks()
  assert.equal(harness.reasoningOptionsFailed.value, true)
  assert.equal(harness.reasoningOptionsLoading.value, false)
  assert.deepEqual(harness.errors, [
    { error: loadError, key: 'chat.reasoning_effort_load_failed' }
  ])

  await harness.loadReasoningOptions()
  assert.equal(harness.reasoningOptionsFailed.value, false)
  assert.equal(harness.reasoningOptionsLoading.value, false)
  assert.deepEqual(harness.reasoningOptions.value, [])
  assert.deepEqual(harness.reasoningDefaults.value, [])
  assert.equal(harness.errors.length, 1)

  await harness.loadReasoningOptions()
  assert.equal(harness.reasoningOptionsFailed.value, false)
  assert.deepEqual(harness.reasoningOptions.value, [])
  assert.deepEqual(harness.reasoningDefaults.value, [])
})

test('updates only the new-session draft and treats none as a string', async t => {
  const harness = createHarness(t)
  await flushMicrotasks()

  await harness.updateSessionReasoningEffort('  high  ')
  assert.equal(harness.draftReasoningEffort.value, 'high')
  assert.equal(harness.currentSessionReasoningEffort.value, 'high')
  assert.deepEqual(harness.saveCalls, [])

  await harness.updateSessionReasoningEffort('none')
  assert.equal(harness.draftReasoningEffort.value, 'none')
  assert.equal(typeof harness.draftReasoningEffort.value, 'string')
  assert.deepEqual(harness.saveCalls, [])

  await harness.updateSessionReasoningEffort(null)
  assert.equal(harness.draftReasoningEffort.value, null)
  assert.equal(harness.currentSessionReasoningEffort.value, null)
  assert.deepEqual(harness.saveCalls, [])
})

test('commits an existing session by captured row and suppresses stale-session errors', async t => {
  const harness = createHarness(t, {
    currentSessionId: 'A',
    sessions: [
      { session_id: 'A', reasoning_effort: 'low' },
      { session_id: 'B', reasoning_effort: 'b' }
    ]
  })
  await flushMicrotasks()

  const firstSave = deferred()
  harness.behavior.save = () => firstSave.promise
  const firstUpdate = harness.updateSessionReasoningEffort(' high ')
  assert.equal(harness.sessions.value[0].reasoning_effort, 'low')
  assert.equal(harness.reasoningEffortSubmitting.value, true)
  assert.deepEqual(harness.saveCalls, [
    { sessionId: 'A', payload: { reasoning_effort: 'high' } }
  ])

  harness.currentSessionId.value = 'B'
  firstSave.resolve()
  await firstUpdate
  assert.equal(harness.sessions.value[0].reasoning_effort, 'high')
  assert.equal(harness.sessions.value[1].reasoning_effort, 'b')
  assert.equal(harness.reasoningEffortSubmitting.value, false)

  harness.currentSessionId.value = 'A'
  await flushMicrotasks()
  const saveError = new Error('save failed')
  const failedSave = deferred()
  harness.behavior.save = () => failedSave.promise
  const failedUpdate = harness.updateSessionReasoningEffort('medium')
  harness.currentSessionId.value = 'B'
  failedSave.reject(saveError)
  await failedUpdate
  assert.equal(harness.sessions.value[0].reasoning_effort, 'high')
  assert.equal(harness.sessions.value[1].reasoning_effort, 'b')
  assert.deepEqual(harness.errors, [])

  harness.currentSessionId.value = 'A'
  await flushMicrotasks()
  harness.behavior.save = async () => undefined
  await harness.updateSessionReasoningEffort(null)
  assert.deepEqual(harness.saveCalls.at(-1), {
    sessionId: 'A',
    payload: { reasoning_effort: null }
  })
  assert.equal(harness.sessions.value[0].reasoning_effort, null)
})

test('rejects invalid efforts, accepts the 64-character boundary, and respects loading', async t => {
  const harness = createHarness(t, {
    currentSessionId: 'A',
    sessions: [{ session_id: 'A', reasoning_effort: 'base' }],
    saveBehavior: async () => undefined
  })
  await flushMicrotasks()

  for (const value of ['   ', 42, 'a'.repeat(65)]) {
    const callsBefore = harness.saveCalls.length
    await harness.updateSessionReasoningEffort(value)
    assert.equal(harness.saveCalls.length, callsBefore)
    assert.deepEqual(harness.errors.at(-1), {
      error: null,
      key: 'chat.reasoning_effort_invalid'
    })
  }

  const boundary = 'a'.repeat(64)
  await harness.updateSessionReasoningEffort(boundary)
  assert.deepEqual(harness.saveCalls.at(-1), {
    sessionId: 'A',
    payload: { reasoning_effort: boundary }
  })
  assert.equal(harness.sessions.value[0].reasoning_effort, boundary)

  const callsBeforeLoading = harness.saveCalls.length
  const errorsBeforeLoading = harness.errors.length
  harness.loading.value = true
  await harness.updateSessionReasoningEffort('blocked')
  assert.equal(harness.saveCalls.length, callsBeforeLoading)
  assert.equal(harness.errors.length, errorsBeforeLoading)
})

test('avoids normalized duplicates and blocks concurrent submissions', async t => {
  const harness = createHarness(t, {
    currentSessionId: 'A',
    sessions: [{ session_id: 'A', reasoning_effort: '  low ' }]
  })
  await flushMicrotasks()

  await harness.updateSessionReasoningEffort('low')
  assert.deepEqual(harness.saveCalls, [])
  assert.equal(harness.sessions.value[0].reasoning_effort, '  low ')

  const pendingSave = deferred()
  harness.behavior.save = () => pendingSave.promise
  const firstUpdate = harness.updateSessionReasoningEffort('high')
  assert.equal(harness.reasoningEffortSubmitting.value, true)
  assert.equal(harness.saveCalls.length, 1)
  await harness.updateSessionReasoningEffort('medium')
  assert.equal(harness.saveCalls.length, 1)
  assert.equal(harness.sessions.value[0].reasoning_effort, '  low ')

  pendingSave.resolve()
  await firstUpdate
  await harness.updateSessionReasoningEffort(' high ')
  assert.equal(harness.saveCalls.length, 1)
  assert.equal(harness.sessions.value[0].reasoning_effort, 'high')
})

test('ignores late options and saves after the scope stops', async t => {
  const pendingOptions = deferred()
  const optionHarness = createHarness(t, {
    optionsBehavior: () => pendingOptions.promise
  })
  optionHarness.stop()
  pendingOptions.resolve(optionResponse(['late'], ['late-default']))
  await flushMicrotasks()
  assert.deepEqual(optionHarness.reasoningOptions.value, [])
  assert.deepEqual(optionHarness.reasoningDefaults.value, [])
  assert.equal(optionHarness.reasoningOptionsFailed.value, false)
  assert.deepEqual(optionHarness.errors, [])

  const pendingSave = deferred()
  const saveHarness = createHarness(t, {
    currentSessionId: 'A',
    sessions: [{ session_id: 'A', reasoning_effort: 'low' }],
    saveBehavior: () => pendingSave.promise
  })
  await flushMicrotasks()
  const save = saveHarness.updateSessionReasoningEffort('high')
  saveHarness.stop()
  pendingSave.reject(new Error('late save'))
  await save
  assert.equal(saveHarness.sessions.value[0].reasoning_effort, 'low')
  assert.deepEqual(saveHarness.errors, [])
})
