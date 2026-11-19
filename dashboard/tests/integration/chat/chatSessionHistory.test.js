import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import { receiveWs, selectSession } from './sessionCompositionHarness.js'

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
