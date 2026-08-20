import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { defaultProfileConfigs, routeNameMap } from '../src/constants/index.js'
import enMemories from '../src/i18n/locales/en/memories.js'
import enProfiles from '../src/i18n/locales/en/profiles.js'
import zhMemories from '../src/i18n/locales/zh/memories.js'
import zhProfiles from '../src/i18n/locales/zh/profiles.js'

const dashboardRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const readSource = relativePath => readFileSync(resolve(dashboardRoot, relativePath), 'utf8')

const apiSource = readSource('src/api/index.js')
const appSource = readSource('src/App.vue')
const appScssSource = readSource('src/assets/css/app.scss')
const constantsSource = readSource('src/constants/index.js')
const memoriesSource = readSource('src/views/MemoriesView.vue')
const memoryEmbeddingDialogSource = readSource('src/components/MemoryEmbeddingDialog.vue')
const profileFormSource = readSource('src/components/ProfileFormDialog.vue')
const profilesScssSource = readSource('src/assets/css/ProfilesView.scss')
const profilesSource = readSource('src/views/ProfilesView.vue')
const routerSource = readSource('src/router/index.js')

test('memoryApi contains every memory endpoint with the required HTTP method', () => {
  const endpoints = [
    {
      name: 'list',
      pattern: /list:\s*\(params,\s*config\s*=\s*\{\}\)\s*=>\s*request\.get\('\/memories\/list',\s*\{\s*\.\.\.config,\s*params\s*\}\)/
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
      pattern: /jobs:\s*\(params,\s*config\s*=\s*\{\}\)\s*=>\s*request\.get\('\/memories\/jobs',\s*\{\s*\.\.\.config,\s*params\s*\}\)/
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
      name: 'resumeCurrent',
      pattern: /resumeCurrent:\s*\(id,\s*data\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/resume-current`,\s*data\)/
    },
    {
      name: 'settings',
      pattern: /settings:\s*\(config\s*=\s*\{\}\)\s*=>\s*request\.get\('\/memories\/settings',\s*config\)/
    },
    {
      name: 'reindex',
      pattern: /reindex:\s*\(data\)\s*=>\s*request\.post\('\/memories\/reindex',\s*data\)/
    },
    {
      name: 'migrations',
      pattern: /migrations:\s*\(params,\s*config\s*=\s*\{\}\)\s*=>\s*request\.get\('\/memories\/embedding-migrations',\s*\{\s*\.\.\.config,\s*params\s*\}\)/
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
    },
    {
      name: 'updateSettings',
      pattern: /updateSettings:\s*\(data\)\s*=>\s*request\.post\('\/memories\/settings',\s*data\)/
    },
    {
      name: 'organize',
      pattern: /organize:\s*\(data\)\s*=>\s*request\.post\('\/memories\/organize',\s*data\)/
    },
    {
      name: 'pin',
      pattern: /pin:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/pin`\)/
    },
    {
      name: 'unpin',
      pattern: /unpin:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/unpin`\)/
    }
  ]

  for (const endpoint of endpoints) {
    assert.match(apiSource, endpoint.pattern, `memoryApi.${endpoint.name} is incomplete`)
  }
})

test('memoryApi pin and unpin POST requests do not send a request body', () => {
  assert.match(apiSource, /pin:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/pin`\)/)
  assert.match(apiSource, /unpin:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/unpin`\)/)
  assert.doesNotMatch(apiSource, /pin:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/pin`,/)
  assert.doesNotMatch(apiSource, /unpin:\s*\(id\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/unpin`,/)
})

test('profileApi exposes user memory settings through the profile GET endpoint', () => {
  assert.match(apiSource, /memorySettings:\s*\(params\s*=\s*\{\}\)\s*=>\s*request\.get\('\/profiles\/memory-settings',\s*\{\s*params\s*\}\)/)
})

test('ProfileFormDialog separates profile memory settings from user auto organization settings', () => {
  const baseStart = profileFormSource.indexOf('<el-tab-pane :label="$t(\'profiles.base_settings\')"')
  const memoryStart = profileFormSource.indexOf('<el-tab-pane :label="$t(\'profiles.memory_settings\')"')
  const modelStart = profileFormSource.indexOf('<el-tab-pane :label="$t(\'profiles.model_settings\')"')
  assert.ok(baseStart >= 0 && memoryStart > baseStart && modelStart > memoryStart)

  const baseSource = profileFormSource.slice(baseStart, memoryStart)
  const memorySource = profileFormSource.slice(memoryStart, modelStart)
  assert.doesNotMatch(baseSource, /long_term_memory|form\.configs\.memory|memory_embedding|memory_organization/)
  assert.match(memorySource, /class="model-summary memory-embedding-summary"/)
  assert.match(memorySource, /form\.configs\.memory\.enabled/)
  assert.match(memorySource, /form\.configs\.memory\.top_k/)
  assert.match(memorySource, /form\.configs\.memory\.candidate_k/)
  assert.match(memorySource, /form\.configs\.memory\.result_max_chars/)
  assert.match(memorySource, /memory_embedding_settings/)
  assert.match(memorySource, /memoryEmbeddingCurrentLabel/)
  assert.match(memorySource, /manage-memory-embedding/)
  assert.match(memorySource, /el-form-item__content memory-embedding-action/)
  assert.doesNotMatch(memorySource, /memoryEmbeddingTargetKey/)
  assert.doesNotMatch(memorySource, /preview-memory-embedding/)
  assert.match(memorySource, /form\.memory_organization\.auto_organize_enabled/)
  assert.match(memorySource, /form\.memory_organization\.organization_channel_id/)
  assert.match(memorySource, /form\.memory_organization\.organization_model_id/)
  assert.match(memorySource, /<el-option v-for="channel in memoryOrganizationChannels"[^>]*:label="channel\.name"/)
  assert.doesNotMatch(memorySource, /memoryOrganizationChannels"[^>]*:[^>]*label="[^"\n]*channel\.id/)
  const configLineStyle = profilesScssSource.match(/\.config-line\s*\{([\s\S]*?)\}/)
  assert.ok(configLineStyle)
  assert.match(configLineStyle[1], /justify-content:\s*space-between;/)
  const memoryEmbeddingSummaryStyle = profilesScssSource.match(/\.memory-embedding-summary\s*\{([\s\S]*?)\n  \}\n}/)
  assert.ok(memoryEmbeddingSummaryStyle)
  assert.match(memoryEmbeddingSummaryStyle[1], /\.config-line\s*\{[\s\S]*?justify-content:\s*flex-start;[\s\S]*?gap:\s*8px;/)
  assert.match(memoryEmbeddingSummaryStyle[1], /\.config-line\s*>\s*b\s*\{[\s\S]*?text-align:\s*left;/)
  const memoryEmbeddingActionStyle = profilesScssSource.match(/\.memory-embedding-action\s*\{([\s\S]*?)\}/)
  assert.ok(memoryEmbeddingActionStyle)
  assert.match(memoryEmbeddingActionStyle[1], /width:\s*100%;/)
  assert.match(memoryEmbeddingActionStyle[1], /justify-content:\s*flex-end;/)
  assert.match(profileFormSource, /<el-button type="primary"[\s\S]*:disabled="memorySettingsLoading \|\| memorySettingsUnavailable \|\| !memorySettingsReady"[\s\S]*\$t\('profiles\.save'\)/)
})

test('MemoryEmbeddingDialog keeps target detection and confirmation independent', () => {
  const targetLabelStart = memoryEmbeddingDialogSource.indexOf('<div class="memory-embedding-dialog__field-label">')
  const controlRowStart = memoryEmbeddingDialogSource.indexOf('<div class="memory-embedding-dialog__control-row">')
  const previewStart = memoryEmbeddingDialogSource.indexOf('<div v-if="props.preview" class="memory-embedding-dialog__preview">', controlRowStart)
  assert.ok(targetLabelStart >= 0 && controlRowStart > targetLabelStart)
  assert.ok(controlRowStart >= 0 && previewStart > controlRowStart)

  const targetLabelSource = memoryEmbeddingDialogSource.slice(targetLabelStart, controlRowStart)
  assert.equal((targetLabelSource.match(/<HelpTooltip\b/g) || []).length, 1)
  assert.match(targetLabelSource, /<HelpTooltip\s+:content="memoryEmbeddingTargetHint"\s*\/>/)

  const hintStart = memoryEmbeddingDialogSource.indexOf('const memoryEmbeddingTargetHint = computed')
  const hintEnd = memoryEmbeddingDialogSource.indexOf('const isInitialSelection = computed', hintStart)
  assert.ok(hintStart >= 0 && hintEnd > hintStart)
  const hintSource = memoryEmbeddingDialogSource.slice(hintStart, hintEnd)
  assert.match(hintSource, /`\$\{t\('profiles\.memory_embedding_target_hint'\)\}\s+\$\{t\('profiles\.memory_embedding_preview_hint'\)\}`/)

  const controlRowSource = memoryEmbeddingDialogSource.slice(controlRowStart, previewStart)
  const selectIndex = controlRowSource.indexOf('<el-select')
  const detectButtonIndex = controlRowSource.indexOf('<el-button')
  assert.ok(selectIndex >= 0 && detectButtonIndex > selectIndex)
  assert.doesNotMatch(controlRowSource, /HelpTooltip/)
  assert.doesNotMatch(memoryEmbeddingDialogSource, /memory-embedding-dialog__preview-action/)

  assert.match(memoryEmbeddingDialogSource, /:model-value="props\.targetKey"/)
  assert.match(memoryEmbeddingDialogSource, /@update:model-value="handleTargetKeyChange"/)
  assert.match(memoryEmbeddingDialogSource, /t\('profiles\.memory_embedding_preview'\)/)
  assert.match(memoryEmbeddingDialogSource, /emit\('detect'\)/)
  assert.match(memoryEmbeddingDialogSource, /emit\('confirm'\)/)
  assert.match(memoryEmbeddingDialogSource, /const sameConfiguration = computed\(\(\) => Boolean\(props\.preview\)[\s\S]*&& !isInitialSelection\.value[\s\S]*&& !props\.requiresMigration\)/)
  assert.match(memoryEmbeddingDialogSource, /const confirmationRequired = computed\(\(\) => Boolean\(props\.preview\) && !sameConfiguration\.value\)/)
  assert.match(memoryEmbeddingDialogSource, /v-if="confirmationRequired"/)
  assert.match(memoryEmbeddingDialogSource, /isInitialSelection\.value\s*\?\s*t\('profiles\.memory_embedding_confirm_enable'\)\s*:\s*t\('profiles\.memory_embedding_start_migration'\)/)
  assert.match(memoryEmbeddingDialogSource, /:disabled="!props\.confirmationChecked \|\| props\.previewing"/)
  assert.match(memoryEmbeddingDialogSource, /const handleConfirm = \(\) => \{\s*if \(!confirmationRequired\.value \|\| !props\.confirmationChecked \|\| props\.confirming \|\| props\.previewing\) return/)
})

test('profile knowledge base binding keeps loading state separate from profile saving', () => {
  assert.match(profilesSource, /const knowledgeBasesLoading = ref\(false\)/)
  assert.match(profilesSource, /const knowledgeBasesReady = ref\(false\)/)
  assert.match(profilesSource, /const knowledgeBasesUnavailable = ref\(false\)/)

  const fetchStart = profilesSource.indexOf('const fetchKnowledgeBases = async')
  const fetchEnd = profilesSource.indexOf('const fetchUsers = async', fetchStart)
  assert.ok(fetchStart >= 0 && fetchEnd > fetchStart)
  const fetchSource = profilesSource.slice(fetchStart, fetchEnd)
  assert.match(fetchSource, /knowledgeBasesLoading\.value = true/)
  assert.match(fetchSource, /knowledgeBasesReady\.value = false/)
  assert.match(fetchSource, /knowledgeBasesUnavailable\.value = false/)
  assert.match(fetchSource, /knowledgeBasesReady\.value = true/)
  assert.match(fetchSource, /knowledgeBasesUnavailable\.value = true/)
  assert.match(fetchSource, /knowledgeBasesLoading\.value = false/)
  assert.doesNotMatch(fetchSource, /knowledgeBases\.value = \[\]/)

  const watcherStart = profilesSource.indexOf('const filterFormKnowledgeBaseIds =')
  const watcherEnd = profilesSource.indexOf('const normalizeMemoryOrganizationSelection', watcherStart)
  assert.ok(watcherStart >= 0 && watcherEnd > watcherStart)
  const watcherSource = profilesSource.slice(watcherStart, watcherEnd)
  assert.match(watcherSource, /if \(!knowledgeBasesReady\.value\) return/)
  assert.match(watcherSource, /filterKnowledgeBaseIdsForOwner\(form\.knowledge_base_ids, knowledgeBases\.value, form\.uid\)/)
  assert.match(watcherSource, /watch\(\(\) => form\.uid, filterFormKnowledgeBaseIds\)/)
  assert.match(watcherSource, /watch\(\[knowledgeBasesReady, knowledgeBases\], filterFormKnowledgeBaseIds\)/)
})

test('profile knowledge base selection follows the three-state availability contract', () => {
  assert.match(profileFormSource, /knowledgeBasesLoading: \{ type: Boolean, required: true \}/)
  assert.match(profileFormSource, /knowledgeBasesReady: \{ type: Boolean, required: true \}/)
  assert.match(profileFormSource, /knowledgeBasesUnavailable: \{ type: Boolean, required: true \}/)
  assert.match(profileFormSource, /<el-select(?=[\s\S]*v-model="form\.knowledge_base_ids")(?=[\s\S]*:disabled="knowledgeBasesLoading \|\| knowledgeBasesUnavailable \|\| !knowledgeBasesReady")[\s\S]*>/)
})

test('profile save payload includes knowledge base ids only when the list is ready', () => {
  const submitStart = profilesSource.indexOf('const submitForm =')
  const submitEnd = profilesSource.indexOf('onMounted(() =>', submitStart)
  assert.ok(submitStart >= 0 && submitEnd > submitStart)
  const submitSource = profilesSource.slice(submitStart, submitEnd)

  assert.match(profilesSource, /buildKnowledgeBaseBindingPayload,\s*\n\s*filterKnowledgeBaseIdsForOwner/)
  assert.match(submitSource, /const payload = \{[\s\S]*const knowledgeBaseIds = buildKnowledgeBaseBindingPayload\(form\.knowledge_base_ids, knowledgeBasesReady\.value\)/)
  assert.match(submitSource, /if \(knowledgeBaseIds !== undefined\) \{\s*payload\.knowledge_base_ids = knowledgeBaseIds\s*\}/)
  assert.match(submitSource, /profileApi\.create\(payload\)/)
  assert.match(submitSource, /profileApi\.update\(form\.id, payload\)/)
  assert.doesNotMatch(submitSource, /knowledge_base_ids:\s*form\.knowledge_base_ids/)
  const footerStart = profileFormSource.indexOf('<template #footer>')
  assert.doesNotMatch(profileFormSource.slice(footerStart), /:disabled="[^"\n]*knowledgeBases(?:Loading|Ready|Unavailable)/)
})

const requiredProfileMemoryKeys = [
  'memory_settings', 'long_term_memory_settings', 'long_term_memory_enabled', 'long_term_memory_enabled_hint',
  'memory_storage_not_configured', 'memory_settings_unavailable', 'memory_top_k', 'memory_top_k_hint',
  'memory_candidate_k', 'memory_candidate_k_hint', 'memory_result_max_chars', 'memory_result_max_chars_hint',
  'memory_embedding_settings', 'memory_embedding_configure', 'memory_embedding_change',
  'memory_embedding_migration_active', 'memory_embedding_workflow_hint',
  'memory_embedding_dialog_configure_title', 'memory_embedding_dialog_change_title', 'memory_embedding_dialog_hint',
  'memory_embedding_migration_target', 'memory_embedding_migration_status', 'memory_embedding_migration_notice',
  'memory_embedding_confirm_enable', 'memory_embedding_start_migration', 'memory_embedding_enable_success',
  'memory_embedding_migration_started',
  'memory_embedding_target', 'memory_embedding_target_placeholder', 'memory_embedding_target_hint',
  'memory_embedding_current', 'memory_embedding_preview', 'memory_embedding_preview_hint',
  'memory_embedding_not_configured', 'memory_embedding_dimensions', 'memory_embedding_estimated_records',
  'memory_embedding_confirmation_title', 'memory_embedding_confirmation_first_notice',
  'memory_embedding_confirmation_change_notice', 'memory_embedding_confirmation_same_notice',
  'memory_embedding_confirmation_check', 'memory_embedding_confirm', 'memory_embedding_confirm_success',
  'memory_embedding_create_hint', 'memory_organization_settings', 'auto_organize_enabled',
  'auto_organize_enabled_hint', 'organization_channel', 'organization_channel_placeholder',
  'organization_model', 'organization_model_placeholder', 'selected_model', 'context_window_k',
  'model_max_tokens', 'required_output_tokens', 'organization_model_not_selected',
  'organization_selection_pair_required', 'organization_model_limits_invalid',
  'organization_max_tokens_too_small', 'organization_model_required', 'organization_model_invalid',
  'load_memory_settings_failed', 'memory_settings_unavailable'
]

test('English and Chinese profiles namespaces contain matching memory settings keys', () => {
  for (const [locale, messages] of [['en', enProfiles], ['zh', zhProfiles]]) {
    for (const key of requiredProfileMemoryKeys) {
      assert.equal(typeof messages[key], 'string', `${locale}.profiles.${key} is missing`)
    }
  }
  assert.deepEqual(Object.keys(enProfiles).sort(), Object.keys(zhProfiles).sort())
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

test('app layout adds memory page spacing and keeps the fixed footer above it', () => {
  const appMainMatch = appScssSource.match(/\.app-main\s*\{([\s\S]*?)\}/)
  const memoriesAppMainMatch = appScssSource.match(/\.app-main\.app-main--memories\s*\{([\s\S]*?)\}/)
  const appFooterMatch = appScssSource.match(/\.app-footer\s*\{([\s\S]*?)\}/)
  assert.ok(appMainMatch, '.app-main source block is missing')
  assert.ok(memoriesAppMainMatch, '.app-main--memories source block is missing')
  assert.ok(appFooterMatch, '.app-footer source block is missing')

  const appMainSource = appMainMatch[1]
  const memoriesAppMainSource = memoriesAppMainMatch[1]
  const appFooterSource = appFooterMatch[1]
  assert.match(appSource, /<el-main class="app-main" :class="\{ 'app-main--memories': \$route\.path === '\/memories' \}">/)
  assert.doesNotMatch(appMainSource, /padding-bottom:\s*80px\s*;/)
  assert.match(memoriesAppMainSource, /padding-bottom:\s*80px\s*;/)
  assert.match(appFooterSource, /\bposition:\s*fixed\s*;/)

  const footerZIndex = appFooterSource.match(/\bz-index:\s*(\d+)\s*;/)
  assert.ok(footerZIndex, '.app-footer z-index declaration is missing')
  assert.ok(Number(footerZIndex[1]) >= 1000, '.app-footer must stay above the table layers')
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
  assert.match(memoriesSource, /pollTimer\.value = window\.setTimeout\(async \(\) => \{[\s\S]*await refreshAll\(\)/)
  assert.match(memoriesSource, /onBeforeUnmount\(\(\) => \{[\s\S]*window\.clearTimeout\(pollTimer\.value\)[\s\S]*\}\)/)
})

test('ProfilesView performs memory embedding preview and confirmation as separate steps', () => {
  const previewStart = profilesSource.indexOf('const previewMemoryEmbedding =')
  const openStart = profilesSource.indexOf('const openMemoryEmbeddingDialog =', previewStart)
  const closeStart = profilesSource.indexOf('const closeMemoryEmbeddingDialog', openStart)
  const confirmStart = profilesSource.indexOf('const confirmMemoryEmbedding =')
  const migrateStart = profilesSource.indexOf('const migrateToolConfig', confirmStart)
  const showDialogStart = profilesSource.indexOf('const showDialog =')
  const ownerChangeStart = profilesSource.indexOf('const handleMemoryOwnerChange', showDialogStart)
  const visibilityStart = profilesSource.indexOf('const handleDialogVisibilityChange =')
  const buildConfigsStart = profilesSource.indexOf('const buildConfigsForSave', visibilityStart)
  assert.notEqual(previewStart, -1)
  assert.notEqual(openStart, -1)
  assert.notEqual(closeStart, -1)
  assert.notEqual(confirmStart, -1)
  assert.notEqual(migrateStart, -1)
  assert.notEqual(showDialogStart, -1)
  assert.notEqual(ownerChangeStart, -1)
  assert.notEqual(visibilityStart, -1)
  assert.notEqual(buildConfigsStart, -1)

  const previewSource = profilesSource.slice(previewStart, openStart)
  const openSource = profilesSource.slice(openStart, closeStart)
  const confirmSource = profilesSource.slice(confirmStart, migrateStart)
  const showDialogSource = profilesSource.slice(showDialogStart, ownerChangeStart)
  const visibilitySource = profilesSource.slice(visibilityStart, buildConfigsStart)

  assert.match(previewSource, /const profileId = form\.id[\s\S]*const targetKey = target\.key/)
  assert.match(previewSource, /profileApi\.memoryEmbeddingPreview\(\{[\s\S]*profile_id:\s*profileId[\s\S]*embedding_channel_id:\s*target\.channel_id[\s\S]*embedding_model_id:\s*target\.model_id/)
  const previewResetIndex = previewSource.indexOf('memoryPreview.value = null')
  const confirmationResetIndex = previewSource.indexOf('memoryConfirmationChecked.value = false')
  const previewRequestIndex = previewSource.indexOf('profileApi.memoryEmbeddingPreview(')
  assert.notEqual(previewResetIndex, -1)
  assert.notEqual(confirmationResetIndex, -1)
  assert.notEqual(previewRequestIndex, -1)
  assert.ok(previewResetIndex < previewRequestIndex, 'preview must clear stale data before requesting a new preview')
  assert.ok(confirmationResetIndex < previewRequestIndex, 'preview must clear confirmation before requesting a new preview')
  const previewIdentityCheck = previewSource.indexOf('if (!isCurrentMemoryEmbeddingRequest(requestGeneration, profileId, targetKey)) return')
  const previewResponseApply = previewSource.indexOf('memoryPreview.value = res.data.data || null')
  assert.notEqual(previewIdentityCheck, -1)
  assert.notEqual(previewResponseApply, -1)
  assert.ok(previewIdentityCheck < previewResponseApply, 'preview must validate request identity before applying the response')
  assert.doesNotMatch(previewSource, /memoryEmbeddingConfirm\(/)
  assert.doesNotMatch(previewSource, /memoryEmbeddingDialogVisible\.value\s*=\s*true/)

  assert.match(openSource, /const openMemoryEmbeddingDialog = \(\) => \{\s*if \(dialogType\.value !== 'edit' \|\| !form\.id \|\| memoryEmbeddingMigrationActive\.value\) return[\s\S]*memoryEmbeddingDialogVisible\.value = true/)
  assert.match(profilesSource, /@manage-memory-embedding="openMemoryEmbeddingDialog"/)
  assert.match(profilesSource, /<MemoryEmbeddingDialog[\s\S]*@detect="previewMemoryEmbedding"[\s\S]*@confirm="confirmMemoryEmbedding"/)

  assert.match(confirmSource, /if \(!memoryPreview\.value \|\| !memoryConfirmationChecked\.value \|\| !form\.id\) return/)
  assert.match(confirmSource, /const profileId = form\.id[\s\S]*const targetKey = target\.key/)
  const memoryPayloadStart = confirmSource.indexOf('const memory =')
  const memoryPayloadEnd = confirmSource.indexOf('const embeddingSelectionSignature', memoryPayloadStart)
  assert.ok(memoryPayloadStart >= 0 && memoryPayloadEnd > memoryPayloadStart)
  const memoryPayloadSource = confirmSource.slice(memoryPayloadStart, memoryPayloadEnd)
  assert.match(memoryPayloadSource, /const memory = \{[\s\S]*\.\.\.persistedMemoryConfig\.value,[\s\S]*embedding_channel_id:\s*target\.channel_id,[\s\S]*embedding_model_id:\s*target\.model_id[\s\S]*\}/)
  assert.doesNotMatch(memoryPayloadSource, /form\.configs\.memory/)
  assert.doesNotMatch(confirmSource, /const currentMemory = form\.configs\.memory/)
  assert.match(confirmSource, /profileApi\.memoryEmbeddingConfirm\(\{[\s\S]*profile_id:\s*profileId[\s\S]*memory,\s*embedding_selection_signature:\s*embeddingSelectionSignature/)
  const confirmIdentityCheck = confirmSource.indexOf('if (!isCurrentMemoryEmbeddingRequest(requestGeneration, profileId, targetKey)')
  const confirmResponseApply = confirmSource.indexOf('const confirmed = res.data.data || {}')
  assert.notEqual(confirmIdentityCheck, -1)
  assert.notEqual(confirmResponseApply, -1)
  assert.ok(confirmIdentityCheck < confirmResponseApply, 'confirmation must validate request identity before applying the response')
  assert.doesNotMatch(confirmSource, /memoryEmbeddingPreview\(/)

  assert.match(showDialogSource, /const showDialog = \(type, row = null\) => \{\s*invalidateMemoryEmbeddingRequests\(\)/)
  assert.match(visibilitySource, /const handleDialogVisibilityChange = \(visible\) => \{\s*if \(visible\) return\s*closeMemoryEmbeddingDialog\(\)/)
})

test('ProfilesView loads user memory settings with a latest-request tracker', () => {
  const loadStart = profilesSource.indexOf('const loadMemorySettings = async')
  const loadEnd = profilesSource.indexOf('const removeAllowedOperationDir', loadStart)
  assert.notEqual(loadStart, -1)
  assert.notEqual(loadEnd, -1)

  const loadSource = profilesSource.slice(loadStart, loadEnd)
  assert.match(profilesSource, /const memorySettingsRequestTracker = createLatestRequestTracker\(\)/)
  assert.match(loadSource, /const requestSeq = memorySettingsRequestTracker\.begin\(\)/)
  assert.match(loadSource, /profileApi\.memorySettings\(params\)/)
  assert.match(loadSource, /if \(!memorySettingsRequestTracker\.isCurrent\(requestSeq\)\) return/)
  assert.match(loadSource, /if \(memorySettingsRequestTracker\.isCurrent\(requestSeq\)\) memorySettingsLoading\.value = false/)
})

test('ProfilesView reloads user memory settings when an administrator changes the create owner', () => {
  const ownerStart = profilesSource.indexOf('const handleMemoryOwnerChange =')
  const visibilityStart = profilesSource.indexOf('const handleDialogVisibilityChange', ownerStart)
  assert.notEqual(ownerStart, -1)
  assert.notEqual(visibilityStart, -1)

  const ownerSource = profilesSource.slice(ownerStart, visibilityStart)
  assert.match(profilesSource, /@owner-change="handleMemoryOwnerChange"/)
  assert.match(ownerSource, /if \(!dialogVisible\.value \|\| dialogType\.value !== 'create'\) return/)
  assert.match(ownerSource, /form\.uid = uid \|\| null/)
  assert.match(ownerSource, /form\.memory_organization = \{[\s\S]*organization_channel_id: null[\s\S]*organization_model_id: null/)
  assert.match(ownerSource, /loadMemorySettings\(\)/)
})

test('ProfilesView sends auto organization settings at the top level and keeps configs.memory profile-scoped', () => {
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

  assert.match(saveSource, /const payload = \{[\s\S]*memory_organization:\s*buildOrganizationSettingsPayload\(form\.memory_organization\),[\s\S]*configs:\s*buildConfigsForSave\(\)/)
  assert.match(saveSource, /profileApi\.create\(payload\)[\s\S]*profileApi\.update\(form\.id, payload\)/)
  assert.match(profilesSource, /import \{[\s\S]*buildOrganizationSettingsPayload,[\s\S]*validateOrganizationSettings[\s\S]*\} from '\.\.\/utils\/memoryManagement'/)

  assert.match(currentSource, /channel_id:\s*memoryRuntime\.value\.embedding_channel_id\s*\?\?\s*form\.configs\.memory\?\.embedding_channel_id/)
  assert.match(currentSource, /model_id:\s*memoryRuntime\.value\.embedding_model_id\s*\?\?\s*form\.configs\.memory\?\.embedding_model_id/)
  assert.match(buildSource, /const active = currentMemoryEmbedding\.value/)
  assert.match(buildSource, /configs\.memory\.embedding_channel_id = active\.channel_id \|\| null/)
  assert.match(buildSource, /configs\.memory\.embedding_model_id = active\.model_id \|\| null/)
  assert.doesNotMatch(buildSource, /auto_organize_enabled|organization_channel_id|organization_model_id/)
  assert.match(profilesSource, /watch\(memoryEmbeddingTargetKey, \(\) => \{[\s\S]*memoryPreview\.value = null[\s\S]*memoryConfirmationChecked\.value = false[\s\S]*\}\)/)
  const confirmedMemoryStart = profilesSource.indexOf('if (confirmed.configs?.memory) {')
  const confirmedMemoryEnd = profilesSource.indexOf('memoryRuntime.value =', confirmedMemoryStart)
  assert.ok(confirmedMemoryStart >= 0 && confirmedMemoryEnd > confirmedMemoryStart)
  const confirmedMemorySource = profilesSource.slice(confirmedMemoryStart, confirmedMemoryEnd)
  assert.match(confirmedMemorySource, /persistedMemoryConfig\.value = JSON\.parse\(JSON\.stringify\(confirmed\.configs\.memory\)\)/)
  assert.match(confirmedMemorySource, /form\.configs\.memory = \{\s*\.\.\.form\.configs\.memory,\s*embedding_channel_id:\s*confirmed\.configs\.memory\.embedding_channel_id,\s*embedding_model_id:\s*confirmed\.configs\.memory\.embedding_model_id\s*\}/)
  assert.doesNotMatch(confirmedMemorySource, /\.\.\.confirmed\.configs\.memory/)
})

const requiredMemoryKeys = [
  'title', 'actions', 'memories', 'jobs', 'migrations', 'settings', 'expand_settings', 'collapse_settings', 'current_task', 'active_config', 'target_config',
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
  'no_history', 'deleted_history_read_only', 'job_id', 'operation', 'status', 'attempt',
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
  assert.match(memoriesSource, /const canShowDeletedHistory = \(row\) => row\.operation === 'delete_cleanup'/)
  assert.match(memoriesSource, /row\?\.result\?\.record_snapshot/)
  assert.match(memoriesSource, /row\?\.payload\?\.record_snapshot/)
  assert.doesNotMatch(memoriesSource, /historyReadOnly|restoreRevision|memories\.restore/)
  assert.match(memoriesSource, /<el-alert type="info"[\s\S]*memories\.deleted_history_read_only/)
  assert.match(memoriesSource, /memories\.deleted_history_read_only/)
})

test('memory restore API and history restore action are removed while resume-current remains', () => {
  assert.doesNotMatch(apiSource, /memoryApi\s*=\s*\{[\s\S]*restore:/)
  assert.match(apiSource, /resumeCurrent:\s*\(id,\s*data\)\s*=>\s*request\.post\(`\/memories\/\$\{id\}\/resume-current`,\s*data\)/)
  assert.doesNotMatch(memoriesSource, /memoryApi\.restore|restoreRevision|restore_confirm|restore_success/)
})

test('MemoriesView never offers retry for legacy restore jobs', () => {
  assert.match(memoriesSource, /const canRetry = \(row\) => \{\s*if \(row\.operation === 'restore'\) return false/)
})

test('MemoriesView keeps runtime settings and organization actions read-only', () => {
  assert.doesNotMatch(memoriesSource, /saveSettings/)
  assert.doesNotMatch(memoriesSource, /memories\.save_settings/)
  assert.doesNotMatch(memoriesSource, /organizationForm/)
  assert.doesNotMatch(memoriesSource, /memoryApi\.updateSettings/)
  assert.doesNotMatch(memoriesSource, /auto_organize_enabled|organization_channel_id|organization_model_id/)
  assert.doesNotMatch(memoriesSource, /<el-switch\b|v-model="[^"]*organization/)
  assert.match(memoriesSource, /memoryApi\.settings\(\{/)
  assert.match(memoriesSource, /const refreshAll = async \(\) => \{[\s\S]*loadSettings\(true\)/)
  assert.match(memoriesSource, /const organize = async \(\) =>/)
  assert.match(memoriesSource, /memoryApi\.organize\(buildOrganizePayload\(newDedupeKey\(\)\)\)/)
  assert.match(memoriesSource, /const currentMemoryTask = computed\(\(\) => getCurrentMemoryTask\(settings\)\)/)
  assert.match(memoriesSource, /settings\.organization\?\.current_job_id/)
  assert.match(memoriesSource, /settings\.organization\?\.recent_job\?\.status/)
})

test('MemoriesView polls settings and memories while limiting tab-specific polling', () => {
  const refreshStart = memoriesSource.indexOf('const refreshAll =')
  const scheduleStart = memoriesSource.indexOf('const scheduleRefresh =', refreshStart)
  const organizeStart = memoriesSource.indexOf('const organize =', scheduleStart)
  assert.notEqual(refreshStart, -1)
  assert.ok(scheduleStart > refreshStart)
  assert.ok(organizeStart > scheduleStart)

  const refreshSource = memoriesSource.slice(refreshStart, scheduleStart)
  const scheduleSource = memoriesSource.slice(scheduleStart, organizeStart)
  assert.match(refreshSource, /const refreshAll = async \(\) => \{/)
  assert.match(refreshSource, /loadSettings\(true\)/)
  assert.match(refreshSource, /loadMemories\(true\)/)
  assert.match(refreshSource, /if \(activeTab\.value === 'jobs'\) requests\.push\(loadJobs\(true\)\)/)
  assert.match(refreshSource, /if \(activeTab\.value === 'migrations'\) requests\.push\(loadMigrations\(true\)\)/)
  assert.match(refreshSource, /await Promise\.all\(requests\)/)
  assert.match(scheduleSource, /await refreshAll\(\)[\s\S]*if \(!pollingStopped\) scheduleRefresh\(\)/)
})

test('MemoriesView shows the task summary only for real tasks and keeps it visible when settings expand or collapse', () => {
  assert.match(memoriesSource, /const settingsExpanded = ref\(false\)/)
  assert.match(memoriesSource, /settingsExpanded = !settingsExpanded/)
  const headingStart = memoriesSource.indexOf('<div class="section-heading">')
  const contentStart = memoriesSource.indexOf('<div class="section-heading-content">', headingStart)
  const actionsStart = memoriesSource.indexOf('<div class="heading-actions">', headingStart)
  assert.ok(headingStart >= 0 && contentStart > headingStart && actionsStart > contentStart)
  const contentSource = memoriesSource.slice(contentStart, actionsStart)
  const settingsIndex = contentSource.indexOf('<p>{{ $t(\'memories.settings\') }}</p>')
  const transitionIndex = contentSource.indexOf('<Transition name="memory-task-transition" mode="out-in">')
  const summaryMatch = contentSource.match(/<div\b(?=[^>]*\bclass="memory-task-summary"(?:\s|>))[^>]*>/)
  const summaryIndex = summaryMatch?.index ?? -1
  assert.ok(settingsIndex >= 0 && transitionIndex > settingsIndex)
  assert.ok(summaryIndex > settingsIndex && summaryIndex < actionsStart - contentStart)
  const summaryTag = summaryMatch?.[0]
  assert.ok(summaryTag)
  assert.match(summaryTag, /v-if="currentMemoryTask"/)
  assert.doesNotMatch(summaryTag, /v-(?:if|show)="[^"]*settingsExpanded[^"]*"/)
  assert.doesNotMatch(memoriesSource, /displayMemoryTask/)
  assert.doesNotMatch(memoriesSource, /临时展示占位，验收后删除/)
  assert.match(memoriesSource, /<el-collapse-transition>[\s\S]*v-show="settingsExpanded" class="settings-content"[\s\S]*<\/el-collapse-transition>/)
  assert.match(memoriesSource, /<div class="settings-grid runtime-settings-grid" v-loading="settingsLoading">/)
  assert.match(memoriesSource, /\.settings-content \{[^}]*display:\s*flow-root;/)
  assert.match(memoriesSource, /\.runtime-settings-grid \{[^}]*margin-top:\s*0;/)
  assert.match(memoriesSource, /\.settings-content > \.el-alert \+ \.runtime-settings-grid \{[^}]*margin-top:\s*16px;/)
  assert.match(memoriesSource, /\$t\('memories\.current_task'\)/)
  assert.match(memoriesSource, /\$t\('memories\.progress'\)/)
  assert.match(memoriesSource, /const currentMemoryTask = computed\(\(\) => getCurrentMemoryTask\(settings\)\)/)
  assert.match(contentSource, /operationLabel\(currentMemoryTask\.operation\)/)
  assert.match(contentSource, /currentMemoryTask\.id/)
  assert.match(contentSource, /currentMemoryTask\.total[\s\S]*currentMemoryTask\.completed/)
  assert.match(contentSource, /currentMemoryTask\.percentage/)
  assert.match(contentSource, /statusText\(currentMemoryTask\.status\)/)
  assert.match(memoriesSource, /\.memory-task-summary \{[^}]*white-space: nowrap;[^}]*overflow: hidden;/)
  assert.match(memoriesSource, /\.section-heading-content \{[^}]*min-width: 0;[^}]*flex: 1;/)
  assert.match(memoriesSource, /\.memory-task-summary \{[^}]*width: 100%;/)
  assert.doesNotMatch(memoriesSource, /\.memory-task-summary \{[^}]*flex:\s*1/)
  assert.doesNotMatch(memoriesSource, /\.memory-task-summary \{[^}]*flex-direction:\s*column/)
  assert.doesNotMatch(memoriesSource, /\.memory-task-summary \{[^}]*flex-basis:/)
  assert.match(memoriesSource, /memory-task-transition-(?:enter|leave)-active/)
  assert.match(memoriesSource, /memory-task-transition-(?:enter|leave)-active[^}]*180ms/)
  assert.match(memoriesSource, /\.memory-view \.el-collapse-transition-(?:enter|leave)-active[^}]*transition-duration:\s*180ms\s*!important/)
})

test('MemoriesView removes the memory key list column and right-aligns memory actions', () => {
  assert.doesNotMatch(memoriesSource, /<el-table-column prop="memory_key"[^>]*>/)
  assert.match(memoriesSource, /<el-table-column :label="\$t\('memories\.content_preview'\)" min-width="200">/)
  assert.match(memoriesSource, /\.memory-action-buttons \{ width: 100%; margin-left: auto; display: flex; flex-wrap: wrap; justify-content: flex-end;/)

  const memoriesTabStart = memoriesSource.indexOf('<el-tab-pane :label="$t(\'memories.memories\')"')
  const jobsTabStart = memoriesSource.indexOf('<el-tab-pane :label="$t(\'memories.jobs\')"')
  const migrationsTabStart = memoriesSource.indexOf('<el-tab-pane :label="$t(\'memories.migrations\')"')
  assert.ok(memoriesTabStart >= 0 && jobsTabStart > memoriesTabStart && migrationsTabStart > jobsTabStart)

  const tabSources = [
    memoriesSource.slice(memoriesTabStart, jobsTabStart),
    memoriesSource.slice(jobsTabStart, migrationsTabStart),
    memoriesSource.slice(migrationsTabStart)
  ]
  for (const tabSource of tabSources) {
    assert.match(tabSource, /<el-table-column :label="\$t\('memories\.actions'\)"[^>]*fixed="right"[^>]*header-align="center"[\s\S]*?memory-action-buttons/)
    assert.doesNotMatch(tabSource, /<el-table-column :label="\$t\('memories\.actions'\)"[^>]*align="right"/)
    assert.doesNotMatch(tabSource, /<el-table-column :label="\$t\('memories\.actions'\)"[^>]*(?:class-name|label-class-name)=/)
  }
  assert.doesNotMatch(memoriesSource, /memory-actions-column|memory-actions-header|class-name=|label-class-name=/)
  assert.match(memoriesSource, /\.memory-action-buttons \{ width: 100%; margin-left: auto; display: flex; flex-wrap: wrap; justify-content: flex-end;/)

  assert.match(memoriesSource, /<el-table :data="memories"[\s\S]*?class="memory-table memory-data-table"/)
  assert.match(memoriesSource, /<el-table :data="jobs"[\s\S]*?class="memory-data-table"/)
  assert.match(memoriesSource, /<el-table :data="migrations"[\s\S]*?class="memory-data-table"/)

  const dataTableStyleMatch = memoriesSource.match(/\.memory-data-table\s*\{([\s\S]*?)\}/)
  const loadingMaskStyleMatch = memoriesSource.match(/\.memory-data-table\s*>\s*\.el-loading-mask\s*\{([\s\S]*?)\}/)
  assert.ok(dataTableStyleMatch, '.memory-data-table source block is missing')
  assert.ok(loadingMaskStyleMatch, '.memory-data-table loading mask source block is missing')
  assert.match(dataTableStyleMatch[1], /\bposition:\s*relative\s*;/)
  assert.match(dataTableStyleMatch[1], /\bisolation:\s*isolate\s*;/)
  const loadingMaskZIndex = loadingMaskStyleMatch[1].match(/\bz-index:\s*(\d+)\s*;/)
  assert.ok(loadingMaskZIndex, '.memory-data-table loading mask z-index declaration is missing')
  assert.ok(Number(loadingMaskZIndex[1]) > 2 && Number(loadingMaskZIndex[1]) < 1000, '.memory-data-table loading mask must stay above table layers and below the footer')
})

test('MemoriesView renders memory jobs as a collapsed tree without manual indentation', () => {
  const jobsTabStart = memoriesSource.indexOf('<el-tab-pane :label="$t(\'memories.jobs\')"')
  const migrationsTabStart = memoriesSource.indexOf('<el-tab-pane :label="$t(\'memories.migrations\')"', jobsTabStart)
  assert.ok(jobsTabStart >= 0 && migrationsTabStart > jobsTabStart)

  const jobsSource = memoriesSource.slice(jobsTabStart, migrationsTabStart)
  assert.match(jobsSource, /<el-table\s+:data="jobs"[\s\S]*row-key="id"/)
  assert.match(jobsSource, /:tree-props="\{ children: 'childJobs' \}"/)
  assert.match(jobsSource, /:default-expand-all="false"/)
  assert.doesNotMatch(jobsSource, /paddingLeft/)
  assert.doesNotMatch(jobsSource, /memories\.token_budget/)
  assert.doesNotMatch(jobsSource, /tokenBudgetText\(row\.token_budget\)/)
})

test('MemoriesView uses keyed abortable tasks for every polled collection loader', () => {
  const loaders = [
    {
      name: 'loadSettings',
      key: 'settings',
      tracker: 'settingsRequestTracker',
      request: /memoryApi\.settings\(\{\s*signal:\s*token\.signal\s*\}\)/
    },
    {
      name: 'loadMemories',
      key: 'memories',
      tracker: 'memoriesRequestTracker',
      request: /memoryApi\.list\([\s\S]*,\s*\{\s*signal:\s*token\.signal\s*\}\)/
    },
    {
      name: 'loadJobs',
      key: 'jobs',
      tracker: 'jobsRequestTracker',
      request: /memoryApi\.jobs\([\s\S]*,\s*\{\s*signal:\s*token\.signal\s*\}\)/
    },
    {
      name: 'loadMigrations',
      key: 'migrations',
      tracker: 'migrationsRequestTracker',
      request: /memoryApi\.migrations\([\s\S]*,\s*\{\s*signal:\s*token\.signal\s*\}\)/
    }
  ]

  for (const [index, loader] of loaders.entries()) {
    const loaderStart = memoriesSource.indexOf(`const ${loader.name} = async`)
    const nextLoader = loaders[index + 1]
    const loaderEnd = nextLoader
      ? memoriesSource.indexOf(`const ${nextLoader.name} = async`, loaderStart)
      : memoriesSource.indexOf('const resetAndLoadMemories =', loaderStart)
    assert.ok(loaderStart >= 0 && loaderEnd > loaderStart, `${loader.name} source is missing`)
    const loaderSource = memoriesSource.slice(loaderStart, loaderEnd)

    assert.match(loaderSource, new RegExp(`const token = pollingTaskManager\\.begin\\('${loader.key}'\\)`))
    assert.match(loaderSource, /if \(!token\) return/)
    assert.match(loaderSource, new RegExp(`const requestSeq = ${loader.tracker}\\.begin\\(\\)`))
    assert.match(loaderSource, loader.request)
    assert.ok(loaderSource.includes(`if (!pollingTaskManager.isCurrent(token) || !${loader.tracker}.isCurrent(requestSeq)) return`))
    assert.match(loaderSource, /if \(token\.signal\.aborted \|\| !pollingTaskManager\.isCurrent\(token\)\) return/)
    assert.match(loaderSource, /finally \{[\s\S]*pollingTaskManager\.finish\(token\)/)
  }
})

test('MemoriesView protects ordinary and deleted history with one latest-request flow', () => {
  const historyStart = memoriesSource.indexOf('const loadHistory = async')
  const historyEnd = memoriesSource.indexOf('const isRecordSnapshot =', historyStart)
  assert.ok(historyStart >= 0 && historyEnd > historyStart)

  const historySource = memoriesSource.slice(historyStart, historyEnd)
  assert.match(memoriesSource, /const historyRequestTracker = createLatestRequestTracker\(\)/)
  assert.match(historySource, /const requestSeq = historyRequestTracker\.begin\(\)/)
  assert.match(historySource, /history\.value = \[\]/)
  assert.match(historySource, /memoryApi\.history\(memoryId, \{ page: 1, size: 100 \}\)/)
  assert.match(historySource, /if \(!historyRequestTracker\.isCurrent\(requestSeq\)\) return/)
  const staleCheckIndex = historySource.indexOf('if (!historyRequestTracker.isCurrent(requestSeq)) return')
  const historyWriteIndex = historySource.indexOf('history.value = data.items')
  assert.ok(staleCheckIndex >= 0 && historyWriteIndex > staleCheckIndex)
  assert.match(historySource, /if \(historyRequestTracker\.isCurrent\(requestSeq\)\) ElMessage\.error\(/)
  assert.match(historySource, /if \(historyRequestTracker\.isCurrent\(requestSeq\)\) historyLoading\.value = false/)
  assert.match(memoriesSource, /const showHistory = \(row\) => loadHistory\(row\.id, row\)/)
  assert.match(memoriesSource, /const showDeletedHistory = \(row\) => \{[\s\S]*loadHistory\(row\.memory_id,/)
  assert.doesNotMatch(historySource, /restoreRevision|memoryApi\.restore|memories\.restore/)
})

test('MemoriesView creates and invalidates all five request trackers on unmount', () => {
  const trackers = [
    'settingsRequestTracker',
    'memoriesRequestTracker',
    'jobsRequestTracker',
    'migrationsRequestTracker',
    'historyRequestTracker'
  ]
  const unmountStart = memoriesSource.indexOf('onBeforeUnmount(() =>')
  assert.notEqual(unmountStart, -1)
  const unmountSource = memoriesSource.slice(unmountStart)

  for (const tracker of trackers) {
    assert.match(memoriesSource, new RegExp(`const ${tracker} = createLatestRequestTracker\\(\\)`))
    assert.match(unmountSource, new RegExp(`${tracker}\\.invalidate\\(\\)`))
  }
  assert.match(unmountSource, /pollingStopped = true/)
  assert.match(unmountSource, /window\.clearTimeout\(pollTimer\.value\)/)
  assert.match(unmountSource, /pollingTaskManager\.invalidate\(\)/)
})

test('MemoriesView uses dedicated management payload builders and id-only pin actions', () => {
  const organizeStart = memoriesSource.indexOf('const organize = async')
  const resetFormStart = memoriesSource.indexOf('const resetForm =', organizeStart)
  const togglePinStart = memoriesSource.indexOf('const togglePin = async')
  const historyStart = memoriesSource.indexOf('const loadHistory = async', togglePinStart)
  assert.ok(organizeStart >= 0)
  assert.ok(resetFormStart > organizeStart)
  assert.ok(togglePinStart >= 0 && historyStart > togglePinStart)

  const organizeSource = memoriesSource.slice(organizeStart, resetFormStart)
  const pinSource = memoriesSource.slice(togglePinStart, historyStart)

  assert.match(organizeSource, /memoryApi\.organize\(buildOrganizePayload\(newDedupeKey\(\)\)\)/)
  assert.match(pinSource, /memoryApi\.unpin\(row\.id\)/)
  assert.match(pinSource, /memoryApi\.pin\(row\.id\)/)
  assert.doesNotMatch(pinSource, /memoryApi\.(pin|unpin)\([^)]*,/)

  for (const field of ['uid', 'records', 'session', 'collection']) {
    assert.doesNotMatch(organizeSource, new RegExp(`\\b${field}\\b`), `organize must not mention ${field}`)
    assert.doesNotMatch(pinSource, new RegExp(`\\b${field}\\b`), `pin action must not mention ${field}`)
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

test('MemoriesView renders embedding channel names without channel IDs', () => {
  assert.match(memoriesSource, /channelName\(nestedSetting\('active', 'channel_id', 'active_embedding_channel_id'\)\)/)
  assert.match(memoriesSource, /channelName\(nestedSetting\('target', 'channel_id', 'target_embedding_channel_id'\)\)/)

  const channelNameStart = memoriesSource.indexOf('const channelName =')
  const numericSettingStart = memoriesSource.indexOf('const numericSetting =', channelNameStart)
  assert.ok(channelNameStart >= 0 && numericSettingStart > channelNameStart)
  const channelNameSource = memoriesSource.slice(channelNameStart, numericSettingStart)
  assert.match(channelNameSource, /channelId === null \|\| channelId === undefined \|\| channelId === '' \|\| channelId === '-'/)
  assert.match(channelNameSource, /channels\.value\.find\(item => String\(item\.id\) === String\(channelId\)\)/)
  assert.match(channelNameSource, /typeof channel\?\.name === 'string'/)
  assert.match(channelNameSource, /: '-'/)
  assert.doesNotMatch(channelNameSource, /: String\(channelId\)/)
  assert.doesNotMatch(channelNameSource, /return String\(channelId\)/)

  assert.match(memoriesSource, /channels\.value = allChannels\n/)
  assert.doesNotMatch(memoriesSource, /organizationChannels|organizationForm/)
})
