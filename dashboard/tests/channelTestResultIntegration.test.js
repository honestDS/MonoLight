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
  assert.match(channelFormSource, /const deferredMemoryOrganizationSettings = data\?\.deferred_memory_organization_settings \|\| 0/)
  assert.match(channelFormSource, /const pendingDeletionModels = data\?\.pending_deletion_models \|\| 0/)
  assert.match(
    channelFormSource,
    /messages\.push\(t\(['"]channels\.pending_deletion_models_warning['"],\s*\{\s*count:\s*pendingDeletionModels\s*\}\)\)/,
  )
  assert.match(
    channelFormSource,
    /messages\.push\(t\(['"]channels\.memory_organization_deferred['"],\s*\{\s*count:\s*deferredMemoryOrganizationSettings\s*\}\)\)/,
  )
  assert.match(channelFormSource, /h\(\s*['"]div['"]\s*,\s*\{\s*class:\s*['"]config-impact-warning['"]\s*\}\s*,\s*messages\.map/)
  assert.match(
    channelFormSource,
    /h\(\s*['"]div['"]\s*,\s*\{\s*class:\s*['"]config-impact-warning__item['"]\s*,\s*key:\s*index\s*\}\s*,\s*message\s*\)/,
  )
  assert.equal(channelFormSource.replaceAll('ElMessageBox.alert(', '').includes('alert('), false)
  assert.equal(channelFormSource.includes('showConfigImpactWarning'), false)
  assert.equal(channelFormSource.includes("messages.join('\\n')"), false)
})

test('ChannelFormDialog locks pending-delete model entries', () => {
  assert.match(channelFormSource, /:locked="entry\.lifecycle_status === 'pending_delete'"/)
  assert.match(modelEntrySource, /<el-button v-if="props\.showRemove"[^>]*class="remove"[^>]*:disabled="props\.locked"/)
  assert.match(modelEntrySource, /<el-input v-model="props\.entry\.model_id"[^>]*:disabled="props\.locked"/)
  assert.match(modelEntrySource, /<el-select v-model="props\.entry\.usage"[^>]*:disabled="props\.locked"/)
  assert.match(
    modelEntrySource,
    /<el-tag v-if="props\.locked"[^>]*>\s*\{\{ \$t\(['"]channels\.model_pending_delete['"]\) \}\}\s*<\/el-tag>/,
  )
})

test('ChannelFormDialog confirms config impact before the final edit update', () => {
  assert.match(
    channelFormSource,
    /if \(isEdit\.value\) \{[\s\S]*?let finalResponse = await channelApi\.update\(currentId\.value, payload\)[\s\S]*?if \(finalResponse\.data\?\.data\?\.requires_confirmation\) \{[\s\S]*?const confirmed = await confirmConfigImpact\(finalResponse\.data\?\.data\)[\s\S]*?if \(!confirmed\) return[\s\S]*?finalResponse = await channelApi\.update\(currentId\.value, \{ \.\.\.payload, confirm_config_impact: true \}\)/,
  )
})

test('ChannelFormDialog shows concurrent memory organization disabled notice after a successful edit', () => {
  assert.match(channelFormSource, /data\?\.concurrently_disabled_memory_organization_settings \|\| 0/)
  assert.match(channelFormSource, /ElMessageBox\.alert\(/)
  assert.match(
    channelFormSource,
    /t\(['"]channels\.memory_organization_concurrently_disabled_notice['"],\s*\{\s*count\s*\}\)/,
  )
  assert.match(
    channelFormSource,
    /t\(['"]channels\.memory_organization_concurrently_disabled_title['"]\)/,
  )
  assert.match(
    channelFormSource,
    /ElMessage\.success\(t\(['"]channels\.update_success['"]\)\)[\s\S]*?await showConcurrentMemoryOrganizationDisabledNotice\(finalResponse\.data\?\.data\)/,
  )
})
