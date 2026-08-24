import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const readSource = relativePath => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const channelFormSource = readSource('../src/components/ChannelFormDialog.vue')
const setupSource = readSource('../src/views/SetupView.vue')
const modelEntrySource = readSource('../src/components/ChannelModelEntry.vue')

const countMatches = (source, pattern) => source.match(pattern)?.length || 0

test('ChannelFormDialog uses one shared result dialog with model result state', () => {
  assert.match(channelFormSource, /import ModelTestResultDialog from ['"]\.\/ModelTestResultDialog\.vue['"]/)
  assert.equal(countMatches(channelFormSource, /<ModelTestResultDialog\b/g), 1)
  assert.match(channelFormSource, /v-model:visible="modelTestResultDialogVisible"/)
  assert.match(channelFormSource, /:results="modelTestResults"/)
  assert.match(channelFormSource, /:active-id="activeModelTestResultId"/)
  assert.match(channelFormSource, /@view-test-result="openModelTestResult(?:\(entry\))?"/)
})

test('ChannelFormDialog assigns locally stable WeakMap result ids and opens on test start', () => {
  assert.match(channelFormSource, /const modelTestEntryIds = new WeakMap\(\)/)
  assert.match(
    channelFormSource,
    /const getModelTestEntryId = \(entry\) => \{[\s\S]*?modelTestEntryIds\.get\(entry\)[\s\S]*?modelTestEntryIds\.set\(entry, id\)[\s\S]*?\}/,
  )
  assert.match(
    channelFormSource,
    /const beginModelTest = \(entry, state\) => \{[\s\S]*?activeModelTestResultId\.value = getModelTestEntryId\(entry\)[\s\S]*?modelTestResultDialogVisible\.value = true[\s\S]*?\}/,
  )

  for (const legacyMarker of ['modelTestResultExpanded', 'test-result-expanded']) {
    assert.equal(channelFormSource.includes(legacyMarker), false, `legacy marker remains: ${legacyMarker}`)
  }
})

test('SetupView uses the shared result dialog contract', () => {
  assert.match(setupSource, /import ModelTestResultDialog from ['"]@\/components\/ModelTestResultDialog\.vue['"]/)
  assert.equal(countMatches(setupSource, /<ModelTestResultDialog\b/g), 1)
  assert.match(setupSource, /v-model:visible="modelTestResultDialogVisible"/)
  assert.match(setupSource, /:results="modelTestResults"/)
  assert.match(setupSource, /:active-id="activeModelTestResultId"/)
  assert.match(setupSource, /@view-test-result="openModelTestResult"/)
})

test('ChannelModelEntry keeps metadata detection and result viewing as separate events', () => {
  assert.match(modelEntrySource, /@click="emit\('detect-metadata'\)"/)
  assert.match(modelEntrySource, /@click="emit\('view-test-result'\)"/)
  assert.match(
    modelEntrySource,
    /const emit = defineEmits\([\s\S]*?'detect-metadata'[\s\S]*?'view-test-result'[\s\S]*?\)/,
  )
  assert.notEqual(
    modelEntrySource.indexOf("emit('detect-metadata')"),
    modelEntrySource.indexOf("emit('view-test-result')"),
  )
})

test('ChannelFormDialog renders config impact messages as block VNodes', () => {
  assert.match(channelFormSource, /import\s+\{[^}]*\bh\b[^}]*\}\s+from\s+['"]vue['"]/)
  assert.match(channelFormSource, /import\s+\{[^}]*\bElMessageBox\b[^}]*\}\s+from\s+['"]element-plus['"]/)
  assert.match(channelFormSource, /ElMessageBox\.confirm\(/)
  assert.match(channelFormSource, /const syncedMemoryOrganizationSettings = data\?\.synced_memory_organization_settings \|\| 0/)
  assert.match(channelFormSource, /const retainedMemoryOrganizationSettings = data\?\.retained_memory_organization_settings \|\| 0/)
  assert.match(channelFormSource, /const disabledMemoryOrganizationSettings = data\?\.disabled_memory_organization_settings \|\| 0/)
  assert.match(channelFormSource, /h\(\s*['"]div['"]\s*,\s*\{\s*class:\s*['"]config-impact-warning['"]\s*\}\s*,\s*messages\.map/)
  assert.match(
    channelFormSource,
    /h\(\s*['"]div['"]\s*,\s*\{\s*class:\s*['"]config-impact-warning__item['"]\s*,\s*key:\s*index\s*\}\s*,\s*message\s*\)/,
  )
  assert.equal(channelFormSource.includes('alert('), false)
  assert.equal(channelFormSource.includes('showConfigImpactWarning'), false)
  assert.equal(channelFormSource.includes("messages.join('\\n')"), false)
})

test('ChannelFormDialog confirms config impact before the final edit update', () => {
  assert.match(
    channelFormSource,
    /if \(isEdit\.value\) \{[\s\S]*?const res = await channelApi\.update\(currentId\.value, payload\)[\s\S]*?if \(res\.data\?\.data\?\.requires_confirmation\) \{[\s\S]*?const confirmed = await confirmConfigImpact\(res\.data\?\.data\)[\s\S]*?if \(!confirmed\) return[\s\S]*?await channelApi\.update\(currentId\.value, \{ \.\.\.payload, confirm_config_impact: true \}\)/,
  )
})
