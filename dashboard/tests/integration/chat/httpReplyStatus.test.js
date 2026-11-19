import assert from 'node:assert/strict'
import test from 'node:test'
import { createDeferred, createHarness } from './replyControlHarness.js'

test('cancelled HTTP work status finishes quietly and clears its lifecycle state', async () => {
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http' }],
    currentSessionId: 'A',
    inputMsg: 'keep this draft',
    messages: [
      { id: 'history', db_id: 42, role: 'assistant', content: 'historical answer' },
      {
        id: 'work-history',
        db_id: 43,
        role: 'assistant',
        content: 'completed answer',
        reasoning_content: 'completed reasoning',
        work_id: 'work-A'
      },
      { id: 'tool-result', role: 'tool', content: 'completed tool result', work_id: 'work-A' },
      { id: 'queued', role: 'user', content: 'queued prompt', request_id: 'request-A', status: 'queued' },
      { id: 'thinking', role: 'thinking', content: 'partial reply', work_id: 'work-A', request_ids: ['request-A', 'request-B'] },
      { id: 'reasoning', role: 'reasoning', content: 'temporary reasoning', work_id: 'work-A' },
      { id: 'unrelated-reasoning', role: 'reasoning', content: 'unrelated reasoning', work_id: 'work-other', request_id: 'request-other' }
    ],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }],
      ['request-B', { sessionId: 'A', workId: 'work-A' }],
      ['request-other', { sessionId: 'B', workId: 'work-B' }]
    ]
  })

  const work = { work_id: 'work-A', request_ids: ['request-A', 'request-B'] }
  const statusData = {
    session_id: 'A',
    status: 'cancelled',
    error: 'backend cancellation details',
    request_ids: ['request-A', 'request-B']
  }
  const result = harness.applyHttpWorkStatus(work, statusData, 'A')

  assert.equal(result?.terminal, true)
  assert.equal(result?.succeeded, false)
  assert.deepEqual(harness.messageProcessorErrors, [])
  assert.deepEqual(harness.errors, [])
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
  assert.equal(harness.chatState.messages.value.some(message => message.id === 'reasoning'), false)
  assert.equal(
    harness.chatState.messages.value.find(message => message.id === 'unrelated-reasoning').content,
    'unrelated reasoning'
  )
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'queued').status, undefined)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'history').content, 'historical answer')
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'work-history').content, 'completed answer')
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'work-history').reasoning_content, 'completed reasoning')
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'tool-result').content, 'completed tool result')
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.equal(harness.pendingHttpRequests.has('request-A'), false)
  assert.equal(harness.pendingHttpRequests.has('request-B'), false)
  assert.equal(harness.pendingHttpRequests.has('request-other'), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)

  const messagesAfterCancellation = harness.chatState.messages.value.map(message => ({ ...message }))
  harness.applyHttpWorkStatus(work, statusData, 'A')
  assert.deepEqual(harness.chatState.messages.value, messagesAfterCancellation)

  const afterLateCancellation = harness.workLifecycleTracker.finishWorkLifecycle(
    harness.chatState.messages.value,
    {
      type: 'cancelled',
      session_id: 'A',
      work_id: 'work-A',
      request_ids: ['request-A', 'request-B']
    }
  )
  assert.deepEqual(afterLateCancellation, messagesAfterCancellation)

  const afterLateLoop = harness.workLifecycleTracker.startAgentLoop(
    harness.chatState.messages.value,
    { work_id: 'work-A', response_id: 'late-response', request_ids: ['request-A', 'request-B'] }
  )
  assert.deepEqual(afterLateLoop, messagesAfterCancellation)
  assert.equal(afterLateLoop.some(message => message.role === 'thinking'), false)
})

test('cancelled merged HTTP work clears reasoning by resolved work and request fallback', () => {
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http' }],
    currentSessionId: 'A',
    messages: [
      { id: 'original-thinking', role: 'thinking', content: 'original work', work_id: 10, request_id: 'merge-request' },
      { id: 'original-reasoning', role: 'reasoning', content: 'original reasoning', work_id: '10', request_id: 'merge-request' },
      { id: 'resolved-reasoning', role: 'reasoning', content: 'resolved work', work_id: '20' },
      { id: 'request-reasoning', role: 'reasoning', content: 'request only', request_id: 'merge-request' },
      { id: 'unrelated-reasoning', role: 'reasoning', content: 'unrelated', work_id: 'work-other', request_id: 'request-other' }
    ],
    pendingHttpRequests: [
      ['merge-request', { sessionId: 'A', workId: '10' }],
      ['request-other', { sessionId: 'A', workId: 'work-other' }]
    ]
  })
  const work = { work_id: 10, request_ids: ['merge-request'] }
  const statusData = {
    session_id: 'A',
    status: 'cancelled',
    resolved_work_id: '20'
  }

  const result = harness.applyHttpWorkStatus(
    work,
    statusData,
    'A'
  )

  assert.equal(result?.terminal, true)
  assert.deepEqual(
    harness.chatState.messages.value.map(message => message.id),
    ['unrelated-reasoning']
  )
  assert.equal(
    harness.chatState.messages.value.find(message => message.id === 'unrelated-reasoning').content,
    'unrelated'
  )
  assert.equal(harness.pendingHttpRequests.has('merge-request'), false)
  assert.equal(harness.pendingHttpRequests.has('request-other'), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('20'), true)
})

test('succeeded and failed HTTP work statuses preserve temporary reasoning', () => {
  for (const status of ['succeeded', 'failed']) {
    const harness = createHarness({
      sessions: [{ session_id: 'A', source: 'http' }],
      currentSessionId: 'A',
      messages: [
        { id: 'thinking', role: 'thinking', content: 'partial reply', work_id: 'work-A' },
        { id: 'reasoning', role: 'reasoning', content: 'temporary reasoning', work_id: 'work-A' }
      ]
    })

    harness.applyHttpWorkStatus(
      { work_id: 'work-A', request_ids: ['request-A'] },
      {
        session_id: 'A',
        status,
        error: status === 'failed' ? 'backend failure details' : undefined,
        request_ids: ['request-A']
      },
      'A'
    )

    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
    assert.equal(harness.chatState.messages.value.find(message => message.id === 'reasoning').content, 'temporary reasoning')
  }
})

test('failed HTTP work status reports its actual error once and cleans up lifecycle state', async () => {
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http' }],
    currentSessionId: 'A',
    inputMsg: 'keep this draft',
    messages: [
      { id: 'history', db_id: 42, role: 'assistant', content: 'historical answer' },
      { id: 'queued', role: 'user', content: 'queued prompt', request_id: 'request-A', status: 'queued' },
      { id: 'thinking', role: 'thinking', content: 'partial reply', work_id: 'work-A', request_ids: ['request-A', 'request-B'] }
    ],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }],
      ['request-B', { sessionId: 'A', workId: 'work-A' }],
      ['request-other', { sessionId: 'B', workId: 'work-B' }]
    ]
  })

  const statusData = {
    session_id: 'A',
    status: 'failed',
    error: 'backend failure details',
    result_message_id: 100,
    request_ids: ['request-A', 'request-B']
  }
  const result = harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A', 'request-B'] },
    statusData,
    'A'
  )

  assert.equal(result?.terminal, true)
  assert.equal(result?.succeeded, false)
  assert.equal(harness.messageProcessorErrors.length, 1)
  assert.equal(harness.messageProcessorErrors[0][1], 'backend failure details')
  assert.deepEqual(harness.errors, ['backend failure details'])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'queued').status, undefined)
  assert.equal(harness.chatState.messages.value.find(message => message.id === 'history').content, 'historical answer')
  assert.equal(harness.chatState.inputMsg.value, 'keep this draft')
  assert.equal(harness.pendingHttpRequests.has('request-A'), false)
  assert.equal(harness.pendingHttpRequests.has('request-B'), false)
  assert.equal(harness.pendingHttpRequests.has('request-other'), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)

  harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A', 'request-B'] },
    statusData,
    'A'
  )
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
  assert.deepEqual(harness.errors, ['backend failure details'])
})

test('failed HTTP status does not duplicate an existing error result message', async () => {
  const harness = createHarness({
    sessions: [{ session_id: 'A', source: 'http' }],
    currentSessionId: 'A',
    messages: [
      { id: 'thinking', role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] },
      { id: 'existing-error', role: 'err', db_id: 77, content: 'already recorded' }
    ],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }]
    ]
  })

  const result = harness.applyHttpWorkStatus(
    { work_id: 'work-A', request_ids: ['request-A'] },
    {
      session_id: 'A',
      status: 'failed',
      error: 'backend failure details',
      result_message_id: 77,
      request_ids: ['request-A']
    },
    'A'
  )

  assert.equal(result?.terminal, true)
  assert.deepEqual(harness.messageProcessorErrors, [])
  assert.deepEqual(harness.errors, [])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
})

test('HTTP terminal status is fetched once and duplicate snapshots do not report a second error', async () => {
  const session = {
    session_id: 'A',
    source: 'http',
    is_reply_running: false,
    reply_works: [
      { work_id: 'work-A', status: 'failed', request_ids: ['request-A'] }
    ]
  }
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }]
    ]
  })
  harness.replyWorkStatusBehavior = async workId => ({
    data: {
      data: {
        session_id: 'A',
        status: 'failed',
        error: `failure for ${workId}`,
        request_ids: ['request-A']
      }
    }
  })

  await harness.processHttpSessionSnapshot([session])
  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
  assert.deepEqual(harness.errors, ['failure for work-A'])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)

  await harness.processHttpSessionSnapshot([session])
  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
  assert.deepEqual(harness.errors, ['failure for work-A'])
  assert.equal(harness.chatState.messages.value.filter(message => message.role === 'err').length, 1)
})

test('HTTP status from another session or transport mode is not applied', async () => {
  const session = {
    session_id: 'A',
    source: 'http',
    reply_works: [{ work_id: 'work-A', request_ids: ['request-A'] }]
  }
  const messages = [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }]
  const harness = createHarness({
    sessions: [session],
    currentSessionId: 'A',
    messages,
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }]
    ]
  })
  const status = {
    status: 'failed',
    error: 'should not be applied',
    request_ids: ['request-A']
  }

  assert.equal(
    harness.applyHttpWorkStatus({ work_id: 'work-A', request_ids: ['request-A'] }, {
      ...status,
      session_id: 'B'
    }, 'A'),
    null
  )
  assert.deepEqual(harness.chatState.messages.value, messages)
  assert.equal(harness.pendingHttpRequests.has('request-A'), true)

  harness.transport.transportMode.value = 'ws'
  assert.equal(
    harness.applyHttpWorkStatus({ work_id: 'work-A', request_ids: ['request-A'] }, {
      ...status,
      session_id: 'A'
    }, 'A'),
    null
  )
  assert.deepEqual(harness.chatState.messages.value, messages)
  assert.equal(harness.pendingHttpRequests.has('request-A'), true)
  assert.deepEqual(harness.errors, [])
})

test('HTTP status queries returning after reset or session/mode changes are ignored', async () => {
  for (const transition of ['reset', 'session', 'mode']) {
    const statusDeferred = createDeferred()
    const sessionA = {
      session_id: 'A',
      source: 'http',
      is_reply_running: false,
      reply_works: [{ work_id: 'work-A', status: 'failed', request_ids: ['request-A'] }]
    }
    const sessionB = {
      session_id: 'B',
      source: 'http',
      is_reply_running: false,
      reply_works: []
    }
    const harness = createHarness({
      sessions: [sessionA, sessionB],
      currentSessionId: 'A',
      messages: [{ role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }],
      pendingHttpRequests: [
        ['request-A', { sessionId: 'A', workId: 'work-A' }]
      ]
    })
    harness.replyWorkStatusBehavior = () => statusDeferred.promise

    const processing = harness.processHttpSessionSnapshot([sessionA])
    assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
    if (transition === 'reset') {
      harness.resetHttpPollingState()
    } else if (transition === 'session') {
      harness.currentSessionId.value = 'B'
    } else {
      harness.transport.transportMode.value = 'ws'
    }

    statusDeferred.resolve({
      data: {
        data: {
          session_id: 'A',
          status: 'failed',
          error: 'late failure',
          request_ids: ['request-A']
        }
      }
    })
    await processing

    assert.deepEqual(harness.errors, [])
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'err'), false)
  }
})

test('HTTP same-work sequence recovery refetches terminal status after lifecycle reset', async () => {
  for (const nextSequenceNo of [6, 7]) {
    const nextRequestId = 'request-A'
    const makeSession = (sequenceNo, status, isReplyRunning, requestId) => ({
      session_id: 'A',
      source: 'http',
      is_reply_running: isReplyRunning,
      reply_works: [{
        work_id: 'work-A',
        status,
        sequence_no: sequenceNo,
        request_ids: [requestId]
      }]
    })
    const harness = createHarness({
      sessions: [makeSession(5, 'running', true, 'request-A')],
      currentSessionId: 'A',
      messages: [
        { id: 'queued', role: 'user', request_id: 'request-A', status: 'queued' },
        { id: 'thinking', role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }
      ],
      pendingHttpRequests: [
        ['request-A', { sessionId: 'A', workId: 'work-A' }]
      ]
    })
    harness.replyWorkStatusBehavior = () => {
      const callNo = harness.replyWorkStatusCalls.length
      const requestId = callNo === 1 ? 'request-A' : nextRequestId
      const sequenceNo = callNo === 1 ? 5 : nextSequenceNo
      return {
        data: {
          data: {
            session_id: 'A',
            status: 'succeeded',
            work_sequence_no: sequenceNo,
            request_ids: [requestId],
            response: { request_ids: [requestId] }
          }
        }
      }
    }

    const processSnapshot = async snapshot => {
      harness.sessions.value = [snapshot]
      await harness.processHttpSessionSnapshot([snapshot])
    }

    await processSnapshot(makeSession(5, 'running', true, 'request-A'))
    assert.deepEqual(harness.replyWorkStatusCalls, [])
    assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), false)

    await processSnapshot(makeSession(5, 'succeeded', false, 'request-A'))
    assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
    assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)
    assert.equal(harness.pendingHttpRequests.has('request-A'), false)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
    assert.deepEqual(harness.errors, [])

    if (nextSequenceNo === 6) {
      await processSnapshot(makeSession(6, 'running', true, nextRequestId))
      assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), false)
      assert.equal(harness.chatState.loading.value, true)
      assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
    }

    harness.chatState.messages.value = harness.workLifecycleTracker.startAgentLoop(
      harness.chatState.messages.value,
      {
        work_id: 'work-A',
        work_sequence_no: nextSequenceNo,
        response_id: `response-${nextSequenceNo}`,
        request_ids: [nextRequestId]
      }
    )
    assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), false)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)

    const nextTerminalSnapshot = makeSession(nextSequenceNo, 'succeeded', false, nextRequestId)
    await processSnapshot(nextTerminalSnapshot)
    assert.deepEqual(harness.replyWorkStatusCalls, ['work-A', 'work-A'])
    assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)
    assert.equal(harness.pendingHttpRequests.size, 0)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
    assert.deepEqual(harness.errors, [])

    await processSnapshot(nextTerminalSnapshot)
    assert.deepEqual(harness.replyWorkStatusCalls, ['work-A', 'work-A'])
    assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)
    assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
  }
})

test('late HTTP status from an older same-work sequence cannot finish a newer lifecycle', async () => {
  const oldStatusDeferred = createDeferred()
  const makeSession = (sequenceNo, status, isReplyRunning, requestId) => ({
    session_id: 'A',
    source: 'http',
    is_reply_running: isReplyRunning,
    reply_works: [{
      work_id: 'work-A',
      status,
      sequence_no: sequenceNo,
      request_ids: [requestId]
    }]
  })
  const harness = createHarness({
    sessions: [makeSession(5, 'running', true, 'request-A')],
    currentSessionId: 'A',
    messages: [
      { id: 'thinking', role: 'thinking', work_id: 'work-A', request_ids: ['request-A'] }
    ],
    pendingHttpRequests: [
      ['request-A', { sessionId: 'A', workId: 'work-A' }]
    ]
  })
  harness.replyWorkStatusBehavior = () => oldStatusDeferred.promise

  const processSnapshot = async snapshot => {
    harness.sessions.value = [snapshot]
    await harness.processHttpSessionSnapshot([snapshot])
  }

  await processSnapshot(makeSession(5, 'running', true, 'request-A'))
  const oldStatusProcessing = processSnapshot(makeSession(5, 'failed', false, 'request-A'))
  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])

  harness.pendingHttpRequests.clear()
  harness.trackHttpSubmission('request-B', 'A', 'work-A')
  await processSnapshot(makeSession(6, 'running', true, 'request-B'))
  harness.chatState.messages.value = harness.workLifecycleTracker.startAgentLoop(
    harness.chatState.messages.value,
    {
      work_id: 'work-A',
      work_sequence_no: 6,
      response_id: 'response-6',
      request_ids: ['request-B']
    }
  )
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), false)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)

  oldStatusDeferred.resolve({
    data: {
      data: {
        session_id: 'A',
        status: 'failed',
        work_sequence_no: 5,
        error: 'stale failure',
        request_ids: ['request-A']
      }
    }
  })
  await oldStatusProcessing

  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A'])
  assert.deepEqual(harness.errors, [])
  assert.deepEqual(harness.messageProcessorErrors, [])
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'err'), false)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), true)
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), false)
  assert.equal(harness.pendingHttpRequests.has('request-B'), true)

  harness.replyWorkStatusBehavior = () => ({
    data: {
      data: {
        session_id: 'A',
        status: 'succeeded',
        work_sequence_no: 6,
        request_ids: ['request-B'],
        response: { request_ids: ['request-B'] }
      }
    }
  })
  const newTerminalSnapshot = makeSession(6, 'succeeded', false, 'request-B')
  await processSnapshot(newTerminalSnapshot)

  assert.deepEqual(harness.replyWorkStatusCalls, ['work-A', 'work-A'])
  assert.deepEqual(harness.errors, [])
  assert.equal(harness.workLifecycleTracker.isWorkTerminal('work-A'), true)
  assert.equal(harness.chatState.messages.value.some(message => message.role === 'thinking'), false)
  assert.equal(harness.pendingHttpRequests.has('request-B'), false)
})
