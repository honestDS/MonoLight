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
