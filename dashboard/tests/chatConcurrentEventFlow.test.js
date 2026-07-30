import assert from 'node:assert/strict'
import test from 'node:test'

import { createHistoryMergeTracker } from '../src/composables/chat/historyMergeTracker.js'
import { createWorkLifecycleTracker } from '../src/composables/chat/workLifecycleTracker.js'
import { mergeAssistantResponseIntoList } from '../src/utils/assistantResponseIdentity.js'

const userMessage = requestId => ({
  id: `user-${requestId}`,
  role: 'user',
  content: requestId,
  request_id: requestId
})

const assistantMessage = (overrides = {}) => ({
  id: 'assistant-live',
  role: 'assistant',
  content: 'body',
  request_id: 'request-a',
  work_id: 'work-a',
  response_id: 'response-a1',
  ...overrides
})

const createEventDriver = () => {
  const lifecycle = createWorkLifecycleTracker()
  const history = createHistoryMergeTracker()
  let messages = [userMessage('request-a'), userMessage('request-b'), userMessage('request-c')]

  return {
    lifecycle(method, event) {
      messages = lifecycle[method](messages, event)
    },
    response(message, terminalEvent = null) {
      if (
        lifecycle.isWorkTerminal(message.work_id)
        && !lifecycle.isAcceptedTerminalEvent(terminalEvent)
      ) return
      messages = mergeAssistantResponseIntoList(messages, message)
    },
    beginHistory() {
      return history.begin()
    },
    applyHistory(token, snapshot) {
      if (!history.isLatest(token)) return
      snapshot.forEach(message => {
        messages = mergeAssistantResponseIntoList(messages, message)
      })
    },
    get messages() {
      return messages
    }
  }
}

test('concurrent work events converge without reviving terminal UI state or duplicating responses', () => {
  const driver = createEventDriver()

  driver.lifecycle('markInputQueued', {
    request_id: 'request-b', work_id: 'work-b', event_sequence_no: 1
  })
  driver.lifecycle('markInputQueued', {
    request_id: 'request-c', work_id: 'work-c', event_sequence_no: 1
  })
  driver.lifecycle('startAgentLoop', {
    work_id: 'work-a', response_id: 'response-a1', turn: 1, event_sequence_no: 1
  })
  driver.response(assistantMessage({ content: 'A first stream body' }))
  driver.response(assistantMessage({ id: 'assistant-a1-repeat', content: 'A first stream body' }))
  driver.lifecycle('stopAgentLoop', {
    work_id: 'work-a', response_id: 'response-a1', turn: 1, event_sequence_no: 2
  })
  driver.lifecycle('startAgentLoop', {
    work_id: 'work-a', response_id: 'response-a2', turn: 2, event_sequence_no: 3
  })
  driver.response(assistantMessage({
    id: 'assistant-a2-content', response_id: 'response-a2', content: 'A second stream body'
  }))

  driver.lifecycle('markInputsDequeued', {
    request_ids: ['request-b', 'request-b'], work_id: 'work-b', event_sequence_no: 2
  })
  driver.lifecycle('markInputsDequeued', {
    request_ids: ['request-c'], work_id: 'work-c', event_sequence_no: 2
  })

  const historyA = driver.beginHistory()
  const historyB = driver.beginHistory()
  const historyC = driver.beginHistory()
  driver.applyHistory(historyC, [assistantMessage({
    id: 'assistant-c-history', db_id: 33, request_id: 'request-c', work_id: 'work-c', response_id: 'response-c1', content: 'C history body'
  })])
  driver.applyHistory(historyB, [assistantMessage({
    id: 'assistant-b-stale-history', db_id: 22, request_id: 'request-b', work_id: 'work-b', response_id: 'response-b1', content: 'stale B history body'
  })])
  driver.applyHistory(historyA, [assistantMessage({
    id: 'assistant-a-stale-history', db_id: 11, response_id: 'response-a2', content: 'stale A history body'
  })])

  driver.lifecycle('startAgentLoop', {
    work_id: 'work-b', response_id: 'response-b1', turn: 1, event_sequence_no: 3
  })
  driver.response(assistantMessage({
    id: 'assistant-b-content', request_id: 'request-b', work_id: 'work-b', response_id: 'response-b1', content: 'B body'
  }))
  const errorB = {
    work_id: 'work-b', request_ids: ['request-b'], terminal: 'error', event_sequence_no: 4
  }
  const doneB = {
    work_id: 'work-b', request_ids: ['request-b'], terminal: 'done', event_sequence_no: 5
  }
  driver.lifecycle('finishWorkLifecycle', errorB)
  driver.lifecycle('finishWorkLifecycle', doneB)
  driver.response(assistantMessage({
    id: 'assistant-b-done', request_id: 'request-b', work_id: 'work-b', response_id: 'response-b1', content: 'B done body'
  }), doneB)
  driver.lifecycle('startAgentLoop', {
    work_id: 'work-b', response_id: 'response-b-late', turn: 2, event_sequence_no: 6
  })

  driver.lifecycle('startAgentLoop', {
    work_id: 'work-c', response_id: 'response-c1', turn: 1, event_sequence_no: 3
  })
  driver.response(assistantMessage({
    id: 'assistant-c-content', request_id: 'request-c', work_id: 'work-c', response_id: 'response-c1', content: 'C final body'
  }))
  driver.lifecycle('stopAgentLoop', {
    work_id: 'work-c', response_id: 'response-c1', turn: 1, event_sequence_no: 4
  })
  driver.lifecycle('finishWorkLifecycle', {
    work_id: 'work-c', request_ids: ['request-c'], event_sequence_no: 5
  })

  driver.lifecycle('stopAgentLoop', {
    work_id: 'work-a', response_id: 'response-a2', turn: 2, event_sequence_no: 4
  })
  driver.response(assistantMessage({
    id: 'assistant-a2-turn-end', db_id: 41, response_id: 'response-a2', content: 'A turn end body'
  }))
  const doneA = {
    work_id: 'work-a', request_ids: ['request-a'], event_sequence_no: 5
  }
  driver.lifecycle('finishWorkLifecycle', doneA)
  driver.response(assistantMessage({
    id: 'assistant-a2-done', db_id: 41, response_id: 'session-reply-work:work-a', content: 'A final body'
  }), doneA)
  driver.response(assistantMessage({
    id: 'assistant-a2-empty-history', db_id: 41, response_id: 'response-a2', content: ''
  }))
  driver.lifecycle('startAgentLoop', {
    work_id: 'work-a', response_id: 'response-a1', turn: 1, event_sequence_no: 6
  })
  driver.lifecycle('stopAgentLoop', {
    work_id: 'work-a', response_id: 'response-a1', turn: 1, event_sequence_no: 7
  })
  driver.response(assistantMessage({
    id: 'assistant-a1-late', response_id: 'response-a1', content: 'A first late body'
  }))
  driver.response(assistantMessage({
    id: 'assistant-a2-late-turn-end', db_id: 41, response_id: 'response-a2', content: 'A late turn end body'
  }))

  const users = driver.messages.filter(message => message.role === 'user')
  const assistants = driver.messages.filter(message => message.role === 'assistant')
  const responseIds = assistants.map(message => message.response_id)

  assert.deepEqual(users.map(message => message.request_id), ['request-a', 'request-b', 'request-c'])
  assert.equal(driver.messages.some(message => message.role === 'thinking'), false)
  assert.equal(driver.messages.some(message => message.status === 'queued'), false)
  assert.equal(new Set(responseIds).size, responseIds.length)
  assert.equal(assistants.filter(message => message.content === 'A final body').length, 1)
  assert.equal(assistants.find(message => message.response_id === 'response-a1').content, 'A first stream body')
  assert.equal(assistants.find(message => message.response_id === 'response-a2').content, 'A final body')
  assert.equal(assistants.find(message => message.response_id === 'response-b1').content, 'B body')
  assert.equal(assistants.find(message => message.response_id === 'response-c1').content, 'C final body')
  assert.equal(assistants.some(message => message.response_id === 'response-b-late'), false)
})
