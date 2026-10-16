import assert from 'node:assert/strict'
import test from 'node:test'

import { createHarness, createSession } from './chatDraftHarness.js'

const SESSION_ID = 'A'
const CALLBACKS = ['send', 'resume']
const FOREGROUND_SOURCES = ['foreground', 'confirmed_tool_execution']
const BACKGROUND_SOURCES = ['background_task', 'scheduled_task']

const receiveEvent = async (harness, event) => {
  harness.receiveWs(event)
  await harness.flush()
}

const sendRequest = async (harness, text, workId) => {
  const sendPromise = harness.chat.wsSend(text)
  await harness.flush()
  const request = harness.wsManager.sent
    .filter(message => message.type === 'chat')
    .at(-1)
  assert.ok(request)

  await receiveEvent(harness, {
    type: 'input_accepted',
    session_id: SESSION_ID,
    request_id: request.request_id,
    work_id: workId,
    submission_status: 'accepted'
  })
  await sendPromise
  return request.request_id
}

const prepareCallbacks = async (t, callbacks, {
  sessionsHistory = () => [],
  sessionOverrides = {},
  workId = 1
} = {}) => {
  const harness = await createHarness(t, {
    sessions: [createSession(SESSION_ID, {
      source: 'ws',
      ...(callbacks === 'resume'
        ? { is_loading: true, is_reply_running: true }
        : {}),
      ...sessionOverrides
    })],
    initialRoute: { path: '/', query: { session_id: SESSION_ID } },
    sessionsHistory
  })
  await harness.flush()

  if (callbacks === 'resume') {
    assert.ok(harness.wsManager.sent.some(message => message.type === 'resume'))
    return { harness, requestId: 'resume-request' }
  }

  return {
    harness,
    requestId: await sendRequest(harness, '前台输入', workId)
  }
}

const completeResume = async (harness, callbacks) => {
  if (callbacks !== 'resume') return
  await receiveEvent(harness, {
    type: 'resume_complete',
    session_id: SESSION_ID
  })
}

const startStream = async (harness, {
  requestId,
  workId,
  responseId,
  reasoning = null,
  content,
  sequence = 1
}) => {
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_ID,
    request_id: requestId,
    request_ids: [requestId],
    work_id: workId,
    response_id: responseId,
    turn: 1,
    event_sequence_no: sequence
  })
  if (reasoning) {
    await receiveEvent(harness, {
      type: 'reasoning',
      session_id: SESSION_ID,
      request_id: requestId,
      work_id: workId,
      response_id: responseId,
      turn: 1,
      content: reasoning,
      event_sequence_no: sequence + 1
    })
  }
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_ID,
    request_id: requestId,
    work_id: workId,
    response_id: responseId,
    turn: 1,
    content,
    event_sequence_no: sequence + (reasoning ? 2 : 1)
  })
}

const createTurnEnd = ({
  requestId,
  workId,
  responseId,
  content,
  reasoningContent = null,
  messageId,
  eventSequenceNo
}) => ({
  type: 'turn_end',
  session_id: SESSION_ID,
  request_id: requestId,
  request_ids: [requestId],
  work_id: workId,
  response_id: responseId,
  turn: 1,
  content,
  ...(reasoningContent ? { reasoning_content: reasoningContent } : {}),
  message_id: messageId,
  finish_reason: 'stop',
  event_sequence_no: eventSequenceNo
})

const createDone = ({
  requestId,
  workId,
  content,
  reasoningContent = null,
  messageId,
  eventSequenceNo
}) => ({
  type: 'done',
  session_id: SESSION_ID,
  request_id: requestId,
  request_ids: [requestId],
  work_id: workId,
  response_id: `session-reply-work:${workId}`,
  event_sequence_no: eventSequenceNo,
  response: {
    choices: [{
      message: {
        role: 'assistant',
        content,
        ...(reasoningContent ? { reasoning_content: reasoningContent } : {})
      },
      finish_reason: 'stop'
    }],
    history: [],
    work_id: workId,
    message_id: messageId
  }
})

const assistantMessages = harness => harness.chat.messages.value
  .filter(message => message.role === 'assistant')

const errorMessages = harness => harness.chat.messages.value
  .filter(message => message.role === 'err')

const errorNotifications = harness => harness.messages
  .filter(message => message.type === 'error')

const assertSingleFinalReply = (harness, {
  content,
  reasoningContent,
  messageId,
  responseId = null
}) => {
  const replies = assistantMessages(harness)
  assert.equal(replies.length, 1)
  assert.equal(replies[0].content, content)
  assert.equal(replies[0].reasoning_content, reasoningContent)
  assert.equal(String(replies[0].db_id), String(messageId))
  if (responseId) assert.equal(replies[0].response_id, responseId)

  assert.equal(harness.chat.messages.value.filter(message => message.role === 'reasoning').length, 0)
  assert.equal(harness.chat.messages.value.filter(message => message.role === 'thinking').length, 0)
  assert.equal(
    harness.chat.messages.value.filter(message => (
      typeof message.reasoning_content === 'string' && message.reasoning_content.trim()
    )).length,
    1
  )
}

for (const callbacks of CALLBACKS) {
  for (const source of FOREGROUND_SOURCES) {
    test(`${callbacks} ignores ${source} proactive reply until stream terminal events`, async t => {
      const workId = `${callbacks}-${source}-reply`
      const { harness, requestId } = await prepareCallbacks(t, callbacks, { workId })
      const responseId = `response-${workId}`
      const messageId = 501

      await startStream(harness, {
        requestId,
        workId,
        responseId,
        reasoning: '流式思考',
        content: '流式正文'
      })

      const proactiveReply = {
        type: 'proactive_reply',
        session_id: SESSION_ID,
        request_id: requestId,
        request_ids: [requestId],
        work_id: workId,
        response_id: responseId,
        message_id: messageId,
        content: '主动快照',
        source,
        event_sequence_no: 20
      }
      await receiveEvent(harness, proactiveReply)
      await receiveEvent(harness, { ...proactiveReply })

      const partialReply = assistantMessages(harness)
        .find(message => message.response_id === responseId)
      assert.ok(partialReply)
      assert.equal(partialReply.content, '流式正文')
      assert.equal(
        harness.chat.messages.value.filter(message => message.role === 'thinking').length,
        1
      )
      assert.equal(errorMessages(harness).length, 0)
      assert.equal(errorNotifications(harness).length, 0)
      assert.equal(harness.chat.loading.value, true)

      const turnEnd = createTurnEnd({
        requestId,
        workId,
        responseId,
        content: '最终正文',
        reasoningContent: '完整思考',
        messageId,
        eventSequenceNo: 4
      })
      const done = createDone({
        requestId,
        workId,
        content: '最终正文',
        reasoningContent: '完整思考',
        messageId,
        eventSequenceNo: 5
      })

      await receiveEvent(harness, turnEnd)
      assertSingleFinalReply(harness, {
        content: '最终正文',
        reasoningContent: '完整思考',
        messageId,
        responseId
      })

      await receiveEvent(harness, { ...turnEnd })
      assertSingleFinalReply(harness, {
        content: '最终正文',
        reasoningContent: '完整思考',
        messageId,
        responseId
      })

      await receiveEvent(harness, done)
      assertSingleFinalReply(harness, {
        content: '最终正文',
        reasoningContent: '完整思考',
        messageId,
        responseId
      })

      await receiveEvent(harness, { ...done })
      await receiveEvent(harness, { ...proactiveReply })
      assertSingleFinalReply(harness, {
        content: '最终正文',
        reasoningContent: '完整思考',
        messageId,
        responseId
      })
      await completeResume(harness, callbacks)
      assert.equal(harness.chat.loading.value, false)
    })
  }
}

for (const callbacks of CALLBACKS) {
  for (const source of FOREGROUND_SOURCES) {
    test(`${callbacks} lets content continue after ${source} proactive reply error`, async t => {
      const workId = `${callbacks}-${source}-error`
      const { harness, requestId } = await prepareCallbacks(t, callbacks, { workId })
      const responseId = `response-${workId}`

      const proactiveReplyError = {
        type: 'proactive_reply_error',
        session_id: SESSION_ID,
        request_id: requestId,
        request_ids: [requestId],
        work_id: workId,
        event_id: `${workId}-notification-error`,
        message_id: 601,
        content: '通知错误',
        source,
        event_sequence_no: 1
      }
      await receiveEvent(harness, proactiveReplyError)
      assert.equal(errorMessages(harness).length, 0)
      assert.equal(errorNotifications(harness).length, 0)
      assert.equal(harness.chat.loading.value, true)

      await startStream(harness, {
        requestId,
        workId,
        responseId,
        content: '错误前正文',
        sequence: 2
      })
      const partialReply = assistantMessages(harness)
        .find(message => message.response_id === responseId)
      assert.ok(partialReply)
      assert.equal(partialReply.content, '错误前正文')
      assert.equal(errorMessages(harness).length, 0)
      assert.equal(errorNotifications(harness).length, 0)

      const actualError = {
        type: 'error',
        session_id: SESSION_ID,
        request_id: requestId,
        request_ids: [requestId],
        work_id: workId,
        event_id: `${workId}-actual-error`,
        message_id: 602,
        message: '真实错误',
        source,
        event_sequence_no: 5
      }
      await receiveEvent(harness, actualError)
      await receiveEvent(harness, { ...actualError })

      assert.equal(errorMessages(harness).length, 1)
      assert.equal(errorMessages(harness)[0].content, '真实错误')
      assert.equal(String(errorMessages(harness)[0].db_id), '602')
      assert.equal(errorNotifications(harness).length, 1)
      await completeResume(harness, callbacks)
      assert.equal(harness.chat.loading.value, false)
    })
  }
}

for (const callbacks of CALLBACKS) {
  for (const terminalType of ['error', 'cancelled']) {
    test(`${callbacks} ignores a late turn_end after ${terminalType} without blocking another work`, async t => {
      const workA = `${callbacks}-terminal-a`
      const { harness, requestId: requestA } = await prepareCallbacks(t, callbacks, { workId: workA })
      const responseA = `response-${workA}`

      await startStream(harness, {
        requestId: requestA,
        workId: workA,
        responseId: responseA,
        content: '局部正文'
      })

      const terminal = terminalType === 'error'
        ? {
            type: 'error',
            session_id: SESSION_ID,
            request_id: requestA,
            request_ids: [requestA],
            work_id: workA,
            event_id: `${workA}-error`,
            message_id: 701,
            message: '工作失败',
            event_sequence_no: 3
          }
        : {
            type: 'cancelled',
            session_id: SESSION_ID,
            request_id: requestA,
            request_ids: [requestA],
            work_id: workA,
            event_sequence_no: 3
          }
      await receiveEvent(harness, terminal)
      await completeResume(harness, callbacks)
      assert.equal(harness.chat.loading.value, false)

      await receiveEvent(harness, createTurnEnd({
        requestId: requestA,
        workId: workA,
        responseId: `${responseA}-late`,
        content: '迟到正文',
        messageId: 702,
        eventSequenceNo: 4
      }))

      const partialReply = assistantMessages(harness)
        .find(message => message.response_id === responseA)
      assert.ok(partialReply)
      assert.equal(partialReply.content, '局部正文')
      assert.equal(
        assistantMessages(harness).some(message => message.response_id === `${responseA}-late`),
        false
      )
      assert.equal(errorMessages(harness).length, terminalType === 'error' ? 1 : 0)

      const workB = `${callbacks}-terminal-b`
      const requestB = callbacks === 'send'
        ? await sendRequest(harness, '另一工作', workB)
        : 'resume-request-b'
      const responseB = `response-${workB}`
      await startStream(harness, {
        requestId: requestB,
        workId: workB,
        responseId: responseB,
        content: '另一工作正文'
      })
      await receiveEvent(harness, createTurnEnd({
        requestId: requestB,
        workId: workB,
        responseId: responseB,
        content: '另一工作正文',
        messageId: 703,
        eventSequenceNo: 3
      }))

      const replyB = assistantMessages(harness)
        .filter(message => message.response_id === responseB)
      assert.equal(replyB.length, 1)
      assert.equal(replyB[0].content, '另一工作正文')
      assert.equal(String(replyB[0].db_id), '703')
      assert.equal(partialReply.content, '局部正文')
    })
  }
}

for (const callbacks of CALLBACKS) {
  test(`${callbacks} keeps late turn_end identity merge after successful done`, async t => {
    const workId = `${callbacks}-done-merge`
    const { harness, requestId } = await prepareCallbacks(t, callbacks, { workId })
    const responseId = `response-${workId}`
    const messageId = 801

    await startStream(harness, {
      requestId,
      workId,
      responseId,
      content: '前段正文'
    })
    await receiveEvent(harness, createDone({
      requestId,
      workId,
      content: '终态正文',
      reasoningContent: '终态思考',
      messageId,
      eventSequenceNo: 4
    }))
    assertSingleFinalReply(harness, {
      content: '终态正文',
      reasoningContent: '终态思考',
      messageId
    })
    await completeResume(harness, callbacks)
    assert.equal(harness.chat.loading.value, false)

    await receiveEvent(harness, createTurnEnd({
      requestId,
      workId,
      responseId,
      content: '终态正文',
      reasoningContent: '终态思考',
      messageId,
      eventSequenceNo: 5
    }))
    assertSingleFinalReply(harness, {
      content: '终态正文',
      reasoningContent: '终态思考',
      messageId,
      responseId
    })
  })
}

test('resume displays done content without turn_end or changed history', async t => {
  let historyRows = []
  const { harness } = await prepareCallbacks(t, 'resume', {
    sessionsHistory: () => historyRows
  })
  await receiveEvent(harness, createDone({
    requestId: 'resume-done-request',
    workId: 'resume-done-work',
    content: '恢复终态正文',
    messageId: 901,
    eventSequenceNo: 2
  }))

  const replies = assistantMessages(harness)
  assert.equal(replies.length, 1)
  assert.equal(replies[0].content, '恢复终态正文')
  assert.equal(String(replies[0].db_id), '901')
  assert.equal(harness.chat.messages.value.filter(message => message.role === 'thinking').length, 0)
  await completeResume(harness, 'resume')
  assert.equal(harness.chat.loading.value, false)
  assert.deepEqual(historyRows, [])
})

for (const callbacks of CALLBACKS) {
  for (const source of BACKGROUND_SOURCES) {
    test(`${callbacks} renders ${source} proactive results from history`, async t => {
      let historyRows = []
      const activeWorkId = `${source}-active-work`
      const { harness } = await prepareCallbacks(t, callbacks, {
        sessionsHistory: () => historyRows,
        workId: activeWorkId
      })
      historyRows = [{
        id: 1001,
        role: 'assistant',
        content: '后台历史结果',
        created_at: 1700000000
      }]

      await receiveEvent(harness, {
        type: 'proactive_reply',
        session_id: SESSION_ID,
        work_id: `${source}-work`,
        message_id: 1001,
        content: '主动通知快照',
        source,
        event_sequence_no: 1
      })

      const replies = assistantMessages(harness)
      assert.equal(replies.length, 1)
      assert.equal(replies[0].content, '后台历史结果')
      assert.equal(String(replies[0].db_id), '1001')
      assert.notEqual(replies[0].content, '主动通知快照')
      assert.equal(errorMessages(harness).length, 0)
      assert.equal(errorNotifications(harness).length, 0)
    })
  }
}

for (const historyCase of [
  {
    name: 'latest persisted message',
    historyRows: [
      { id: 41, role: 'user', content: '历史问题', created_at: 1700000000 },
      { id: 50, role: 'assistant', content: '历史答案', created_at: 1700000001 }
    ],
    expectedCursor: 50
  },
  {
    name: 'empty history',
    historyRows: [],
    expectedCursor: 0
  }
]) {
  test(`normal wsSend uses ${historyCase.name} history boundary`, async t => {
    const { harness } = await prepareCallbacks(t, 'send', {
      sessionsHistory: () => historyCase.historyRows,
      workId: 'history-cursor-work'
    })
    const chatPayload = harness.wsManager.sent
      .filter(message => message.type === 'chat')
      .at(-1)

    assert.ok(chatPayload)
    assert.equal(chatPayload.history_message_id, historyCase.expectedCursor)
  })
}

for (const source of BACKGROUND_SOURCES) {
  test(`send keeps foreground context summary isolated from ${source} errors`, async t => {
    const foregroundWork = `${source}-foreground-summary`
    const { harness, requestId } = await prepareCallbacks(t, 'send', {
      workId: foregroundWork
    })

    await receiveEvent(harness, {
      type: 'context_summary_start',
      session_id: SESSION_ID,
      work_id: foregroundWork,
      request_id: requestId,
      event_sequence_no: 1
    })
    assert.equal(harness.chat.isContextSummarizing.value, true)

    const foregroundThinking = harness.chat.messages.value.find(message => (
      message.role === 'thinking' && message.request_id === requestId
    ))
    assert.ok(foregroundThinking)
    assert.equal(harness.chat.loading.value, true)

    await receiveEvent(harness, {
      type: 'proactive_reply_error',
      session_id: SESSION_ID,
      source,
      work_id: `${source}-background-work`,
      event_id: `${source}-background-error`,
      message_id: 1101,
      content: '后台错误'
    })

    const errors = errorMessages(harness)
    assert.equal(errors.length, 1)
    assert.equal(errors[0].work_id, `${source}-background-work`)
    assert.equal(errors[0].event_id, `${source}-background-error`)
    assert.equal(String(errors[0].db_id), '1101')
    assert.equal(errors[0].content, '后台错误')
    assert.notEqual(errors[0].request_id, requestId)
    assert.equal(errorNotifications(harness).length, 1)
    assert.equal(harness.chat.isContextSummarizing.value, true)
    assert.equal(
      harness.chat.messages.value.some(message => (
        message.role === 'thinking' && message.request_id === requestId
      )),
      true
    )
    assert.equal(harness.chat.loading.value, true)

    await receiveEvent(harness, {
      type: 'context_summary_end',
      session_id: SESSION_ID,
      work_id: foregroundWork,
      request_id: requestId,
      event_sequence_no: 2
    })
    assert.equal(harness.chat.isContextSummarizing.value, false)
  })
}
