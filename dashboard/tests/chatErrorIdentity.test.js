import assert from 'node:assert/strict'
import test from 'node:test'

import { createHarness, createSession } from './chatDraftHarness.js'

const SESSION_ID = 'A'
const LONG_ERROR = `错误正文-${'x'.repeat(600)}`
const TRUNCATED_ERROR = `${LONG_ERROR.slice(0, 497)}...`
const HISTORY_CREATED_AT = 1700000000

const createFailureEvent = (type, {
  messageId = 91,
  workId = 7,
  eventId = `failure-${workId}`
} = {}) => ({
  type,
  message_id: messageId,
  work_id: workId,
  event_id: eventId,
  session_id: SESSION_ID,
  ...(type === 'error' ? { message: LONG_ERROR } : { content: LONG_ERROR })
})

const createHistoryError = id => ({
  id,
  role: 'err',
  type: 'text',
  content: LONG_ERROR,
  created_at: HISTORY_CREATED_AT
})

const receiveEvent = async (harness, event) => {
  harness.receiveWs(event)
  await harness.flush()
}

const errorMessages = harness => Array.from(harness.chat.messages.value).filter(message => message.role === 'err')
const errorNotifications = harness => harness.messages.filter(message => message.type === 'error')

const assertSingleError = (harness, content) => {
  const errors = errorMessages(harness)
  assert.equal(errors.length, 1)
  assert.equal(errors[0].db_id, 91)
  assert.equal(errors[0].content, content)
}

const prepareCallbacks = async (harness, callbacks) => {
  await harness.flush()
  if (callbacks === 'resume') {
    assert.ok(harness.wsManager.sent.some(message => message.type === 'resume'))
    return
  }

  const sendPromise = harness.chat.wsSend('消息')
  await harness.flush()
  const request = harness.wsManager.sent
    .filter(message => message.type === 'chat')
    .at(-1)
  assert.ok(request)
  harness.receiveWs({
    type: 'input_accepted',
    session_id: SESSION_ID,
    request_id: request.request_id,
    work_id: 7,
    submission_status: 'accepted'
  })
  await sendPromise
  await harness.flush()
}

for (const callbacks of ['send', 'resume']) {
  for (const eventType of ['error', 'proactive_reply_error']) {
    for (const order of ['event-first', 'history-first']) {
      test(`deduplicates ${callbacks} ${eventType} in ${order} order`, async t => {
        let historyRows = order === 'history-first' ? [createHistoryError(91)] : []
        const harness = await createHarness(t, {
          sessions: [createSession(SESSION_ID, { source: 'ws' })],
          initialRoute: { path: '/', query: { session_id: SESSION_ID } },
          sessionsHistory: () => historyRows
        })
        await prepareCallbacks(harness, callbacks)

        const event = createFailureEvent(eventType)
        const repeatedProactiveEvent = createFailureEvent('proactive_reply_error')

        if (order === 'event-first') {
          await receiveEvent(harness, event)
          assertSingleError(harness, TRUNCATED_ERROR)
          assert.equal(errorNotifications(harness).length, 1)

          historyRows = [createHistoryError(91)]
          await receiveEvent(harness, event)
          await receiveEvent(harness, repeatedProactiveEvent)
          assertSingleError(harness, LONG_ERROR)
          assert.equal(errorNotifications(harness).length, 1)
          historyRows = [createHistoryError(91)]
          await receiveEvent(harness, repeatedProactiveEvent)

          assertSingleError(harness, LONG_ERROR)
          assert.equal(errorNotifications(harness).length, 1)
          return
        }

        assertSingleError(harness, LONG_ERROR)
        assert.equal(errorNotifications(harness).length, 0)
        await receiveEvent(harness, event)
        assertSingleError(harness, LONG_ERROR)
        assert.equal(errorNotifications(harness).length, 0)

        await receiveEvent(harness, event)
        historyRows = [createHistoryError(91)]
        await receiveEvent(harness, repeatedProactiveEvent)
        await receiveEvent(harness, repeatedProactiveEvent)

        assertSingleError(harness, LONG_ERROR)
        assert.equal(errorNotifications(harness).length, 0)
      })
    }
  }
}

test('keeps same-text errors for distinct tasks after history and notification replays', async t => {
  let historyRows = []
  const harness = await createHarness(t, {
    sessions: [createSession(SESSION_ID, { source: 'ws' })],
    initialRoute: { path: '/', query: { session_id: SESSION_ID } },
    sessionsHistory: () => historyRows
  })
  await prepareCallbacks(harness, 'resume')

  const taskEvents = [
    createFailureEvent('error', { messageId: 91, workId: 7, eventId: 'failure-7' }),
    createFailureEvent('proactive_reply_error', { messageId: 92, workId: 8, eventId: 'failure-8' })
  ]
  for (const event of taskEvents) {
    await receiveEvent(harness, event)
  }
  assert.equal(errorMessages(harness).length, 2)
  assert.equal(errorNotifications(harness).length, 2)

  historyRows = [createHistoryError(91), createHistoryError(92)]
  for (const event of taskEvents) {
    await receiveEvent(harness, createFailureEvent('proactive_reply_error', {
      messageId: event.message_id,
      workId: event.work_id,
      eventId: event.event_id
    }))
    await receiveEvent(harness, event)
  }
  historyRows = [createHistoryError(91), createHistoryError(92)]
  for (const event of taskEvents) {
    await receiveEvent(harness, createFailureEvent('proactive_reply_error', {
      messageId: event.message_id,
      workId: event.work_id,
      eventId: event.event_id
    }))
  }

  const errors = errorMessages(harness)
  assert.equal(errors.length, 2)
  assert.deepEqual(
    errors.map(message => message.db_id).sort((left, right) => left - right),
    [91, 92]
  )
  assert.deepEqual(
    errors.map(message => message.work_id).sort((left, right) => left - right),
    [7, 8]
  )
  assert.deepEqual(
    errors.map(message => message.event_id).sort(),
    ['failure-7', 'failure-8']
  )
  assert.deepEqual(errors.map(message => message.content), [LONG_ERROR, LONG_ERROR])
  assert.equal(errorNotifications(harness).length, 2)
})
