const hasValue = value => value !== undefined

const firstDefined = (...values) => values.find(hasValue)

const toFiniteNumber = (value, fallback) => {
  if (value === null || value === undefined || typeof value === 'boolean' || (typeof value === 'string' && !value.trim())) return fallback
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

const isObject = value => value && typeof value === 'object' && !Array.isArray(value)

const activeMemoryTaskStatuses = new Set([
  'pending',
  'queued',
  'waiting',
  'retry',
  'retrying',
  'running',
  'processing',
  'preparing',
  'building',
  'catching_up',
  'validating',
  'switching',
  'reindexing',
  'in_progress',
  'active'
])

const firstAvailable = (...values) => values.find(value => value !== null && value !== undefined && value !== '')

const numericProgressValue = (sources, keys) => {
  for (const source of sources) {
    if (!isObject(source)) continue
    for (const key of keys) {
      const value = toFiniteNumber(source[key], null)
      if (value !== null) return value
    }
  }
  return null
}

const taskProgress = (job, completedKeys, totalKeys) => {
  const sources = [job, job?.progress, job?.payload?.progress]
  const rawCompleted = numericProgressValue(sources, completedKeys)
  const completed = rawCompleted === null ? null : Math.max(0, rawCompleted)
  const total = numericProgressValue(sources, totalKeys)
  const percentage = total > 0 && completed !== null
    ? Math.max(0, Math.min(100, Math.round(completed * 100 / total)))
    : null
  return { completed, total, percentage }
}

const taskStatus = job => firstAvailable(job?.status, job?.migration_status, job?.cleanup_status)
const isActiveMemoryTask = job => activeMemoryTaskStatuses.has(String(taskStatus(job) || '').toLowerCase())

const buildCurrentMemoryTask = (job, operation, completedKeys, totalKeys) => {
  if (!isObject(job) || !isActiveMemoryTask(job)) return null
  const progress = taskProgress(job, completedKeys, totalKeys)
  return {
    id: firstAvailable(job.id, job.job_id, job.migration_job_id) ?? null,
    operation: operation || firstAvailable(job.operation) || null,
    status: taskStatus(job),
    ...progress
  }
}

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

export const getCurrentMemoryTask = (settings) => {
  const input = isObject(settings) ? settings : {}
  const migration = isObject(input.migration) ? input.migration : {}
  const currentJobOperation = firstAvailable(input.current_job?.operation)
  const currentJob = currentJobOperation === 'embedding_migration'
    ? {
        ...migration,
        ...input.current_job,
        id: firstAvailable(input.current_job?.id, input.current_job?.job_id, input.current_job?.migration_job_id),
        operation: currentJobOperation,
        status: firstAvailable(migration.status, migration.migration_status, input.current_job?.status)
      }
    : input.current_job
  const currentJobTask = buildCurrentMemoryTask(
    currentJob,
    currentJobOperation,
    ['completed', 'completed_count', 'success_count', 'cursor'],
    ['total', 'total_count', 'snapshot_count', 'snapshot_boundary']
  )
  if (currentJobTask) return currentJobTask

  const organizationJob = input.organization?.current_job
  const organizationTask = buildCurrentMemoryTask(
    organizationJob,
    firstAvailable(organizationJob?.operation),
    ['completed', 'completed_count', 'success_count'],
    ['total', 'total_count']
  )
  if (organizationTask) return organizationTask

  const migrationJob = isObject(input.migration_job) ? input.migration_job : {}
  const migrationStatus = firstAvailable(migration.status, migration.migration_status, migrationJob.status)
  const migrationTask = buildCurrentMemoryTask(
    {
      ...migrationJob,
      ...migration,
      id: firstAvailable(migrationJob.id, migrationJob.job_id, migration.id, migration.job_id),
      operation: firstAvailable(migrationJob.operation, migration.operation, 'embedding_migration'),
      status: migrationStatus
    },
    'embedding_migration',
    ['success_count'],
    ['total_count']
  )
  if (migrationTask) return migrationTask

  const maintenance = input.blocking?.maintenance
  if (isObject(maintenance) && maintenance.operation === 'reindex') {
    const reindexTask = buildCurrentMemoryTask(
      {
        ...maintenance,
        status: firstAvailable(
          maintenance.status,
          input.index?.status,
          input.index_status,
          input.store?.index_status
        )
      },
      'reindex',
      ['success_count'],
      ['total_count']
    )
    if (reindexTask) return reindexTask
  }

  const cleanup = isObject(input.old_collection_cleanup)
    ? {
        ...input.old_collection_cleanup,
        status: firstAvailable(
          input.old_collection_cleanup.status,
          input.old_collection_cleanup.cleanup_status,
          input.old_collection_cleanup_status,
          input.store?.old_collection_cleanup_status
        )
      }
    : {
        job_id: input.store?.old_collection_cleanup_job_id,
        status: firstAvailable(input.old_collection_cleanup_status, input.store?.old_collection_cleanup_status)
      }
  return buildCurrentMemoryTask(cleanup, 'delete_cleanup', [], [])
}

export const decorateMemoryJobs = (items) => {
  if (!Array.isArray(items)) return []
  const validItems = items.filter(isObject)
  const byId = new Map(validItems.map(item => [item.id, item]))
  const parentCandidatesByChild = new Map()
  validItems.forEach(item => {
    if (!Array.isArray(item.child_job_ids)) return
    item.child_job_ids.forEach(childId => {
      if (!parentCandidatesByChild.has(childId)) parentCandidatesByChild.set(childId, new Set())
      parentCandidatesByChild.get(childId).add(item.id)
    })
  })

  const parentByChild = new Map()
  parentCandidatesByChild.forEach((parentIds, childId) => {
    if (parentIds.size === 1) parentByChild.set(childId, [...parentIds][0])
  })
  const parentByJob = new Map()
  validItems.forEach(item => {
    const parentId = item.parent_job_id || parentByChild.get(item.id)
    parentByJob.set(item.id, parentId)
  })

  const getJobLevel = item => {
    let parentId = item.parent_job_id || parentByChild.get(item.id)
    let level = Number.isInteger(parentId) && parentId > 0 ? 1 : 0
    const seen = new Set()
    while (parentId && byId.has(parentId) && !seen.has(parentId)) {
      seen.add(parentId)
      const nextParentId = parentByJob.get(parentId)
      if (!Number.isInteger(nextParentId) || nextParentId <= 0) break
      level += 1
      parentId = nextParentId
    }
    return level
  }

  const safeParentByChild = new Map()
  validItems.forEach(item => {
    const childId = item.id
    const parentId = parentByJob.get(childId)
    if (!Number.isInteger(parentId) || parentId <= 0 || !byId.has(parentId)) return

    const seen = new Set([childId])
    let currentId = parentId
    while (Number.isInteger(currentId) && currentId > 0 && byId.has(currentId) && !seen.has(currentId)) {
      seen.add(currentId)
      currentId = parentByJob.get(currentId)
    }
    if (currentId && seen.has(currentId)) return
    safeParentByChild.set(childId, parentId)
  })

  const decoratedItems = validItems.map(item => ({
    ...item,
    jobLevel: getJobLevel(item),
    childJobs: []
  }))
  const decoratedById = new Map(decoratedItems.map(item => [item.id, item]))
  const topLevelItems = []

  items.forEach(item => {
    if (!isObject(item)) {
      topLevelItems.push(item)
      return
    }

    const decoratedItem = decoratedById.get(item.id)
    const parentId = safeParentByChild.get(item.id)
    if (parentId !== undefined && decoratedById.has(parentId)) {
      decoratedById.get(parentId).childJobs.push(decoratedItem)
    } else {
      topLevelItems.push(decoratedItem)
    }
  })

  return topLevelItems
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
