import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import {
  canManageKnowledgeBaseDocuments,
  canManageManagedKnowledge,
  createManagedKnowledgeDedupeKey,
  getManagedKnowledgeMutationFeedback,
  getKnowledgeBaseProfileIds,
  normalizeKnowledgeBase
} from '../src/utils/knowledgeBaseManagement.js'

test('knowledge base management visibility follows explicit type', () => {
  const user = normalizeKnowledgeBase({ id: 1, knowledge_base_type: 'user', profile_ids: [2, 3] })
  const managed = normalizeKnowledgeBase({ id: 2, knowledge_base_type: 'llm_managed', managed_profile_id: 4 })

  assert.equal(canManageKnowledgeBaseDocuments(user), true)
  assert.equal(canManageManagedKnowledge(user), false)
  assert.equal(canManageKnowledgeBaseDocuments(managed), false)
  assert.equal(canManageManagedKnowledge(managed), true)
  assert.deepEqual(getKnowledgeBaseProfileIds(user), [2, 3])
  assert.deepEqual(getKnowledgeBaseProfileIds(managed), [4])
})

test('legacy knowledge base responses safely downgrade to user knowledge bases', () => {
  const legacy = normalizeKnowledgeBase({ id: 7, name: 'legacy' })

  assert.equal(legacy.knowledge_base_type, 'user')
  assert.deepEqual(legacy.profile_ids, [])
  assert.equal(legacy.managed_profile_id, null)
  assert.equal(legacy.migration_status, null)
  assert.equal(canManageKnowledgeBaseDocuments(legacy), true)
  assert.equal(canManageManagedKnowledge(legacy), false)
})

test('managed knowledge mutation feedback distinguishes submission from no-op results', () => {
  assert.deepEqual(getManagedKnowledgeMutationFeedback('create', 'created'), { type: 'success', key: 'managed_create_submitted' })
  assert.deepEqual(getManagedKnowledgeMutationFeedback('create', 'existing_key'), { type: 'warning', key: 'managed_existing_key' })
  assert.deepEqual(getManagedKnowledgeMutationFeedback('create', 'existing_content'), { type: 'warning', key: 'managed_existing_content' })
  assert.deepEqual(getManagedKnowledgeMutationFeedback('update', 'unchanged'), { type: 'info', key: 'managed_no_changes' })
  assert.deepEqual(getManagedKnowledgeMutationFeedback('delete', 'deleted'), { type: 'success', key: 'managed_delete_submitted' })
})

test('managed knowledge UI creates a fresh idempotency key for each user action', () => {
  const first = createManagedKnowledgeDedupeKey('create')
  const second = createManagedKnowledgeDedupeKey('create')

  assert.match(first, /^managed-ui-create:/)
  assert.match(second, /^managed-ui-create:/)
  assert.notEqual(first, second)
  assert.ok(first.length <= 255)
})

test('knowledge base view exposes separate user-document and managed-knowledge entries', () => {
  const source = fs.readFileSync(new URL('../src/views/KnowledgeBase.vue', import.meta.url), 'utf8')
  const commonStyles = fs.readFileSync(new URL('../src/assets/css/common.scss', import.meta.url), 'utf8')

  assert.match(source, /canManageKnowledgeBaseDocuments\(row\)/)
  assert.match(source, /canManageManagedKnowledge\(row\)/)
  assert.match(source, /v-if="canManageKnowledgeBaseDocuments\(row\)"[^>]*showDocumentDialog/)
  assert.match(source, /v-if="canManageManagedKnowledge\(row\)"[^>]*showManagedKnowledgeDialog/)
  assert.match(source, /v-if="canManageKnowledgeBaseDocuments\(row\)" type="success"[^>]*>[\s\S]*?knowledgeBase\.documents/)
  assert.match(source, /v-if="canManageManagedKnowledge\(row\)" type="success"[^>]*>[\s\S]*?knowledgeBase\.documents/)
  assert.match(source, /<el-dropdown[^>]*trigger="click"[^>]*@command="handleKnowledgeBaseMoreAction\(\$event, row\)"/)
  assert.match(source, /v-if="canManageKnowledgeBaseDocuments\(row\)"[^>]*command="import_document"/)
  assert.match(source, /command="embedding_status"/)
  assert.match(source, /command="edit"/)
  assert.match(source, /command="delete"/)
  assert.match(source, /showManagedKnowledgeDialog/)
  assert.match(source, /knowledgeBaseApi\.managedItems/)
  assert.match(source, /knowledgeBaseApi\.managedHistory/)
  assert.match(source, /managed-knowledge-action-buttons/)
  assert.match(source, /managed_llm_maintainable'\)" width="140"/)
  assert.match(source, /knowledgeBase\.actions'\)" width="180"[\s\S]*managed-knowledge-action-buttons/)
  assert.match(source, /handleManagedKnowledgeMoreAction\(\$event, row\)/)
  assert.match(source, /command="history"/)
  assert.match(source, /v-if="row\.publication_job_status === 'failed'" command="retry"/)
  assert.match(source, /knowledgeBaseApi\.retryManagedItem/)
  assert.match(source, /publication_job_status === 'failed'[\s\S]*managed_status_failed/)
  assert.match(source, /createAbortableTaskManager/)
  assert.match(source, /createLatestRequestTracker/)
  assert.match(source, /managedKnowledgeRequestTracker\.invalidate\(\)/)
  assert.match(source, /signal: token\.signal/)
  assert.match(source, /createManagedKnowledgeDedupeKey\('create'\)/)
  assert.match(source, /createManagedKnowledgeDedupeKey\('update'\)/)
  assert.match(source, /createManagedKnowledgeDedupeKey\('delete'\)/)
  assert.match(source, /createManagedKnowledgeDedupeKey\('retry'\)/)
  assert.match(source, /v-if="selectedKb\?\.knowledge_base_type === 'user'"[\s\S]*submitEmbeddingMigration/)
  assert.match(source, /onBeforeUnmount\(\(\) => \{[\s\S]*stopManagedKnowledgePolling\(\)/)
  assert.doesNotMatch(commonStyles, /--el-table-fixed-right-column:\s*none\s*!important/)
})
