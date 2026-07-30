import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createWorkLifecycleTracker,
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

test('stateful tracker blocks late lifecycle events without affecting other work', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = [userMessage('request-a'), userMessage('request-b'), userMessage('request-c')]

  messages = tracker.markInputQueued(messages, {
    request_id: 'request-b',
    work_id: 'work-b',
    event_sequence_no: 1
  })
  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-b', 'request-b'],
    work_id: 'work-b',
    event_sequence_no: 2
  })
  messages = tracker.markInputQueued(messages, {
    request_id: 'request-b',
    work_id: 'work-b',
    event_sequence_no: 1
  })
  assert.equal('status' in messages[1], false)

  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-c'],
    work_id: 'work-c',
    event_sequence_no: 1
  })
  messages = tracker.markInputQueued(messages, {
    request_id: 'request-c',
    work_id: 'work-c',
    event_sequence_no: 2
  })
  assert.equal('status' in messages[2], false)

  messages = tracker.startAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a1',
    turn: 1,
    event_sequence_no: 1
  })
  messages = tracker.stopAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a1',
    turn: 1,
    event_sequence_no: 2
  })
  messages = tracker.startAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a1',
    turn: 1,
    event_sequence_no: 1
  })
  assert.equal(messages.some(message => message.role === 'thinking'), false)

  messages = tracker.startAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a2',
    turn: 2,
    event_sequence_no: 3
  })
  messages = tracker.startAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a-late',
    turn: 1,
    event_sequence_no: 4
  })
  assert.equal(messages.at(-1).response_id, 'response-a2')

  messages = tracker.startAgentLoop(messages, {
    work_id: 'work-b',
    response_id: 'response-b1',
    turn: 1,
    event_sequence_no: 3
  })
  assert.equal(messages.filter(message => message.role === 'thinking').length, 2)

  messages = tracker.finishWorkLifecycle(messages, {
    work_id: 'work-a',
    request_ids: ['request-a', 'request-a'],
    event_sequence_no: 5
  })
  messages = tracker.finishWorkLifecycle(messages, {
    work_id: 'work-a',
    request_ids: ['request-a'],
    event_sequence_no: 5
  })
  messages = tracker.markInputQueued(messages, {
    request_id: 'request-a',
    work_id: 'work-a',
    event_sequence_no: 6
  })
  messages = tracker.startAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a3',
    turn: 3,
    event_sequence_no: 7
  })
  messages = tracker.stopAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a3',
    turn: 3,
    event_sequence_no: 8
  })

  assert.equal(messages.some(message => message.response_id === 'response-a2'), false)
  assert.equal(messages.some(message => message.response_id === 'response-a3'), false)
  assert.equal(messages.some(message => message.response_id === 'response-b1'), true)
  assert.equal(messages.find(message => message.request_id === 'request-a').status, undefined)
})

test('separate tracker instances do not share terminal or dequeue state', () => {
  const firstTracker = createWorkLifecycleTracker()
  const secondTracker = createWorkLifecycleTracker()
  const source = [userMessage('request-a')]

  firstTracker.finishWorkLifecycle(source, { work_id: 'work-a', request_ids: ['request-a'] })
  const queued = secondTracker.markInputQueued(source, { request_id: 'request-a', work_id: 'work-a' })
  const started = secondTracker.startAgentLoop(queued, { work_id: 'work-a', response_id: 'response-a' })

  assert.equal(queued[0].status, 'queued')
  assert.equal(started.at(-1).role, 'thinking')
})

test('records only the first accepted terminal event for each work', () => {
  const tracker = createWorkLifecycleTracker()
  const firstTerminalEvent = {
    work_id: 'work-a',
    request_ids: ['request-a'],
    terminal: 'error',
    event_sequence_no: 2
  }
  const competingTerminalEvent = {
    work_id: 'work-a',
    request_ids: ['request-a'],
    terminal: 'done',
    event_sequence_no: 3
  }
  let messages = tracker.startAgentLoop([userMessage('request-a')], {
    work_id: 'work-a', response_id: 'response-a', event_sequence_no: 1
  })

  messages = tracker.finishWorkLifecycle(messages, firstTerminalEvent)
  messages = tracker.finishWorkLifecycle(messages, competingTerminalEvent)

  assert.equal(tracker.isWorkTerminal('work-a'), true)
  assert.equal(tracker.isWorkTerminal('work-b'), false)
  assert.equal(tracker.isAcceptedTerminalEvent(firstTerminalEvent), true)
  assert.equal(tracker.isAcceptedTerminalEvent(competingTerminalEvent), false)
  assert.equal(messages.some(message => message.role === 'thinking'), false)

  tracker.resetWorkLifecycle(messages)
  assert.equal(tracker.isWorkTerminal('work-a'), false)
  assert.equal(tracker.isAcceptedTerminalEvent(firstTerminalEvent), false)
})
