import assert from 'node:assert/strict'
import test from 'node:test'

import { filterResponseHistoryToolOutput } from '../src/utils/toolOutputVisibility.js'

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
