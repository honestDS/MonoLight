import test from 'node:test'
import assert from 'node:assert/strict'

import {
  canStartKnowledgeBaseMigration,
  getKnowledgeBaseMigrationProgress,
  isKnowledgeBaseMigrationActive
} from '../src/utils/knowledgeBaseMigration.js'

test('user knowledge base can migrate only when no migration or cleanup blocks it', () => {
  assert.equal(canStartKnowledgeBaseMigration({
    knowledge_base_type: 'user',
    migration_status: null,
    old_collection_cleanup_status: 'none'
  }), true)
  assert.equal(canStartKnowledgeBaseMigration({
    knowledge_base_type: 'llm_managed',
    migration_status: null,
    old_collection_cleanup_status: 'none'
  }), false)
  assert.equal(canStartKnowledgeBaseMigration({
    knowledge_base_type: 'user',
    migration_status: 'building',
    old_collection_cleanup_status: 'none'
  }), false)
  assert.equal(canStartKnowledgeBaseMigration({
    knowledge_base_type: 'user',
    migration_status: 'succeeded',
    old_collection_cleanup_status: 'failed'
  }), false)
})

test('migration active states exclude terminal states', () => {
  for (const status of ['preparing', 'building', 'catching_up', 'validating', 'switching']) {
    assert.equal(isKnowledgeBaseMigrationActive(status), true)
  }
  for (const status of [null, 'succeeded', 'failed', 'cancelled']) {
    assert.equal(isKnowledgeBaseMigrationActive(status), false)
  }
})

test('migration progress is bounded and preserves completed progress', () => {
  assert.equal(getKnowledgeBaseMigrationProgress({
    migration_status: 'preparing',
    migration_total_count: 0,
    migration_success_count: 0
  }), 0)
  assert.equal(getKnowledgeBaseMigrationProgress({
    migration_status: 'building',
    migration_total_count: 4,
    migration_success_count: 1
  }), 25)
  assert.equal(getKnowledgeBaseMigrationProgress({
    migration_status: 'building',
    migration_total_count: 4,
    migration_success_count: 10
  }), 100)
  assert.equal(getKnowledgeBaseMigrationProgress({
    migration_status: 'succeeded',
    migration_total_count: 0,
    migration_success_count: 0
  }), 100)
})
