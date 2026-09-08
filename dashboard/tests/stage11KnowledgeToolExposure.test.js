import assert from 'node:assert/strict'
import test from 'node:test'

import { defaultProfileConfigs } from '../src/constants/index.js'

test('query_knowledge_base is not stored in default enabled tools', () => {
  assert.equal(defaultProfileConfigs().tool.enabled_tools.includes('query_knowledge_base'), false)
})
