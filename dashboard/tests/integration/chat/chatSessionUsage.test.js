import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import { getContextSummaryProgress } from '../../../src/composables/chat/requestUsagePresentation.js'
import {
  createContextMetadata,
  createMainMetadata,
  createProactiveReply,
  createTaskMetadata,
  getSessionMetadata,
  receiveWs,
  selectSession,
  snapshotMetadata
} from './sessionCompositionHarness.js'

test('composed session keeps confirmed LLM metadata scoped and ordered', async t => {
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', { source: 'ws' }),
      createSession('B', { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()

  const providerMetadata = {
    type: 'llm_request_metadata',
    session_id: 'A',
    input_tokens: 123,
    input_tokens_source: 'provider',
    context_window_tokens: 8192,
    max_output_tokens: 1024,
    channel_id: 7,
    model_id: 'model-a',
    protocol: 'responses',
    context_summary_revision: 2,
    context_content_revision: 4,
    summary_trigger_tokens: 200,
    work_sequence_no: 2,
    event_sequence_no: 5,
    request_purpose: 'main_dialogue'
  }
  await receiveWs(harness, providerMetadata)

  assert.equal(chat.llmRequestMetadata.value.input_tokens, 123)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
  const firstMetadata = JSON.stringify(chat.llmRequestMetadata.value)

  await receiveWs(harness, { ...providerMetadata })
  assert.equal(JSON.stringify(chat.llmRequestMetadata.value), firstMetadata)

  await receiveWs(harness, {
    ...providerMetadata,
    input_tokens: 77,
    work_sequence_no: 1,
    event_sequence_no: 99
  })
  await receiveWs(harness, {
    ...providerMetadata,
    input_tokens: 88,
    work_sequence_no: 3,
    event_sequence_no: 1,
    request_purpose: 'context_summary'
  })
  await receiveWs(harness, {
    ...providerMetadata,
    session_id: 'B',
    input_tokens: 66,
    work_sequence_no: 9,
    event_sequence_no: 9
  })
  assert.equal(chat.llmRequestMetadata.value.input_tokens, 123)

  await receiveWs(harness, {
    ...providerMetadata,
    input_tokens: 999,
    input_tokens_source: 'estimated',
    work_sequence_no: 2,
    event_sequence_no: 6
  })
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

  const sessionMetadata = chat.sessions.value.find(item => item.session_id === 'A').llm_request_metadata
  assert.equal(sessionMetadata.input_tokens, null)
  assert.equal(sessionMetadata.input_tokens_source, 'estimated')
  assert.equal(sessionMetadata.last_confirmed_context_usage.input_tokens, 123)
  assert.equal(sessionMetadata.last_confirmed_context_usage.summary_trigger_tokens, 200)
  assert.equal(getContextSummaryProgress(sessionMetadata).status, 'previous')
  assert.equal(getContextSummaryProgress(sessionMetadata).ratio, 123 / 200)
})

test('composed session accumulates LLM totals across ordered main and scheduled metadata', async t => {
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', {
        source: 'ws',
        llm_request_metadata: {
          total_input_tokens: 1000,
          total_cached_tokens: 250,
          total_output_tokens: 200,
          cache_hit_rate: 250 / 1000
        }
      }),
      createSession('B', { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const currentMetadata = () => chat.llmRequestMetadata.value
  const sessionMetadata = () => chat.sessions.value.find(item => item.session_id === 'A').llm_request_metadata
  const totals = () => [
    currentMetadata().total_input_tokens,
    currentMetadata().total_cached_tokens,
    currentMetadata().total_output_tokens,
    currentMetadata().cache_hit_rate
  ]

  await harness.flush()
  assert.deepEqual(totals(), [1000, 250, 200, 250 / 1000])
  assert.equal(currentMetadata().input_tokens, undefined)

  const mainMetadata = {
    type: 'llm_request_metadata',
    session_id: 'A',
    input_tokens: 120,
    input_tokens_source: 'provider',
    context_window_tokens: 8192,
    max_output_tokens: 1024,
    work_sequence_no: 3,
    event_sequence_no: 1,
    request_purpose: 'main_dialogue',
    total_input_tokens: 1120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_hit_rate: 270 / 1120
  }
  await receiveWs(harness, mainMetadata)
  assert.deepEqual(totals(), [1120, 270, 207, 270 / 1120])
  assert.equal(currentMetadata().input_tokens, 120)
  assert.equal(currentMetadata().context_window_tokens, 8192)
  assert.equal(currentMetadata().max_output_tokens, 1024)
  assert.equal(currentMetadata().work_sequence_no, 3)
  assert.deepEqual(sessionMetadata(), currentMetadata())

  const olderMetadata = {
    ...mainMetadata,
    input_tokens: 999,
    context_window_tokens: 16384,
    max_output_tokens: 2048,
    work_sequence_no: 2,
    event_sequence_no: 9,
    total_input_tokens: 1420,
    total_cached_tokens: 360,
    total_output_tokens: 218,
    cache_hit_rate: 360 / 1420
  }
  await receiveWs(harness, olderMetadata)
  assert.deepEqual(totals(), [1420, 360, 218, 360 / 1420])
  assert.equal(currentMetadata().input_tokens, 120)
  assert.equal(currentMetadata().context_window_tokens, 8192)
  assert.equal(currentMetadata().max_output_tokens, 1024)
  assert.equal(currentMetadata().work_sequence_no, 3)

  await receiveWs(harness, { ...olderMetadata })
  assert.deepEqual(totals(), [1420, 360, 218, 360 / 1420])
  await receiveWs(harness, {
    ...olderMetadata,
    input_tokens: 88,
    context_window_tokens: 4096,
    max_output_tokens: 512,
    event_sequence_no: 10,
    total_input_tokens: 1300,
    total_cached_tokens: 300,
    total_output_tokens: 210,
    cache_hit_rate: 300 / 1300
  })
  assert.deepEqual(totals(), [1420, 360, 218, 360 / 1420])

  for (const [index, request_purpose] of [
    'memory_recall',
    'context_summary',
    'audit_auxiliary',
    'background_summary',
    'confirmed_auxiliary'
  ].entries()) {
    await receiveWs(harness, {
      ...mainMetadata,
      input_tokens: 999999,
      context_window_tokens: 1000000,
      max_output_tokens: 4096,
      work_sequence_no: 99,
      event_sequence_no: index + 1,
      request_purpose,
      total_input_tokens: 999999,
      total_cached_tokens: 888888,
      total_output_tokens: 999999,
      cache_hit_rate: 888888 / 999999
    })
  }
  await receiveWs(harness, {
    ...mainMetadata,
    session_id: 'B',
    input_tokens: 500,
    work_sequence_no: 9,
    total_input_tokens: 999999,
    total_cached_tokens: 888888,
    total_output_tokens: 999999,
    cache_hit_rate: 888888 / 999999
  })
  assert.deepEqual(totals(), [1420, 360, 218, 360 / 1420])

  const scheduledMetadata = {
    input_tokens: 120,
    input_tokens_source: 'provider',
    context_window_tokens: 8192,
    max_output_tokens: 1024,
    work_sequence_no: 4,
    event_sequence_no: 1,
    request_purpose: 'main_dialogue',
    total_input_tokens: 1540,
    total_cached_tokens: 380,
    total_output_tokens: 225,
    cache_hit_rate: 380 / 1540
  }
  harness.sessions.find(item => item.session_id === 'A').llm_request_metadata = {
    ...currentMetadata(),
    total_input_tokens: scheduledMetadata.total_input_tokens,
    total_cached_tokens: scheduledMetadata.total_cached_tokens,
    total_output_tokens: scheduledMetadata.total_output_tokens,
    cache_hit_rate: scheduledMetadata.cache_hit_rate
  }

  await receiveWs(harness, {
    type: 'proactive_reply',
    session_id: 'A',
    source: 'scheduled_task',
    request_id: 'scheduled-request',
    request_ids: ['scheduled-request'],
    work_id: 'scheduled-work',
    work_sequence_no: 4,
    event_sequence_no: 1,
    llm_request_metadata: scheduledMetadata
  })
  assert.deepEqual(totals(), [1540, 380, 225, 380 / 1540])
  assert.equal(currentMetadata().work_sequence_no, 3)
  assert.deepEqual(sessionMetadata(), currentMetadata())
})

test('composed context usage stays scoped across estimated refreshes, reloads, and switches', async t => {
  const confirmedA = createContextMetadata({
    responseId: 'confirmed-a',
    workSequenceNo: 1,
    eventSequenceNo: 1
  })
  const oldDatabaseA = {
    ...confirmedA,
    input_tokens: 90,
    response_id: 'old-database-a',
    work_sequence_no: 1,
    event_sequence_no: 2
  }
  const bMetadata = createContextMetadata({
    inputTokens: 700,
    summaryTriggerTokens: 1000,
    channelId: 9,
    modelId: 'model-b',
    contextSummaryRevision: 1,
    contextContentRevision: 1,
    responseId: 'confirmed-b',
    workSequenceNo: 1,
    eventSequenceNo: 1
  })
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', { source: 'ws', llm_request_metadata: confirmedA }),
      createSession('B', { source: 'ws', llm_request_metadata: bMetadata })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const serverA = harness.sessions.find(item => item.session_id === 'A')

  await harness.flush()
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

  const estimatedA = {
    ...confirmedA,
    input_tokens: 999,
    input_tokens_source: 'estimated',
    summary_trigger_tokens: 300,
    channel_id: 8,
    model_id: 'model-b',
    protocol: 'chat_completions',
    response_id: 'estimated-a',
    work_sequence_no: 2,
    event_sequence_no: 1
  }
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...estimatedA
  })
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

  serverA.llm_request_metadata = oldDatabaseA
  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

  await selectSession(harness, 'B')
  assert.equal(chat.currentSessionId.value, 'B')
  assert.equal(chat.llmRequestMetadata.value.input_tokens, 700)
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 0.7)

  await selectSession(harness, 'A')
  assert.equal(chat.currentSessionId.value, 'A')
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

  const newerProviderA = {
    ...estimatedA,
    input_tokens: 180,
    input_tokens_source: 'provider',
    summary_trigger_tokens: 300,
    response_id: 'provider-a-new',
    work_sequence_no: 3,
    event_sequence_no: 1
  }
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...newerProviderA
  })
  assert.equal(chat.llmRequestMetadata.value.input_tokens, 180)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 180 / 300)
  assert.equal(getContextSummaryProgress(getSessionMetadata(chat)).ratio, 180 / 300)
})

for (const [failureName, createFailure] of [
  ['network failure', () => new Error('network failure')],
  ['business rejection', () => {
    const error = new Error('business rejection')
    error.response = { data: { code: 'request_rejected' } }
    return error
  }]
]) {
  test(`composed HTTP send preserves previous context usage after ${failureName}`, async t => {
    const confirmedMetadata = createContextMetadata({
      responseId: `http-confirmed-${failureName}`,
      workSequenceNo: 2,
      eventSequenceNo: 5
    })
    const recoveredMetadata = {
      ...confirmedMetadata,
      input_tokens: 180,
      summary_trigger_tokens: 300,
      response_id: `http-recovered-${failureName}`,
      work_sequence_no: 3,
      event_sequence_no: 1
    }
    let httpPending
    const harness = await createHarness(t, {
      sessions: [createSession('A', {
        source: 'http',
        stream_llm_request: false,
        is_context_summarizing: false,
        context_summary_revision: confirmedMetadata.context_summary_revision,
        context_content_revision: confirmedMetadata.context_content_revision,
        llm_request_metadata: confirmedMetadata
      })],
      httpResponses: ({ pending }) => {
        httpPending = pending
        return pending.promise
      },
      initialRoute: { path: '/', query: { session_id: 'A' } }
    })
    const { chat } = harness
    const serverA = harness.sessions.find(item => item.session_id === 'A')
    const assertPreviousUsage = () => {
      assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
      assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
      assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
      assert.equal(
        getContextSummaryProgress(chat.llmRequestMetadata.value).ratio,
        confirmedMetadata.input_tokens / confirmedMetadata.summary_trigger_tokens
      )
    }

    await harness.flush()
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')

    const sendPromise = chat.httpSend('request while context is stale')
    await harness.flush()
    assert.ok(httpPending)
    assertPreviousUsage()

    serverA.llm_request_metadata = { ...confirmedMetadata }
    await chat.loadSessions()
    await harness.flush()
    assertPreviousUsage()

    httpPending.resolve(createFailure())
    await sendPromise
    await harness.flush()
    assertPreviousUsage()

    serverA.llm_request_metadata = { ...recoveredMetadata }
    await chat.loadSessions()
    await harness.flush()
    assert.equal(chat.llmRequestMetadata.value.input_tokens, recoveredMetadata.input_tokens)
    assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
    assert.equal(
      getContextSummaryProgress(chat.llmRequestMetadata.value).ratio,
      recoveredMetadata.input_tokens / recoveredMetadata.summary_trigger_tokens
    )
  })
}

for (const [errorName, createErrorEvent] of [
  ['structured error', requestId => ({
    type: 'error',
    session_id: 'A',
    request_id: requestId,
    message: 'validation rejected'
  })],
  ['LLMResponse error choice', requestId => ({
    session_id: 'A',
    request_id: requestId,
    choices: [{
      message: { role: 'err', content: 'validation rejected' },
      finish_reason: 'error'
    }]
  })]
]) {
  test(`composed WS ${errorName} preserves previous context usage`, async t => {
    const confirmedA = createContextMetadata({
      responseId: `ws-confirmed-${errorName}`,
      workSequenceNo: 2,
      eventSequenceNo: 5
    })
    const confirmedB = createContextMetadata({
      inputTokens: 700,
      summaryTriggerTokens: 1000,
      contextSummaryRevision: confirmedA.context_summary_revision,
      contextContentRevision: confirmedA.context_content_revision,
      responseId: `ws-b-${errorName}`,
      workSequenceNo: 1,
      eventSequenceNo: 1
    })
    const newProvider = createContextMetadata({
      inputTokens: 180,
      summaryTriggerTokens: 300,
      contextSummaryRevision: confirmedA.context_summary_revision,
      contextContentRevision: confirmedA.context_content_revision,
      responseId: `ws-recovered-${errorName}`,
      workSequenceNo: 3,
      eventSequenceNo: 1
    })
    const totals = metadata => [
      metadata?.total_input_tokens,
      metadata?.total_cached_tokens,
      metadata?.total_output_tokens,
      metadata?.cache_hit_rate
    ]
    const confirmedTotals = totals(confirmedA)
    const harness = await createHarness(t, {
      sessions: [
        createSession('A', {
          source: 'ws',
          context_summary_revision: confirmedA.context_summary_revision,
          context_content_revision: confirmedA.context_content_revision,
          llm_request_metadata: confirmedA
        }),
        createSession('B', {
          source: 'ws',
          context_summary_revision: confirmedB.context_summary_revision,
          context_content_revision: confirmedB.context_content_revision,
          llm_request_metadata: confirmedB
        })
      ],
      initialRoute: { path: '/', query: { session_id: 'A' } }
    })
    const { chat } = harness
    const serverA = harness.sessions.find(item => item.session_id === 'A')
    const assertPreviousUsage = () => {
      assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
      assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
      assert.equal(chat.llmRequestMetadata.value.last_confirmed_context_usage.input_tokens, 123)
      assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
      assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)
      assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)
    }

    await harness.flush()
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

    const sendPromise = chat.wsSend('early rejection')
    await harness.flush()
    const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
    assert.ok(request)
    assertPreviousUsage()

    const errorEvent = createErrorEvent(request.request_id)
    await receiveWs(harness, errorEvent)
    await sendPromise
    assertPreviousUsage()

    await receiveWs(harness, { ...errorEvent })
    assertPreviousUsage()

    serverA.llm_request_metadata = { ...confirmedA }
    await chat.loadSessions()
    await harness.flush()
    assertPreviousUsage()

    await selectSession(harness, 'B')
    assert.equal(chat.currentSessionId.value, 'B')
    assert.equal(chat.llmRequestMetadata.value.input_tokens, 700)
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 0.7)

    await selectSession(harness, 'A')
    assert.equal(chat.currentSessionId.value, 'A')
    assertPreviousUsage()

    await receiveWs(harness, {
      type: 'llm_request_metadata',
      session_id: 'A',
      ...newProvider
    })
    assert.equal(chat.llmRequestMetadata.value.input_tokens, 180)
    assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 180 / 300)
    assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)
  })
}

test('composed cumulative-only independent responses preserve the main context', async t => {
  const mainMetadata = createMainMetadata()
  const createCumulativeMetadata = (
    totalInputTokens,
    totalCachedTokens,
    totalOutputTokens,
    workId,
    workSequenceNo,
    eventSequenceNo
  ) => ({
    total_input_tokens: totalInputTokens,
    total_cached_tokens: totalCachedTokens,
    total_output_tokens: totalOutputTokens,
    cache_hit_rate: totalCachedTokens / totalInputTokens,
    request_purpose: 'main_dialogue',
    work_id: workId,
    work_sequence_no: workSequenceNo,
    event_sequence_no: eventSequenceNo
  })
  const metadataEvent = (metadata, sessionId = 'A') => ({
    type: 'llm_request_metadata',
    session_id: sessionId,
    ...metadata
  })
  const doneEvent = (metadata, nested, responseId) => {
    const response = {
      work_id: metadata.work_id,
      response_id: responseId,
      choices: [{
        message: { role: 'assistant', content: responseId },
        finish_reason: 'stop'
      }]
    }
    return {
      type: 'done',
      session_id: 'A',
      work_id: metadata.work_id,
      work_sequence_no: metadata.work_sequence_no,
      event_sequence_no: metadata.event_sequence_no,
      ...(nested
        ? { response: { ...response, llm_request_metadata: metadata } }
        : { llm_request_metadata: metadata, response })
    }
  }
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', { source: 'ws', llm_request_metadata: mainMetadata }),
      createSession('B', { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const currentMetadata = () => chat.llmRequestMetadata.value
  const pickMainFields = metadata => Object.fromEntries([
    'input_tokens',
    'context_window_tokens',
    'max_output_tokens',
    'response_id',
    'output_tokens',
    'cached_tokens',
    'work_sequence_no',
    'event_sequence_no'
  ].map(field => [field, metadata[field]]))
  const pickTotals = metadata => Object.fromEntries([
    'total_input_tokens',
    'total_cached_tokens',
    'total_output_tokens',
    'cache_hit_rate'
  ].map(field => [field, metadata[field]]))
  const assertMainContext = expected => assert.deepEqual(
    pickMainFields(currentMetadata()),
    pickMainFields(expected)
  )
  const assertTotals = expected => assert.deepEqual(pickTotals(currentMetadata()), expected)

  await harness.flush()
  assertMainContext(mainMetadata)
  assertTotals(pickTotals(mainMetadata))

  const firstMetadata = createCumulativeMetadata(50100, 260, 204, 'confirmed-work', 3, 2)
  const nestedMetadata = createCumulativeMetadata(50200, 280, 210, 'agent-work', 4, 3)
  const topLevelMetadata = createCumulativeMetadata(50300, 300, 216, 'agent-work-top-level', 5, 4)
  const backgroundMetadata = createCumulativeMetadata(50400, 320, 223, 'background-work', 6, 1)
  const independentEvents = [
    [metadataEvent(firstMetadata), pickTotals(firstMetadata)],
    [doneEvent(nestedMetadata, true, 'confirmed-response'), pickTotals(nestedMetadata)],
    [doneEvent(topLevelMetadata, false, 'agent-work-response'), pickTotals(topLevelMetadata)],
    [{
      type: 'proactive_reply',
      session_id: 'A',
      source: 'background_task',
      work_id: backgroundMetadata.work_id,
      work_sequence_no: backgroundMetadata.work_sequence_no,
      event_sequence_no: backgroundMetadata.event_sequence_no,
      llm_request_metadata: backgroundMetadata
    }, pickTotals(backgroundMetadata)]
  ]

  for (const [event, expectedTotals] of independentEvents) {
    await receiveWs(harness, event)
    assertMainContext(mainMetadata)
    assertTotals(expectedTotals)
  }

  await receiveWs(harness, { ...independentEvents[3][0] })
  assertMainContext(mainMetadata)
  assertTotals(pickTotals(backgroundMetadata))

  const lateMetadata = createCumulativeMetadata(50150, 255, 205, 'late-work', 7, 1)
  await receiveWs(harness, metadataEvent(lateMetadata))
  assertMainContext(mainMetadata)
  assertTotals(pickTotals(backgroundMetadata))

  const nextMainMetadata = {
    ...createMainMetadata({ responseId: 'main-next-response', eventSequenceNo: 5 }),
    input_tokens: 51000,
    output_tokens: 190,
    cached_tokens: 240,
    total_input_tokens: 51400,
    total_cached_tokens: 360,
    total_output_tokens: 230,
    cache_hit_rate: 360 / 51400
  }
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    work_id: 'main-work-2',
    ...nextMainMetadata
  })
  assertMainContext(nextMainMetadata)
  assertTotals(pickTotals(nextMainMetadata))

  await selectSession(harness, 'B')
  const bMetadata = createCumulativeMetadata(700, 70, 12, 'background-b', 8, 1)
  await receiveWs(harness, metadataEvent(bMetadata, 'B'))
  assert.deepEqual(currentMetadata(), pickTotals(bMetadata))

  await selectSession(harness, 'A')
  assertMainContext(nextMainMetadata)
  assertTotals(pickTotals(nextMainMetadata))

  await receiveWs(harness, { type: 'connection_closed' })
  await receiveWs(harness, { type: 'connection_reopened' })
  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  await chat.loadSessions()
  await harness.flush()
  assertMainContext(nextMainMetadata)
  assertTotals(pickTotals(nextMainMetadata))
})

test('composed proactive task results do not overwrite main dialogue token metadata', async t => {
  const taskTotals = {
    total_input_tokens: 50120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_hit_rate: 270 / 50120
  }

  for (const source of ['scheduled_task', 'agent_work']) {
    for (const workSequenceNo of [1, 3]) {
      await t.test(`${source}, work_sequence_no=${workSequenceNo}`, async nested => {
        const mainMetadata = createMainMetadata()
        const taskMetadata = createTaskMetadata(source, workSequenceNo, taskTotals)
        const expectedMetadata = { ...mainMetadata, ...taskTotals }
        const taskContent = `${source} task result ${workSequenceNo}`
        const harness = await createHarness(nested, {
          sessions: [createSession('A', { source: 'ws', llm_request_metadata: mainMetadata })],
          initialRoute: { path: '/', query: { session_id: 'A' } }
        })
        const { chat } = harness
        const currentMetadata = () => chat.llmRequestMetadata.value

        await harness.flush()
        assert.deepEqual(snapshotMetadata(currentMetadata()), mainMetadata)
        assert.deepEqual(snapshotMetadata(getSessionMetadata(chat)), mainMetadata)

        await receiveWs(harness, {
          type: 'llm_request_metadata',
          session_id: 'A',
          ...mainMetadata
        })
        assert.deepEqual(snapshotMetadata(currentMetadata()), mainMetadata)

        const serverSession = harness.sessions.find(item => item.session_id === 'A')
        serverSession.llm_request_metadata = { ...expectedMetadata }
        serverSession.messages.push({
          id: 900 + workSequenceNo,
          role: 'assistant',
          response_id: taskMetadata.response_id,
          content: taskContent
        })

        const proactiveReply = createProactiveReply({
          source,
          workSequenceNo,
          llmRequestMetadata: taskMetadata
        })
        await receiveWs(harness, proactiveReply)
        assert.deepEqual(snapshotMetadata(currentMetadata()), expectedMetadata)
        assert.deepEqual(snapshotMetadata(getSessionMetadata(chat)), expectedMetadata)
        assert.equal(currentMetadata().total_input_tokens, 50120)
        assert.equal(chat.messages.value.filter(message => message.content === taskContent).length, 1)

        const metadataBeforeDuplicate = snapshotMetadata(currentMetadata())
        const messageCountBeforeDuplicate = chat.messages.value.length
        await receiveWs(harness, { ...proactiveReply })
        assert.deepEqual(snapshotMetadata(currentMetadata()), metadataBeforeDuplicate)
        assert.equal(currentMetadata().total_input_tokens, 50120)
        assert.equal(chat.messages.value.length, messageCountBeforeDuplicate)
        assert.equal(chat.messages.value.filter(message => message.content === taskContent).length, 1)

        const expectedNextMetadata = {
          ...expectedMetadata,
          input_tokens: 60000,
          input_tokens_source: 'provider',
          context_window_tokens: 131072,
          max_output_tokens: 8192,
          output_tokens: 191,
          cached_tokens: 300,
          response_id: 'main-next-response',
          turn: 3,
          work_sequence_no: 2,
          event_sequence_no: 5,
          request_purpose: 'main_dialogue',
          total_input_tokens: 110120,
          total_cached_tokens: 570,
          total_output_tokens: 218,
          cache_hit_rate: 570 / 110120
        }
        await receiveWs(harness, {
          type: 'llm_request_metadata',
          session_id: 'A',
          ...expectedNextMetadata
        })
        assert.deepEqual(snapshotMetadata(currentMetadata()), expectedNextMetadata)
        assert.deepEqual(snapshotMetadata(getSessionMetadata(chat)), expectedNextMetadata)
      })
    }
  }
})

test('composed proactive task metadata keeps main context at boundary inputs', async t => {
  const forbiddenRequestFields = [
    'input_tokens',
    'input_tokens_source',
    'context_window_tokens',
    'max_output_tokens',
    'output_tokens',
    'cached_tokens',
    'response_id',
    'turn',
    'work_sequence_no',
    'event_sequence_no'
  ]
  const assertTotalsOnly = (metadata, expectedTotals) => {
    assert.ok(metadata)
    assert.deepEqual({
      total_input_tokens: metadata.total_input_tokens,
      total_cached_tokens: metadata.total_cached_tokens,
      total_output_tokens: metadata.total_output_tokens,
      cache_hit_rate: metadata.cache_hit_rate
    }, expectedTotals)
    for (const field of forbiddenRequestFields) assert.equal(metadata[field], undefined)
  }
  const zeroTotals = {
    total_input_tokens: 0,
    total_cached_tokens: 0,
    total_output_tokens: 0,
    cache_hit_rate: null
  }

  for (const source of ['scheduled_task', 'agent_work']) {
    await t.test(`${source} boundary metadata`, async nested => {
      const zeroHarness = await createHarness(nested, {
        sessions: [createSession('A', { source: 'ws' })],
        initialRoute: { path: '/', query: { session_id: 'A' } }
      })
      await zeroHarness.flush()
      zeroHarness.sessions.find(item => item.session_id === 'A').llm_request_metadata = {
        ...zeroTotals
      }
      await receiveWs(zeroHarness, createProactiveReply({
        source,
        workSequenceNo: 1,
        llmRequestMetadata: createTaskMetadata(source, 1, zeroTotals)
      }))
      assertTotalsOnly(zeroHarness.chat.llmRequestMetadata.value, zeroTotals)
      assertTotalsOnly(getSessionMetadata(zeroHarness.chat), zeroTotals)

      const noTotalsMain = createMainMetadata({ responseId: `${source}-no-totals-main` })
      const noTotalsHarness = await createHarness(nested, {
        sessions: [createSession('A', { source: 'ws', llm_request_metadata: noTotalsMain })],
        initialRoute: { path: '/', query: { session_id: 'A' } }
      })
      await noTotalsHarness.flush()
      await receiveWs(noTotalsHarness, createProactiveReply({
        source,
        workSequenceNo: 3,
        llmRequestMetadata: createTaskMetadata(source, 3)
      }))
      assert.deepEqual(snapshotMetadata(noTotalsHarness.chat.llmRequestMetadata.value), noTotalsMain)
      assert.deepEqual(snapshotMetadata(getSessionMetadata(noTotalsHarness.chat)), noTotalsMain)

      const invalidMain = createMainMetadata({ responseId: `${source}-invalid-main` })
      const invalidHarness = await createHarness(nested, {
        sessions: [
          createSession('A', { source: 'ws', llm_request_metadata: invalidMain }),
          createSession('B', { source: 'ws' })
        ],
        initialRoute: { path: '/', query: { session_id: 'A' } }
      })
      await invalidHarness.flush()
      const invalidEvents = [
        {
          name: 'negative',
          workSequenceNo: 3,
          eventSequenceNo: 1,
          totals: {
            total_input_tokens: -1,
            total_cached_tokens: 0,
            total_output_tokens: 0,
            cache_hit_rate: 0
          }
        },
        {
          name: 'invalid',
          workSequenceNo: 4,
          eventSequenceNo: 1,
          totals: {
            total_input_tokens: 10,
            total_cached_tokens: 11,
            total_output_tokens: 999,
            cache_hit_rate: 1.1
          }
        },
        {
          name: 'foreign',
          sessionId: 'B',
          workSequenceNo: 99,
          eventSequenceNo: 1,
          totals: {
            total_input_tokens: 1000,
            total_cached_tokens: 999,
            total_output_tokens: 888,
            cache_hit_rate: 999 / 1000
          }
        }
      ]
      for (const event of invalidEvents) {
        await receiveWs(invalidHarness, createProactiveReply({
          ...event,
          source,
          requestId: `${source}-${event.name}-request`,
          workId: `${source}-${event.name}-work`,
          llmRequestMetadata: createTaskMetadata(source, event.workSequenceNo, event.totals)
        }))
        assert.deepEqual(snapshotMetadata(invalidHarness.chat.llmRequestMetadata.value), invalidMain)
        assert.deepEqual(snapshotMetadata(getSessionMetadata(invalidHarness.chat)), invalidMain)
      }
    })
  }
})
