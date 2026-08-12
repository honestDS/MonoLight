import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { defaultProfileConfigs, routeNameMap } from '../src/constants/index.js'
import enMemories from '../src/i18n/locales/en/memories.js'
import zhMemories from '../src/i18n/locales/zh/memories.js'

const dashboardRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const readSource = relativePath => readFileSync(resolve(dashboardRoot, relativePath), 'utf8')

const apiSource = readSource('src/api/index.js')
const appSource = readSource('src/App.vue')
const constantsSource = readSource('src/constants/index.js')
const memoriesSource = readSource('src/views/MemoriesView.vue')
const profilesSource = readSource('src/views/ProfilesView.vue')
const routerSource = readSource('src/router/index.js')

test('memoryApi contains every memory endpoint with the required HTTP method', () => {
  const endpoints = [
    {
      name: 'list',
      pattern: /list:\s*\(params\)\s*=>\s*request\.get\('\/memories\/list',\s*\{\s*params\s*\}\)/
    },
    {
      name: 'get',
      pattern: /get:\s*\(id\)\s*=>\s*request\.get\('\/memories\/get',\s*\{\s*params:\s*\{\s*memory_id:\s*id\s*\}\s*\}\)/
    },
    {
      name: 'create',
      pattern: /create:\s*\(data\)\s*=>\s*request\.post\('\/memories\/create',\s*data\)/
    },
    {
      name: 'update',
      pattern: /update:\s*\(data\)\s*=>\s*request\.post\('\/memories\/update',\s*data\)/
    },
    {
      name: 'delete',
      pattern: /delete:\s*\(data\)\s*=>\s*request\.post\('\/memories\/delete',\s*data\)/
    },
    {
      name: 'jobs',
      pattern: /jobs:\s*\(params\)\s*=>\s*request\.get\('\/memories\/jobs',\s*\{\s*params\s*\}\)/
    },
    {
      name: 'job',
      pattern: /job:\s*\(id\)\s*=>\s*request\.get\(`\/memories\/jobs\/\$\{id\}`\)/
    },
    {
      name: 'retryJob',
      pattern: /retryJob:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/jobs\/\$\{id\}\/retry`\)/
    },
    {
      name: 'cancelJob',
      pattern: /cancelJob:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/jobs\/\$\{id\}\/cancel`\)/
    },
    {
      name: 'history',
      pattern: /history:\s*\(id,\s*params\)\s*=>\s*request\.get\(`\/memories\/\$\{id\}\/history`,\s*\{\s*params\s*\}\)/
    },
    {
      name: 'restore',
      pattern: /restore:\s*\(id,\s*data\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/restore`,\s*data\)/
    },
    {
      name: 'resumeCurrent',
      pattern: /resumeCurrent:\s*\(id,\s*data\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/resume-current`,\s*data\)/
    },
    {
      name: 'settings',
      pattern: /settings:\s*\(\)\s*=>\s*request\.get\('\/memories\/settings'\)/
    },
    {
      name: 'reindex',
      pattern: /reindex:\s*\(data\)\s*=>\s*request\.post\('\/memories\/reindex',\s*data\)/
    },
    {
      name: 'migrations',
      pattern: /migrations:\s*\(params\)\s*=>\s*request\.get\('\/memories\/embedding-migrations',\s*\{\s*params\s*\}\)/
    },
    {
      name: 'migration',
      pattern: /migration:\s*\(id\)\s*=>\s*request\.get\(`\/memories\/embedding-migrations\/\$\{id\}`\)/
    },
    {
      name: 'retryMigration',
      pattern: /retryMigration:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/embedding-migrations\/\$\{id\}\/retry`\)/
    },
    {
      name: 'cancelMigration',
      pattern: /cancelMigration:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/embedding-migrations\/\$\{id\}\/cancel`\)/
    },
    {
      name: 'retryCleanup',
      pattern: /retryCleanup:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/collections\/\$\{id\}\/cleanup-retry`\)/
    }
  ]

  for (const endpoint of endpoints) {
    assert.match(apiSource, endpoint.pattern, `memoryApi.${endpoint.name} is incomplete`)
  }
})

test('defaultProfileConfigs includes independent memory defaults', () => {
  const first = defaultProfileConfigs()
  const second = defaultProfileConfigs()

  assert.deepEqual(first.memory, {
    enabled: false,
    embedding_channel_id: null,
    embedding_model_id: null,
    top_k: 5,
    candidate_k: 10,
    result_max_chars: 4000
  })
  assert.notStrictEqual(first, second)
  assert.notStrictEqual(first.memory, second.memory)
  assert.notStrictEqual(first.channel.chat_channel, second.channel.chat_channel)
  assert.notStrictEqual(first.channel.chat_channel.rules, second.channel.chat_channel.rules)
  assert.notStrictEqual(first.tool.enabled_tools, second.tool.enabled_tools)
  assert.notStrictEqual(first.tool.allowed_operation_dirs, second.tool.allowed_operation_dirs)

  first.memory.enabled = true
  first.memory.top_k = 99
  first.channel.chat_channel.rules.push({ channel_id: 1, model_id: 'model-a' })
  first.tool.enabled_tools.pop()
  first.tool.allowed_operation_dirs.push('C:/isolated')

  assert.equal(second.memory.enabled, false)
  assert.equal(second.memory.top_k, 5)
  assert.deepEqual(second.channel.chat_channel.rules, [])
  assert.equal(second.tool.enabled_tools.includes('execute_shell'), true)
  assert.deepEqual(second.tool.allowed_operation_dirs, [])
})

test('route and sidebar menu expose the memories page', () => {
  assert.match(routerSource, /\{\s*path:\s*'\/memories',\s*component:\s*\(\)\s*=>\s*import\('\.\.\/views\/MemoriesView\.vue'\)\s*\}/)
  assert.match(appSource, /<el-menu-item index="\/memories">[\s\S]*common\.menu\.memories[\s\S]*<\/el-menu-item>/)
  assert.match(constantsSource, /['"]\/memories['"]\s*:\s*['"]common\.menu\.memories['"]/)
  assert.equal(routeNameMap['/memories'], 'common.menu.memories')
})

test('MemoriesView keeps mutation bodies free of server-owned fields', () => {
  const payloadStart = memoriesSource.indexOf('const payload =')
  const detailsStart = memoriesSource.indexOf('const showDetails', payloadStart)
  assert.notEqual(payloadStart, -1)
  assert.notEqual(detailsStart, -1)
  const mutationSource = memoriesSource.slice(payloadStart, detailsStart)

  for (const field of ['uid', 'source', 'source_message_id', 'collection', 'signature', 'dimensions']) {
    assert.doesNotMatch(mutationSource, new RegExp(`\\b${field}\\s*:`), `mutation body must not send ${field}`)
  }
  assert.match(mutationSource, /memoryApi\.create\(payload\)/)
  assert.match(mutationSource, /memoryApi\.update\(\{[\s\S]*memory_id:\s*form\.id[\s\S]*expected_version:\s*form\.version/)
})

test('MemoriesView uses processing feedback and clears its polling timer', () => {
  const submitStart = memoriesSource.indexOf('const submitMemory =')
  const detailsStart = memoriesSource.indexOf('const showDetails', submitStart)
  assert.notEqual(submitStart, -1)
  assert.notEqual(detailsStart, -1)
  const submitSource = memoriesSource.slice(submitStart, detailsStart)

  assert.match(submitSource, /if \(editorMode\.value === 'create'\) await memoryApi\.create\(payload\)/)
  assert.match(submitSource, /else await memoryApi\.update\(\{[\s\S]*\}\)/)
  assert.match(submitSource, /ElMessage\.info\(t\('memories\.accepted_processing'\)\)/)
  assert.doesNotMatch(submitSource, /ElMessage\.success\(/)
  assert.match(memoriesSource, /const pollTimer = ref\(null\)/)
  assert.match(memoriesSource, /pollTimer\.value = window\.setInterval\(refreshAll,\s*5000\)/)
  assert.match(memoriesSource, /onBeforeUnmount\(\(\) => \{[\s\S]*window\.clearInterval\(pollTimer\.value\)[\s\S]*\}\)/)
})

test('ProfilesView performs memory embedding preview and confirmation as separate steps', () => {
  const previewStart = profilesSource.indexOf('const previewMemoryEmbedding =')
  const closeStart = profilesSource.indexOf('const closeMemoryConfirmation', previewStart)
  const confirmStart = profilesSource.indexOf('const confirmMemoryEmbedding =')
  const migrateStart = profilesSource.indexOf('const migrateToolConfig', confirmStart)
  assert.notEqual(previewStart, -1)
  assert.notEqual(closeStart, -1)
  assert.notEqual(confirmStart, -1)
  assert.notEqual(migrateStart, -1)

  const previewSource = profilesSource.slice(previewStart, closeStart)
  const confirmSource = profilesSource.slice(confirmStart, migrateStart)

  assert.match(previewSource, /profileApi\.memoryEmbeddingPreview\(\{[\s\S]*profile_id:\s*form\.id[\s\S]*embedding_channel_id:\s*target\.channel_id[\s\S]*embedding_model_id:\s*target\.model_id/)
  assert.match(previewSource, /memoryPreview\.value = res\.data\.data \|\| null/)
  assert.match(previewSource, /memoryConfirmationVisible\.value = true/)
  assert.doesNotMatch(previewSource, /memoryEmbeddingConfirm\(/)

  assert.match(confirmSource, /if \(!memoryPreview\.value \|\| !memoryConfirmationChecked\.value \|\| !form\.id\) return/)
  assert.match(confirmSource, /profileApi\.memoryEmbeddingConfirm\(\{[\s\S]*profile_id:\s*form\.id[\s\S]*memory:\s*\{[\s\S]*embedding_channel_id:\s*target\.channel_id[\s\S]*embedding_model_id:\s*target\.model_id[\s\S]*\}[\s\S]*embedding_selection_signature:\s*memoryPreview\.value\.embedding_selection_signature/)
  assert.doesNotMatch(confirmSource, /memoryEmbeddingPreview\(/)
})

test('ProfilesView ordinary saves omit confirmation fields and keep unconfirmed targets inactive', () => {
  const buildStart = profilesSource.indexOf('const buildConfigsForSave =')
  const submitStart = profilesSource.indexOf('const submitForm', buildStart)
  const saveEnd = profilesSource.indexOf('onMounted(() =>', submitStart)
  const currentStart = profilesSource.indexOf('const currentMemoryEmbedding = computed')
  const currentLabelStart = profilesSource.indexOf('const memoryEmbeddingCurrentLabel', currentStart)
  assert.notEqual(buildStart, -1)
  assert.notEqual(submitStart, -1)
  assert.notEqual(saveEnd, -1)
  assert.notEqual(currentStart, -1)
  assert.notEqual(currentLabelStart, -1)
  const saveSource = profilesSource.slice(buildStart, saveEnd)
  const buildSource = profilesSource.slice(buildStart, submitStart)
  const currentSource = profilesSource.slice(currentStart, currentLabelStart)

  assert.match(saveSource, /profileApi\.create\(\{[\s\S]*configs:\s*buildConfigsForSave\(\)/)
  assert.match(saveSource, /profileApi\.update\(form\.id,\s*\{[\s\S]*configs:\s*buildConfigsForSave\(\)/)

  assert.match(currentSource, /channel_id:\s*memoryRuntime\.value\.embedding_channel_id\s*\?\?\s*form\.configs\.memory\?\.embedding_channel_id/)
  assert.match(currentSource, /model_id:\s*memoryRuntime\.value\.embedding_model_id\s*\?\?\s*form\.configs\.memory\?\.embedding_model_id/)
  assert.match(buildSource, /const active = currentMemoryEmbedding\.value/)
  assert.match(buildSource, /configs\.memory\.embedding_channel_id = active\.channel_id \|\| null/)
  assert.match(buildSource, /configs\.memory\.embedding_model_id = active\.model_id \|\| null/)
  assert.match(profilesSource, /watch\(memoryEmbeddingTargetKey, \(\) => \{[\s\S]*memoryPreview\.value = null[\s\S]*memoryConfirmationChecked\.value = false[\s\S]*\}\)/)
  assert.match(profilesSource, /if \(confirmed\.configs\?\.memory\) form\.configs\.memory = \{ \.\.\.form\.configs\.memory, \.\.\.confirmed\.configs\.memory \}/)
})

const requiredMemoryKeys = [
  'title', 'actions', 'memories', 'jobs', 'migrations', 'settings', 'active_config', 'target_config',
  'channel', 'model', 'dimensions', 'collection', 'revision', 'index_status', 'migration_status',
  'cleanup_status', 'progress', 'capacity', 'no_config', 'reindex', 'cleanup_retry', 'refresh', 'create',
  'view', 'edit', 'delete', 'history', 'resume_current', 'keyword_placeholder', 'all_types',
  'updated_at', 'created_at', 'version', 'descending', 'ascending',
  'memory_id', 'memory_key', 'content', 'content_preview', 'source', 'change_evidence', 'current_status',
  'pending', 'indexed', 'suppressed', 'deleted', 'form_create_title', 'form_edit_title',
  'memory_key_placeholder', 'content_placeholder', 'change_evidence_placeholder',
  'suppress_current', 'suppress_hint', 'save', 'cancel', 'required',
  'accepted_processing', 'delete_confirm', 'save_failed', 'load_failed', 'delete_success',
  'operation_success', 'operation_failed', 'details', 'history_title', 'revision_version', 'published_at',
  'restore', 'restore_confirm', 'restore_success', 'no_history', 'deleted_history_read_only', 'job_id', 'operation', 'status', 'attempt',
  'error', 'retry', 'cancel_job', 'retry_confirm', 'cancel_confirm', 'retry_success', 'cancel_success',
  'migration_detail', 'migration_job', 'snapshot', 'delta', 'total_count', 'success_count', 'failure_count',
  'target', 'migration_retry', 'migration_cancel', 'migration_retry_success', 'migration_cancel_success',
  'migration_error', 'no_migrations', 'type_fact', 'type_preference', 'type_project', 'type_todo',
  'type_constraint', 'status_pending', 'status_running', 'status_retry', 'status_succeeded', 'status_failed',
  'status_cancelled', 'status_preparing', 'status_building', 'status_catching_up', 'status_validating',
  'status_switching', 'status_ready', 'status_reindexing', 'status_confirmed', 'status_none', 'operation_create',
  'operation_update', 'operation_restore', 'operation_reindex', 'operation_delete_cleanup',
  'operation_embedding_migration', 'processing', 'not_available', 'confirm', 'close'
]

test('English and Chinese memories namespaces contain the complete stage 7 key set', () => {
  for (const [locale, messages] of [['en', enMemories], ['zh', zhMemories]]) {
    for (const key of requiredMemoryKeys) {
      assert.equal(typeof messages[key], 'string', `${locale}.memories.${key} is missing`)
    }
  }
  assert.deepEqual(Object.keys(enMemories).sort(), Object.keys(zhMemories).sort())
})

test('MemoriesView exposes deleted history as view-only from delete jobs', () => {
  assert.match(memoriesSource, /const historyReadOnly = ref\(false\)/)
  assert.match(memoriesSource, /const canShowDeletedHistory = \(row\) => row\.operation === 'delete_cleanup'/)
  assert.match(memoriesSource, /row\?\.result\?\.record_snapshot/)
  assert.match(memoriesSource, /row\?\.payload\?\.record_snapshot/)
  assert.match(memoriesSource, /v-if="!historyReadOnly"[\s\S]*restoreRevision\(row\)/)
  assert.match(memoriesSource, /memories\.deleted_history_read_only/)
})

test('MemoriesView marks organization form edits as dirty', () => {
  assert.match(memoriesSource, /const organizationFormDirty = ref\(false\)/)
  assert.match(memoriesSource, /<el-switch[\s\S]*@change="markOrganizationFormDirty"/)
  assert.match(memoriesSource, /<el-select v-model="organizationForm\.channel_id"[\s\S]*@change="handleOrganizationChannelChange\(\)"/)
  assert.match(memoriesSource, /<el-select v-model="organizationForm\.model_id"[\s\S]*@change="markOrganizationFormDirty"/)
  assert.match(memoriesSource, /const markOrganizationFormDirty = \(\) => \{ organizationFormDirty\.value = true \}/)
  assert.match(memoriesSource, /const handleOrganizationChannelChange = \(markDirty = true\) => \{[\s\S]*if \(markDirty\) organizationFormDirty\.value = true/)
})

test('MemoriesView only synchronizes organization form from clean or explicit settings loads', () => {
  const loadStart = memoriesSource.indexOf('const loadSettings = async')
  const loadEnd = memoriesSource.indexOf('const loadChannels = async', loadStart)
  assert.notEqual(loadStart, -1)
  assert.notEqual(loadEnd, -1)

  const loadSource = memoriesSource.slice(loadStart, loadEnd)
  assert.match(loadSource, /applySettings\(data, \{ syncOrganizationForm: !silent \|\| !organizationFormDirty\.value \}\)/)
})

test('MemoriesView polls settings and memories while limiting tab-specific polling', () => {
  const refreshStart = memoriesSource.indexOf('const refreshAll =')
  const refreshEnd = memoriesSource.indexOf('const markOrganizationFormDirty =', refreshStart)
  assert.notEqual(refreshStart, -1)
  assert.notEqual(refreshEnd, -1)

  const refreshSource = memoriesSource.slice(refreshStart, refreshEnd)
  assert.match(refreshSource, /if \(actionLoading\.value !== 'settings'\) loadSettings\(true\)/)
  assert.match(refreshSource, /loadMemories\(true\)/)
  assert.match(refreshSource, /if \(activeTab\.value === 'jobs'\) loadJobs\(true\)/)
  assert.match(refreshSource, /if \(activeTab\.value === 'migrations'\) loadMigrations\(true\)/)
})

test('MemoriesView invalidates settings GETs around settings saves', () => {
  const saveStart = memoriesSource.indexOf('const saveSettings = async')
  const saveEnd = memoriesSource.indexOf('const organize = async', saveStart)
  assert.notEqual(saveStart, -1)
  assert.notEqual(saveEnd, -1)

  const saveSource = memoriesSource.slice(saveStart, saveEnd)
  const sequenceIndex = saveSource.indexOf('settingsRequestSeq += 1')
  const loadingIndex = saveSource.indexOf('settingsLoading.value = false')
  const postIndex = saveSource.indexOf('memoryApi.updateSettings(')
  const applyIndex = saveSource.indexOf('applySettings(data)')
  const catchIndex = saveSource.indexOf('} catch (error)')
  assert.ok(sequenceIndex >= 0 && sequenceIndex < postIndex)
  assert.ok(loadingIndex >= 0 && loadingIndex < postIndex)
  assert.ok(postIndex >= 0 && postIndex < applyIndex)
  assert.ok(applyIndex >= 0 && applyIndex < catchIndex)
  assert.doesNotMatch(saveSource.slice(catchIndex), /applySettings\(|organizationFormDirty\.value\s*=\s*false/)
})

test('MemoriesView rejects stale responses for every polled collection', () => {
  const loaders = [
    ['loadSettings', 'settingsRequestSeq'],
    ['loadMemories', 'memoriesRequestSeq'],
    ['loadJobs', 'jobsRequestSeq'],
    ['loadMigrations', 'migrationsRequestSeq']
  ]

  for (const [loader, sequence] of loaders) {
    assert.match(
      memoriesSource,
      new RegExp(`const ${loader} = async \\(silent = false\\) => \\{[\\s\\S]*?const requestSeq = \\+\\+${sequence}[\\s\\S]*?if \\(requestSeq !== ${sequence}\\) return`),
      `${loader} must reject stale responses`
    )
  }
})

test('MemoriesView only renders available organization counts', () => {
  const countsStart = memoriesSource.indexOf('const jobCountsText =')
  const budgetStart = memoriesSource.indexOf('const tokenBudgetText =', countsStart)
  assert.notEqual(countsStart, -1)
  assert.notEqual(budgetStart, -1)

  const countsSource = memoriesSource.slice(countsStart, budgetStart)
  assert.match(countsSource, /filter\(\(\[, value\]\) => value !== null && value !== undefined\)/)
  assert.match(countsSource, /return counts\.length \? [\s\S]*: '-'/)
  assert.doesNotMatch(countsSource, /\$\{row\.\w+ \?\? 0\}/)
})
