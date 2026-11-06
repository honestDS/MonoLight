import assert from 'node:assert/strict'
import test from 'node:test'

import { defaultProfileConfigs } from '../src/constants/index.js'

const scheduledTaskTool = 'manage_scheduled_tasks'
const agentWorkTool = 'agent_work'

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

test('subagent concurrency stays isolated across profile creation and persistence', () => {
  const firstProfile = defaultProfileConfigs()
  const secondProfile = defaultProfileConfigs()

  assert.equal(firstProfile.tool.subagent_max_concurrency, 4)
  assert.equal(secondProfile.tool.subagent_max_concurrency, 4)

  firstProfile.tool.subagent_max_concurrency = 20
  const savedFirstProfile = JSON.parse(JSON.stringify(firstProfile))

  assert.equal(savedFirstProfile.tool.subagent_max_concurrency, 20)
  assert.equal(savedFirstProfile.tool.scheduled_task_max_concurrency, 4)
  assert.equal(savedFirstProfile.tool.background_task_max_concurrency, 2)
  assert.equal(secondProfile.tool.subagent_max_concurrency, 4)
  assert.equal(defaultProfileConfigs().tool.subagent_max_concurrency, 4)
})

test('subagent management tool enablement stays isolated across profiles and persistence', () => {
  const firstProfile = defaultProfileConfigs()
  const secondProfile = defaultProfileConfigs()
  const defaultTools = [...firstProfile.tool.enabled_tools]

  assert.ok(!firstProfile.tool.enabled_tools.includes(agentWorkTool))
  assert.ok(!secondProfile.tool.enabled_tools.includes(agentWorkTool))
  assert.equal(firstProfile.tool.subagent_max_concurrency, 4)

  firstProfile.tool.enabled_tools.push(agentWorkTool)
  const savedFirstProfile = JSON.parse(JSON.stringify(firstProfile))

  assert.equal(savedFirstProfile.tool.enabled_tools.filter((tool) => tool === agentWorkTool).length, 1)
  assert.ok(!secondProfile.tool.enabled_tools.includes(agentWorkTool))

  const toolIndex = savedFirstProfile.tool.enabled_tools.indexOf(agentWorkTool)
  assert.notEqual(toolIndex, -1)
  savedFirstProfile.tool.enabled_tools.splice(toolIndex, 1)

  assert.equal(firstProfile.tool.enabled_tools.filter((tool) => tool === agentWorkTool).length, 1)
  assert.ok(!savedFirstProfile.tool.enabled_tools.includes(agentWorkTool))
  assert.ok(!secondProfile.tool.enabled_tools.includes(agentWorkTool))

  const newDefaultProfile = defaultProfileConfigs()
  assert.ok(!newDefaultProfile.tool.enabled_tools.includes(agentWorkTool))
  assert.deepEqual(firstProfile.tool.enabled_tools.filter((tool) => tool !== agentWorkTool), defaultTools)
  assert.deepEqual(savedFirstProfile.tool.enabled_tools, defaultTools)
  assert.deepEqual(secondProfile.tool.enabled_tools, defaultTools)
  assert.deepEqual(newDefaultProfile.tool.enabled_tools, defaultTools)
})
