import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createHarness, createSession } from './chatDraftHarness.js'

const selectSession = async (harness, id) => {
  const session = harness.chat.sessions.value.find(item => item.session_id === id)
  await harness.module.handleSelectSession(session)
  await harness.flush()
}

const receiveWs = async (harness, event) => {
  await harness.receiveWs(event)
  await harness.flush()
}

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
  assert.equal(chat.llmRequestMetadata.value.input_tokens, 123)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')

  const sessionMetadata = chat.sessions.value.find(item => item.session_id === 'A').llm_request_metadata
  assert.equal(sessionMetadata.input_tokens, 123)
  assert.equal(sessionMetadata.input_tokens_source, 'provider')
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
          cache_reported_input_tokens: 400,
          cache_reported_cached_tokens: 250,
          cache_hit_rate: 250 / 400,
          cache_coverage: 400 / 1000
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
    currentMetadata().total_output_tokens
  ]

  await harness.flush()
  assert.deepEqual(totals(), [1000, 250, 200])
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
    cache_reported_input_tokens: 448,
    cache_reported_cached_tokens: 270,
    cache_hit_rate: 270 / 448,
    cache_coverage: 448 / 1120
  }
  await receiveWs(harness, mainMetadata)
  assert.deepEqual(totals(), [1120, 270, 207])
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
    cache_reported_input_tokens: 568,
    cache_reported_cached_tokens: 360,
    cache_hit_rate: 360 / 568,
    cache_coverage: 568 / 1420
  }
  await receiveWs(harness, olderMetadata)
  assert.deepEqual(totals(), [1420, 360, 218])
  assert.equal(currentMetadata().input_tokens, 120)
  assert.equal(currentMetadata().context_window_tokens, 8192)
  assert.equal(currentMetadata().max_output_tokens, 1024)
  assert.equal(currentMetadata().work_sequence_no, 3)

  await receiveWs(harness, { ...olderMetadata })
  assert.deepEqual(totals(), [1420, 360, 218])
  await receiveWs(harness, {
    ...olderMetadata,
    input_tokens: 88,
    context_window_tokens: 4096,
    max_output_tokens: 512,
    event_sequence_no: 10,
    total_input_tokens: 1300,
    total_cached_tokens: 300,
    total_output_tokens: 210,
    cache_reported_input_tokens: 520,
    cache_reported_cached_tokens: 300,
    cache_hit_rate: 300 / 520,
    cache_coverage: 520 / 1300
  })
  assert.deepEqual(totals(), [1420, 360, 218])

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
      cache_reported_input_tokens: 900000,
      cache_reported_cached_tokens: 888888,
      cache_hit_rate: 888888 / 900000,
      cache_coverage: 900000 / 999999
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
    cache_reported_input_tokens: 900000,
    cache_reported_cached_tokens: 888888,
    cache_hit_rate: 888888 / 900000,
    cache_coverage: 900000 / 999999
  })
  assert.deepEqual(totals(), [1420, 360, 218])

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
    cache_reported_input_tokens: 616,
    cache_reported_cached_tokens: 380,
    cache_hit_rate: 380 / 616,
    cache_coverage: 616 / 1540
  }
  harness.sessions.find(item => item.session_id === 'A').llm_request_metadata = {
    ...currentMetadata(),
    total_input_tokens: scheduledMetadata.total_input_tokens,
    total_cached_tokens: scheduledMetadata.total_cached_tokens,
    total_output_tokens: scheduledMetadata.total_output_tokens,
    cache_reported_input_tokens: scheduledMetadata.cache_reported_input_tokens,
    cache_reported_cached_tokens: scheduledMetadata.cache_reported_cached_tokens,
    cache_hit_rate: scheduledMetadata.cache_hit_rate,
    cache_coverage: scheduledMetadata.cache_coverage
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
  assert.deepEqual(totals(), [1540, 380, 225])
  assert.equal(currentMetadata().work_sequence_no, 3)
  assert.deepEqual(sessionMetadata(), currentMetadata())
})

test('composed session applies only current-session todo revisions', async t => {
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', { source: 'ws' }),
      createSession('B', { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()

  const currentTodos = [
    { content: 'compose the session', status: 'in_progress' }
  ]
  const currentEvent = {
    type: 'todo_update',
    session_id: 'A',
    revision: 3,
    todos: currentTodos
  }
  await receiveWs(harness, currentEvent)

  assert.deepEqual(chat.currentTodoPlan.value?.todos, currentTodos)
  const firstTodos = JSON.stringify(chat.currentTodoPlan.value?.todos)

  await receiveWs(harness, { ...currentEvent, todos: [...currentTodos] })
  assert.equal(JSON.stringify(chat.currentTodoPlan.value?.todos), firstTodos)

  await receiveWs(harness, {
    ...currentEvent,
    revision: 2,
    todos: [{ content: 'stale', status: 'completed' }]
  })
  await receiveWs(harness, {
    ...currentEvent,
    session_id: 'B',
    revision: 99,
    todos: [{ content: 'foreign', status: 'completed' }]
  })
  assert.equal(JSON.stringify(chat.currentTodoPlan.value?.todos), firstTodos)

  await selectSession(harness, 'B')
  assert.equal(
    (chat.currentTodoPlan.value?.todos ?? []).some(todo => todo.content === 'compose the session'),
    false
  )
  await receiveWs(harness, {
    ...currentEvent,
    revision: 1,
    todos: [{ content: 'old A', status: 'in_progress' }]
  })
  assert.equal(
    (chat.currentTodoPlan.value?.todos ?? []).some(todo => todo.content === 'old A'),
    false
  )
})

test('composed websocket send merges turn_end into the existing tool message', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  const sendPromise = chat.wsSend('request')
  await harness.flush()

  const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
  assert.ok(request)
  const requestId = request.request_id
  await receiveWs(harness, {
    type: 'input_accepted',
    session_id: 'A',
    request_id: requestId
  })
  await sendPromise

  chat.messages.value.push(
    { id: 'plain-1', role: 'assistant', response_id: 'r1', content: 'partial one' },
    { id: 'plain-2', role: 'assistant', response_id: 'r1', content: 'partial two' },
    {
      id: 'tool-1',
      role: 'assistant',
      response_id: 'r1',
      content: JSON.stringify({
        role: 'assistant',
        tool_calls: [{ id: 'tool-1', function: { name: 'tool', arguments: '{}' } }],
        content: 'tool stream'
      })
    },
    { id: 'plain-3', role: 'assistant', response_id: 'r2', content: 'other response' }
  )

  const turnEnd = {
    type: 'turn_end',
    session_id: 'A',
    request_id: requestId,
    response_id: 'r1',
    work_id: 'w1',
    content: 'final body',
    message_id: 77,
    finish_reason: 'stop',
    reasoning_content: 'reasoning'
  }
  await receiveWs(harness, turnEnd)

  let r1Messages = chat.messages.value.filter(message => message.response_id === 'r1')
  assert.equal(r1Messages.length, 1)
  assert.equal(JSON.parse(r1Messages[0].content).content, 'final body')
  assert.equal(String(r1Messages[0].db_id), '77')
  assert.equal(r1Messages[0].reasoning_content, 'reasoning')
  assert.equal(
    chat.messages.value.find(message => message.response_id === 'r2').content,
    'other response'
  )

  await receiveWs(harness, { ...turnEnd })
  r1Messages = chat.messages.value.filter(message => message.response_id === 'r1')
  assert.equal(r1Messages.length, 1)
})

test('composed websocket reuses a work sequence without accepting late prior events', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws', show_tool_calls: true })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const stream = {
    session_id: 'A',
    request_id: 'request-reused',
    request_ids: ['request-reused'],
    work_id: 'work-reused',
    turn: 1
  }
  const toolCallId = 'call-reused'
  const receive = fields => receiveWs(harness, { ...stream, ...fields })

  await harness.flush()
  await receive({
    type: 'agent_loop_start',
    response_id: 'response-old',
    work_sequence_no: 5,
    event_sequence_no: 1
  })
  await receive({
    type: 'done',
    response_id: 'response-old',
    message_id: 101,
    work_sequence_no: 5,
    event_sequence_no: 2,
    response: {
      work_id: 'work-reused',
      response_id: 'response-old',
      message_id: 101,
      choices: [{ message: { role: 'assistant', content: '旧回复' }, finish_reason: 'stop' }]
    }
  })

  await receive({
    type: 'agent_loop_start',
    response_id: 'response-new-tool',
    work_sequence_no: 6,
    event_sequence_no: 3
  })
  await receive({ type: 'cancelled', work_sequence_no: 5, event_sequence_no: 4 })
  assert.equal(chat.messages.value.filter(message => message.role === 'thinking').length, 1)

  await receive({
    type: 'tool_start',
    response_id: 'response-new-tool',
    tool_call_id: toolCallId,
    name: 'lookup',
    arguments: '{}',
    work_sequence_no: 6,
    event_sequence_no: 5
  })
  await receive({
    type: 'content',
    response_id: 'response-old-late',
    content: '旧迟到正文',
    work_sequence_no: 5,
    event_sequence_no: 6
  })
  await receive({
    type: 'tool_end',
    response_id: 'response-new-tool',
    tool_call_id: toolCallId,
    name: 'lookup',
    result: { status: 'queued', task_id: 42 },
    work_sequence_no: 6,
    event_sequence_no: 7
  })
  await receive({
    type: 'tool_end',
    response_id: 'response-old-late',
    tool_call_id: toolCallId,
    name: 'lookup',
    result: { status: 'queued', task_id: 99 },
    work_sequence_no: 5,
    event_sequence_no: 8
  })
  const queuedResult = JSON.parse(chat.messages.value.find(message => message.role === 'tool').content).content
  assert.deepEqual(queuedResult, { status: 'queued', task_id: 42 })

  await receive({
    type: 'tool_end',
    response_id: 'response-new-tool',
    tool_call_id: toolCallId,
    name: 'lookup',
    result: '最终工具结果',
    work_sequence_no: 6,
    event_sequence_no: 9
  })
  await receive({
    type: 'done',
    response_id: 'response-new-final',
    message_id: 102,
    work_sequence_no: 6,
    event_sequence_no: 10,
    response: {
      work_id: 'work-reused',
      response_id: 'response-new-final',
      message_id: 102,
      choices: [{ message: { role: 'assistant', content: '新回复' }, finish_reason: 'stop' }]
    }
  })

  const messages = chat.messages.value
  const toolCalls = messages.filter(message => message.role === 'assistant' && chat.isToolCall(message))
  const toolResults = messages.filter(message => message.role === 'tool')
  assert.equal(toolCalls.length, 1)
  assert.equal(toolResults.length, 1)
  assert.equal(JSON.parse(toolResults[0].content).tool_call_id, toolCallId)
  assert.equal(JSON.parse(toolResults[0].content).content, '最终工具结果')
  assert.equal(messages.filter(message => message.response_id === 'response-old' && message.content === '旧回复').length, 1)
  assert.equal(messages.filter(message => message.response_id === 'response-new-final' && message.content === '新回复').length, 1)
  assert.equal(messages.some(message => message.content === '旧迟到正文'), false)
  assert.equal(messages.some(message => message.role === 'err'), false)
  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('composed websocket done respects background result visibility and preserves complete response', async t => {
  for (const showToolCalls of [true, false]) {
    await t.test(`show_tool_calls=${showToolCalls}`, async nested => {
      const files = [{ name: 'answer.txt', url: '/files/answer.txt' }]
      const backgroundHistory = [
        {
          id: 101,
          type: 'background_result',
          role: 'user',
          content: JSON.stringify({
            type: 'background_tool_result',
            task: 'visible user background result',
            tool_call: { id: 'background-user-call', name: 'background_lookup', arguments: {} }
          })
        },
        { id: 102, type: 'background_result', role: 'user', content: '{broken-json' },
        {
          id: 103,
          type: 'background_result',
          role: 'assistant',
          content: JSON.stringify({
            type: 'background_tool_result',
            task: 'visible assistant background result',
            tool_call: { id: 'background-assistant-call', name: 'background_lookup', arguments: {} }
          })
        },
        { id: 104, type: 'background_result', role: 'assistant', content: '{broken-json' }
      ]
      const internalHistory = [
        ...backgroundHistory,
        { id: 105, role: 'system', content: 'hidden system' },
        { id: 106, type: 'message', role: 'background_system', content: 'hidden legacy system' }
      ]
      const toolCall = {
        id: 201,
        role: 'assistant',
        content: '',
        tool_calls: [{
          id: 'call-1',
          type: 'function',
          function: { name: 'lookup', arguments: '{}' }
        }]
      }
      const toolResult = {
        id: 202,
        role: 'tool',
        tool_call_id: 'call-1',
        content: 'lookup result'
      }
      const finalHistoryMessage = { id: 203, role: 'assistant', content: 'complete reply' }
      const backgroundIds = new Set(backgroundHistory.map(message => String(message.id)))
      const hiddenIds = new Set(internalHistory
        .filter(message => ['system', 'background_system'].includes(message.role))
        .map(message => String(message.id)))
      const harness = await createHarness(nested, {
        sessions: [createSession('A', { source: 'ws', show_tool_calls: showToolCalls })],
        initialRoute: { path: '/', query: { session_id: 'A' } }
      })
      const { chat } = harness

      await harness.flush()
      const sendPromise = chat.wsSend('request')
      await harness.flush()
      const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
      assert.ok(request)
      await receiveWs(harness, {
        type: 'input_accepted',
        session_id: 'A',
        request_id: request.request_id
      })
      await sendPromise

      const doneEvent = {
        type: 'done',
        session_id: 'A',
        request_id: request.request_id,
        work_id: 'done-work',
        response_id: 'done-response',
        response: {
          work_id: 'done-work',
          response_id: 'done-response',
          message_id: 204,
          choices: [{
            message: { role: 'assistant', content: 'complete reply' },
            finish_reason: 'stop'
          }],
          history: [...internalHistory, toolCall, toolResult, finalHistoryMessage],
          files
        }
      }

      const assertDoneMessages = (expectedBackgroundCount = showToolCalls ? backgroundIds.size : 0) => {
        const messages = chat.messages.value
        assert.equal(
          messages.filter(message => backgroundIds.has(String(message.db_id ?? message.id))).length,
          expectedBackgroundCount
        )
        assert.equal(
          messages.some(message => hiddenIds.has(String(message.db_id ?? message.id))),
          false
        )
        assert.equal(
          messages.some(message => ['system', 'background_system'].includes(message.role)),
          false
        )

        const finalMessages = messages.filter(
          message => message.role === 'assistant' && message.content === 'complete reply'
        )
        assert.equal(finalMessages.length, 1)
        assert.deepEqual(finalMessages[0].files, files)
        assert.equal(String(finalMessages[0].db_id), '204')
        assert.equal(
          messages.filter(message => String(message.db_id ?? message.id) === '201').length,
          showToolCalls ? 1 : 0
        )
        assert.equal(
          messages.filter(message => String(message.db_id ?? message.id) === '202').length,
          showToolCalls ? 1 : 0
        )
      }

      const responseHistorySnapshot = JSON.stringify(doneEvent.response.history)
      await receiveWs(harness, doneEvent)
      assertDoneMessages()
      const nonBackgroundMessageCount = chat.messages.value.filter(
        message => !backgroundIds.has(String(message.db_id ?? message.id))
      ).length

      await receiveWs(harness, doneEvent)
      assert.equal(
        chat.messages.value.filter(
          message => !backgroundIds.has(String(message.db_id ?? message.id))
        ).length,
        nonBackgroundMessageCount
      )
      assertDoneMessages(
        chat.messages.value.filter(message => backgroundIds.has(String(message.db_id ?? message.id))).length
      )
      assert.equal(JSON.stringify(doneEvent.response.history), responseHistorySnapshot)
    })
  }
})

test('composed history respects background result visibility and preserves ordinary messages', async t => {
  for (const source of ['ws', 'http', 'external']) {
    for (const showToolCalls of [true, false]) {
      await t.test(`${source}, show_tool_calls=${showToolCalls}`, async nested => {
        const backgroundResults = ['user', 'assistant', 'system'].flatMap((role, roleIndex) => (
          ['valid', 'corrupt', 'empty'].map((contentKind, contentIndex) => ({
            id: 100 + roleIndex * 3 + contentIndex,
            type: 'background_result',
            role,
            content: contentKind === 'valid'
              ? JSON.stringify({
                type: 'background_tool_result',
                task: 'background result',
                tool_call: {
                  id: `background-call-${role}-${contentIndex}`,
                  name: 'background_lookup',
                  arguments: {}
                }
              })
              : contentKind === 'corrupt' ? '{broken-json' : ''
          }))
        ))
        const internalMessages = [
          ...backgroundResults,
          { id: 109, role: 'system', content: JSON.stringify({ type: 'background_tool_result' }) },
          { id: 110, role: 'system', content: '{broken-json' },
          { id: 111, role: 'system', content: '' },
          {
            id: 112,
            type: 'message',
            role: 'background_system',
            content: JSON.stringify({ type: 'background_tool_result' })
          }
        ]
        const ordinaryMessages = [
          { id: 200, role: 'user', content: 'ordinary user message' },
          {
            id: 201,
            role: 'user',
            content: JSON.stringify({
              type: 'background_result',
              role: 'system',
              content: 'ordinary JSON text'
            })
          },
          { id: 202, role: 'assistant', content: 'ordinary assistant message' },
          {
            id: 203,
            type: 'tool_result',
            role: 'tool',
            content: JSON.stringify({
              role: 'tool',
              tool_call_id: 'ordinary-tool',
              content: 'ordinary tool result'
            })
          }
        ]
        const backgroundIds = new Set(backgroundResults
          .filter(message => message.role !== 'system')
          .map(message => String(message.id)))
        const hiddenIds = new Set([
          ...backgroundResults.filter(message => message.role === 'system'),
          ...internalMessages.filter(message => ['system', 'background_system'].includes(message.role))
        ].map(message => String(message.id)))
        const ordinaryIds = new Set(ordinaryMessages.map(message => String(message.id)))
        const ordinaryHistorySnapshot = JSON.stringify([...internalMessages, ...ordinaryMessages])
        const harness = await createHarness(nested, {
          sessions: [createSession('A', {
            source,
            show_tool_calls: showToolCalls,
            messages: [...internalMessages, ...ordinaryMessages]
          })],
          initialRoute: { path: '/', query: { session_id: 'A' } }
        })
        const { chat } = harness

        const assertHistory = () => {
          const messages = chat.messages.value
          assert.equal(
            messages.filter(message => backgroundIds.has(String(message.db_id ?? message.id))).length,
            showToolCalls ? backgroundIds.size : 0
          )
          assert.equal(
            messages.some(message => hiddenIds.has(String(message.db_id ?? message.id))),
            false
          )
          assert.equal(
            messages.some(message => ['system', 'background_system'].includes(message.role)),
            false
          )
          for (const ordinary of ordinaryMessages) {
            const matches = messages.filter(
              message => String(message.db_id ?? message.id) === String(ordinary.id)
            )
            const expectedCount = ordinary.type === 'tool_result' && !showToolCalls ? 0 : 1
            assert.equal(matches.length, expectedCount)
            if (expectedCount === 1) {
              assert.equal(matches[0].role, ordinary.role)
              assert.equal(matches[0].content, ordinary.content)
            }
          }
          assert.equal(
            messages.filter(message => ordinaryIds.has(String(message.db_id ?? message.id))).length,
            showToolCalls ? ordinaryMessages.length : ordinaryMessages.length - 1
          )
          assert.equal(JSON.stringify([...internalMessages, ...ordinaryMessages]), ordinaryHistorySnapshot)
        }

        await harness.flush()
        await selectSession(harness, 'A')
        assertHistory()

        await selectSession(harness, 'A')
        assertHistory()

        await chat.reloadCurrentSessionHistory()
        await harness.flush()
        assertHistory()

        await chat.loadSessions()
        await harness.flush()
        assertHistory()
      })
    }
  }
})

test('composed HTTP send does not resubmit an outcome with unknown confirmation', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'http' })],
    initialRoute: { path: '/', query: { session_id: 'A' } },
    httpResponses: [{}]
  })
  const { chat } = harness

  await harness.flush()
  chat.inputMsg.value = 'once'
  await chat.httpSend()
  await harness.flush()

  assert.equal(harness.apiState.completionRequests.length, 1)
  assert.equal(chat.messages.value.filter(message => (
    message.role === 'user' && message.content === 'once'
  )).length, 1)
  assert.ok(harness.messages.some(entry => (
    entry.type === 'warning' && entry.value === 'chat.submit_outcome_unknown'
  )))
  assert.equal(chat.inputMsg.value, '')
  assert.equal(chat.messages.value.some(message => message.role === 'thinking'), false)

  await chat.loadSessions()
  await harness.flush()
  assert.equal(harness.apiState.completionRequests.length, 1)
})

test('composed websocket scroll history reconciles overlapping local messages', async t => {
  const initialHistory = Array.from({ length: 40 }, (_, index) => ({
    id: 1000 + index,
    role: 'user',
    content: `initial-${index}`
  }))
  const olderHistory = [
    { id: 100, role: 'user', content: 'older-first' },
    { id: 101, role: 'assistant', content: 'older-second' },
    { id: 102, role: 'user', content: 'same prompt', attachments: ['local-file.txt'] }
  ]
  const historyPage = [
    ...olderHistory,
    { id: 1200, role: 'user', content: 'same prompt', attachments: ['other-file.txt'] },
    { id: 1201, role: 'user', content: 'same prompt', attachments: ['local-file.txt'] },
    { id: 1202, role: 'user', content: 'same prompt' },
    {
      id: 301,
      role: 'assistant',
      response_id: 'response-1',
      content: 'assistant answer',
      reasoning_content: 'reasoning'
    },
    ...Array.from({ length: 13 }, (_, index) => ({
      id: 400 + index,
      role: 'user',
      content: `older-filler-${index}`
    }))
  ]
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: 'A' } },
    sessionsHistory: ({ index }) => index === 0 ? initialHistory : historyPage
  })
  const { chat } = harness

  await harness.flush()
  assert.ok(chat.messages.value.length >= initialHistory.length)
  assert.equal(chat.hasMore.value, true)

  const localUser = {
    id: 1700000000000,
    role: 'user',
    content: 'same prompt',
    request_id: 'request-local',
    attachments: ['local-file.txt']
  }
  chat.messages.value.push(
    localUser,
    {
      id: 'assistant-local',
      db_id: 301,
      role: 'assistant',
      response_id: 'response-1',
      content: 'assistant answer',
      reasoning_content: 'reasoning'
    }
  )
  chat.messageList.value = {
    scrollTop: 0,
    captureScrollAnchor: () => ({ top: 0 }),
    restoreScrollAnchor: async () => {},
    addEventListener: () => {},
    removeEventListener: () => {}
  }

  await chat.handleScroll()
  await harness.flush()
  assert.equal(chat.hasMore.value, true)

  assert.deepEqual(
    Array.from(chat.messages.value.slice(0, olderHistory.length), message => message.db_id),
    olderHistory.map(message => message.id)
  )

  const samePromptUsers = chat.messages.value.filter(message => (
    message.role === 'user' && message.content === 'same prompt'
  ))
  assert.equal(samePromptUsers.length, 4)
  const reconciledUser = samePromptUsers.find(message => message.id === localUser.id)
  assert.ok(reconciledUser)
  assert.equal(reconciledUser.request_id, localUser.request_id)
  assert.deepEqual(Array.from(reconciledUser.attachments), localUser.attachments)
  assert.equal(reconciledUser.db_id, 1201)
  const samePromptUsersByDbId = new Map(
    Array.from(samePromptUsers, message => [message.db_id, message])
  )
  assert.deepEqual(
    Array.from(samePromptUsersByDbId.get(102).attachments),
    ['local-file.txt']
  )
  assert.deepEqual(
    Array.from(samePromptUsersByDbId.get(1200).attachments),
    ['other-file.txt']
  )
  assert.deepEqual(
    Array.from(samePromptUsersByDbId.get(1201).attachments),
    ['local-file.txt']
  )
  assert.equal(samePromptUsersByDbId.get(1202).attachments, undefined)
  assert.deepEqual(
    Array.from(samePromptUsers, message => message.db_id).sort((left, right) => left - right),
    [102, 1200, 1201, 1202]
  )

  const reasoningMessages = chat.messages.value.filter(message => (
    message.role === 'assistant' && message.response_id === 'response-1'
  ))
  assert.equal(reasoningMessages.length, 1)
  assert.equal(reasoningMessages[0].reasoning_content, 'reasoning')

  const snapshot = messages => JSON.stringify(messages, (key, value) => (
    key === 'db_id' && value != null ? String(value) : value
  ))
  const firstPageSnapshot = snapshot(chat.messages.value)
  await chat.handleScroll()
  await harness.flush()
  assert.equal(snapshot(chat.messages.value), firstPageSnapshot)
  assert.equal(harness.apiState.historyRequests.length, 3)
})

test('composed initial history preserves local input and streaming response', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: 'A' } },
    sessionsHistory: ({ index, pending }) => index === 0 ? pending.promise : []
  })
  const { chat } = harness

  await harness.flush()
  const sendPromise = chat.wsSend('正在输入')
  await harness.flush()

  const request = harness.wsManager.sent.filter(item => item.type === 'chat').at(-1)
  assert.ok(request)
  await receiveWs(harness, {
    type: 'input_accepted',
    session_id: 'A',
    request_id: request.request_id
  })
  await sendPromise

  const streamEvent = {
    session_id: 'A',
    request_id: request.request_id,
    work_id: 'work-stream-1',
    response_id: 'response-stream-1',
    turn: 1
  }
  await receiveWs(harness, { type: 'agent_loop_start', ...streamEvent })
  await receiveWs(harness, { type: 'content', ...streamEvent, content: '当次流式正文' })
  await receiveWs(harness, { type: 'agent_loop_output', ...streamEvent })

  harness.apiState.historyRequests[0].pending.resolve([
    { id: 1, role: 'user', content: '旧问题' },
    { id: 2, role: 'assistant', content: '旧回复' }
  ])
  await harness.flush()

  const messages = chat.messages.value
  assert.deepEqual(
    Array.from(messages.slice(0, 2), message => String(message.db_id)),
    ['1', '2']
  )
  assert.equal(
    messages.filter(message => message.role === 'user' && message.content === '正在输入').length,
    1
  )
  const streamMessages = messages.filter(message => message.response_id === 'response-stream-1')
  assert.equal(streamMessages.length, 1)
  assert.equal(streamMessages[0].content, '当次流式正文')
})

test('composed initial history deduplicates database ids without deduplicating distinct responses', async t => {
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: 'A' } },
    sessionsHistory: ({ index }) => index === 0 ? [
      { id: 1, role: 'user', content: '重复问题' },
      { id: 1, role: 'user', content: '重复问题' },
      {
        id: 2,
        role: 'assistant',
        response_id: 'response-2',
        content: '相同正文',
        reasoning_content: '回复二推理'
      },
      {
        id: 2,
        role: 'assistant',
        response_id: 'response-2',
        content: '相同正文',
        reasoning_content: '回复二推理'
      },
      {
        id: 3,
        role: 'assistant',
        response_id: 'response-3',
        content: '相同正文',
        reasoning_content: '回复三推理'
      }
    ] : []
  })
  const { chat } = harness

  await harness.flush()

  const messages = chat.messages.value
  const messagesByDbId = new Map()
  for (const message of messages) {
    const dbId = String(message.db_id)
    messagesByDbId.set(dbId, [...(messagesByDbId.get(dbId) || []), message])
  }
  assert.equal(messagesByDbId.get('1').length, 1)
  assert.equal(messagesByDbId.get('2').length, 1)
  assert.equal(messagesByDbId.get('3').length, 1)

  const sameContentResponses = messages.filter(message => (
    message.role === 'assistant' && message.content === '相同正文'
  ))
  assert.equal(sameContentResponses.length, 2)
  assert.deepEqual(
    Array.from(sameContentResponses, message => [String(message.db_id), message.response_id]),
    [['2', 'response-2'], ['3', 'response-3']]
  )
})

const snapshotMetadata = value => value == null ? value : JSON.parse(JSON.stringify(value))

const createMainMetadata = ({
  responseId = 'main-response',
  turn = 2,
  workSequenceNo = 2,
  eventSequenceNo = 4
} = {}) => ({
  input_tokens: 50000,
  input_tokens_source: 'provider',
  context_window_tokens: 131072,
  max_output_tokens: 8192,
  output_tokens: 180,
  cached_tokens: 200,
  response_id: responseId,
  turn,
  work_sequence_no: workSequenceNo,
  event_sequence_no: eventSequenceNo,
  request_purpose: 'main_dialogue',
  total_input_tokens: 50000,
  total_cached_tokens: 250,
  total_output_tokens: 200,
  cache_reported_input_tokens: 20000,
  cache_reported_cached_tokens: 250,
  cache_hit_rate: 250 / 20000,
  cache_coverage: 20000 / 50000
})

const createTaskMetadata = (source, workSequenceNo, totals = null) => ({
  input_tokens: 120,
  input_tokens_source: 'provider',
  context_window_tokens: 8192,
  max_output_tokens: 1024,
  output_tokens: 7,
  cached_tokens: 17,
  response_id: `${source}-response-${workSequenceNo}`,
  turn: 11,
  work_sequence_no: workSequenceNo,
  event_sequence_no: 1,
  request_purpose: 'main_dialogue',
  ...(totals || {})
})

const createProactiveReply = ({
  source,
  sessionId = 'A',
  workSequenceNo,
  eventSequenceNo = 1,
  requestId = `${source}-request-${workSequenceNo}-${eventSequenceNo}`,
  workId = `${source}-work-${workSequenceNo}-${eventSequenceNo}`,
  llmRequestMetadata
}) => ({
  type: 'proactive_reply',
  session_id: sessionId,
  source,
  request_id: requestId,
  request_ids: [requestId],
  work_id: workId,
  work_sequence_no: workSequenceNo,
  event_sequence_no: eventSequenceNo,
  llm_request_metadata: llmRequestMetadata
})

const getSessionMetadata = chat => chat.sessions.value.find(item => item.session_id === 'A').llm_request_metadata

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
    cache_reported_input_tokens: totalInputTokens * 0.4,
    cache_reported_cached_tokens: totalCachedTokens,
    cache_hit_rate: totalCachedTokens / (totalInputTokens * 0.4),
    cache_coverage: (totalInputTokens * 0.4) / totalInputTokens,
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
    'cache_reported_input_tokens',
    'cache_reported_cached_tokens',
    'cache_hit_rate',
    'cache_coverage'
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
    cache_reported_input_tokens: 20560,
    cache_reported_cached_tokens: 360,
    cache_hit_rate: 360 / 20560,
    cache_coverage: 20560 / 51400
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
    cache_reported_input_tokens: 20048,
    cache_reported_cached_tokens: 270,
    cache_hit_rate: 270 / 20048,
    cache_coverage: 20048 / 50120
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
          cache_reported_input_tokens: 44048,
          cache_reported_cached_tokens: 570,
          cache_hit_rate: 570 / 44048,
          cache_coverage: 44048 / 110120
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
      cache_reported_input_tokens: metadata.cache_reported_input_tokens,
      cache_reported_cached_tokens: metadata.cache_reported_cached_tokens,
      cache_hit_rate: metadata.cache_hit_rate,
      cache_coverage: metadata.cache_coverage
    }, expectedTotals)
    for (const field of forbiddenRequestFields) assert.equal(metadata[field], undefined)
  }
  const zeroTotals = {
    total_input_tokens: 0,
    total_cached_tokens: 0,
    total_output_tokens: 0,
    cache_reported_input_tokens: 0,
    cache_reported_cached_tokens: 0,
    cache_hit_rate: null,
    cache_coverage: null
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
            cache_reported_input_tokens: 999,
            cache_reported_cached_tokens: 999,
            cache_hit_rate: 1,
            cache_coverage: 999 / 1000
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

test('composed reconnect refreshes persisted task totals without replaying task events', async t => {
  const mainMetadata = createMainMetadata()
  const taskTotals = {
    total_input_tokens: 50120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_reported_input_tokens: 20048,
    cache_reported_cached_tokens: 270,
    cache_hit_rate: 270 / 20048,
    cache_coverage: 20048 / 50120
  }
  const expectedMetadata = { ...mainMetadata, ...taskTotals }
  const taskContent = 'task completed while disconnected'
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws', llm_request_metadata: mainMetadata })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata
  })

  const sentBeforeReconnect = harness.wsManager.sent.length
  await receiveWs(harness, { type: 'connection_closed' })
  const serverSession = harness.sessions.find(item => item.session_id === 'A')
  serverSession.llm_request_metadata = { ...expectedMetadata }
  serverSession.messages.push({
    id: 1201,
    role: 'assistant',
    response_id: 'disconnected-task-response',
    content: taskContent
  })

  await receiveWs(harness, { type: 'connection_reopened' })
  const resumeRequest = harness.wsManager.sent
    .slice(sentBeforeReconnect)
    .find(item => item.type === 'resume' && item.session_id === 'A')
  assert.ok(resumeRequest)
  assert.equal(resumeRequest.history_message_id, 0)

  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedMetadata)
  assert.equal(chat.llmRequestMetadata.value.total_input_tokens, 50120)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, 50000)
  assert.equal(chat.llmRequestMetadata.value.context_window_tokens, 131072)
  assert.equal(chat.llmRequestMetadata.value.work_sequence_no, 2)
  assert.equal(chat.messages.value.filter(message => message.content === taskContent).length, 1)

  serverSession.llm_request_metadata = null
  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedMetadata)
  assert.equal(chat.llmRequestMetadata.value.total_input_tokens, 50120)
  assert.equal(chat.messages.value.filter(message => message.content === taskContent).length, 1)
  serverSession.llm_request_metadata = { ...expectedMetadata }

  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata,
    event_sequence_no: 3,
    total_input_tokens: 50000,
    total_cached_tokens: 250,
    total_output_tokens: 200,
    cache_reported_input_tokens: 20000,
    cache_reported_cached_tokens: 250,
    cache_hit_rate: 250 / 20000,
    cache_coverage: 20000 / 50000
  })
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedMetadata)
})

test('composed reconnect preserves newer metadata across stale session snapshots', async t => {
  const mainMetadata = createMainMetadata()
  const persistedTotals = {
    total_input_tokens: 50120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_reported_input_tokens: 20048,
    cache_reported_cached_tokens: 270,
    cache_hit_rate: 270 / 20048,
    cache_coverage: 20048 / 50120
  }
  const persistedMetadata = { ...mainMetadata, ...persistedTotals }
  const expectedNextMetadata = {
    ...persistedMetadata,
    input_tokens: 60000,
    output_tokens: 191,
    cached_tokens: 300,
    response_id: 'reconnect-next-response',
    turn: 3,
    event_sequence_no: 5,
    total_input_tokens: 110120,
    total_cached_tokens: 570,
    total_output_tokens: 218,
    cache_reported_input_tokens: 44048,
    cache_reported_cached_tokens: 570,
    cache_hit_rate: 570 / 44048,
    cache_coverage: 44048 / 110120
  }
  let holdNextListRefresh = false
  let capturedPending = null
  let listSnapshot
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws', llm_request_metadata: mainMetadata })],
    sessionsList: ({ pending, sessions }) => {
      if (holdNextListRefresh) {
        holdNextListRefresh = false
        capturedPending = pending
        return pending.promise
      }
      return listSnapshot ?? sessions
    },
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata
  })
  await receiveWs(harness, { type: 'connection_closed' })
  harness.sessions.find(item => item.session_id === 'A').llm_request_metadata = {
    ...persistedMetadata
  }
  await receiveWs(harness, { type: 'connection_reopened' })
  holdNextListRefresh = true
  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.ok(capturedPending)

  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...expectedNextMetadata
  })
  capturedPending.resolve([
    createSession('A', { source: 'ws', llm_request_metadata: persistedMetadata })
  ])
  await harness.flush()
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedNextMetadata)

  const refreshSnapshot = async snapshot => {
    listSnapshot = snapshot
    await chat.loadSessions()
    await harness.flush()
    assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedNextMetadata)
  }

  for (const snapshot of [
    [createSession('A', { source: 'ws', llm_request_metadata: persistedMetadata })],
    [createSession('A', { source: 'ws', llm_request_metadata: {} })],
    [createSession('A', {
      source: 'ws',
      llm_request_metadata: {
        total_input_tokens: -1,
        total_cached_tokens: 0,
        total_output_tokens: 0,
        cache_hit_rate: 0
      }
    })]
  ]) {
    await refreshSnapshot(snapshot)
  }

  await refreshSnapshot(new Error('session list temporarily unavailable'))
  await refreshSnapshot([
    createSession('A', { source: 'ws', llm_request_metadata: expectedNextMetadata })
  ])
})

test('composed reconnect keeps recovered metadata isolated across session switches', async t => {
  const mainMetadata = createMainMetadata()
  const taskTotals = {
    total_input_tokens: 50120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_reported_input_tokens: 20048,
    cache_reported_cached_tokens: 270,
    cache_hit_rate: 270 / 20048,
    cache_coverage: 20048 / 50120
  }
  const recoveredMetadata = { ...mainMetadata, ...taskTotals }
  const otherMetadata = {
    ...createMainMetadata({ responseId: 'other-response', workSequenceNo: 1, eventSequenceNo: 1 }),
    input_tokens: 7000,
    context_window_tokens: 16384,
    max_output_tokens: 4096,
    output_tokens: 70,
    cached_tokens: 70,
    total_input_tokens: 7000,
    total_cached_tokens: 70,
    total_output_tokens: 70,
    cache_reported_input_tokens: 2800,
    cache_reported_cached_tokens: 70,
    cache_hit_rate: 70 / 2800,
    cache_coverage: 2800 / 7000
  }
  let holdNextListRefresh = false
  let capturedPending = null
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', { source: 'ws', llm_request_metadata: mainMetadata }),
      createSession('B', { source: 'ws', llm_request_metadata: otherMetadata })
    ],
    sessionsList: ({ pending, sessions }) => {
      if (holdNextListRefresh) {
        holdNextListRefresh = false
        capturedPending = pending
        return pending.promise
      }
      return sessions
    },
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const sessionsSnapshot = (aMetadata = mainMetadata) => [
    createSession('A', { source: 'ws', llm_request_metadata: aMetadata }),
    createSession('B', { source: 'ws', llm_request_metadata: otherMetadata })
  ]

  await harness.flush()
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata
  })
  await receiveWs(harness, { type: 'connection_closed' })
  const serverSession = harness.sessions.find(item => item.session_id === 'A')
  serverSession.llm_request_metadata = { ...recoveredMetadata }
  serverSession.messages.push({
    id: 1301,
    role: 'assistant',
    response_id: 'isolated-task-response',
    content: 'isolated task result'
  })
  await receiveWs(harness, { type: 'connection_reopened' })
  holdNextListRefresh = true
  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.ok(capturedPending)

  await selectSession(harness, 'B')
  assert.equal(chat.currentSessionId.value, 'B')
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), otherMetadata)

  capturedPending.resolve(sessionsSnapshot(recoveredMetadata))
  await harness.flush()
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), otherMetadata)
  assert.deepEqual(
    snapshotMetadata(chat.sessions.value.find(item => item.session_id === 'A').llm_request_metadata),
    recoveredMetadata
  )

  await selectSession(harness, 'A')
  await harness.flush()
  assert.equal(chat.currentSessionId.value, 'A')
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), recoveredMetadata)
  assert.equal(chat.llmRequestMetadata.value.total_input_tokens, 50120)
  assert.equal(chat.messages.value.filter(message => message.content === 'isolated task result').length, 1)
})

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
    cache_reported_input_tokens: 2800,
    cache_reported_cached_tokens: 70,
    cache_hit_rate: 70 / 2800,
    cache_coverage: 2800 / 7000
  }
  const totals = metadata => [
    metadata?.total_input_tokens,
    metadata?.total_cached_tokens,
    metadata?.total_output_tokens,
    metadata?.cache_reported_input_tokens,
    metadata?.cache_reported_cached_tokens,
    metadata?.cache_hit_rate,
    metadata?.cache_coverage
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

  serverA.llm_request_metadata = { ...recoveredMetadata }
  await chat.loadSessions()
  await harness.flush()
  assert.equal(chat.isContextSummarizing.value, false)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, recoveredMetadata.input_tokens)
  assert.equal(chat.llmRequestMetadata.value.input_tokens_source, 'provider')
  assert.equal(chat.llmRequestMetadata.value.context_summary_revision, recoveredMetadata.context_summary_revision)
  assert.equal(chat.llmRequestMetadata.value.event_sequence_no, recoveredMetadata.event_sequence_no)
  assert.deepEqual(totals(chat.llmRequestMetadata.value), confirmedTotals)

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
  })
})
