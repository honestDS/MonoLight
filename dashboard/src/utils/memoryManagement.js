const hasValue = value => value !== undefined

const firstDefined = (...values) => values.find(hasValue)

const toFiniteNumber = (value, fallback) => {
  if (value === null || value === undefined || typeof value === 'boolean' || (typeof value === 'string' && !value.trim())) return fallback
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

const isObject = value => value && typeof value === 'object' && !Array.isArray(value)

const normalizeOrganizationId = value => (
  value === null || value === undefined || (typeof value === 'string' && value.trim() === '')
    ? null
    : value
)

const normalizeBooleanSetting = value => typeof value === 'boolean' ? value : value === 'true'

export function estimateMemoryTokens(value) {
  const normalized = String(value || '').normalize('NFKC').trim().replace(/\s+/g, ' ')
  if (!normalized) return 0
  const characters = Array.from(normalized)
  const chineseCount = characters.filter(character => character >= '\u4e00' && character <= '\u9fff').length
  return Math.floor(chineseCount * 1.5 + (characters.length - chineseCount) * 0.3)
}

export const isMemoryContentTooLong = (content, maxTokens) => {
  if (maxTokens === null || maxTokens === undefined || typeof maxTokens === 'boolean' || (typeof maxTokens === 'string' && !maxTokens.trim())) return false
  const normalizedMaxTokens = Number(maxTokens)
  if (!Number.isFinite(normalizedMaxTokens) || normalizedMaxTokens < 0) return false
  return estimateMemoryTokens(content) > normalizedMaxTokens
}

export const normalizeMemorySettings = (data) => {
  const input = isObject(data) ? data : {}
  const nestedSettings = isObject(input.settings) ? input.settings : {}
  const source = { ...nestedSettings, ...input }
  const store = isObject(source.store) ? source.store : {}
  const rawCapacity = isObject(source.capacity) ? source.capacity : {}
  const rawOrganization = isObject(source.organization) ? source.organization : {}

  const capacity = {
    ...rawCapacity,
    active_record_count: toFiniteNumber(firstDefined(
      rawCapacity.active_record_count,
      store.active_record_count,
      source.active_record_count
    ), 0),
    max_active_records: toFiniteNumber(firstDefined(
      rawCapacity.max_active_records,
      store.max_active_records,
      source.max_active_records
    ), 50),
    organize_trigger_records: toFiniteNumber(firstDefined(
      rawCapacity.organize_trigger_records,
      store.organize_trigger_records,
      source.organize_trigger_records
    ), 45),
    content_max_tokens: toFiniteNumber(firstDefined(
      rawCapacity.content_max_tokens,
      store.content_max_tokens,
      source.content_max_tokens
    ), 160),
    status: firstDefined(
      rawCapacity.status,
      store.capacity_status,
      source.capacity_status,
      'normal'
    ) || 'normal'
  }

  const organization = {
    ...rawOrganization,
    auto_organize_enabled: normalizeBooleanSetting(firstDefined(
      rawOrganization.auto_organize_enabled,
      store.auto_organize_enabled,
      source.auto_organize_enabled,
      false
    )),
    channel_id: normalizeOrganizationId(firstDefined(
      rawOrganization.channel_id,
      store.organization_channel_id,
      source.organization_channel_id,
      null
    )),
    model_id: normalizeOrganizationId(firstDefined(
      rawOrganization.model_id,
      store.organization_model_id,
      source.organization_model_id,
      null
    )),
    required_output_tokens: toFiniteNumber(firstDefined(
      rawOrganization.required_output_tokens,
      source.required_output_tokens,
      source.organization_required_output_tokens,
      store.required_output_tokens,
      store.organization_required_output_tokens
    ), 0)
  }

  const organizationForm = {
    auto_organize_enabled: organization.auto_organize_enabled,
    channel_id: organization.channel_id,
    model_id: organization.model_id
  }
  const requiredOutputTokens = organization.required_output_tokens
  const contentMaxTokens = capacity.content_max_tokens
  const activeRecordCount = capacity.active_record_count
  const maxActiveRecords = capacity.max_active_records

  return {
    ...source,
    capacity,
    organization,
    organizationForm,
    requiredOutputTokens,
    contentMaxTokens,
    activeRecordCount,
    maxActiveRecords
  }
}

export const getOrganizationModelsForChannel = (channels, channelId) => {
  if (!Array.isArray(channels)) return []
  const channel = channels.find(item => item?.id === channelId)
  if (!Array.isArray(channel?.model_ids)) return []
  return channel.model_ids.filter(model => (
    model?.model_id &&
    String(model.usage || '').toUpperCase() === 'CHAT' &&
    model.is_enabled !== false
  ))
}

export const validateOrganizationSettings = (form, selectedModel, requiredOutputTokens) => {
  const hasChannel = form?.channel_id !== null && form?.channel_id !== undefined && form?.channel_id !== ''
  const hasModel = Boolean(form?.model_id)

  if (hasChannel !== hasModel) return 'organization_selection_pair_required'

  if (hasChannel && (!selectedModel || String(selectedModel.usage || '').toUpperCase() !== 'CHAT' || selectedModel.is_enabled === false)) {
    return 'organization_model_invalid'
  }

  if (hasChannel && (
    !Number.isInteger(Number(selectedModel.context_window_k)) ||
    Number(selectedModel.context_window_k) <= 0 ||
    !Number.isInteger(Number(selectedModel.max_tokens)) ||
    Number(selectedModel.max_tokens) <= 0
  )) {
    return 'organization_model_limits_invalid'
  }

  if (hasChannel && Number(selectedModel.max_tokens) < Number(requiredOutputTokens)) {
    return 'organization_max_tokens_too_small'
  }

  if (form?.auto_organize_enabled && !selectedModel) return 'organization_model_required'
  return null
}

export const buildOrganizationSettingsPayload = (form) => {
  const hasChannel = form?.channel_id !== null && form?.channel_id !== undefined && form?.channel_id !== ''
  const hasModel = Boolean(form?.model_id)
  return {
    auto_organize_enabled: Boolean(form?.auto_organize_enabled),
    organization_channel_id: hasChannel ? form.channel_id : null,
    organization_model_id: hasModel ? form.model_id : null
  }
}

export const buildOrganizePayload = dedupeKey => ({ dedupe_key: dedupeKey })

export const decorateMemoryJobs = (items) => {
  if (!Array.isArray(items)) return []
  const validItems = items.filter(isObject)
  const byId = new Map(validItems.map(item => [item.id, item]))
  const parentByChild = new Map()
  validItems.forEach(item => {
    if (!Array.isArray(item.child_job_ids)) return
    item.child_job_ids.forEach(childId => parentByChild.set(childId, item.id))
  })
  return items.map(item => {
    if (!isObject(item)) return item
    let parentId = item.parent_job_id || parentByChild.get(item.id)
    let level = Number.isInteger(parentId) && parentId > 0 ? 1 : 0
    const seen = new Set()
    while (parentId && byId.has(parentId) && !seen.has(parentId)) {
      seen.add(parentId)
      const nextParentId = byId.get(parentId).parent_job_id || parentByChild.get(parentId)
      if (!Number.isInteger(nextParentId) || nextParentId <= 0) break
      level += 1
      parentId = nextParentId
    }
    return { ...item, jobLevel: level }
  })
}

export const createLatestRequestTracker = () => {
  let latestRequest = 0
  return {
    begin: () => ++latestRequest,
    isCurrent: request => request === latestRequest,
    invalidate: () => ++latestRequest
  }
}

const knownMemoryOperations = new Set([
  'create',
  'update',
  'restore',
  'reindex',
  'delete_cleanup',
  'embedding_migration',
  'create_with_eviction',
  'organize',
  'organize_merge'
])

const knownMemorySources = new Set(['user_api', 'llm_tool', 'auto_extract', 'auto_organize'])

export const memoryOperationLabelKey = value => (
  value === null || value === undefined || value === ''
    ? '-'
    : knownMemoryOperations.has(value) ? `memories.operation_${value}` : value
)

export const memorySourceLabelKey = value => (
  value === null || value === undefined || value === ''
    ? '-'
    : knownMemorySources.has(value) ? `memories.source_${value}` : value
)
