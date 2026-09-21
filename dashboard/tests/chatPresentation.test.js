import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { getReasoningCollapseName, isFollowableLlmOutput, resolveChatActivityNotice } from '../src/utils/chatPresentation.js'

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
})

test('chat message list routes reasoning through the same output-follow path', () => {
  const listSource = readFileSync(new URL('../src/components/ChatMessageList.vue', import.meta.url), 'utf8')

  assert.match(
    listSource,
    /const isFollowableIncomingMessage = message => \(\s*isFollowableLlmOutput\(message\)/
  )
})
