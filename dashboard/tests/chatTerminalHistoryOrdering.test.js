import assert from 'node:assert/strict'
import test from 'node:test'
import {
  insertTerminalHistory,
  processStreamToolStart
} from '../src/composables/chat/terminalHistory.js'

const parseContent = message => {
  if (typeof message?.content !== 'string') return message?.content
  try { return JSON.parse(message.content) } catch { return null }
}

const toolCallMessage = (id, name) => ({
  id: `tool-call-${id}`,
  role: 'assistant',
  content: JSON.stringify({
    role: 'assistant',
    tool_calls: [{ id, name, arguments: '{}' }]
  })
})

const toolResultMessage = id => ({
  id: `tool-result-${id}`,
  role: 'tool',
  content: JSON.stringify({ role: 'tool', tool_call_id: id, content: 'ok' })
})

test('terminal history reconciliation keeps a missing later tool round before the already streamed final assistant', () => {
  const messagesRef = {
    value: [
      { ...toolCallMessage('write-1', 'write_file'), work_id: 'work-1' },
      { ...toolResultMessage('write-1'), work_id: 'work-1' },
      {
        id: 'user-stop',
        role: 'user',
        content: '停下',
        request_id: 'request-stop',
        work_id: 'work-1'
      },
      {
        id: 'assistant-final-live',
        role: 'assistant',
        content: '好的，我已经停下了。',
        response_id: 'response-final',
        request_id: 'request-stop',
        work_id: 'work-1',
        turn: 1
      }
    ]
  }

  insertTerminalHistory(
    messagesRef,
    [
      toolCallMessage('shell-2', 'execute_shell'),
      toolResultMessage('shell-2'),
      {
        id: 'assistant-final-persisted',
        role: 'assistant',
        content: '好的，我已经停下了。',
        response_id: 'response-final',
        request_id: 'request-stop',
        work_id: 'work-1',
        turn: 1,
        db_id: '42'
      }
    ],
    null,
    'request-stop',
    'work-1'
  )

  assert.deepEqual(
    messagesRef.value.map(message => message.id),
    [
      'tool-call-write-1',
      'tool-result-write-1',
      'user-stop',
      'tool-call-shell-2',
      'tool-result-shell-2',
      'assistant-final-live'
    ]
  )
  assert.deepEqual(parseContent(messagesRef.value[3]).tool_calls.map(call => call.id), ['shell-2'])
  assert.equal(parseContent(messagesRef.value[4]).tool_call_id, 'shell-2')
  assert.equal(messagesRef.value[5].response_id, 'response-final')
})

test('terminal history reconciliation does not duplicate an already streamed tool call', () => {
  const messagesRef = {
    value: [
      {
        ...toolCallMessage('shell-live', 'execute_shell'),
        response_id: 'response-shell-live',
        request_id: 'request-stop',
        work_id: 'work-1'
      },
      {
        ...toolResultMessage('shell-live'),
        response_id: 'response-shell-live',
        request_id: 'request-stop',
        work_id: 'work-1'
      },
      {
        id: 'assistant-final-live',
        role: 'assistant',
        content: '好的，我已经停下了。',
        response_id: 'response-final',
        request_id: 'request-stop',
        work_id: 'work-1',
        turn: 1
      }
    ]
  }

  insertTerminalHistory(
    messagesRef,
    [
      toolCallMessage('shell-live', 'execute_shell'),
      toolResultMessage('shell-live'),
      {
        id: 'assistant-final-persisted',
        role: 'assistant',
        content: '好的，我已经停下了。',
        response_id: 'response-final',
        request_id: 'request-stop',
        work_id: 'work-1',
        turn: 1,
        db_id: '42'
      }
    ],
    null,
    'request-stop',
    'work-1'
  )

  assert.deepEqual(
    messagesRef.value.map(message => message.id),
    ['tool-call-shell-live', 'tool-result-shell-live', 'assistant-final-live']
  )
  assert.equal(
    messagesRef.value.filter(message => parseContent(message)?.tool_calls?.some(call => call.id === 'shell-live')).length,
    1
  )
  assert.equal(
    messagesRef.value.filter(message => parseContent(message)?.tool_call_id === 'shell-live').length,
    1
  )
})

test('tool-call conversion preserves both streamed body and archived reasoning', () => {
  const messagesRef = { value: [{
    id: 'assistant-live',
    role: 'assistant',
    content: '**Executing Python script**',
    reasoning_content: 'persist me',
    response_id: 'response-1',
    request_id: 'request-1',
    work_id: 'work-1',
    turn: 1,
    db_id: 42
  }] }

  processStreamToolStart(
    messagesRef,
    { id: 'call-1', name: 'tool', arguments: '{}' },
    null,
    'response-1',
    'request-1',
    'work-1'
  )

  const converted = messagesRef.value[0]
  const content = parseContent(converted)
  assert.equal(messagesRef.value.length, 1)
  assert.equal(content.content, '**Executing Python script**')
  assert.deepEqual(content.tool_calls, [{ id: 'call-1', name: 'tool', arguments: '{}' }])
  assert.equal(converted.reasoning_content, 'persist me')
  assert.equal(converted.response_id, 'response-1')
  assert.equal(converted.request_id, 'request-1')
  assert.equal(converted.work_id, 'work-1')
  assert.equal(converted.turn, 1)
  assert.equal(converted.db_id, 42)
})

test('empty terminal history leaves live messages unchanged', () => {
  const initialMessages = [{ id: 'live', role: 'assistant', content: 'live' }]
  const messagesRef = { value: initialMessages }

  insertTerminalHistory(messagesRef, [], null, 'request-1', 'work-1')
  insertTerminalHistory(messagesRef, null, null, 'request-1', 'work-1')

  assert.strictEqual(messagesRef.value, initialMessages)
  assert.deepEqual(messagesRef.value, [{ id: 'live', role: 'assistant', content: 'live' }])
})

test('empty assistant body is retained when files or reasoning are present', () => {
  const messagesRef = { value: [] }
  const archivedFiles = {
    id: 'assistant-files',
    role: 'assistant',
    content: '',
    files: [{ name: 'result.txt' }]
  }
  const archivedReasoning = {
    id: 'assistant-reasoning',
    role: 'assistant',
    content: '  ',
    reasoning_content: 'reasoning survives'
  }

  insertTerminalHistory(messagesRef, [archivedFiles, archivedReasoning])

  assert.deepEqual(messagesRef.value.map(message => message.id), ['assistant-files', 'assistant-reasoning'])
  assert.deepEqual(messagesRef.value[0].files, [{ name: 'result.txt' }])
  assert.equal(messagesRef.value[1].reasoning_content, 'reasoning survives')
})

test('terminal history inserts tool messages before the matching thinking message', () => {
  const messagesRef = {
    value: [
      { id: 'user-1', role: 'user', content: 'run it' },
      { id: 'thinking-1', role: 'thinking', content: '', request_id: 'request-1' },
      { id: 'user-2', role: 'user', content: 'still waiting' }
    ]
  }

  insertTerminalHistory(
    messagesRef,
    [toolCallMessage('shell-1', 'execute_shell'), toolResultMessage('shell-1')],
    'thinking-1',
    'request-1'
  )

  assert.deepEqual(
    messagesRef.value.map(message => message.id),
    ['user-1', 'tool-call-shell-1', 'tool-result-shell-1', 'thinking-1', 'user-2']
  )
})

test('duplicate terminal history entries are inserted once and keep tool order', () => {
  const messagesRef = { value: [] }
  const history = [
    toolCallMessage('call-a', 'first'),
    toolCallMessage('call-a', 'first'),
    toolResultMessage('call-a'),
    toolResultMessage('call-a'),
    toolCallMessage('call-b', 'second'),
    toolResultMessage('call-b')
  ]

  insertTerminalHistory(messagesRef, history)
  insertTerminalHistory(messagesRef, history)

  assert.deepEqual(
    messagesRef.value.map(message => {
      const content = parseContent(message)
      return content?.tool_calls ? `call:${content.tool_calls[0].id}` : `result:${content.tool_call_id}`
    }),
    ['call:call-a', 'result:call-a', 'call:call-b', 'result:call-b']
  )
})

test('duplicate stream tool starts are idempotent', () => {
  const messagesRef = { value: [] }

  processStreamToolStart(messagesRef, { id: 'call-1', name: 'tool', arguments: '{}' }, null, 'response-1', 'request-1', 'work-1')
  processStreamToolStart(messagesRef, { id: 'call-1', name: 'updated-tool', arguments: '{"value":1}' }, null, 'response-1', 'request-1', 'work-1')

  const content = parseContent(messagesRef.value[0])
  assert.equal(messagesRef.value.length, 1)
  assert.deepEqual(content.tool_calls, [{ id: 'call-1', name: 'updated-tool', arguments: '{"value":1}' }])
})

test('multiple tool starts with one response share one assistant message', () => {
  const messagesRef = { value: [] }

  processStreamToolStart(messagesRef, { id: 'call-a', name: 'first', arguments: '{}' }, null, 'response-1', 'request-1', 'work-1')
  processStreamToolStart(messagesRef, { id: 'call-b', name: 'second', arguments: '{"n":2}' }, null, 'response-1', 'request-1', 'work-1')

  const content = parseContent(messagesRef.value[0])
  assert.equal(messagesRef.value.length, 1)
  assert.deepEqual(content.tool_calls.map(toolCall => toolCall.id), ['call-a', 'call-b'])
  assert.equal(messagesRef.value[0].response_id, 'response-1')
})
