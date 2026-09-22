import assert from 'node:assert/strict'
import test from 'node:test'

import { defaultProfileConfigs } from '../src/constants/index.js'


test('memory precheck defaults to enabled in new profiles', () => {
  assert.equal(defaultProfileConfigs().memory.precheck_enabled, true)
})
