import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import {
  findAssistantResponseReplacementIndex,
  isAssistantResponse,
  isPlainAssistantResponse,
  mergeAssistantResponseIntoList
} from '../src/utils/assistantResponseIdentity.js'

const processorSource = fs.readFileSync(new URL('../src/composables/chat/useMessageProcessor.js', import.meta.url), 'utf8').replace(/\r\n/g, '\n')

const extractInsertAiMessagesByThinking = () => {
  const startMarker = '  const _insertAiMessagesByThinking = '
  const start = processorSource.indexOf(startMarker)
  assert.notEqual(start, -1, 'expected _insertAiMessagesByThinking in useMessageProcessor.js')
  const endMarker = '\n\n  return {'
  const end = processorSource.indexOf(endMarker, start)
  assert.notEqual(end, -1, 'expected end of _insertAiMessagesByThinking in useMessageProcessor.js')
  const declaration = processorSource.slice(start + 2, end)
  const expression = declaration.replace(/^const _insertAiMessagesByThinking = /, '')
  return new Function(
    'findAssistantResponseReplacementIndex',
    'getToolMessageDedupeKeys',
    'isAssistantResponse',
    'isPlainAssistantResponse',
    'mergeAssistantResponseIntoList',
    'resolveAssistantDisplayContent',
    'findThinkingIndex',
    'findLastRelatedStreamMessageIndex',
    `return (${expression})`
  )(
    findAssistantResponseReplacementIndex,
    getToolMessageDedupeKeys,
    isAssistantResponse,
    isPlainAssistantResponse,
    mergeAssistantResponseIntoList,
    content => content ?? '',
    () => -1,
    findLastRelatedStreamMessageIndex
  )
}

const parseContent = message => {
  if (typeof message?.content !== 'string') return message?.content
  try { return JSON.parse(message.content) } catch { return null }
}

function getToolMessageDedupeKeys(message) {
  const content = parseContent(message)
  const keys = []
  const toolCalls = Array.isArray(message?.tool_calls)
    ? message.tool_calls
    : Array.isArray(content?.tool_calls)
      ? content.tool_calls
      : []
  for (const toolCall of toolCalls) {
    const id = toolCall?.id || toolCall?.function?.id
    if (id) keys.push(`tool_call:${id}`)
  }
  const toolCallId = message?.tool_call_id || content?.tool_call_id
  if ((message?.role === 'tool' || content?.role === 'tool') && toolCallId) {
    keys.push(`tool_result:${toolCallId}`)
  }
  return keys
}

function findLastRelatedStreamMessageIndex(messages, workId, requestId) {
  if (workId !== undefined && workId !== null && workId !== '') {
    const stableWorkId = String(workId)
    const index = messages.findLastIndex(message => message.role !== 'thinking' && String(message.work_id ?? '') === stableWorkId)
    if (index !== -1) return index
  }
  if (requestId === undefined || requestId === null || requestId === '') return -1
  return messages.findLastIndex(message => message.role !== 'thinking' && message.request_id === requestId)
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
  const insertAiMessagesByThinking = extractInsertAiMessagesByThinking()
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

  const terminalHistoryMessages = [
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
  ]

  insertAiMessagesByThinking(
    messagesRef,
    terminalHistoryMessages,
    null,
    'request-stop',
    'work-1'
  )

  const relevantOrder = messagesRef.value
    .map(message => {
      const keys = getToolMessageDedupeKeys(message)
      if (keys.includes('tool_call:shell-2')) return 'shell-call'
      if (keys.includes('tool_result:shell-2')) return 'shell-result'
      if (message.response_id === 'response-final') return 'final-assistant'
      return null
    })
    .filter(Boolean)

  assert.deepEqual(
    relevantOrder,
    ['shell-call', 'shell-result', 'final-assistant'],
    'terminal history reconciliation must preserve server history order'
  )
})
test('terminal history reconciliation does not duplicate an already streamed tool call', () => {
  const insertAiMessagesByThinking = extractInsertAiMessagesByThinking()
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

  insertAiMessagesByThinking(
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

  const shellCallCount = messagesRef.value.filter(message =>
    getToolMessageDedupeKeys(message).includes('tool_call:shell-live')
  ).length
  const shellResultCount = messagesRef.value.filter(message =>
    getToolMessageDedupeKeys(message).includes('tool_result:shell-live')
  ).length

  assert.equal(shellCallCount, 1, 'terminal history must not duplicate an already streamed tool call')
  assert.equal(shellResultCount, 1, 'terminal history must keep the existing tool result paired with the call')
})
