import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildOrganizePayload,
  buildOrganizationSettingsPayload,
  createLatestRequestTracker,
  decorateMemoryJobs,
  estimateMemoryTokens,
  getCurrentMemoryTask,
  getOrganizationModelsForChannel,
  isMemoryContentTooLong,
  memoryOperationLabelKey,
  memorySourceLabelKey,
  normalizeMemorySettings,
  validateOrganizationSettings
} from '../src/utils/memoryManagement.js'

const clone = value => structuredClone(value)

const findTextWithTokenCount = target => {
  for (let length = 0; length < 2000; length += 1) {
    const text = 'a'.repeat(length)
    if (estimateMemoryTokens(text) === target) return text
  }
  assert.fail(`could not construct ASCII text with ${target} estimated tokens`)
}

const validOrganizationModel = (overrides = {}) => ({
  model_id: 'organizer-model',
  usage: 'CHAT',
  is_enabled: true,
  context_window_k: 32,
  max_tokens: 6400,
  ...overrides
})

const validOrganizationForm = (overrides = {}) => ({
  auto_organize_enabled: false,
  channel_id: 7,
  model_id: 'organizer-model',
  ...overrides
})

test('normalizes memory tokens before estimating and applies a strict greater-than limit', () => {
  const text159 = findTextWithTokenCount(159)
  const text160 = findTextWithTokenCount(160)
  const text161 = findTextWithTokenCount(161)

  assert.equal(estimateMemoryTokens(''), 0)
  assert.equal(estimateMemoryTokens('   \n\t  '), 0)
  assert.equal(estimateMemoryTokens(text159), 159)
  assert.equal(estimateMemoryTokens(text160), 160)
  assert.equal(estimateMemoryTokens(text161), 161)
  assert.equal(isMemoryContentTooLong(text159, 160), false)
  assert.equal(isMemoryContentTooLong(text160, 160), false)
  assert.equal(isMemoryContentTooLong(text161, 160), true)
})

test('uses Unicode NFKC and whitespace normalization for token display data', () => {
  const normalized = 'A B 中文'
  const compatibilityText = '  Ａ\n\tＢ   中 文  '

  assert.equal(
    estimateMemoryTokens(compatibilityText),
    estimateMemoryTokens(normalized)
  )
  assert.equal(
    estimateMemoryTokens('ＡＢＣ'),
    estimateMemoryTokens('ABC')
  )
  assert.equal(
    estimateMemoryTokens('甲\n\n乙'),
    estimateMemoryTokens('甲 乙')
  )
})

test('normalizes nested settings into stable capacity and organization display values', () => {
  const settings = {
    configured: true,
    active: { channel_id: 2, model_id: 'embedding-a', dimensions: 1536, collection: 'memory-a' },
    target: { channel_id: 3, model_id: 'embedding-b', dimensions: 3072, collection: 'memory-b' },
    capacity: {
      active_record_count: '47',
      max_active_records: '50',
      organize_trigger_records: '45',
      content_max_tokens: '160',
      status: 'over_limit'
    },
    organization: {
      auto_organize_enabled: true,
      channel_id: 8,
      model_id: 'organizer-model',
      required_output_tokens: '6400'
    }
  }
  const original = clone(settings)

  const normalized = normalizeMemorySettings(settings)

  assert.deepEqual(normalized.capacity, {
    active_record_count: 47,
    max_active_records: 50,
    organize_trigger_records: 45,
    content_max_tokens: 160,
    status: 'over_limit'
  })
  assert.deepEqual(normalized.organizationForm, {
    auto_organize_enabled: true,
    channel_id: 8,
    model_id: 'organizer-model'
  })
  assert.equal(normalized.requiredOutputTokens, 6400)
  assert.equal(normalized.contentMaxTokens, 160)
  assert.equal(normalized.activeRecordCount, 47)
  assert.equal(normalized.maxActiveRecords, 50)
  assert.equal(normalized.organizationForm.channel_id, 8)
  assert.equal(normalized.organizationForm.model_id, 'organizer-model')
  assert.deepEqual(settings, original)
})

test('normalizes legacy flat settings with the same display shape and defaults', () => {
  const settings = {
    active_record_count: '12',
    max_active_records: '50',
    organize_trigger_records: '45',
    content_max_tokens: '160',
    capacity_status: 'normal',
    auto_organize_enabled: false,
    organization_channel_id: 4,
    organization_model_id: 'legacy-organizer',
    required_output_tokens: '3200'
  }
  const original = clone(settings)

  const normalized = normalizeMemorySettings(settings)

  assert.deepEqual(normalized.capacity, {
    active_record_count: 12,
    max_active_records: 50,
    organize_trigger_records: 45,
    content_max_tokens: 160,
    status: 'normal'
  })
  assert.deepEqual(normalized.organizationForm, {
    auto_organize_enabled: false,
    channel_id: 4,
    model_id: 'legacy-organizer'
  })
  assert.equal(normalized.requiredOutputTokens, 3200)
  assert.equal(normalized.contentMaxTokens, 160)
  assert.equal(normalized.activeRecordCount, 12)
  assert.equal(normalized.maxActiveRecords, 50)
  assert.deepEqual(settings, original)
})

test('normalizes null, empty, and invalid settings values to safe top-level defaults', () => {
  const invalidSettings = {
    capacity: {
      active_record_count: null,
      max_active_records: '',
      organize_trigger_records: 'not-a-number',
      content_max_tokens: Number.POSITIVE_INFINITY
    },
    organization: {
      auto_organize_enabled: null,
      channel_id: '',
      model_id: '',
      required_output_tokens: Number.NaN
    }
  }
  const original = clone(invalidSettings)
  const normalized = normalizeMemorySettings(invalidSettings)

  assert.equal(normalized.capacity.active_record_count, 0)
  assert.equal(normalized.capacity.max_active_records, 50)
  assert.equal(normalized.capacity.organize_trigger_records, 45)
  assert.equal(normalized.capacity.content_max_tokens, 160)
  assert.equal(normalized.organizationForm.auto_organize_enabled, false)
  assert.equal(normalized.organizationForm.channel_id, null)
  assert.equal(normalized.organizationForm.model_id, null)
  assert.equal(normalized.requiredOutputTokens, 0)
  assert.equal(normalized.contentMaxTokens, 160)
  assert.equal(normalized.activeRecordCount, 0)
  assert.equal(normalized.maxActiveRecords, 50)
  assert.deepEqual(invalidSettings, original)

  for (const value of [null, undefined, '', [], 'invalid']) {
    const safe = normalizeMemorySettings(value)
    assert.equal(safe.activeRecordCount, 0)
    assert.equal(safe.maxActiveRecords, 50)
    assert.equal(safe.contentMaxTokens, 160)
    assert.equal(safe.requiredOutputTokens, 0)
    assert.deepEqual(safe.organizationForm, {
      auto_organize_enabled: false,
      channel_id: null,
      model_id: null
    })
  }
})

test('does not misclassify content when maxTokens is invalid', () => {
  const content = 'aaaa'

  for (const maxTokens of [null, undefined, '', 'invalid', Number.NaN, Number.POSITIVE_INFINITY, -1]) {
    assert.equal(isMemoryContentTooLong(content, maxTokens), false, `invalid maxTokens: ${String(maxTokens)}`)
  }
  assert.equal(isMemoryContentTooLong(content, 0), true)
  assert.equal(isMemoryContentTooLong(content, '160'), false)
})

test('filters organization models by channel, CHAT usage, model id, and enabled state', () => {
  const channels = [
    {
      id: 7,
      model_ids: [
        { model_id: 'chat-upper', usage: 'CHAT', is_enabled: true },
        { model_id: 'chat-lower', usage: 'chat', is_enabled: true },
        { model_id: 'not-chat', usage: 'EMBEDDING', is_enabled: true },
        { model_id: 'disabled', usage: 'CHAT', is_enabled: false },
        { usage: 'CHAT', is_enabled: true }
      ]
    },
    { id: 8, model_ids: [{ model_id: 'other-channel', usage: 'CHAT', is_enabled: true }] }
  ]
  const original = clone(channels)

  assert.deepEqual(
    getOrganizationModelsForChannel(channels, 7),
    [
      { model_id: 'chat-upper', usage: 'CHAT', is_enabled: true },
      { model_id: 'chat-lower', usage: 'chat', is_enabled: true }
    ]
  )
  assert.deepEqual(getOrganizationModelsForChannel(channels, 99), [])
  assert.deepEqual(channels, original)
})

test('validates organization selection pairs and auto-organization requirements', () => {
  assert.equal(
    validateOrganizationSettings(validOrganizationForm({ channel_id: 7, model_id: null }), validOrganizationModel(), 6400),
    'organization_selection_pair_required'
  )
  assert.equal(
    validateOrganizationSettings(validOrganizationForm({ channel_id: null, model_id: 'organizer-model' }), validOrganizationModel(), 6400),
    'organization_selection_pair_required'
  )
  assert.equal(
    validateOrganizationSettings(validOrganizationForm({ auto_organize_enabled: true, channel_id: null, model_id: null }), null, 6400),
    'organization_model_required'
  )
})

test('rejects organization models that are not enabled CHAT models', () => {
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), validOrganizationModel({ usage: 'EMBEDDING' }), 6400),
    'organization_model_invalid'
  )
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), validOrganizationModel({ usage: 'chat', is_enabled: false }), 6400),
    'organization_model_invalid'
  )
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), null, 6400),
    'organization_model_invalid'
  )
})

test('rejects organization models with invalid positive integer limits', () => {
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), validOrganizationModel({ context_window_k: 0 }), 6400),
    'organization_model_limits_invalid'
  )
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), validOrganizationModel({ context_window_k: 32.5 }), 6400),
    'organization_model_limits_invalid'
  )
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), validOrganizationModel({ max_tokens: 0 }), 6400),
    'organization_model_limits_invalid'
  )
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), validOrganizationModel({ max_tokens: 6400.5 }), 6400),
    'organization_model_limits_invalid'
  )
})

test('rejects models whose output budget is below the required organization output', () => {
  assert.equal(
    validateOrganizationSettings(validOrganizationForm(), validOrganizationModel({ max_tokens: 6399 }), 6400),
    'organization_max_tokens_too_small'
  )
})

test('accepts cleared settings and valid enabled or disabled organization configurations', () => {
  assert.equal(
    validateOrganizationSettings(
      validOrganizationForm({ auto_organize_enabled: false, channel_id: null, model_id: null }),
      null,
      6400
    ),
    null
  )
  assert.equal(
    validateOrganizationSettings(
      validOrganizationForm({ auto_organize_enabled: true }),
      validOrganizationModel({ usage: 'chat' }),
      6400
    ),
    null
  )
})

test('builds organization settings payload without server-owned or contextual fields', () => {
  const form = {
    auto_organize_enabled: true,
    channel_id: 7,
    model_id: 'organizer-model',
    uid: 'user-1',
    records: [{ id: 1 }],
    session_id: 'session-1',
    collection: 'private-collection',
    organization: { required_output_tokens: 6400 }
  }
  const original = clone(form)

  assert.deepEqual(buildOrganizationSettingsPayload(form), {
    auto_organize_enabled: true,
    organization_channel_id: 7,
    organization_model_id: 'organizer-model'
  })
  assert.deepEqual(form, original)
})

test('builds a manual organize payload containing only the dedupe key', () => {
  assert.deepEqual(
    buildOrganizePayload('manual-organize-1'),
    { dedupe_key: 'manual-organize-1' }
  )
  assert.deepEqual(
    buildOrganizePayload('manual-organize-1', {
      uid: 'user-1',
      records: [{ id: 1 }],
      session_id: 'session-1',
      collection: 'private-collection'
    }),
    { dedupe_key: 'manual-organize-1' }
  )
})

test('finds the active organization task and preserves its operation and progress', () => {
  const settings = {
    organization: {
      current_job: {
        id: 12,
        operation: 'organize',
        status: 'running',
        completed: 3,
        total: 5
      }
    }
  }
  const original = clone(settings)

  assert.deepEqual(getCurrentMemoryTask(settings), {
    id: 12,
    operation: 'organize',
    status: 'running',
    completed: 3,
    total: 5,
    percentage: 60
  })
  assert.deepEqual(settings, original)
})

test('prefers the top-level current job for any operation and reads payload progress', () => {
  assert.deepEqual(
    getCurrentMemoryTask({
      current_job: {
        id: 13,
        operation: 'create',
        status: 'running',
        payload: { progress: { success_count: 2, total_count: 5 } }
      },
      organization: {
        current_job: { id: 99, operation: 'organize', status: 'running', completed: 1, total: 2 }
      }
    }),
    { id: 13, operation: 'create', status: 'running', completed: 2, total: 5, percentage: 40 }
  )
  assert.deepEqual(
    getCurrentMemoryTask({ current_job: { id: 14, operation: 'organize', status: 'retry' } }),
    { id: 14, operation: 'organize', status: 'retry', completed: null, total: null, percentage: null }
  )
})

test('keeps top-level migration job identity and uses migration stage progress', () => {
  assert.deepEqual(
    getCurrentMemoryTask({
      current_job: {
        id: 23,
        operation: 'embedding_migration',
        status: 'pending'
      },
      migration: {
        status: 'building',
        success_count: 4,
        total_count: 10
      }
    }),
    { id: 23, operation: 'embedding_migration', status: 'building', completed: 4, total: 10, percentage: 40 }
  )
})

test('merges migration job identity with migration stage progress', () => {
  assert.deepEqual(
    getCurrentMemoryTask({
      migration_job: { id: 21, operation: 'embedding_migration', status: 'running' },
      migration: { job_id: 21, status: 'building', success_count: 4, total_count: 10 }
    }),
    { id: 21, operation: 'embedding_migration', status: 'building', completed: 4, total: 10, percentage: 40 }
  )
})

test('identifies reindex from maintenance using index status when maintenance has no status', () => {
  assert.deepEqual(
    getCurrentMemoryTask({
      index: { status: 'reindexing' },
      blocking: { maintenance: { operation: 'reindex', job_id: 22, payload: { progress: { success_count: 8, total_count: 16 } } } }
    }),
    { id: 22, operation: 'reindex', status: 'reindexing', completed: 8, total: 16, percentage: 50 }
  )
})

test('returns cleanup tasks without inventing progress and ignores missing or terminal tasks', () => {
  assert.deepEqual(
    getCurrentMemoryTask({ old_collection_cleanup: { job_id: 31, status: 'retry' } }),
    { id: 31, operation: 'delete_cleanup', status: 'retry', completed: null, total: null, percentage: null }
  )
  for (const status of ['succeeded', 'failed', 'cancelled']) {
    assert.equal(getCurrentMemoryTask({ organization: { current_job: { id: 1, operation: 'organize', status } } }), null)
    assert.equal(getCurrentMemoryTask({ old_collection_cleanup: { job_id: 2, status } }), null)
  }
  assert.equal(getCurrentMemoryTask({}), null)
  assert.equal(getCurrentMemoryTask(null), null)
})

test('limits task percentages and accepts numeric strings without mutating settings', () => {
  const settings = {
    migration: { id: 'migration-1', status: 'running', success_count: '-4', total_count: '2' }
  }
  const original = clone(settings)

  assert.deepEqual(getCurrentMemoryTask(settings), {
    id: 'migration-1',
    operation: 'embedding_migration',
    status: 'running',
    completed: 0,
    total: 2,
    percentage: 0
  })
  assert.deepEqual(settings, original)
  assert.equal(getCurrentMemoryTask({ migration: { status: 'running', success_count: 4, total_count: 0 } }).percentage, null)
  assert.equal(getCurrentMemoryTask({ migration: { status: 'running', success_count: 12, total_count: 10 } }).percentage, 100)
})

test('returns known operation and source i18n keys and stable unknown fallbacks', () => {
  for (const operation of [
    'create',
    'update',
    'restore',
    'reindex',
    'delete_cleanup',
    'embedding_migration',
    'create_with_eviction',
    'organize',
    'organize_merge'
  ]) {
    assert.equal(memoryOperationLabelKey(operation), `memories.operation_${operation}`)
  }
  for (const source of ['user_api', 'llm_tool', 'auto_extract', 'auto_organize']) {
    assert.equal(memorySourceLabelKey(source), `memories.source_${source}`)
  }
  assert.equal(memoryOperationLabelKey('future_operation'), 'future_operation')
  assert.equal(memorySourceLabelKey('future_source'), 'future_source')
  for (const emptyValue of [null, undefined, '']) {
    assert.equal(memoryOperationLabelKey(emptyValue), '-')
    assert.equal(memorySourceLabelKey(emptyValue), '-')
  }
})

test('builds separate multi-level job trees without mutating input jobs', () => {
  const jobs = [
    { id: 1, operation: 'organize', child_job_ids: [2] },
    { id: 10, operation: 'organize', child_job_ids: [11] },
    { id: 2, operation: 'organize_merge', child_job_ids: [3] },
    { id: 11, operation: 'organize_merge', parent_job_id: 10 },
    { id: 3, operation: 'delete_cleanup', parent_job_id: 2 },
    { id: 20, operation: 'create', parent_job_id: 999 }
  ]
  const original = clone(jobs)

  const decorated = decorateMemoryJobs(jobs)

  assert.deepEqual(decorated.map(job => job.id), [1, 10, 20])
  assert.deepEqual(decorated[0].childJobs.map(job => [job.id, job.jobLevel]), [[2, 1]])
  assert.deepEqual(decorated[0].childJobs[0].childJobs.map(job => [job.id, job.jobLevel]), [[3, 2]])
  assert.deepEqual(decorated[1].childJobs.map(job => [job.id, job.jobLevel]), [[11, 1]])
  assert.deepEqual(decorated[2].childJobs, [])
  assert.notStrictEqual(decorated, jobs)
  assert.notStrictEqual(decorated[0], jobs[0])
  assert.deepEqual(jobs, original)
})

test('keeps an orphaned cross-page child at one level and infers parents from child ids', () => {
  const jobs = [
    { id: 11, parent_job_id: 999 },
    { id: 12, child_job_ids: [13] },
    { id: 13 },
    { id: 14, child_job_ids: [1000] }
  ]

  const decorated = decorateMemoryJobs(jobs)

  assert.deepEqual(decorated.map(job => job.id), [11, 12, 14])
  assert.equal(decorated[0].jobLevel, 1)
  assert.deepEqual(decorated[1].childJobs.map(job => [job.id, job.jobLevel]), [[13, 1]])
  assert.deepEqual(decorated[2].childJobs, [])
})

test('protects job decoration from circular parent graphs', () => {
  const jobs = [
    { id: 21, parent_job_id: 22 },
    { id: 22, parent_job_id: 21 },
    { id: 23, parent_job_id: 22 }
  ]

  const decorated = decorateMemoryJobs(jobs)

  assert.deepEqual(decorated.map(job => job.id), [21, 22, 23])
  assert.ok(decorated.every(job => Number.isFinite(job.jobLevel)))
  assert.ok(decorated.every(job => job.jobLevel >= 0))
  assert.ok(decorated.every(job => job.childJobs.length === 0))
})

test('safely decorates null jobs and jobs with invalid child_job_ids', () => {
  const jobs = [
    null,
    { id: 31, child_job_ids: null },
    { id: 32, child_job_ids: '33' },
    { id: 33, child_job_ids: {} },
    { id: 34, child_job_ids: [35] },
    { id: 35 }
  ]
  const original = clone(jobs)

  const decorated = decorateMemoryJobs(jobs)

  assert.deepEqual(decorated.map(job => job?.id ?? null), [null, 31, 32, 33, 34])
  assert.equal(decorated[0], null)
  assert.equal(decorated[1].jobLevel, 0)
  assert.equal(decorated[2].jobLevel, 0)
  assert.equal(decorated[3].jobLevel, 0)
  assert.deepEqual(decorated[4].childJobs.map(job => [job.id, job.jobLevel]), [[35, 1]])
  assert.deepEqual(jobs, original)
})

test('applies only the newest request when polling responses complete C/B/A out of order', () => {
  const tracker = createLatestRequestTracker()
  const requestA = tracker.begin()
  const requestB = tracker.begin()
  const requestC = tracker.begin()
  const applied = []

  for (const [request, response] of [[requestC, 'C'], [requestB, 'B'], [requestA, 'A']]) {
    if (tracker.isCurrent(request)) applied.push(response)
  }

  assert.deepEqual(applied, ['C'])
  assert.equal(tracker.isCurrent(requestA), false)
  assert.equal(tracker.isCurrent(requestB), false)
  assert.equal(tracker.isCurrent(requestC), true)
})

test('invalidate makes all outstanding requests stale and does not revive old tokens', () => {
  const tracker = createLatestRequestTracker()
  const requestA = tracker.begin()
  const requestB = tracker.begin()

  tracker.invalidate()

  assert.equal(tracker.isCurrent(requestA), false)
  assert.equal(tracker.isCurrent(requestB), false)

  const requestC = tracker.begin()
  assert.equal(tracker.isCurrent(requestA), false)
  assert.equal(tracker.isCurrent(requestB), false)
  assert.equal(tracker.isCurrent(requestC), true)
})

test('repeated polling completion checks are idempotent for the current request', () => {
  const tracker = createLatestRequestTracker()
  const request = tracker.begin()

  assert.equal(tracker.isCurrent(request), true)
  assert.equal(tracker.isCurrent(request), true)
  assert.equal(tracker.isCurrent(request), true)

  const nextRequest = tracker.begin()
  assert.equal(tracker.isCurrent(request), false)
  assert.equal(tracker.isCurrent(nextRequest), true)
  assert.equal(tracker.isCurrent(nextRequest), true)
})
