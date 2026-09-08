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
