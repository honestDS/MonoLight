const ACTIVE_MIGRATION_STATUSES = new Set([
  'preparing',
  'building',
  'catching_up',
  'validating',
  'switching'
])

const BLOCKING_CLEANUP_STATUSES = new Set([
  'pending',
  'running',
  'failed'
])

export const isKnowledgeBaseMigrationActive = (status) => ACTIVE_MIGRATION_STATUSES.has(status)

export const canStartKnowledgeBaseMigration = (knowledgeBase) => {
  if (!knowledgeBase || knowledgeBase.knowledge_base_type !== 'user') return false
  if (isKnowledgeBaseMigrationActive(knowledgeBase.migration_status)) return false
  return !BLOCKING_CLEANUP_STATUSES.has(knowledgeBase.old_collection_cleanup_status)
}

export const getKnowledgeBaseMigrationProgress = (knowledgeBase) => {
  if (!knowledgeBase) return 0
  if (knowledgeBase.migration_status === 'succeeded') return 100

  const total = Number(knowledgeBase.migration_total_count || 0)
  const success = Number(knowledgeBase.migration_success_count || 0)
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(success)) return 0

  return Math.max(0, Math.min(100, Math.round((success / total) * 100)))
}

export const getManagedKnowledgeBaseMigrationTerminalHintKey = (knowledgeBase) => {
  if (!knowledgeBase || knowledgeBase.knowledge_base_type !== 'llm_managed') return null
  if (knowledgeBase.migration_status === 'failed') return 'knowledgeBase.managed_embedding_failed_hint'
  if (knowledgeBase.migration_status === 'cancelled') return 'knowledgeBase.managed_embedding_cancelled_hint'
  return null
}
