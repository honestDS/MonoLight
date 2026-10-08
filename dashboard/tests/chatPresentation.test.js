import assert from 'node:assert/strict'
import test from 'node:test'

import {
  getMessageListScrollState,
  getReasoningCollapseName,
  isFollowableLlmOutput,
  resolveChatActivityNotice,
} from '../src/utils/chatPresentation.js'

test('message list scroll state handles visible and overflowing content', () => {
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 0, clientHeight: 480, scrollTop: 0 }), {
    hasOverflow: false,
    atBottom: true,
    bottomDistance: 0,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 320, clientHeight: 480, scrollTop: 0 }), {
    hasOverflow: false,
    atBottom: true,
    bottomDistance: 0,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 480, clientHeight: 480, scrollTop: 0 }), {
    hasOverflow: false,
    atBottom: true,
    bottomDistance: 0,
  })

  const maxScrollTop = 600
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 0 }), {
    hasOverflow: true,
    atBottom: false,
    bottomDistance: maxScrollTop,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 300 }), {
    hasOverflow: true,
    atBottom: false,
    bottomDistance: 300,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: maxScrollTop }), {
    hasOverflow: true,
    atBottom: true,
    bottomDistance: 0,
  })
  assert.equal(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 576 }).atBottom, true)
  assert.equal(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 575 }).atBottom, false)
})

test('message list scroll state clamps scroll position and rejects invalid measurements', () => {
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: -50 }), {
    hasOverflow: true,
    atBottom: false,
    bottomDistance: 600,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 9999 }), {
    hasOverflow: true,
    atBottom: true,
    bottomDistance: 0,
  })

  const invalidMeasurements = [
    { scrollHeight: 100, clientHeight: 0, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: -1, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: Number.NaN, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: Number.POSITIVE_INFINITY, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: undefined, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: null, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: '400', scrollTop: 0 },
    { scrollHeight: Number.NaN, clientHeight: 100, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: 100, scrollTop: null },
    { scrollHeight: 100, clientHeight: 100, scrollTop: '0' },
  ]
  for (const metrics of invalidMeasurements) assert.equal(getMessageListScrollState(metrics), null)
})

test('message list scroll state reflects measurement changes without retaining state', () => {
  const initiallyVisible = getMessageListScrollState({ scrollHeight: 320, clientHeight: 400, scrollTop: 0 })
  const contentGrew = getMessageListScrollState({ scrollHeight: 600, clientHeight: 400, scrollTop: 0 })
  const windowGrew = getMessageListScrollState({ scrollHeight: 600, clientHeight: 700, scrollTop: 0 })
  const contentCollapsed = getMessageListScrollState({ scrollHeight: 320, clientHeight: 400, scrollTop: 180 })

  assert.equal(initiallyVisible.hasOverflow, false)
  assert.equal(contentGrew.hasOverflow, true)
  assert.equal(contentGrew.atBottom, false)
  assert.equal(windowGrew.hasOverflow, false)
  assert.deepEqual(contentCollapsed, { hasOverflow: false, atBottom: true, bottomDistance: 0 })
  assert.deepEqual(
    getMessageListScrollState({ scrollHeight: 600, clientHeight: 400, scrollTop: 200 }),
    getMessageListScrollState({ scrollHeight: 600, clientHeight: 400, scrollTop: 200 })
  )
})

test('reasoning collapse identity survives transient reasoning becoming a tool group', () => {
  const transient = { role: 'reasoning', response_id: 'response-1', work_id: 'work-1', turn: 2 }
  const toolGroup = { type: 'tool_group', role: 'assistant', response_id: 'response-1', work_id: 'work-1', turn: 2 }

  assert.equal(getReasoningCollapseName(transient), 'reasoning:response:response-1')
  assert.equal(getReasoningCollapseName(toolGroup), getReasoningCollapseName(transient))
})

test('reasoning collapse identity falls back to work and turn when response identity is absent', () => {
  assert.equal(
    getReasoningCollapseName({ role: 'assistant', work_id: 17, turn: 3 }),
    'reasoning:work:17:turn:3'
  )
  assert.equal(getReasoningCollapseName({ db_id: 17, id: 42 }), 'reasoning:db:17')
  assert.equal(getReasoningCollapseName({}), 'reasoning:message:unknown')
})

test('chat activity notice uses one explicit priority order', () => {
  assert.equal(resolveChatActivityNotice({ contextSummarizing: true, thinking: true, historyLoading: true }), 'context_summary')
  assert.equal(resolveChatActivityNotice({ contextSummarizing: false, thinking: true, historyLoading: true }), 'thinking')
  assert.equal(resolveChatActivityNotice({ contextSummarizing: false, thinking: false, historyLoading: true }), 'history_loading')
  assert.equal(resolveChatActivityNotice({ contextSummarizing: false, thinking: false, historyLoading: false }), null)
})

test('streamed reasoning is followable output just like assistant and tool messages', () => {
  assert.equal(isFollowableLlmOutput({ role: 'assistant' }), true)
  assert.equal(isFollowableLlmOutput({ role: 'tool' }), true)
  assert.equal(isFollowableLlmOutput({ role: 'reasoning' }), true)
  assert.equal(isFollowableLlmOutput({ type: 'tool_group', role: 'assistant' }), true)
  assert.equal(isFollowableLlmOutput({ role: 'thinking' }), false)
  assert.equal(isFollowableLlmOutput({ role: 'user' }), false)
  assert.equal(isFollowableLlmOutput(), false)
  assert.equal(isFollowableLlmOutput(null), false)
})
