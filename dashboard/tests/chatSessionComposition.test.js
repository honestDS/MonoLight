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

test('composed history preserves background result payloads and ordinary messages', async t => {
  const backgroundPayload = {
    type: 'background_tool_result',
    tool_name: 'lookup',
    content: 'background result'
  }
  const harness = await createHarness(t, {
    sessions: [createSession('A', {
      source: 'external',
      messages: [
        {
          id: 1,
          type: 'background_result',
          role: 'system',
          content: JSON.stringify(backgroundPayload)
        },
        { id: 2, type: 'message', role: 'user', content: 'ordinary message' }
      ]
    })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  await selectSession(harness, 'A')

  const background = chat.messages.value.find(message => message.db_id === 1 || message.id === 1)
  assert.ok(background)
  assert.equal(background.role, 'background_system')
  assert.equal(background.db_id, 1)
  assert.deepEqual(JSON.parse(background.content), backgroundPayload)

  const ordinary = chat.messages.value.find(message => message.content === 'ordinary message')
  assert.ok(ordinary)
  assert.equal(ordinary.role, 'user')
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
