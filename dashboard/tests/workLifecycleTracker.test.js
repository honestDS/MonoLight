import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createWorkLifecycleTracker,
  finishWorkLifecycle,
  markInputQueued,
  markInputsDequeued,
  shouldApplyOwnProactiveReply,
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

test('tracks pending request thinking through dequeue, agent loop, and output', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = tracker.startRequestLifecycle([], { request_id: 'request-a' })
  const repeatedStart = tracker.startRequestLifecycle(messages, { request_id: 'request-a' })

  assert.equal(messages.length, 1)
  assert.deepEqual(repeatedStart, messages)
  assert.equal(messages[0].role, 'thinking')
  assert.equal(messages[0].request_id, 'request-a')
  assert.deepEqual(messages[0].request_ids, ['request-a'])

  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-a'],
    work_id: 'work-a',
    event_sequence_no: 1
  })
  assert.equal(messages.length, 1)
  assert.equal(messages[0].work_id, 'work-a')

  messages = tracker.startAgentLoop(messages, {
    request_id: 'request-a',
    work_id: 'work-a',
    response_id: 'response-a',
    turn: 1,
    event_sequence_no: 2
  })
  assert.equal(messages.filter(message => message.role === 'thinking').length, 1)
  assert.equal(messages[0].work_id, 'work-a')
  assert.equal(messages[0].response_id, 'response-a')
  assert.equal(messages[0].turn, 1)

  messages = tracker.stopAgentLoop(messages, {
    work_id: 'work-a',
    response_id: 'response-a',
    event_sequence_no: 3
  })
  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('finishes pending non-streaming request thinking without a work id', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = tracker.startRequestLifecycle([], { request_id: 'request-a' })

  messages = tracker.finishWorkLifecycle(messages, { request_ids: ['request-a'] })

  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('queued input removes only its unbound pending thinking and updates the user message', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = [userMessage('request-a')]
  messages = tracker.startRequestLifecycle(messages, { request_id: 'request-a' })
  messages.push({
    id: 'thinking-work-b',
    role: 'thinking',
    content: 'Thinking...',
    work_id: 'work-b',
    request_id: 'request-b'
  })

  messages = tracker.markInputQueued(messages, {
    request_id: 'request-a',
    work_id: 'work-a',
    event_sequence_no: 1
  })

  assert.equal(messages.some(message => message.request_id === 'request-a' && message.role === 'thinking'), false)
  assert.equal(messages.find(message => message.role === 'user').status, 'queued')
  assert.equal(messages.find(message => message.role === 'user').work_id, 'work-a')
  assert.equal(messages.some(message => message.work_id === 'work-b' && message.role === 'thinking'), true)
})

test('single request id terminal cleanup prevents pending thinking resurrection', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = tracker.startRequestLifecycle([], { request_id: 'request-a' })

  messages = tracker.finishWorkLifecycle(messages, { request_id: 'request-a' })
  const afterStaleStart = tracker.startRequestLifecycle(messages, { request_id: 'request-a' })

  assert.equal(messages.some(message => message.role === 'thinking'), false)
  assert.equal(afterStaleStart.some(message => message.role === 'thinking'), false)
})

test('keeps concurrent pending requests isolated during agent loop takeover', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = tracker.startRequestLifecycle([], { request_id: 'request-a' })
  messages = tracker.startRequestLifecycle(messages, { request_id: 'request-b' })

  messages = tracker.startAgentLoop(messages, {
    request_id: 'request-c',
    work_id: 'work-c',
    response_id: 'response-c',
    turn: 1,
    event_sequence_no: 1
  })
  assert.equal(messages.find(message => message.request_id === 'request-a').work_id, undefined)
  assert.equal(messages.find(message => message.request_id === 'request-b').work_id, undefined)
  assert.equal(messages.find(message => message.response_id === 'response-c').work_id, 'work-c')

  messages = tracker.finishWorkLifecycle(messages, { request_ids: ['request-a'] })
  assert.equal(messages.some(message => message.request_id === 'request-a'), false)
  assert.equal(messages.some(message => message.request_id === 'request-b'), true)

  messages = tracker.startAgentLoop(messages, {
    work_id: 'work-b',
    response_id: 'response-b',
    turn: 1,
    event_sequence_no: 1
  })
  const requestBThinking = messages.find(message => message.response_id === 'response-b')
  assert.equal(requestBThinking.work_id, 'work-b')
  assert.equal(requestBThinking.request_id, undefined)
  assert.equal(messages.find(message => message.response_id === 'response-c').work_id, 'work-c')
})

test('does not resurrect pending thinking after dequeue or terminal lifecycle events', () => {
  const dequeuedTracker = createWorkLifecycleTracker()
  let dequeuedMessages = dequeuedTracker.startRequestLifecycle([], { request_id: 'request-a' })
  dequeuedMessages = dequeuedTracker.markInputsDequeued(dequeuedMessages, {
    request_ids: ['request-a'],
    work_id: 'work-a',
    event_sequence_no: 1
  })
  const afterDequeuedStart = dequeuedTracker.startRequestLifecycle(dequeuedMessages, {
    request_id: 'request-a'
  })

  assert.deepEqual(afterDequeuedStart, dequeuedMessages)

  const terminalTracker = createWorkLifecycleTracker()
  let terminalMessages = terminalTracker.startRequestLifecycle([], { request_id: 'request-b' })
  terminalMessages = terminalTracker.finishWorkLifecycle(terminalMessages, {
    work_id: 'work-b',
    request_ids: ['request-b'],
    event_sequence_no: 1
  })
  const afterTerminalStart = terminalTracker.startRequestLifecycle(terminalMessages, {
    request_id: 'request-b',
    work_id: 'work-b'
  })

  assert.equal(afterTerminalStart.some(message => message.role === 'thinking'), false)
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

test('applies an own proactive reply only when it is the accepted terminal event', () => {
  const tracker = createWorkLifecycleTracker()
  const proactiveReply = {
    work_id: 'work-a',
    request_ids: ['request-a'],
    event_sequence_no: 1
  }
  const done = {
    work_id: 'work-a',
    request_ids: ['request-a'],
    event_sequence_no: 2
  }
  const lateProactiveReply = {
    work_id: 'work-a',
    request_ids: ['request-a'],
    event_sequence_no: 3
  }

  let messages = [userMessage('request-a')]
  messages = tracker.finishWorkLifecycle(messages, proactiveReply)

  assert.equal(shouldApplyOwnProactiveReply(tracker, proactiveReply, 'request-a'), true)

  messages = tracker.finishWorkLifecycle(messages, done)
  assert.equal(shouldApplyOwnProactiveReply(tracker, done, 'request-a'), false)
  assert.equal(shouldApplyOwnProactiveReply(tracker, lateProactiveReply, 'request-a'), false)
})

test('does not apply an own proactive reply after done wins the terminal event', () => {
  const tracker = createWorkLifecycleTracker()
  const done = {
    work_id: 'work-a',
    request_ids: ['request-a'],
    event_sequence_no: 1
  }
  const lateProactiveReply = {
    work_id: 'work-a',
    request_ids: ['request-a'],
    event_sequence_no: 2
  }

  tracker.finishWorkLifecycle([userMessage('request-a')], done)

  assert.equal(shouldApplyOwnProactiveReply(tracker, lateProactiveReply, 'request-a'), false)
})

test('requires the current request id but accepts own proactive replies without work ids', () => {
  const tracker = createWorkLifecycleTracker()

  assert.equal(shouldApplyOwnProactiveReply(tracker, {
    work_id: 'work-a',
    request_ids: ['other-request']
  }, 'request-a'), false)
  assert.equal(shouldApplyOwnProactiveReply(tracker, {
    request_ids: ['request-a']
  }, 'request-a'), true)
})

test('cleans up a migrated thinking marker by request id without a work id', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = tracker.startRequestLifecycle([], { request_id: 'request-a' })

  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-a'],
    work_id: 'work-a'
  })
  messages = tracker.startAgentLoop(messages, {
    request_id: 'request-a',
    work_id: 'work-a',
    response_id: 'response-a'
  })

  assert.deepEqual(messages[0].request_ids, ['request-a'])
  messages = tracker.finishWorkLifecycle(messages, { request_id: 'request-a' })

  assert.equal(messages.some(message => message.role === 'thinking'), false)
})

test('cleans up only the matching request marker without a work id', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = tracker.startRequestLifecycle([], { request_id: 'request-a' })
  messages = tracker.startRequestLifecycle(messages, { request_id: 'request-b' })
  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-a'],
    work_id: 'work-a'
  })
  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-b'],
    work_id: 'work-b'
  })
  messages = tracker.startAgentLoop(messages, {
    request_id: 'request-a',
    work_id: 'work-a',
    response_id: 'response-a'
  })
  messages = tracker.startAgentLoop(messages, {
    request_id: 'request-b',
    work_id: 'work-b',
    response_id: 'response-b'
  })

  assert.deepEqual(messages.find(message => message.response_id === 'response-a').request_ids, ['request-a'])
  assert.deepEqual(messages.find(message => message.response_id === 'response-b').request_ids, ['request-b'])
  messages = tracker.finishWorkLifecycle(messages, { request_id: 'request-a' })

  assert.equal(messages.some(message => message.response_id === 'response-a'), false)
  assert.equal(messages.some(message => message.response_id === 'response-b'), true)
})

test('cleans up only the matching work marker when the terminal event has a work id', () => {
  const tracker = createWorkLifecycleTracker()
  let messages = tracker.startRequestLifecycle([], { request_id: 'request-a' })
  messages = tracker.startRequestLifecycle(messages, { request_id: 'request-b' })
  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-a'],
    work_id: 'work-a'
  })
  messages = tracker.markInputsDequeued(messages, {
    request_ids: ['request-b'],
    work_id: 'work-b'
  })
  messages = tracker.startAgentLoop(messages, {
    request_id: 'request-a',
    work_id: 'work-a',
    response_id: 'response-a'
  })
  messages = tracker.startAgentLoop(messages, {
    request_id: 'request-b',
    work_id: 'work-b',
    response_id: 'response-b'
  })

  messages = tracker.finishWorkLifecycle(messages, {
    work_id: 'work-a',
    request_id: 'request-a'
  })

  assert.equal(messages.some(message => message.response_id === 'response-a'), false)
  assert.equal(messages.some(message => message.response_id === 'response-b'), true)
})
