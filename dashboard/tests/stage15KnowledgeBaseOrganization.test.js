import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import {
  getKnowledgeOrganizationPublishedSuccessCount
} from '../src/utils/knowledgeBaseManagement.js'

const readSource = relativePath => fs.readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const apiSource = readSource('../src/api/index.js')
const viewSource = readSource('../src/views/KnowledgeBase.vue')
const zhLocaleSource = readSource('../src/i18n/locales/zh/knowledgeBase.js')
const enLocaleSource = readSource('../src/i18n/locales/en/knowledgeBase.js')

const getSection = (source, startMarker, endMarker) => {
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker, start + startMarker.length)
  assert.ok(start >= 0, `missing source marker: ${startMarker}`)
  assert.ok(end > start, `missing source marker: ${endMarker}`)
  return source.slice(start, end)
}

test('knowledge base organization API uses the required methods and paths', () => {
  const contracts = [
    {
      name: 'organize',
      pattern: /organize:\s*\(id,\s*data\)\s*=>\s*request\.post\(`\/knowledge-base\/organization\?kb_id=\$\{id\}`,\s*data\)/
    },
    {
      name: 'organizationJobs',
      pattern: /organizationJobs:\s*\(id,\s*params\)\s*=>\s*request\.get\('\/knowledge-base\/organization\/jobs',\s*\{\s*params:\s*\{\s*\.\.\.params,\s*kb_id:\s*id\s*\}\s*\}\)/
    },
    {
      name: 'organizationJob',
      pattern: /organizationJob:\s*\(id,\s*jobId\)\s*=>\s*request\.get\(`\/knowledge-base\/organization\/\$\{jobId\}`,\s*\{\s*params:\s*\{\s*kb_id:\s*id\s*\}\s*\}\)/
    },
    {
      name: 'cancelOrganizationJob',
      pattern: /cancelOrganizationJob:\s*\(id,\s*jobId\)\s*=>\s*request\.post\(`\/knowledge-base\/organization\/\$\{jobId\}\/cancel\?kb_id=\$\{id\}`\)/
    },
    {
      name: 'retryOrganizationJob',
      pattern: /retryOrganizationJob:\s*\(id,\s*jobId\)\s*=>\s*request\.post\(`\/knowledge-base\/organization\/\$\{jobId\}\/retry\?kb_id=\$\{id\}`\)/
    }
  ]

  for (const contract of contracts) {
    assert.match(apiSource, contract.pattern, `knowledgeBaseApi.${contract.name} is incomplete`)
  }
})

test('knowledge base organization success count uses actual published mutation count only', () => {
  assert.equal(
    getKnowledgeOrganizationPublishedSuccessCount({
      publication_success_count: 2,
      child_terminal_count: 5,
      child_failed_count: 2
    }),
    2
  )

  assert.equal(
    getKnowledgeOrganizationPublishedSuccessCount({
      publication_success_count: 0,
      child_terminal_count: 5,
      child_failed_count: 0,
      result: { update_count: 5 }
    }),
    0
  )
})

test('knowledge base organization success count handles invalid server values', () => {
  for (const value of [null, undefined, true, false, {}, [], 'invalid', Infinity, NaN, -1]) {
    assert.equal(
      getKnowledgeOrganizationPublishedSuccessCount({ publication_success_count: value }),
      0
    )
  }

  assert.equal(
    getKnowledgeOrganizationPublishedSuccessCount({ publication_success_count: '5.9' }),
    5
  )
})


test('knowledge base organization supports managed-knowledge multi-select and full or selected submission', () => {
  assert.match(
    viewSource,
    /<el-table\s+ref="managedKnowledgeTableRef"[\s\S]*?@selection-change="handleManagedKnowledgeSelectionChange"/
  )
  assert.match(
    viewSource,
    /<el-table-column\s+type="selection"\s+width="55"\s+:reserve-selection="true"\s+:selectable="isManagedKnowledgeOrganizable"\s*\/>/
  )

  const organizableSource = getSection(
    viewSource,
    'const isManagedKnowledgeOrganizable =',
    'const handleManagedKnowledgeSelectionChange ='
  )
  assert.match(organizableSource, /item\?\.llm_maintainable\s*===\s*true/)
  assert.match(organizableSource, /item\?\.is_recallable\s*===\s*true/)
  assert.match(organizableSource, /!item\?\.pending_job_id/)
  assert.match(organizableSource, /item\?\.indexed_version\s*===\s*item\?\.version/)

  assert.match(viewSource, /@click="submitKnowledgeOrganization\(false\)"/)
  assert.match(viewSource, /@click="submitKnowledgeOrganization\(true\)"/)
  assert.match(viewSource, /:disabled="!managedKnowledgeSelectedIds\.length"/)

  const submitSource = getSection(
    viewSource,
    'const submitKnowledgeOrganization = async',
    'const handleCancelOrganizationJob = async'
  )
  assert.match(submitSource, /const knowledgeIds = \[\.\.\.managedKnowledgeSelectedIds\.value\]/)
  assert.match(submitSource, /if \(selectedOnly && !knowledgeIds\.length\)/)
  assert.match(
    submitSource,
    /knowledgeBaseApi\.organize\(selectedId,\s*selectedOnly \? \{\s*knowledge_ids:\s*knowledgeIds\s*\} : \{\}\)/
  )
})

test('knowledge base organization displays progress, exposes job actions, and polls in one flight', () => {
  assert.match(viewSource, /job\?\.organization_progress\s*&&\s*typeof job\.organization_progress === 'object'/)
  assert.match(viewSource, /getKnowledgeOrganizationPublishedSuccessCount\(job\)/)
  assert.match(
    viewSource,
    /:label="\$t\('knowledgeBase\.organization_stage_progress'\)"[\s\S]*?getOrganizationStageProgress\(latestOrganizationJob\)[\s\S]*?<el-progress/
  )
  assert.match(
    viewSource,
    /:label="\$t\('knowledgeBase\.organization_fragment_progress'\)"[\s\S]*?getOrganizationFragmentProgress\(latestOrganizationJob\)[\s\S]*?<el-progress/
  )

  assert.match(viewSource, /v-if="canCancelOrganizationJob\(row\)"[\s\S]*?@click="handleCancelOrganizationJob\(row\)"/)
  assert.match(viewSource, /v-if="canRetryOrganizationJob\(row\)"[\s\S]*?@click="handleRetryOrganizationJob\(row\)"/)

  const cancelSource = getSection(
    viewSource,
    'const handleCancelOrganizationJob = async',
    'const handleRetryOrganizationJob = async'
  )
  assert.match(cancelSource, /knowledgeBaseApi\.cancelOrganizationJob\(selectedId,\s*jobId\)/)

  const retrySource = getSection(
    viewSource,
    'const handleRetryOrganizationJob = async',
    'const showManagedKnowledgeDialog = async'
  )
  assert.match(retrySource, /knowledgeBaseApi\.retryOrganizationJob\(selectedId,\s*jobId\)/)

  const fetchSource = getSection(
    viewSource,
    'const fetchOrganizationJobs = async',
    'const beginOrganizationPollingSession ='
  )
  assert.match(fetchSource, /const taskKey = `organization-jobs-\$\{session\}`/)
  assert.match(fetchSource, /const token = organizationTaskManager\.begin\(taskKey\)/)
  assert.match(fetchSource, /if \(!token\) return/)
  assert.match(fetchSource, /organizationTaskManager\.isCurrent\(token\)/)
  assert.match(fetchSource, /organizationTaskManager\.finish\(token\)/)

  const scheduleSource = getSection(
    viewSource,
    'const scheduleOrganizationPolling =',
    'const showOrganizationDialog = async'
  )
  assert.match(scheduleSource, /if \(organizationPollTimer\) \{[\s\S]*clearTimeout\(organizationPollTimer\)/)
  assert.match(scheduleSource, /!organizationJobs\.value\.some\(job => !isOrganizationJobTerminal\(job\)\)/)
  assert.match(scheduleSource, /organizationPollTimer = setTimeout\(async \(\) =>/)
  assert.match(scheduleSource, /await fetchOrganizationJobs\(false, session\)/)
  assert.match(scheduleSource, /if \(session === organizationPollingSession\) scheduleOrganizationPolling\(session\)/)
})

test('knowledge base organization stops polling when closed or unmounted', () => {
  assert.match(viewSource, /class="standard-dialog organization-dialog"[\s\S]*?@closed="stopOrganizationPolling"/)

  const beginSource = getSection(
    viewSource,
    'const beginOrganizationPollingSession =',
    'const stopOrganizationPolling ='
  )
  assert.match(beginSource, /if \(organizationPollTimer\) \{[\s\S]*clearTimeout\(organizationPollTimer\)[\s\S]*organizationPollTimer = null/)
  assert.match(beginSource, /organizationPollingSession \+= 1/)
  assert.match(beginSource, /organizationRequestTracker\.invalidate\(\)/)

  const stopSource = getSection(
    viewSource,
    'const stopOrganizationPolling =',
    'const scheduleOrganizationPolling ='
  )
  assert.match(stopSource, /stopOrganizationPolling = \(\) => \{\s*beginOrganizationPollingSession\(\)\s*\}/)

  const lifecycleSource = getSection(viewSource, 'onBeforeUnmount(() =>', '</script>')
  assert.match(lifecycleSource, /stopOrganizationPolling\(\)/)
})

const organizationLocaleKeys = [
  'organization',
  'organization_title',
  'organization_select_hint',
  'organization_start_full',
  'organization_start_selected',
  'organization_selected_count',
  'organization_jobs',
  'organization_job_id',
  'organization_status',
  'organization_snapshot',
  'organization_stage_progress',
  'organization_fragment_progress',
  'organization_counts',
  'organization_success_count',
  'organization_failure_count',
  'organization_conflict_count',
  'organization_error',
  'organization_no_jobs',
  'organization_fetch_failed',
  'organization_submit_failed',
  'organization_cancel_failed',
  'organization_retry_failed',
  'organization_submitted',
  'organization_cancelled',
  'organization_retried',
  'organization_cancel',
  'organization_retry',
  'organization_cancel_confirm',
  'organization_retry_confirm',
  'organization_select_at_least_one',
  'organization_status_pending',
  'organization_status_running',
  'organization_status_retry',
  'organization_status_succeeded',
  'organization_status_failed',
  'organization_status_cancelled',
  'organization_no_snapshot'
]

const parseOrganizationLocale = source => Object.fromEntries(
  [...source.matchAll(/^[ \t]+(organization(?:_[a-z0-9_]+)*):[ \t]*(['"])(.*?)\2[ \t]*,?[ \t]*$/gim)]
    .map(([, key, , value]) => [key, value])
)

test('Chinese and English knowledge base locales share non-empty organization keys', () => {
  const zh = parseOrganizationLocale(zhLocaleSource)
  const en = parseOrganizationLocale(enLocaleSource)

  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort())
  assert.deepEqual(Object.keys(zh).sort(), [...organizationLocaleKeys].sort())

  for (const key of organizationLocaleKeys) {
    assert.equal(typeof zh[key], 'string', `missing Chinese locale key: ${key}`)
    assert.equal(typeof en[key], 'string', `missing English locale key: ${key}`)
    assert.ok(zh[key].trim(), `empty Chinese locale value: ${key}`)
    assert.ok(en[key].trim(), `empty English locale value: ${key}`)
  }

  assert.equal(zh.organization_error, '整理失败')
  assert.equal(en.organization_error, 'Organization failed')
  assert.equal('organization_stale_count' in zh, false)
  assert.equal('organization_stale_count' in en, false)
})
