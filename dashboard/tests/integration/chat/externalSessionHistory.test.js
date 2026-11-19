import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createDeferred,
  createExternalHttpPollingFixture,
  createManualTimerQueue,
  loadSessionLoadingModule
} from '../../support/chat/sessionListHarness.js'

test('session list snapshots drive external readonly history without blocking list polling', async () => {
  const { createSessionListPoller } = await loadSessionLoadingModule()
  const timers = createManualTimerQueue()
  const historyResponse = createDeferred()
  const fetches = []
  let activeFetches = 0
  let maxActiveFetches = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: async (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      activeFetches += 1
      maxActiveFetches = Math.max(maxActiveFetches, activeFetches)
      try {
        return await historyResponse.promise
      } finally {
        activeFetches -= 1
      }
    }
  })
  const snapshots = [
    [{ session_id: 'external', latest_message_id: 1, is_reply_running: false }],
    [{ session_id: 'external', latest_message_id: 1, is_reply_running: false }],
    [
      { session_id: 'external', latest_message_id: 2, is_reply_running: false },
      { session_id: 'other', latest_message_id: 99, is_reply_running: false }
    ],
    [
      { session_id: 'external', latest_message_id: 2, is_reply_running: false },
      { session_id: 'other', latest_message_id: 99, is_reply_running: false }
    ],
    [
      { session_id: 'external', latest_message_id: 2, is_reply_running: false },
      { session_id: 'other', latest_message_id: 100, is_reply_running: false }
    ]
  ]
  const observed = []
  const processing = []
  const poller = createSessionListPoller({
    refreshSessions: async () => {
      const snapshot = snapshots.shift() || []
      observed.push(snapshot)
      fixture.sessionManager.sessions.value = snapshot
      processing.push(fixture.polling.processHttpSessionSnapshot(snapshot))
      return snapshot
    },
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  await poller.refreshNow()
  await timers.tick()
  await timers.tick()
  assert.equal(observed.length, 3)
  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 1, limit: 50 }])

  await timers.tick()
  assert.equal(observed.length, 4)
  assert.equal(fetches.length, 1)
  assert.equal(maxActiveFetches, 1)
  assert.equal(timers.pending.length, 1)

  historyResponse.resolve([{ db_id: 2, content: 'two' }])
  await Promise.all(processing)
  await timers.tick()
  await Promise.all(processing)

  assert.equal(observed.length, 5)
  assert.equal(fetches.length, 1)
  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 2)
  assert.equal(timers.pending.length, 1)
  poller.dispose()
})

test('external readonly history waits for initial history and validates list message ids', async () => {
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    initialHistoryLoaded: false,
    fetchPage: (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      return []
    }
  })
  const validSnapshot = [{ session_id: 'external', latest_message_id: '5' }]

  await fixture.polling.processHttpSessionSnapshot(validSnapshot)
  fixture.sessionManager.currentSessionId.value = null
  await fixture.polling.processHttpSessionSnapshot(validSnapshot)
  fixture.sessionManager.currentSessionId.value = 'external'
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'other', latest_message_id: 5 }
  ])

  fixture.initialHistoryLoaded.value = true
  for (const latestMessageId of [
    undefined,
    null,
    '',
    '   ',
    0,
    -1,
    1.5,
    'not-a-number',
    Infinity,
    Number.MAX_SAFE_INTEGER + 1,
    true,
    [],
    {}
  ]) {
    await fixture.polling.processHttpSessionSnapshot([
      { session_id: 'external', latest_message_id: latestMessageId }
    ])
  }

  assert.deepEqual(fetches, [])
  await fixture.polling.processHttpSessionSnapshot(validSnapshot)

  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 0, limit: 50 }])
})

test('external readonly history falls back to the loaded history maximum when its cursor is uncached', async () => {
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    messages: [{ db_id: 5, content: 'five' }],
    fetchPage: (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      return [{ db_id: 6, content: 'six' }]
    }
  })

  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 5 }
  ])
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 4 }
  ])
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 6 }
  ])
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 6 }
  ])

  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 5, limit: 50 }])
})

test('external readonly history drains bounded pages on repeated identical list snapshots', async () => {
  const serverMessages = Array.from({ length: 7 }, (_, index) => ({
    db_id: index + 2,
    content: `message-${index + 2}`
  }))
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    pageSize: 2,
    maxPages: 1,
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: (sessionId, { afterId, limit }) => {
      fetches.push({ sessionId, afterId, limit })
      return serverMessages.filter(message => Number(message.db_id) > afterId).slice(0, limit)
    }
  })
  const snapshot = [{ session_id: 'external', latest_message_id: 8 }]

  for (let attempt = 0; attempt < 5; attempt += 1) {
    await fixture.polling.processHttpSessionSnapshot(snapshot)
  }

  assert.deepEqual(fetches, [
    { sessionId: 'external', afterId: 1, limit: 2 },
    { sessionId: 'external', afterId: 3, limit: 2 },
    { sessionId: 'external', afterId: 5, limit: 2 },
    { sessionId: 'external', afterId: 7, limit: 2 }
  ])
  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2, 3, 4, 5, 6, 7, 8]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 8)
})

test('external readonly history does not confirm a filtered list id as its history cursor', async () => {
  const afterIds = []
  let attempt = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 5]]),
    fetchPage: (sessionId, { afterId }) => {
      afterIds.push({ sessionId, afterId })
      attempt += 1
      return attempt === 1 ? [] : [{ db_id: 10, content: 'ten' }]
    }
  })

  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 9 }
  ])
  assert.equal(fixture.merger.lastMessageIds.get('external'), 5)
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 9 }
  ])
  assert.deepEqual(afterIds, [{ sessionId: 'external', afterId: 5 }])

  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'external', latest_message_id: 10 }
  ])
  assert.deepEqual(afterIds, [
    { sessionId: 'external', afterId: 5 },
    { sessionId: 'external', afterId: 5 }
  ])
  assert.equal(fixture.merger.lastMessageIds.get('external'), 10)
})

test('external readonly history uses the actual cursor when raw history is not visible', async () => {
  const visibleMessages = [{ db_id: 5, content: 'visible' }]
  const fetches = []
  const fixture = createExternalHttpPollingFixture({
    messages: visibleMessages,
    initialLastMessageIds: new Map([['external', 5]]),
    fetchPage: (sessionId, { afterId }) => {
      fetches.push({ sessionId, afterId })
      return [{ db_id: 9, content: 'filtered raw message' }]
    }
  })
  const snapshot = [{ session_id: 'external', latest_message_id: 9 }]

  await fixture.polling.processHttpSessionSnapshot(snapshot)
  assert.deepEqual(fixture.chatState.messages.value, visibleMessages)
  assert.equal(fixture.merger.lastMessageIds.get('external'), 9)

  fixture.polling.resetHttpPollingState()
  await fixture.polling.processHttpSessionSnapshot(snapshot)
  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 5 }])
})

test('external readonly history retries the same list snapshot after a failed page without advancing its cursor', async () => {
  const failure = new Error('temporary history failure')
  const afterIds = []
  let attempt = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: (sessionId, { afterId }) => {
      afterIds.push({ sessionId, afterId })
      attempt += 1
      if (attempt === 1) return Promise.reject(failure)
      return [{ db_id: 2, content: 'two' }]
    }
  })
  const snapshot = [{ session_id: 'external', latest_message_id: 2 }]

  await assert.rejects(
    fixture.polling.processHttpSessionSnapshot(snapshot),
    error => error === failure
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 1)
  assert.deepEqual(fixture.merger.mergedBySession.get('external') || [], [])

  await fixture.polling.processHttpSessionSnapshot(snapshot)
  assert.deepEqual(afterIds, [
    { sessionId: 'external', afterId: 1 },
    { sessionId: 'external', afterId: 1 }
  ])
  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 2)
})

test('external readonly history serializes duplicate and higher list ids while a page is delayed', async () => {
  const firstResponse = createDeferred()
  const secondResponse = createDeferred()
  const fetches = []
  let activeFetches = 0
  let maxActiveFetches = 0
  let attempt = 0
  const fixture = createExternalHttpPollingFixture({
    initialLastMessageIds: new Map([['external', 1]]),
    fetchPage: (sessionId, request) => {
      fetches.push({ sessionId, ...request })
      activeFetches += 1
      maxActiveFetches = Math.max(maxActiveFetches, activeFetches)
      const response = attempt === 0 ? firstResponse : secondResponse
      attempt += 1
      return response.promise.finally(() => {
        activeFetches -= 1
      })
    }
  })
  const duplicateSnapshot = [{ session_id: 'external', latest_message_id: 2 }]
  const higherSnapshot = [{ session_id: 'external', latest_message_id: 3 }]

  const firstSync = fixture.polling.processHttpSessionSnapshot(duplicateSnapshot)
  await fixture.polling.processHttpSessionSnapshot(duplicateSnapshot)
  await fixture.polling.processHttpSessionSnapshot(higherSnapshot)
  assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 1, limit: 50 }])
  assert.equal(maxActiveFetches, 1)

  firstResponse.resolve([{ db_id: 2, content: 'two' }])
  await firstSync
  const catchUp = fixture.polling.processHttpSessionSnapshot(higherSnapshot)
  assert.deepEqual(fetches, [
    { sessionId: 'external', afterId: 1, limit: 50 },
    { sessionId: 'external', afterId: 2, limit: 50 }
  ])
  secondResponse.resolve([{ db_id: 3, content: 'three' }])
  await catchUp

  assert.deepEqual(
    (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
    [2, 3]
  )
  assert.equal(fixture.merger.lastMessageIds.get('external'), 3)
  assert.equal(activeFetches, 0)
})

test('external readonly history invalidates stale A responses across an A to B to A reset cycle', async () => {
  const firstAResponse = createDeferred()
  const secondAResponse = createDeferred()
  const fetches = []
  let aRequestCount = 0
  const fixture = createExternalHttpPollingFixture({
    currentSessionId: 'session-a',
    fetchPage: (sessionId, request) => {
      fetches.push({ sessionId, ...request })
      if (sessionId === 'session-a') {
        aRequestCount += 1
        return (aRequestCount === 1 ? firstAResponse : secondAResponse).promise
      }
      return []
    }
  })
  const firstASnapshot = [{ session_id: 'session-a', latest_message_id: 1 }]
  const secondASnapshot = [{ session_id: 'session-a', latest_message_id: 2 }]

  const staleA = fixture.polling.processHttpSessionSnapshot(firstASnapshot)
  fixture.sessionManager.currentSessionId.value = 'session-b'
  fixture.polling.resetHttpPollingState()
  await fixture.polling.processHttpSessionSnapshot([
    { session_id: 'session-b', latest_message_id: 1 }
  ])
  fixture.sessionManager.currentSessionId.value = 'session-a'
  fixture.polling.resetHttpPollingState()
  const currentA = fixture.polling.processHttpSessionSnapshot(secondASnapshot)
  assert.equal(aRequestCount, 2)

  firstAResponse.resolve([{ db_id: 1, content: 'stale A' }])
  await staleA
  assert.deepEqual(fixture.merger.mergedBySession.get('session-a') || [], [])
  assert.equal(fixture.merger.lastMessageIds.get('session-a'), undefined)

  await fixture.polling.processHttpSessionSnapshot(secondASnapshot)
  assert.equal(aRequestCount, 2)
  secondAResponse.resolve([{ db_id: 2, content: 'current A' }])
  await currentA

  assert.deepEqual(
    (fixture.merger.mergedBySession.get('session-a') || []).map(message => message.db_id),
    [2]
  )
  assert.equal(fixture.merger.lastMessageIds.get('session-a'), 2)
  assert.deepEqual(fetches.filter(({ sessionId }) => sessionId === 'session-a').map(({ afterId }) => afterId), [0, 0])
})

test('external readonly history ignores reset, leave, source, and initial-history stale returns and retries after recovery', async () => {
  const runStaleCase = async ({ mutate, restore, staleType }) => {
    const staleResponse = createDeferred()
    let attempts = 0
    const fixture = createExternalHttpPollingFixture({
      fetchPage: () => {
        attempts += 1
        if (attempts === 1) return staleResponse.promise
        return [{ db_id: 1, content: 'retry' }]
      }
    })
    const snapshot = [{ session_id: 'external', latest_message_id: 1 }]
    const staleSync = fixture.polling.processHttpSessionSnapshot(snapshot)
    mutate(fixture)
    if (staleType === 'error') {
      staleResponse.reject(new Error('stale history failure'))
    } else {
      staleResponse.resolve([{ db_id: 1, content: 'stale' }])
    }
    await staleSync

    assert.equal(attempts, 1)
    assert.deepEqual(fixture.merger.mergedBySession.get('external') || [], [])
    assert.equal(fixture.merger.lastMessageIds.get('external'), undefined)
    restore(fixture)
    const retry = fixture.polling.processHttpSessionSnapshot(snapshot)
    assert.equal(attempts, 2)
    await retry
    assert.deepEqual(
      (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
      [1]
    )
    assert.equal(fixture.merger.lastMessageIds.get('external'), 1)
  }

  await runStaleCase({
    mutate: fixture => fixture.polling.resetHttpPollingState(),
    restore: () => {},
    staleType: 'response'
  })
  await runStaleCase({
    mutate: fixture => {
      fixture.sessionManager.currentSessionId.value = null
    },
    restore: fixture => {
      fixture.sessionManager.currentSessionId.value = 'external'
    },
    staleType: 'error'
  })
  await runStaleCase({
    mutate: fixture => {
      fixture.isCurrentSessionReadOnly.value = false
    },
    restore: fixture => {
      fixture.isCurrentSessionReadOnly.value = true
    },
    staleType: 'error'
  })
  await runStaleCase({
    mutate: fixture => {
      fixture.initialHistoryLoaded.value = false
    },
    restore: fixture => {
      fixture.initialHistoryLoaded.value = true
    },
    staleType: 'error'
  })
})

test('external readonly history works for every transport and loading state without writable HTTP effects', async () => {
  for (const transportMode of ['http', 'ws']) {
    for (const loading of [false, true]) {
      const visibleMessages = [{ db_id: 7, content: 'visible' }]
      const pendingHttpRequests = new Map([
        ['request-1', { sessionId: 'external', workId: 'work-1' }]
      ])
      const pendingBefore = Array.from(pendingHttpRequests.entries())
      const fetches = []
      const fixture = createExternalHttpPollingFixture({
        transportMode,
        loading,
        messages: visibleMessages,
        initialLastMessageIds: new Map([['external', 7]]),
        pendingHttpRequests,
        fetchPage: (sessionId, { afterId, limit }) => {
          fetches.push({ sessionId, afterId, limit })
          return [{ db_id: 8, content: 'eight' }]
        }
      })
      const snapshot = [{
        session_id: 'external',
        latest_message_id: 8,
        is_reply_running: loading,
        reply_works: [{
          work_id: 'work-1',
          status: 'succeeded',
          request_ids: ['request-1']
        }]
      }]

      await fixture.polling.processHttpSessionSnapshot(snapshot)
      assert.deepEqual(fetches, [{ sessionId: 'external', afterId: 7, limit: 50 }])
      assert.deepEqual(
        (fixture.merger.mergedBySession.get('external') || []).map(message => message.db_id),
        [8]
      )
      assert.equal(fixture.chatState.loading.value, loading)
      assert.deepEqual(fixture.chatState.messages.value, visibleMessages)
      assert.deepEqual(Array.from(pendingHttpRequests.entries()), pendingBefore)
    }
  }
})
