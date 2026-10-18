import assert from 'node:assert/strict'
import test from 'node:test'

import { defaultProfileConfigs } from '../src/constants/index.js'

const scheduledTaskTool = 'manage_scheduled_tasks'

test('new profile configurations enable scheduled task management by default', () => {
  const config = defaultProfileConfigs()

  assert.ok(config.tool.enabled_tools.includes(scheduledTaskTool))
})

test('disabling scheduled task management affects only that profile configuration', () => {
  const disabledConfig = defaultProfileConfigs()
  const independentConfig = defaultProfileConfigs()

  assert.notStrictEqual(disabledConfig.tool.enabled_tools, independentConfig.tool.enabled_tools)

  const toolIndex = disabledConfig.tool.enabled_tools.indexOf(scheduledTaskTool)
  assert.notEqual(toolIndex, -1)
  disabledConfig.tool.enabled_tools.splice(toolIndex, 1)

  assert.ok(!disabledConfig.tool.enabled_tools.includes(scheduledTaskTool))
  assert.ok(independentConfig.tool.enabled_tools.includes(scheduledTaskTool))
  assert.ok(defaultProfileConfigs().tool.enabled_tools.includes(scheduledTaskTool))
})
