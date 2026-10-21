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

test('new profile configurations enable the current default tools only', () => {
  const enabledTools = defaultProfileConfigs().tool.enabled_tools

  for (const tool of ['web_tool', 'manage_background_tasks']) {
    assert.equal(enabledTools.filter((enabledTool) => enabledTool === tool).length, 1)
  }

  for (const tool of [
    'firecrawl',
    'firecrawl_search',
    'firecrawl_scrape',
    'list_background_tasks',
    'cancel_background_task',
    'write_file',
  ]) {
    assert.ok(!enabledTools.includes(tool))
  }
})

test('removing current default tools affects only that profile configuration', () => {
  const disabledConfig = defaultProfileConfigs()
  const independentConfig = defaultProfileConfigs()

  for (const tool of ['web_tool', 'manage_background_tasks']) {
    const toolIndex = disabledConfig.tool.enabled_tools.indexOf(tool)
    assert.notEqual(toolIndex, -1)
    disabledConfig.tool.enabled_tools.splice(toolIndex, 1)
  }

  assert.ok(!disabledConfig.tool.enabled_tools.includes('web_tool'))
  assert.ok(!disabledConfig.tool.enabled_tools.includes('manage_background_tasks'))
  assert.ok(independentConfig.tool.enabled_tools.includes('web_tool'))
  assert.ok(independentConfig.tool.enabled_tools.includes('manage_background_tasks'))
  assert.ok(defaultProfileConfigs().tool.enabled_tools.includes('web_tool'))
  assert.ok(defaultProfileConfigs().tool.enabled_tools.includes('manage_background_tasks'))
})
