import assert from 'node:assert/strict'
import test from 'node:test'
import {
  finishWorkLifecycle,
  markInputQueued,
  markInputsDequeued,
  startAgentLoop,
  stopAgentLoop
} from '../src/composables/chat/workLifecycleTracker.js'

const userMessage = requestId => ({
  id: `user_${requestId}`,
  role: 'user',
  content: requestId,
  request_id: requestId
})

test('tracks queued inputs and agent loop markers independently', () => {
  let messages = [userMessage('request-a'), userMessage('request-b')]

  messages = markInputQueued(messages, { request_id: 'request-b', work_id: 'work-b' })
  assert.equal(messages[1].status, 'queued')
  assert.equal(messages[1].work_id, 'work-b')

  messages = startAgentLoop(messages, { work_id: 'work-a', response_id: 'response-a', turn: 1 })
  assert.deepEqual(messages.at(-1), {
    id: 'thinking_work-a_response-a',
    role: 'thinking',
    content: 'Thinking...',
    work_id: 'work-a',
    response_id: 'response-a',
    turn: 1
  })

  messages = stopAgentLoop(messages, { work_id: 'work-a', response_id: 'response-a' })
  assert.equal(messages.some(message => message.role === 'thinking'), false)
  assert.equal(messages[1].status, 'queued')

  messages = markInputsDequeued(messages, { request_ids: ['request-b'], work_id: 'work-b' })
  assert.equal('status' in messages[1], false)

  messages = startAgentLoop(messages, { work_id: 'work-a', response_id: 'response-b', turn: 2 })
  assert.equal(messages.at(-1).role, 'thinking')
  assert.equal(messages.at(-1).response_id, 'response-b')

  messages = finishWorkLifecycle(messages, { work_id: 'work-a', request_ids: ['request-a'] })
  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('ignores incomplete or unrelated lifecycle events and is idempotent', () => {
  let messages = [userMessage('request-a'), userMessage('request-b')]

  messages = markInputQueued(messages, { request_id: 'request-b', work_id: 2 })
  messages = markInputQueued(messages, { request_id: 'request-b', work_id: 2 })
  assert.equal(messages.filter(message => message.status === 'queued').length, 1)

  messages = startAgentLoop(messages, { work_id: 1, response_id: 'response-a' })
  const repeatedStart = startAgentLoop(messages, { work_id: 1, response_id: 'response-a' })
  assert.deepEqual(repeatedStart, messages)

  messages = startAgentLoop(repeatedStart, { work_id: '1', response_id: 'response-a' })
  assert.equal(messages.filter(message => message.role === 'thinking').length, 1)

  const unchangedByWrongOutput = stopAgentLoop(messages, { work_id: 'other-work', response_id: 'response-a' })
  assert.equal(unchangedByWrongOutput.filter(message => message.role === 'thinking').length, 1)

  const unchangedByWrongFinish = finishWorkLifecycle(unchangedByWrongOutput, {
    work_id: 'other-work',
    request_ids: ['request-a']
  })
  assert.equal(unchangedByWrongFinish.filter(message => message.role === 'thinking').length, 1)

  const unchangedByMissingWork = finishWorkLifecycle(unchangedByWrongFinish, { request_ids: [] })
  assert.equal(unchangedByMissingWork.filter(message => message.role === 'thinking').length, 1)
  assert.equal(unchangedByMissingWork.find(message => message.request_id === 'request-b').status, 'queued')

  messages = stopAgentLoop(unchangedByMissingWork, { work_id: 1, response_id: 'response-a' })
  const repeatedOutput = stopAgentLoop(messages, { work_id: 1, response_id: 'response-a' })
  assert.deepEqual(repeatedOutput, messages)
  assert.equal(repeatedOutput.some(message => message.role === 'thinking'), false)
})
