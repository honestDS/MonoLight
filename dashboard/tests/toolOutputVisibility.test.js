import assert from 'node:assert/strict'
import test from 'node:test'

import {
  filterResponseHistoryToolOutput,
  filterToolOutputMessages
} from '../src/utils/toolOutputVisibility.js'

test('non-stream response hides tool-round reasoning with tool output but preserves final reasoning', () => {
  const response = {
    choices: [
      {
        message: {
          role: 'assistant',
          content: 'final answer',
          reasoning_content: 'final reasoning'
        },
        finish_reason: 'stop'
      }
    ],
    history: [
      {
        role: 'assistant',
        content: 'tool round body',
        reasoning_content: 'tool round reasoning',
        tool_calls: [{ id: 'call-1', name: 'search', arguments: { query: 'MonoLight' } }]
      },
      { role: 'tool', tool_call_id: 'call-1', content: 'tool result' },
      { role: 'assistant', content: 'final answer', reasoning_content: 'final reasoning' }
    ]
  }

  const filtered = filterResponseHistoryToolOutput(response, false)

  assert.equal(filtered.choices[0].message.reasoning_content, 'final reasoning')
  assert.deepEqual(filtered.history, [
    { role: 'assistant', content: 'final answer', reasoning_content: 'final reasoning' }
  ])
})

const internalMessages = [
  {
    type: 'background_result',
    role: 'assistant',
    content: JSON.stringify({
      type: 'background_tool_result',
      tool_call: { id: 'background-call-1', name: 'search', arguments: { query: 'MonoLight' } },
      task: 'background result'
    })
  },
  { type: 'background_result', role: 'user', content: { status: 'completed' } },
  { type: 'background_result', role: 'assistant', content: '{"status":"completed"}' },
  { type: 'background_result', role: 'user', content: '{"status":"completed"' },
  { type: 'background_result', role: 'assistant', content: '' },
  { type: 'background_result', role: 'user', content: null },
  { role: 'system', content: 'internal system message' },
  { role: 'background_system', content: 'legacy internal message' }
]
const backgroundResults = internalMessages.filter(
  message => message.type === 'background_result'
)
const guidanceCases = {
  pending: {
    id: 'guidance-pending',
    role: 'system',
    type: 'guidance',
    content: '[系统提示信息]pending guidance[系统提示信息结束]',
    is_processed: false
  },
  used: {
    id: 'guidance-used',
    role: 'system',
    type: 'guidance',
    content: '[系统提示信息]used guidance[系统提示信息结束]',
    is_processed: true
  }
}

for (const showToolCalls of [true, false]) {
  test(`background results follow showToolCalls=${showToolCalls}`, () => {
    assert.deepEqual(
      filterToolOutputMessages(internalMessages, showToolCalls),
      showToolCalls ? backgroundResults : []
    )
  })
}

const visibilityCases = {
  user: { role: 'user', content: 'visible user message' },
  userWithBackgroundJson: {
      role: 'user',
      content: '{"type":"background_result","output":"user text"}'
    },
  assistant: { role: 'assistant', content: 'visible assistant message' },
  toolCall: { type: 'tool_call', role: 'assistant', content: 'tool call' },
  toolResult: { type: 'tool_result', role: 'tool', content: 'tool result' },
  auditConfirmation: {
    type: 'audit_confirmation',
    role: 'assistant',
    tool_calls: [{ id: 'confirm-1' }],
    content: 'confirm action'
  },
  highRiskOverride: {
    role: 'assistant',
    confirmation_mode: 'high_risk_override',
    tool_calls: [{ id: 'confirm-2' }],
    content: 'confirm high risk action'
  }
}
const allVisibilityMessages = Object.values(visibilityCases)

for (const [showToolCalls, expected] of [
  [true, allVisibilityMessages],
  [false, [
    visibilityCases.user,
    visibilityCases.userWithBackgroundJson,
    visibilityCases.assistant,
    visibilityCases.auditConfirmation,
    visibilityCases.highRiskOverride
  ]]
]) {
  test(`visible messages follow showToolCalls=${showToolCalls}`, () => {
    assert.deepEqual(
      filterToolOutputMessages(allVisibilityMessages, showToolCalls),
      expected
    )
  })
}

const mixedGuidanceMessages = [
  visibilityCases.user,
  guidanceCases.pending,
  ...internalMessages,
  visibilityCases.assistant,
  guidanceCases.used,
  visibilityCases.toolCall,
  visibilityCases.toolResult
]

for (const showToolCalls of [true, false]) {
  test(`guidance messages remain visible with showToolCalls=${showToolCalls}`, () => {
    const original = structuredClone(mixedGuidanceMessages)
    const filtered = filterToolOutputMessages(mixedGuidanceMessages, showToolCalls)
    const expected = showToolCalls
      ? [
          visibilityCases.user,
          guidanceCases.pending,
          ...backgroundResults,
          visibilityCases.assistant,
          guidanceCases.used,
          visibilityCases.toolCall,
          visibilityCases.toolResult
        ]
      : [
          visibilityCases.user,
          guidanceCases.pending,
          visibilityCases.assistant,
          guidanceCases.used
        ]

    assert.deepEqual(filtered, expected)
    assert.deepEqual(mixedGuidanceMessages, original)
    assert.deepEqual(
      filterToolOutputMessages(filtered, showToolCalls),
      filtered
    )
  })
}

test('response history filtering applies tool visibility without mutating response data', () => {
  const response = {
    id: 'response-1',
    choices: [
      {
        message: { role: 'assistant', content: 'final answer' },
        finish_reason: 'stop'
      }
    ],
    files: [{ name: 'result.txt', content: 'final file' }],
    metadata: { request_id: 'request-1', trace: { complete: true } },
    history: [
      ...internalMessages,
      visibilityCases.user,
      guidanceCases.pending,
      visibilityCases.assistant,
      guidanceCases.used,
      visibilityCases.toolResult,
      visibilityCases.auditConfirmation
    ]
  }
  const original = structuredClone(response)

  for (const showToolCalls of [true, false]) {
    const filtered = filterResponseHistoryToolOutput(response, showToolCalls)
    const expectedHistory = [
      ...(showToolCalls ? backgroundResults : []),
      visibilityCases.user,
      guidanceCases.pending,
      visibilityCases.assistant,
      guidanceCases.used,
      ...(showToolCalls
        ? [visibilityCases.toolResult]
        : []),
      visibilityCases.auditConfirmation
    ]

    assert.deepEqual(filtered.history, expectedHistory)
    assert.deepEqual(filtered.choices, response.choices)
    assert.deepEqual(filtered.files, response.files)
    assert.deepEqual(filtered.metadata, response.metadata)
    assert.deepEqual(response, original)
    assert.deepEqual(
      filterResponseHistoryToolOutput(filtered, showToolCalls),
      filtered
    )
  }
})
