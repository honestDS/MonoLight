import test from 'node:test'
import assert from 'node:assert/strict'

import {
  applyOpenRouterModelMetadata,
  getModelReasoningEfforts,
  normalizeReasoningEfforts
} from '../src/utils/channelModelMetadata.js'
import { defaultChannelRule, defaultModelEntry } from '../src/constants/index.js'
import { buildSetupRequest } from '../src/utils/setupForm.js'

test('reasoning candidates remain preserved from metadata through setup requests', () => {
  assert.deepEqual(
    normalizeReasoningEfforts(['  low ', 'low', '', '   ', null, 3, 'medium', ' medium ']),
    ['low', 'medium']
  )
  assert.deepEqual(normalizeReasoningEfforts('high'), [])

  assert.deepEqual(getModelReasoningEfforts({ reasoning_effort: '  legacy ' }), ['legacy'])
  assert.deepEqual(
    getModelReasoningEfforts({ reasoning_efforts: [], reasoning_effort: 'legacy' }),
    []
  )

  const entry = { reasoning_efforts: ['manual', 'medium'] }
  const model = { reasoning: { supported_efforts: ['low', 'medium', ' high ', 'low'] } }
  const firstMetadata = applyOpenRouterModelMetadata(entry, model)

  assert.deepEqual(entry.reasoning_efforts, ['manual', 'medium', 'low', 'high'])
  assert.deepEqual(firstMetadata.fields, ['reasoning_efforts'])

  const secondMetadata = applyOpenRouterModelMetadata(entry, model)
  assert.deepEqual(entry.reasoning_efforts, ['manual', 'medium', 'low', 'high'])
  assert.deepEqual(secondMetadata.fields, ['reasoning_efforts'])

  const rule = defaultChannelRule()
  assert.equal(rule.reasoning_effort, null)
  assert.equal(Object.hasOwn(rule, 'reasoning_efforts'), false)

  const firstEntry = defaultModelEntry()
  const secondEntry = defaultModelEntry()
  assert.deepEqual(firstEntry.reasoning_efforts, [])
  assert.notStrictEqual(firstEntry.reasoning_efforts, secondEntry.reasoning_efforts)
  firstEntry.reasoning_efforts.push('manual')
  assert.deepEqual(secondEntry.reasoning_efforts, [])

  const request = buildSetupRequest({
    channel: {
      reasoning_efforts: ['manual', 'low', 'high'],
      reasoning_effort: 'legacy'
    }
  })
  assert.deepEqual(request.channel.reasoning_efforts, ['manual', 'low', 'high'])
  assert.equal(Object.hasOwn(request.channel, 'reasoning_effort'), false)
})
