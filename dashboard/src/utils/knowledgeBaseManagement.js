export const normalizeKnowledgeBase = (knowledgeBase = {}) => ({
  ...knowledgeBase,
  knowledge_base_type: knowledgeBase.knowledge_base_type || 'user',
  profile_ids: Array.isArray(knowledgeBase.profile_ids) ? knowledgeBase.profile_ids : [],
  managed_profile_id: knowledgeBase.managed_profile_id ?? null,
  migration_status: knowledgeBase.migration_status ?? null,
  target_embedding_channel_id: knowledgeBase.target_embedding_channel_id ?? null,
  target_embedding_model_id: knowledgeBase.target_embedding_model_id ?? null,
  old_collection_cleanup_status: knowledgeBase.old_collection_cleanup_status || 'none'
})

export const canManageKnowledgeBaseDocuments = (knowledgeBase) => (
  normalizeKnowledgeBase(knowledgeBase).knowledge_base_type === 'user'
)

export const canManageManagedKnowledge = (knowledgeBase) => (
  normalizeKnowledgeBase(knowledgeBase).knowledge_base_type === 'llm_managed'
)

export const getKnowledgeBaseProfileIds = (knowledgeBase) => {
  const normalized = normalizeKnowledgeBase(knowledgeBase)
  if (normalized.knowledge_base_type === 'llm_managed') {
    return normalized.managed_profile_id ? [normalized.managed_profile_id] : []
  }
  return normalized.profile_ids
}

const submittedStatusByOperation = {
  create: 'created',
  update: 'updated',
  delete: 'deleted'
}

export const getManagedKnowledgeMutationFeedback = (operation, status) => {
  if (status === 'retry_submitted') return { type: 'success', key: 'managed_retry_submitted' }
  if (status === 'existing_key') return { type: 'warning', key: 'managed_existing_key' }
  if (status === 'existing_content') return { type: 'warning', key: 'managed_existing_content' }
  if (status === 'unchanged') return { type: 'info', key: 'managed_no_changes' }
  if (submittedStatusByOperation[operation] === status) {
    return { type: 'success', key: `managed_${operation}_submitted` }
  }
  return { type: 'info', key: 'managed_operation_processed' }
}

export const createManagedKnowledgeDedupeKey = (operation) => {
  const suffix = globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`
  return `managed-ui-${operation}:${suffix}`.slice(0, 255)
}
