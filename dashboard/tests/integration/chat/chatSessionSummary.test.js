import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import { getContextSummaryProgress } from '../../../src/composables/chat/requestUsagePresentation.js'
import { createContextMetadata, createMainMetadata, receiveWs, selectSession } from './sessionCompositionHarness.js'

test('composed streaming context summaries preserve previous usage through failure and revision changes', async t => {
  const confirmedMetadata = createContextMetadata({
    responseId: 'confirmed-stream',
    workSequenceNo: 2,
    eventSequenceNo: 5
  })
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws', llm_request_metadata: confirmedMetadata })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

  await receiveWs(harness, {
    type: 'context_summary_start',
    session_id: 'A',
    work_id: 'summary-stream-failed',
    request_id: 'summary-request-failed',
    work_sequence_no: 3,
    event_sequence_no: 1
  })
  assert.equal(chat.isContextSummarizing.value, true)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(
    getContextSummaryProgress(chat.llmRequestMetadata.value, chat.isContextSummarizing.value).status,
    'summarizing'
  )
  assert.equal(chat.llmRequestMetadata.value.last_confirmed_context_usage.input_tokens, 123)
  assert.equal(chat.llmRequestMetadata.value.last_confirmed_context_usage.summary_trigger_tokens, 200)

  await receiveWs(harness, {
    type: 'context_summary_end',
    session_id: 'A',
    work_id: 'summary-stream-failed',
    request_id: 'summary-request-failed',
    work_sequence_no: 3,
    event_sequence_no: 2
  })
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

  await receiveWs(harness, {
    type: 'context_summary_start',
    session_id: 'A',
    work_id: 'summary-stream-stale',
    request_id: 'summary-request-stale',
    work_sequence_no: 4,
    event_sequence_no: 1
  })
  await receiveWs(harness, {
    type: 'context_summary_end',
    session_id: 'A',
    work_id: 'summary-stream-new',
    request_id: 'summary-request-new',
    work_sequence_no: 5,
    event_sequence_no: 1,
    context_summary_revision: 3,
    context_content_revision: 5
  })
  assert.equal(chat.isContextSummarizing.value, true)
  assert.equal(chat.llmRequestMetadata.value.last_confirmed_context_usage, undefined)

  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...confirmedMetadata,
    response_id: 'old-confirmation',
    work_sequence_no: 6,
    event_sequence_no: 1
  })
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(
    getContextSummaryProgress(chat.llmRequestMetadata.value, chat.isContextSummarizing.value).status,
    'summarizing'
  )

  const staleEnd = {
    type: 'context_summary_end',
    session_id: 'A',
    work_id: 'summary-stream-stale',
    request_id: 'summary-request-stale',
    work_sequence_no: 4,
    event_sequence_no: 2
  }
  await receiveWs(harness, staleEnd)
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'waiting')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, null)

  await receiveWs(harness, { ...staleEnd })
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'waiting')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, null)

  const recoveredMetadata = {
    ...confirmedMetadata,
    input_tokens: 180,
    summary_trigger_tokens: 300,
    channel_id: 8,
    model_id: 'model-b',
    response_id: 'provider-after-summary',
    work_sequence_no: 7,
    event_sequence_no: 1,
    context_summary_revision: 3,
    context_content_revision: 5
  }
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...recoveredMetadata
  })
  assert.equal(chat.llmRequestMetadata.value.input_tokens, 180)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 180 / 300)
})

for (const eventType of ['audit_tool_results_update', 'tool_results_update']) {
  test(`composed ${eventType} invalidates stale context usage through session refresh`, async t => {
    const confirmedA = createContextMetadata({
      responseId: `audit-confirmed-${eventType}`,
      workSequenceNo: 2,
      eventSequenceNo: 5
    })
    const confirmedB = createContextMetadata({
      inputTokens: 700,
      summaryTriggerTokens: 1000,
      contextSummaryRevision: 1,
      contextContentRevision: 1,
      responseId: `audit-b-${eventType}`,
      workSequenceNo: 1,
      eventSequenceNo: 1
    })
    const toolResult = {
      id: 420,
      type: 'tool_result',
      role: 'tool',
      content: JSON.stringify({
        role: 'tool',
        tool_call_id: `audit-call-${eventType}`,
        content: 'updated history result'
      })
    }
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

    await harness.flush()
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 123 / 200)

    serverA.context_summary_revision = 3
    serverA.context_content_revision = 5
    serverA.llm_request_metadata = { ...confirmedA }
    serverA.messages.push({ ...toolResult })
    const update = {
      type: eventType,
      session_id: 'A',
      work_id: `audit-history-work-${eventType}`,
      event_sequence_no: 10,
      messages: [{ ...toolResult }]
    }

    await receiveWs(harness, update)
    assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
    assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'waiting')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, null)
    assert.equal(
      chat.messages.value.filter(message => String(message.db_id ?? message.id) === '420').length,
      1
    )

    await receiveWs(harness, { ...update })
    await receiveWs(harness, { ...update, event_sequence_no: 9 })
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'waiting')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, null)
    assert.equal(
      chat.messages.value.filter(message => String(message.db_id ?? message.id) === '420').length,
      1
    )

    await selectSession(harness, 'B')
    assert.equal(chat.currentSessionId.value, 'B')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 0.7)

    await receiveWs(harness, { ...update, event_sequence_no: 11 })
    assert.equal(chat.currentSessionId.value, 'B')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, 0.7)

    await selectSession(harness, 'A')
    assert.equal(chat.currentSessionId.value, 'A')
    assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
    assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'waiting')
    assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, null)
    assert.equal(
      chat.messages.value.filter(message => String(message.db_id ?? message.id) === '420').length,
      1
    )
  })
}

test('composed non-stream session refresh keeps context summary usage scoped and resumable', async t => {
  const confirmedMetadata = {
    ...createMainMetadata({ responseId: 'confirmed-a', workSequenceNo: 2, eventSequenceNo: 5 }),
    channel_id: 7,
    model_id: 'model-a',
    protocol: 'responses',
    context_summary_revision: 2,
    context_content_revision: 4,
    summary_trigger_tokens: 100000
  }
  const recoveredMetadata = {
    ...confirmedMetadata,
    input_tokens: 1200,
    output_tokens: 190,
    cached_tokens: 80,
    response_id: 'recovered-a',
    context_summary_revision: 3,
    context_content_revision: 5,
    event_sequence_no: 6
  }
  const bMetadata = {
    ...confirmedMetadata,
    input_tokens: 7000,
    output_tokens: 70,
    cached_tokens: 70,
    response_id: 'main-b',
    turn: 1,
    work_sequence_no: 1,
    event_sequence_no: 1,
    context_summary_revision: 1,
    context_content_revision: 1,
    total_input_tokens: 7000,
    total_cached_tokens: 70,
    total_output_tokens: 70,
    cache_hit_rate: 70 / 7000
  }
  const totals = metadata => [
    metadata?.total_input_tokens,
    metadata?.total_cached_tokens,
    metadata?.total_output_tokens,
    metadata?.cache_hit_rate
  ]
  const confirmedTotals = totals(confirmedMetadata)
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', {
        source: 'http',
        stream_llm_request: false,
        is_context_summarizing: false,
        context_summary_revision: confirmedMetadata.context_summary_revision,
        context_content_revision: confirmedMetadata.context_content_revision,
        llm_request_metadata: confirmedMetadata
      }),
      createSession('B', {
        source: 'http',
        stream_llm_request: false,
        is_context_summarizing: false,
        context_summary_revision: bMetadata.context_summary_revision,
        context_content_revision: bMetadata.context_content_revision,
        llm_request_metadata: bMetadata
      })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const serverA = harness.sessions.find(item => item.session_id === 'A')

  await harness.flush()
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, confirmedMetadata.input_tokens)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
  assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)

  serverA.is_context_summarizing = true
  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.isContextSummarizing.value, true)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)
  assert.equal(
    getContextSummaryProgress(chat.llmRequestMetadata.value, chat.isContextSummarizing.value).status,
    'summarizing'
  )

  serverA.is_context_summarizing = false
  serverA.context_summary_revision = confirmedMetadata.context_summary_revision
  serverA.context_content_revision = confirmedMetadata.context_content_revision
  serverA.llm_request_metadata = { ...confirmedMetadata }
  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
  assert.equal(
    getContextSummaryProgress(chat.llmRequestMetadata.value).ratio,
    confirmedMetadata.input_tokens / confirmedMetadata.summary_trigger_tokens
  )

  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
  assert.equal(
    getContextSummaryProgress(chat.llmRequestMetadata.value).ratio,
    confirmedMetadata.input_tokens / confirmedMetadata.summary_trigger_tokens
  )

  await selectSession(harness, 'B')
  assert.equal(chat.currentSessionId.value, 'B')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).ratio, bMetadata.input_tokens / bMetadata.summary_trigger_tokens)

  await selectSession(harness, 'A')
  assert.equal(chat.currentSessionId.value, 'A')
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'previous')
  assert.equal(
    getContextSummaryProgress(chat.llmRequestMetadata.value).ratio,
    confirmedMetadata.input_tokens / confirmedMetadata.summary_trigger_tokens
  )

  serverA.is_context_summarizing = false
  serverA.context_summary_revision = recoveredMetadata.context_summary_revision
  serverA.context_content_revision = recoveredMetadata.context_content_revision
  serverA.llm_request_metadata = { ...confirmedMetadata }
  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, null)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'estimated')
  assert.equal(chat.llmRequestMetadata.value.context_summary_revision, confirmedMetadata.context_summary_revision)
  assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'waiting')

  serverA.llm_request_metadata = { ...recoveredMetadata }
  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, recoveredMetadata.input_tokens)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
  assert.equal(chat.llmRequestMetadata.value.context_summary_revision, recoveredMetadata.context_summary_revision)
  assert.equal(chat.llmRequestMetadata.value.event_sequence_no, recoveredMetadata.event_sequence_no)
  assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)
  assert.equal(getContextSummaryProgress(chat.llmRequestMetadata.value).status, 'ready')
  assert.equal(
    getContextSummaryProgress(chat.llmRequestMetadata.value).ratio,
    recoveredMetadata.input_tokens / recoveredMetadata.summary_trigger_tokens
  )

  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.llmRequestMetadata.value.input_tokens, recoveredMetadata.input_tokens)
  assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)

  await selectSession(harness, 'B')
  assert.equal(chat.currentSessionId.value, 'B')
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, bMetadata.input_tokens)
  assert.notEqual(chat.llmRequestMetadata.value.input_tokens, recoveredMetadata.input_tokens)
  assert.deepEqual(totals(chat.llmRequestMetadata.value), totals(bMetadata))

  await t.test('initial load preserves waiting usage while non-stream summarization is active', async nested => {
    const initialHarness = await createHarness(nested, {
      sessions: [createSession('A', {
        source: 'http',
        stream_llm_request: false,
        is_context_summarizing: true,
        context_summary_revision: recoveredMetadata.context_summary_revision,
        context_content_revision: recoveredMetadata.context_content_revision,
        llm_request_metadata: recoveredMetadata
      })],
      initialRoute: { path: '/', query: { session_id: 'A' } }
    })
    const { chat: initialChat } = initialHarness

    await initialHarness.flush()
    assert.equal(initialChat.currentSessionId.value, 'A')
    assert.equal(initialChat.isContextSummarizing.value, true)
    assert.equal(initialChat.llmRequestMetadata.value.input_tokens, null)
    assert.equal(initialChat.llmRequestMetadata.value.input_tokens_source, 'estimated')
    assert.deepEqual(totals(initialChat.llmRequestMetadata.value), confirmedTotals)
    assert.equal(
      getContextSummaryProgress(initialChat.llmRequestMetadata.value, initialChat.isContextSummarizing.value).status,
      'summarizing'
    )
  })
})
