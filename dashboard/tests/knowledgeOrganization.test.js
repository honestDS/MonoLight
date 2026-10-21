import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { computed, effectScope, ref } from 'vue'
import { createI18n } from 'vue-i18n'

import * as knowledgeBaseManagement from '../src/utils/knowledgeBaseManagement.js'
import { createKnowledgeBaseScope } from '../src/utils/knowledgeBaseScope.js'
import * as requestTaskManager from '../src/utils/requestTaskManager.js'
import zhKnowledgeBase from '../src/i18n/locales/zh/knowledgeBase.js'
import enKnowledgeBase from '../src/i18n/locales/en/knowledgeBase.js'

const useKnowledgeOrganizationSource = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeOrganization.js', import.meta.url),
  'utf8',
)

const stripImports = sourceText => {
  const keptLines = []
  let inImport = false

  for (const line of sourceText.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const stripModuleSyntax = sourceText => stripImports(sourceText).replace(/\bexport\s+/g, '')
const useKnowledgeOrganizationScript = stripModuleSyntax(useKnowledgeOrganizationSource)
const dataResponse = data => ({ data: { data } })
const normalize = value => JSON.parse(JSON.stringify(value))
const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}
const createOrganizationJob = (knowledgeBaseId, id, status = 'succeeded', extra = {}) => ({
  id: `${knowledgeBaseId}-job-${id}`,
  knowledge_base_id: knowledgeBaseId,
  status,
  ...extra,
})
const createOrganizationJobs = (knowledgeBaseId, total, statusForId = () => 'succeeded') => (
  Array.from({ length: total }, (_, index) => {
    const id = total - index
    return createOrganizationJob(knowledgeBaseId, id, statusForId(id))
  })
)
const pagedOrganizationResponse = (jobs, { page, size }) => dataResponse({
  items: jobs.slice((page - 1) * size, page * size),
  total: jobs.length,
})

const createKnowledgeOrganizationHarness = async (t, options = {}) => {
  const selectedKb = ref(options.selectedKb ?? null)
  const organizes = []
  const organizationJobs = []
  const retries = []
  const refreshed = []
  const scheduled = []
  const messages = []
  const confirms = []
  const timers = []
  const knowledgeBaseApi = {
    organize: (selectedId, payload) => {
      organizes.push({ selectedId, payload })
      return options.organize?.({ selectedId, payload }) ?? Promise.resolve(dataResponse({}))
    },
    organizationJobs: (selectedId, params) => {
      organizationJobs.push({ selectedId, params })
      return options.organizationJobs?.({ selectedId, params })
        ?? Promise.resolve(dataResponse({ items: [], total: 0 }))
    },
    retryOrganizationJob: (selectedId, jobId) => {
      retries.push({ selectedId, jobId })
      return Promise.resolve(dataResponse({}))
    },
  }
  const ElMessage = {}
  for (const type of ['success', 'error']) ElMessage[type] = (...args) => messages.push({ type, args })
  const ElMessageBox = {
    confirm: async (...args) => {
      confirms.push(args)
      const result = typeof options.confirm === 'function'
        ? await options.confirm(...args)
        : options.confirm ?? true
      if (!result) throw 'cancel'
      return 'confirm'
    },
  }
  const i18n = createI18n({
    legacy: false,
    locale: options.locale ?? 'zh',
    messages: { zh: { knowledgeBase: zhKnowledgeBase }, en: { knowledgeBase: enKnowledgeBase } },
  })
  const fetchManagedKnowledgeItems = (...args) => {
    refreshed.push(args)
    return Promise.resolve()
  }
  const scheduleManagedKnowledgePolling = (...args) => scheduled.push(args)
  const scope = createKnowledgeBaseScope({
    confirm: (...args) => ElMessageBox.confirm(...args),
    closeConfirmations: () => {},
  })
  const vueEffectScope = effectScope()
  const context = vm.createContext({
    AbortController,
    clearTimeout() {},
    computed,
    ElMessage,
    ElMessageBox,
    fetchManagedKnowledgeItems,
    knowledgeBaseApi,
    ref,
    scheduleManagedKnowledgePolling,
    selectedKb,
    scope,
    setTimeout: (...args) => (timers.push(args), timers.length),
    t: (...args) => i18n.global.t(...args),
    ...knowledgeBaseManagement,
    ...requestTaskManager,
    console: { log() {}, warn() {}, error() {}, info() {} },
  })
  vueEffectScope.run(() => vm.runInContext(
    `${useKnowledgeOrganizationScript}\n` +
    'globalThis.__module = useKnowledgeOrganization({ t, selectedKb, scope, fetchManagedKnowledgeItems, scheduleManagedKnowledgePolling })',
    context,
    { filename: 'useKnowledgeOrganization.js' },
  ))
  const harness = {
    api: { organizes, organizationJobs, retries },
    confirms,
    messages,
    module: context.__module,
    refreshed,
    scheduled,
    selectedKb,
    scope,
    timers,
    dispose: () => {
      scope.invalidate()
      vueEffectScope.stop()
    },
  }
  t.after(harness.dispose)
  return harness
}

test('submits organization without selected items and refreshes managed knowledge', async t => {
  for (const locale of ['zh', 'en']) {
    const selectedKb = { id: `managed-${locale}`, knowledge_base_type: 'llm_managed', organization_error: 'failed' }
    const harness = await createKnowledgeOrganizationHarness(t, { locale, selectedKb })
    await harness.module.submitKnowledgeOrganization()

    assert.deepEqual(normalize(harness.api.organizes), [{ selectedId: selectedKb.id, payload: {} }])
    assert.equal(selectedKb.organization_error, null)
    assert.deepEqual(harness.refreshed, [[true, true]])
    assert.deepEqual(harness.scheduled, [[]])
    assert.equal(harness.messages[0].args[0], locale === 'zh' ? '知识整理已提交' : 'Organization job submitted')
    assert.equal(harness.module.organizationSubmitting.value, false)
  }
})

test('guards duplicate organization submissions and localizes failures before allowing retry', async t => {
  for (const locale of ['zh', 'en']) {
    const pending = deferred()
    let callCount = 0
    const harness = await createKnowledgeOrganizationHarness(t, {
      locale,
      selectedKb: { id: `managed-${locale}`, knowledge_base_type: 'llm_managed' },
      organize: () => ++callCount === 1 ? pending.promise : Promise.resolve(dataResponse({})),
    })
    const firstSubmit = harness.module.submitKnowledgeOrganization()
    const duplicateSubmit = harness.module.submitKnowledgeOrganization()

    assert.equal(harness.api.organizes.length, 1)
    assert.equal(harness.module.organizationSubmitting.value, true)
    pending.reject(new Error('network error'))
    await firstSubmit
    await duplicateSubmit

    assert.equal(
      harness.messages[harness.messages.length - 1].args[0],
      locale === 'zh' ? '提交知识整理失败: network error' : 'Failed to submit organization job: network error',
    )
    assert.equal(harness.module.organizationSubmitting.value, false)
    await harness.module.submitKnowledgeOrganization()
    assert.equal(harness.api.organizes.length, 2)
  }
})

test('does not submit organization without a managed knowledge base', async t => {
  for (const selectedKb of [null, { id: 'user-kb', knowledge_base_type: 'user' }]) {
    const harness = await createKnowledgeOrganizationHarness(t, { selectedKb })
    await harness.module.submitKnowledgeOrganization()

    assert.equal(harness.api.organizes.length, 0)
    assert.deepEqual(harness.refreshed, [])
    assert.equal(harness.module.organizationSubmitting.value, false)
  }
})

test('retries failed and cancelled jobs when the server allows it', async t => {
  for (const locale of ['zh', 'en']) {
    for (const status of ['failed', 'cancelled']) {
      const selectedKb = { id: `managed-${locale}-${status}`, knowledge_base_type: 'llm_managed' }
      const job = {
        id: `organization-${locale}-${status}`,
        knowledge_base_id: selectedKb.id,
        status,
        can_retry: true,
      }
      const harness = await createKnowledgeOrganizationHarness(t, { locale, selectedKb })

      assert.equal(harness.module.canRetryOrganizationJob(job), true)
      await harness.module.handleRetryOrganizationJob(job)

      assert.deepEqual(normalize(harness.api.retries), [{ selectedId: selectedKb.id, jobId: job.id }])
      assert.equal(harness.confirms.length, 1)
      assert.deepEqual(normalize(harness.confirms[0][2]), {
        confirmButtonText: locale === 'zh' ? '重试整理' : 'Retry Organization',
        cancelButtonText: locale === 'zh' ? '取消' : 'Cancel',
        type: 'warning',
      })
      assert.equal(
        harness.messages[harness.messages.length - 1].args[0],
        locale === 'zh' ? '整理作业已重新提交' : 'Organization job retried',
      )
      assert.deepEqual(harness.refreshed, [[true, true]])
      assert.equal(harness.module.organizationActionJobId.value, null)
    }
  }
})

test('does not retry jobs the server marks as ineligible', async t => {
  const selectedKb = { id: 'managed-kb', knowledge_base_type: 'llm_managed' }
  const cases = [
    { selectedKb, job: { id: 'failed-denied', knowledge_base_id: selectedKb.id, status: 'failed', can_retry: false } },
    { selectedKb, job: { id: 'cancelled-denied', knowledge_base_id: selectedKb.id, status: 'cancelled', can_retry: false } },
    {
      selectedKb,
      job: { id: 'legacy-failed', knowledge_base_id: selectedKb.id, status: 'failed', can_retry: false, payload: { knowledge_ids: [1] } },
    },
    {
      selectedKb,
      job: { id: 'legacy-cancelled', knowledge_base_id: selectedKb.id, status: 'cancelled', can_retry: false, payload: { knowledge_ids: [1] } },
    },
    { selectedKb, job: { id: 'missing-flag', knowledge_base_id: selectedKb.id, status: 'failed' } },
    { selectedKb, job: { id: 'numeric-flag', knowledge_base_id: selectedKb.id, status: 'failed', can_retry: 1 } },
    { selectedKb, job: { id: 'string-flag', knowledge_base_id: selectedKb.id, status: 'failed', can_retry: 'true' } },
    { selectedKb, job: { id: 'other-kb', knowledge_base_id: 'another-kb', status: 'failed', can_retry: true } },
    { selectedKb: null, job: { id: 'without-selection', knowledge_base_id: selectedKb.id, status: 'failed', can_retry: true } },
  ]

  for (const { selectedKb: currentKb, job } of cases) {
    const harness = await createKnowledgeOrganizationHarness(t, { selectedKb: currentKb })

    assert.equal(harness.module.canRetryOrganizationJob(job), false)
    await harness.module.handleRetryOrganizationJob(job)

    assert.equal(harness.confirms.length, 0)
    assert.deepEqual(normalize(harness.api.retries), [])
    assert.deepEqual(harness.refreshed, [])
  }
})

test('rechecks can_retry after the confirmation resolves', async t => {
  const pending = deferred()
  const selectedKb = { id: 'managed-kb', knowledge_base_type: 'llm_managed' }
  const job = {
    id: 'organization-guarded',
    knowledge_base_id: selectedKb.id,
    status: 'failed',
    can_retry: true,
  }
  const harness = await createKnowledgeOrganizationHarness(t, {
    selectedKb,
    confirm: () => pending.promise,
  })
  const retry = harness.module.handleRetryOrganizationJob(job)

  assert.equal(harness.confirms.length, 1)
  job.can_retry = false
  pending.resolve(true)
  await retry

  assert.deepEqual(normalize(harness.api.retries), [])
  assert.deepEqual(harness.refreshed, [])
  assert.equal(harness.module.organizationActionJobId.value, null)
})

test('uses live organization jobs instead of a cached knowledge-base error', async t => {
  const selectedKb = {
    id: 'managed-kb',
    knowledge_base_type: 'llm_managed',
    organization_error: 'stale cached organization error'
  }
  let jobs = [{
    id: 'organization-live',
    knowledge_base_id: selectedKb.id,
    status: 'running',
    error: null
  }]
  const harness = await createKnowledgeOrganizationHarness(t, {
    selectedKb,
    organizationJobs: () => dataResponse({ items: jobs, total: jobs.length })
  })

  await harness.module.showOrganizationDialog()
  assert.equal(harness.module.organizationDialogVisible.value, true)
  assert.equal(harness.module.latestOrganizationJob.value.status, 'running')
  assert.equal(harness.module.latestOrganizationJob.value.error, null)

  const failedJob = {
    ...jobs[0],
    status: 'failed',
    error: 'organization failed with the original server error',
    can_retry: true
  }
  jobs = [failedJob]
  await harness.module.fetchOrganizationJobs()
  assert.equal(harness.module.latestOrganizationJob.value.status, 'failed')
  assert.equal(harness.module.latestOrganizationJob.value.error, 'organization failed with the original server error')

  const successJob = {
    id: 'organization-success',
    knowledge_base_id: selectedKb.id,
    status: 'succeeded',
    error: null,
    can_retry: false
  }
  jobs = [successJob, failedJob]
  await harness.module.fetchOrganizationJobs()
  assert.equal(harness.module.latestOrganizationJob.value.id, 'organization-success')
  assert.equal(harness.module.latestOrganizationJob.value.status, 'succeeded')
  assert.equal(harness.module.latestOrganizationJob.value.error, null)

  const cancelledJob = {
    id: 'organization-cancelled',
    knowledge_base_id: selectedKb.id,
    status: 'cancelled',
    error: null,
    can_retry: true
  }
  jobs = [cancelledJob, successJob, failedJob]
  await harness.module.fetchOrganizationJobs()
  assert.equal(harness.module.latestOrganizationJob.value.id, 'organization-cancelled')
  assert.equal(harness.module.latestOrganizationJob.value.status, 'cancelled')
  assert.equal(harness.module.latestOrganizationJob.value.error, null)

  await harness.module.fetchOrganizationJobs()
  assert.equal(harness.module.organizationJobs.value.length, 3)
  assert.equal(harness.module.latestOrganizationJob.value.id, 'organization-cancelled')
  assert.equal(harness.module.organizationJobs.value[2].id, 'organization-live')
  assert.equal(harness.module.organizationJobs.value[2].error, 'organization failed with the original server error')
  assert.equal(selectedKb.organization_error, 'stale cached organization error')
})

test('keeps the organization dialog visible and shows the new job after retry', async t => {
  const selectedKb = { id: 'managed-kb', knowledge_base_type: 'llm_managed' }
  const failedJob = {
    id: 'organization-failed',
    knowledge_base_id: selectedKb.id,
    status: 'failed',
    error: 'organization failed before retry',
    can_retry: true
  }
  const pendingJob = {
    id: 'organization-retry',
    knowledge_base_id: selectedKb.id,
    status: 'pending',
    error: null,
    can_retry: false
  }
  let jobs = [failedJob]
  const harness = await createKnowledgeOrganizationHarness(t, {
    selectedKb,
    organizationJobs: () => dataResponse({ items: jobs, total: jobs.length })
  })

  await harness.module.showOrganizationDialog()
  assert.equal(harness.module.organizationDialogVisible.value, true)
  assert.equal(harness.module.latestOrganizationJob.value.id, failedJob.id)
  assert.equal(harness.module.latestOrganizationJob.value.error, failedJob.error)

  jobs = [pendingJob, failedJob]
  await harness.module.handleRetryOrganizationJob(failedJob)

  assert.deepEqual(normalize(harness.api.retries), [{ selectedId: selectedKb.id, jobId: failedJob.id }])
  assert.equal(harness.module.organizationDialogVisible.value, true)
  assert.equal(harness.module.latestOrganizationJob.value.id, pendingJob.id)
  assert.equal(harness.module.latestOrganizationJob.value.status, 'pending')
  assert.equal(harness.module.latestOrganizationJob.value.error, null)
  assert.equal(harness.module.organizationJobs.value[1].id, failedJob.id)
  assert.equal(harness.module.organizationJobs.value[1].status, 'failed')
  assert.equal(harness.module.organizationJobs.value[1].error, failedJob.error)
})

test('ignores late organization submissions after a new session starts', async t => {
  for (const outcome of ['success', 'failure']) {
    const oldDeferred = deferred()
    const newDeferred = deferred()
    const selectedKb = { id: 'managed-kb', knowledge_base_type: 'llm_managed' }
    let submissionCount = 0
    const harness = await createKnowledgeOrganizationHarness(t, {
      selectedKb,
      organize: () => ++submissionCount === 1 ? oldDeferred.promise : newDeferred.promise,
    })

    const oldSubmit = harness.module.submitKnowledgeOrganization()
    harness.scope.invalidate()
    harness.module.resetOrganizationState()
    const newSubmit = harness.module.submitKnowledgeOrganization()

    assert.equal(harness.module.organizationSubmitting.value, true)
    if (outcome === 'success') oldDeferred.resolve(dataResponse({}))
    else oldDeferred.reject(new Error('late organization failure'))
    await oldSubmit

    assert.deepEqual(harness.messages, [])
    assert.deepEqual(harness.refreshed, [])
    assert.deepEqual(harness.scheduled, [])
    assert.equal(harness.module.organizationSubmitting.value, true)
    assert.deepEqual(normalize(harness.module.organizationJobs.value), [])

    newDeferred.resolve(dataResponse({}))
    await newSubmit

    assert.equal(harness.messages.length, 1)
    assert.equal(harness.messages[0].type, 'success')
    assert.equal(harness.messages[0].args[0], '知识整理已提交')
    assert.deepEqual(harness.refreshed, [[true, true]])
    assert.deepEqual(harness.scheduled, [[]])
    assert.equal(harness.module.organizationSubmitting.value, false)
  }
})

test('paginates organization history without losing or duplicating jobs', async t => {
  const selectedKb = { id: 'managed-paged-kb', knowledge_base_type: 'llm_managed' }
  const jobs = createOrganizationJobs(selectedKb.id, 45)
  const harness = await createKnowledgeOrganizationHarness(t, {
    selectedKb,
    organizationJobs: ({ params }) => pagedOrganizationResponse(jobs, params),
  })

  await harness.module.showOrganizationDialog()
  const latestJobId = jobs[0].id
  const firstPageHistoryIds = harness.module.organizationHistoryJobs.value.map(job => job.id)
  assert.equal(harness.module.organizationJobPage.value, 1)
  assert.equal(harness.module.organizationTotal.value, 45)
  assert.equal(harness.module.organizationHistoryTotal.value, 44)
  assert.equal(harness.module.organizationJobs.value.length, 20)
  assert.equal(harness.module.latestOrganizationJob.value.id, latestJobId)
  assert.equal(firstPageHistoryIds.length, 19)

  await harness.module.handleOrganizationPageChange(1)
  assert.equal(harness.api.organizationJobs.length, 1)

  await harness.module.handleOrganizationPageChange(2)
  const secondPageHistoryIds = harness.module.organizationHistoryJobs.value.map(job => job.id)
  assert.equal(harness.module.organizationJobPage.value, 2)
  assert.equal(harness.module.organizationJobs.value.length, 20)
  assert.equal(harness.module.organizationJobs.value[0].id, jobs[20].id)
  assert.equal(harness.module.latestOrganizationJob.value.id, latestJobId)
  assert.equal(secondPageHistoryIds.length, 20)

  await harness.module.handleOrganizationPageChange(2)
  assert.equal(harness.api.organizationJobs.length, 3)

  await harness.module.handleOrganizationPageChange(3)
  const thirdPageHistoryIds = harness.module.organizationHistoryJobs.value.map(job => job.id)
  assert.equal(harness.module.organizationJobPage.value, 3)
  assert.equal(harness.module.organizationJobs.value.length, 5)
  assert.equal(harness.module.latestOrganizationJob.value.id, latestJobId)
  assert.equal(thirdPageHistoryIds.length, 5)

  const visitedHistoryIds = [...firstPageHistoryIds, ...secondPageHistoryIds, ...thirdPageHistoryIds]
  assert.equal(visitedHistoryIds.length, 44)
  assert.equal(new Set(visitedHistoryIds).size, 44)
  assert.deepEqual(visitedHistoryIds.sort(), jobs.slice(1).map(job => job.id).sort())

  await harness.module.handleOrganizationPageChange(1)
  await harness.module.fetchOrganizationJobs()
  await harness.module.fetchOrganizationJobs()
  assert.deepEqual(harness.module.organizationHistoryJobs.value.map(job => job.id), firstPageHistoryIds)
  assert.equal(harness.module.organizationHistoryJobs.value.length, 19)
  assert.equal(harness.module.latestOrganizationJob.value.id, latestJobId)
  assert.deepEqual(
    harness.api.organizationJobs.map(({ selectedId, params }) => [selectedId, params.page, params.size]),
    [
      [selectedKb.id, 1, 20],
      [selectedKb.id, 2, 20],
      [selectedKb.id, 1, 1],
      [selectedKb.id, 3, 20],
      [selectedKb.id, 1, 1],
      [selectedKb.id, 1, 20],
      [selectedKb.id, 1, 20],
      [selectedKb.id, 1, 20],
    ],
  )
})

test('handles organization history total and page boundaries', async t => {
  for (const total of [0, 1, 20, 21]) {
    const selectedKb = { id: `managed-boundary-${total}`, knowledge_base_type: 'llm_managed' }
    const jobs = createOrganizationJobs(selectedKb.id, total)
    const harness = await createKnowledgeOrganizationHarness(t, {
      selectedKb,
      organizationJobs: ({ params }) => pagedOrganizationResponse(jobs, params),
    })

    await harness.module.showOrganizationDialog()
    const maxPage = Math.max(1, Math.ceil(total / 20))
    assert.equal(harness.module.organizationTotal.value, total)
    assert.equal(harness.module.organizationHistoryTotal.value, Math.max(total - 1, 0))
    assert.equal(harness.module.organizationHistoryJobs.value.length, Math.min(Math.max(total - 1, 0), 19))
    assert.equal(harness.module.latestOrganizationJob.value?.id ?? null, jobs[0]?.id ?? null)

    for (const invalidPage of [0, -1, 1.5, '2', Number.NaN, maxPage + 1]) {
      await harness.module.handleOrganizationPageChange(invalidPage)
    }
    await harness.module.handleOrganizationPageChange(1)
    assert.equal(harness.api.organizationJobs.length, 1)
    assert.equal(harness.module.organizationJobPage.value, 1)

    if (total === 21) {
      await harness.module.handleOrganizationPageChange(2)
      assert.equal(harness.module.organizationJobPage.value, 2)
      assert.equal(harness.module.organizationJobs.value.length, 1)
      assert.equal(harness.module.organizationJobs.value[0].id, `${selectedKb.id}-job-1`)
      assert.equal(harness.module.organizationHistoryJobs.value[0].id, `${selectedKb.id}-job-1`)
      assert.equal(harness.module.latestOrganizationJob.value.id, `${selectedKb.id}-job-21`)
    }
  }
})

test('keeps the displayed organization page intact when either page request fails', async t => {
  for (const failedRequest of ['page', 'latest']) {
    const selectedKb = { id: `managed-failure-${failedRequest}`, knowledge_base_type: 'llm_managed' }
    const jobs = createOrganizationJobs(selectedKb.id, 45)
    let failure = failedRequest
    const harness = await createKnowledgeOrganizationHarness(t, {
      selectedKb,
      organizationJobs: ({ params }) => {
        const failsCurrentPage = failure === 'page' && params.page === 2 && params.size === 20
        const failsLatestJob = failure === 'latest' && params.page === 1 && params.size === 1
        if (failsCurrentPage || failsLatestJob) return Promise.reject(new Error(`${failure} request unavailable`))
        return pagedOrganizationResponse(jobs, params)
      },
    })

    await harness.module.showOrganizationDialog()
    const previousLatest = normalize(harness.module.latestOrganizationJob.value)
    const previousJobIds = harness.module.organizationJobs.value.map(job => job.id)
    const previousHistoryIds = harness.module.organizationHistoryJobs.value.map(job => job.id)
    const previousTotal = harness.module.organizationTotal.value

    await harness.module.handleOrganizationPageChange(2)
    assert.equal(harness.module.organizationJobPage.value, 1)
    assert.deepEqual(normalize(harness.module.latestOrganizationJob.value), previousLatest)
    assert.deepEqual(harness.module.organizationJobs.value.map(job => job.id), previousJobIds)
    assert.deepEqual(harness.module.organizationHistoryJobs.value.map(job => job.id), previousHistoryIds)
    assert.equal(harness.module.organizationTotal.value, previousTotal)
    assert.equal(harness.module.organizationHistoryTotal.value, previousTotal - 1)
    assert.equal(harness.module.organizationLoading.value, false)
    assert.deepEqual(harness.messages.map(message => message.args[0]), [
      `${zhKnowledgeBase.organization_fetch_failed}${failedRequest} request unavailable`,
    ])

    failure = null
    await harness.module.handleOrganizationPageChange(2)
    assert.equal(harness.module.organizationJobPage.value, 2)
    assert.equal(harness.module.organizationJobs.value.length, 20)
    assert.equal(harness.module.organizationJobs.value[0].id, jobs[20].id)
    assert.equal(harness.module.latestOrganizationJob.value.id, jobs[0].id)
    assert.equal(harness.module.organizationTotal.value, 45)
    assert.equal(harness.module.organizationLoading.value, false)
  }
})

test('keeps polling the latest organization job while viewing terminal history', async t => {
  const selectedKb = { id: 'managed-polling-kb', knowledge_base_type: 'llm_managed' }
  const jobs = createOrganizationJobs(selectedKb.id, 25, id => id === 25 ? 'running' : 'succeeded')
  const setLatest = (status, completedStageCount) => {
    jobs[0] = {
      ...jobs[0],
      status,
      organization_progress: { completed_stage_count: completedStageCount, stage_count: 4 },
    }
  }
  setLatest('running', 1)
  const harness = await createKnowledgeOrganizationHarness(t, {
    selectedKb,
    organizationJobs: ({ params }) => pagedOrganizationResponse(jobs, params),
  })

  await harness.module.showOrganizationDialog()
  await harness.module.handleOrganizationPageChange(2)
  const pageTwoIds = harness.module.organizationJobs.value.map(job => job.id)
  assert.equal(harness.module.organizationJobs.value.every(job => job.status === 'succeeded'), true)
  assert.equal(harness.module.latestOrganizationJob.value.status, 'running')

  setLatest('running', 2)
  const timerCountBeforeRunningRefresh = harness.timers.length
  await harness.timers.at(-1)[0]()
  assert.equal(harness.module.organizationJobPage.value, 2)
  assert.deepEqual(harness.module.organizationJobs.value.map(job => job.id), pageTwoIds)
  assert.equal(harness.module.latestOrganizationJob.value.organization_progress.completed_stage_count, 2)
  assert.equal(harness.timers.length, timerCountBeforeRunningRefresh + 1)

  setLatest('succeeded', 4)
  const timerCountBeforeTerminalRefresh = harness.timers.length
  await harness.timers.at(-1)[0]()
  assert.equal(harness.module.latestOrganizationJob.value.status, 'succeeded')
  assert.equal(harness.module.organizationJobPage.value, 2)
  assert.deepEqual(harness.module.organizationJobs.value.map(job => job.id), pageTwoIds)
  assert.equal(harness.timers.length, timerCountBeforeTerminalRefresh)
})

test('ignores stale first-page responses after navigating to another organization page', async t => {
  for (const outcome of ['success', 'failure']) {
    const selectedKb = { id: `managed-stale-poll-${outcome}`, knowledge_base_type: 'llm_managed' }
    const jobs = createOrganizationJobs(selectedKb.id, 45)
    const pendingPoll = deferred()
    let delayPoll = false
    const harness = await createKnowledgeOrganizationHarness(t, {
      selectedKb,
      organizationJobs: ({ params }) => {
        if (delayPoll && params.page === 1 && params.size === 20) {
          delayPoll = false
          return pendingPoll.promise
        }
        return pagedOrganizationResponse(jobs, params)
      },
    })

    await harness.module.showOrganizationDialog()
    delayPoll = true
    const stalePoll = harness.module.fetchOrganizationJobs()
    await harness.module.handleOrganizationPageChange(2)
    const pageTwoIds = harness.module.organizationJobs.value.map(job => job.id)

    if (outcome === 'success') {
      pendingPoll.resolve(dataResponse({ items: [createOrganizationJob(selectedKb.id, 'stale')], total: 99 }))
    } else {
      pendingPoll.reject(new Error('late first-page failure'))
    }
    await stalePoll

    assert.equal(harness.module.organizationJobPage.value, 2)
    assert.deepEqual(harness.module.organizationJobs.value.map(job => job.id), pageTwoIds)
    assert.equal(harness.module.organizationTotal.value, 45)
    assert.equal(harness.module.latestOrganizationJob.value.id, jobs[0].id)
    assert.deepEqual(harness.messages, [])
  }
})

test('ignores stale organization page responses after resetting and reopening another knowledge base', async t => {
  for (const outcome of ['success', 'failure']) {
    const oldKb = { id: `managed-old-kb-${outcome}`, knowledge_base_type: 'llm_managed' }
    const newKb = { id: `managed-new-kb-${outcome}`, knowledge_base_type: 'llm_managed' }
    const oldJobs = createOrganizationJobs(oldKb.id, 45)
    const newJobs = createOrganizationJobs(newKb.id, 3)
    const pendingOldPage = deferred()
    let delayOldPage = true
    const harness = await createKnowledgeOrganizationHarness(t, {
      selectedKb: oldKb,
      organizationJobs: ({ selectedId, params }) => {
        if (selectedId === oldKb.id && delayOldPage && params.page === 2 && params.size === 20) {
          delayOldPage = false
          return pendingOldPage.promise
        }
        const jobs = selectedId === oldKb.id ? oldJobs : newJobs
        return pagedOrganizationResponse(jobs, params)
      },
    })

    await harness.module.showOrganizationDialog()
    const stalePageChange = harness.module.handleOrganizationPageChange(2)
    assert.equal(harness.module.organizationJobPage.value, 1)

    harness.scope.invalidate()
    harness.selectedKb.value = newKb
    harness.module.resetOrganizationState()
    await harness.module.showOrganizationDialog()

    if (outcome === 'success') {
      pendingOldPage.resolve(dataResponse({ items: oldJobs.slice(20, 40), total: oldJobs.length }))
    } else {
      pendingOldPage.reject(new Error('late old knowledge-base failure'))
    }
    await stalePageChange

    assert.equal(harness.module.organizationJobPage.value, 1)
    assert.deepEqual(harness.module.organizationJobs.value.map(job => job.id), newJobs.map(job => job.id))
    assert.equal(harness.module.organizationTotal.value, newJobs.length)
    assert.equal(harness.module.organizationHistoryTotal.value, 2)
    assert.equal(harness.module.latestOrganizationJob.value.id, newJobs[0].id)
    assert.deepEqual(harness.messages, [])
  }
})
