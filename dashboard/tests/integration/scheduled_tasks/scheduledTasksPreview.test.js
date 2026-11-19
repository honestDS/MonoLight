import assert from 'node:assert/strict'
import test from 'node:test'
import { nextTick } from 'vue'
import {
  createHarness,
  fillForm,
  flushPreview,
  flushVue,
  previewResponse,
  snapshot,
} from './scheduledTasksHarness.js'

test('automatic preview stays collapsed while toggling preserves the result and allows saving', async () => {
  const { apiState, page } = createHarness()
  page.showDialog('create')
  fillForm(page.form)
  await flushVue()

  const previewTimesBeforeToggle = snapshot(page.previewTimes.value)
  assert.equal(apiState.previewRequests.length, 1)
  assert.equal(page.previewExpanded.value, false)
  assert.equal(page.previewMatches.value, true)

  await page.togglePreview()
  assert.equal(page.previewExpanded.value, true)
  assert.deepEqual(snapshot(page.previewTimes.value), previewTimesBeforeToggle)
  assert.equal(page.previewMatches.value, true)

  await page.togglePreview()
  assert.equal(page.previewExpanded.value, false)
  await page.togglePreview()
  assert.equal(page.previewExpanded.value, true)
  await page.togglePreview()
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), previewTimesBeforeToggle)
  assert.equal(page.previewMatches.value, true)
  assert.equal(apiState.previewRequests.length, 1)

  await page.submitForm()
  assert.equal(apiState.createRequests.length, 1)
  assert.deepEqual(snapshot(apiState.createRequests[0].payload), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    interval_seconds: 300,
    run_once: false,
  })
  assert.equal(page.dialogVisible.value, false)
})

test('schedule changes collapse and clear the old preview until the new rule is requested', async () => {
  const { apiState, page } = createHarness({
    previewHandler: request => request.payload.interval_seconds === 300
      ? previewResponse(['2026-10-20T09:00:00.000Z'])
      : previewResponse(['2026-10-25T09:00:00.000Z']),
  })
  page.showDialog('create')
  fillForm(page.form)
  await flushVue()
  await page.togglePreview()

  page.form.interval_seconds = 600
  await nextTick()
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)

  await page.submitForm()
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 0)
  assert.equal(page.scheduleValidationError.value, 'scheduledTasks.preview_required')

  await page.togglePreview()
  await flushVue()
  assert.equal(page.previewExpanded.value, true)
  assert.deepEqual(snapshot(apiState.previewRequests[1].payload), {
    interval_seconds: 600,
    run_once: false,
  })
  assert.deepEqual(snapshot(page.previewTimes.value), ['2026-10-25T09:00:00.000Z'])
  assert.equal(page.previewMatches.value, true)
  assert.equal(page.scheduleValidationError.value, '')
})

test('schedule changes invalidate the old preview and block save until a matching preview exists', async () => {
  const { apiState, page } = createHarness({
    previewHandler: request => request.deferred.promise,
  })
  page.showDialog('create')
  fillForm(page.form)
  await nextTick()

  assert.equal(apiState.previewRequests.length, 1)
  assert.equal(page.previewLoading.value, true)
  const staleRequest = apiState.previewRequests[0]

  page.form.interval_seconds = 600
  await nextTick()

  assert.equal(page.previewLoading.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)
  assert.equal(page.previewExpanded.value, false)
  await page.submitForm()
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 0)

  staleRequest.deferred.resolve(previewResponse(['2026-10-20T09:00:00.000Z']))
  await flushVue()
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)
  assert.equal(page.previewExpanded.value, false)
  assert.equal(apiState.createRequests.length, 0)
})

test('submitting while preview is loading does not send a create or update request', async () => {
  const { apiState, page } = createHarness({
    previewHandler: request => request.deferred.promise,
  })
  page.showDialog('create')
  fillForm(page.form, { schedule_mode: 'interval' })
  await nextTick()

  assert.equal(page.previewLoading.value, true)
  await page.submitForm()
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 0)
  apiState.previewRequests[0].deferred.resolve(previewResponse())
  await flushVue()
})

test('manual preview stays expanded while loading and ignores repeated clicks', async () => {
  let previewCallCount = 0
  const harness = createHarness({
    previewHandler: request => {
      previewCallCount += 1
      return previewCallCount === 1
        ? previewResponse(['2026-10-20T09:00:00.000Z'])
        : request.deferred.promise
    },
  })
  const { apiState, page } = harness
  page.showDialog('create')
  fillForm(page.form)
  await flushVue()
  assert.equal(page.previewMatches.value, true)
  assert.equal(page.previewExpanded.value, false)

  page.form.interval_seconds = 600
  await nextTick()
  const manualPreview = page.togglePreview()
  assert.equal(apiState.previewRequests.length, 2)
  assert.deepEqual(snapshot(apiState.previewRequests[1].payload), {
    interval_seconds: 600,
    run_once: false,
  })
  assert.equal(page.previewLoading.value, true)
  assert.equal(page.previewExpanded.value, true)

  await page.togglePreview()
  assert.equal(apiState.previewRequests.length, 2)
  assert.equal(page.previewExpanded.value, true)

  page.form.interval_seconds = 900
  await nextTick()
  assert.equal(page.previewLoading.value, false)
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)

  apiState.previewRequests[1].deferred.resolve(previewResponse(['2026-10-21T09:00:00.000Z']))
  await manualPreview
  await flushVue()
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)
})

test('ignores old preview responses after a rule switch, close, or reopening the same rule', async () => {
  const switchingHarness = createHarness({
    previewHandler: request => request.deferred.promise,
  })
  switchingHarness.page.showDialog('create')
  fillForm(switchingHarness.page.form)
  await nextTick()
  const switchingRequest = switchingHarness.apiState.previewRequests[0]
  switchingHarness.page.form.schedule_mode = 'once'
  await nextTick()
  switchingRequest.deferred.resolve(previewResponse(['2026-10-20T09:00:00.000Z']))
  await flushVue()
  assert.deepEqual(snapshot(switchingHarness.page.previewTimes.value), [])
  assert.equal(switchingHarness.page.previewMatches.value, false)
  assert.equal(switchingHarness.page.previewExpanded.value, false)

  const closeHarness = createHarness({
    previewHandler: request => request.deferred.promise,
  })
  closeHarness.page.showDialog('create')
  fillForm(closeHarness.page.form)
  await nextTick()
  const closeRequest = closeHarness.apiState.previewRequests[0]
  closeHarness.page.closeDialog()
  assert.equal(closeHarness.page.dialogVisible.value, false)
  await closeHarness.page.togglePreview()
  assert.equal(closeHarness.apiState.previewRequests.length, 1)
  assert.equal(closeHarness.page.previewExpanded.value, false)
  closeRequest.deferred.resolve(previewResponse(['2026-10-21T09:00:00.000Z']))
  await flushVue()
  assert.deepEqual(snapshot(closeHarness.page.previewTimes.value), [])
  assert.equal(closeHarness.page.previewMatches.value, false)
  assert.equal(closeHarness.page.previewExpanded.value, false)

  const reopenHarness = createHarness({
    previewHandler: request => request.deferred.promise,
  })
  reopenHarness.page.showDialog('create')
  fillForm(reopenHarness.page.form)
  await nextTick()
  const oldRequest = reopenHarness.apiState.previewRequests[0]
  reopenHarness.page.closeDialog()
  reopenHarness.page.showDialog('create')
  fillForm(reopenHarness.page.form)
  await nextTick()
  assert.equal(reopenHarness.apiState.previewRequests.length, 2)
  const newRequest = reopenHarness.apiState.previewRequests[1]

  oldRequest.deferred.resolve(previewResponse(['2026-10-22T09:00:00.000Z']))
  await flushVue()
  assert.deepEqual(snapshot(reopenHarness.page.previewTimes.value), [])
  assert.equal(reopenHarness.page.previewMatches.value, false)
  assert.equal(reopenHarness.page.previewExpanded.value, false)

  newRequest.deferred.resolve(previewResponse(['2026-10-23T09:00:00.000Z']))
  await flushVue()
  assert.deepEqual(snapshot(reopenHarness.page.previewTimes.value), ['2026-10-23T09:00:00.000Z'])
  assert.equal(reopenHarness.page.previewMatches.value, true)
  assert.equal(reopenHarness.page.previewExpanded.value, false)
})

test('automatic preview failures remain visible, block save, and can be retried by the button', async () => {
  const { apiState, page } = createHarness({
    previewHandler: request => request.deferred.promise,
  })
  page.showDialog('create')
  fillForm(page.form)
  await nextTick()
  const failedRequest = apiState.previewRequests[0]
  const previewFailure = new Error('preview failed')
  failedRequest.deferred.reject(previewFailure)
  await flushVue()

  assert.equal(page.previewLoading.value, false)
  assert.equal(page.previewError.value, previewFailure.message)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)
  assert.equal(page.previewExpanded.value, false)

  await page.submitForm()
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 0)
  assert.equal(page.scheduleValidationError.value, 'scheduledTasks.preview_required')

  const retry = page.togglePreview()
  assert.equal(apiState.previewRequests.length, 2)
  assert.equal(page.previewExpanded.value, true)
  const retryRequest = apiState.previewRequests[1]
  retryRequest.deferred.resolve(previewResponse(['2026-10-24T09:00:00.000Z']))
  await retry
  await flushVue()

  assert.equal(page.previewError.value, '')
  assert.deepEqual(snapshot(page.previewTimes.value), ['2026-10-24T09:00:00.000Z'])
  assert.equal(page.previewMatches.value, true)
  assert.equal(page.previewExpanded.value, true)

  await page.togglePreview()
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), ['2026-10-24T09:00:00.000Z'])
  assert.equal(page.previewMatches.value, true)
  assert.equal(apiState.previewRequests.length, 2)
})

test('manual preview failures retain the error and block save before a successful retry', async () => {
  let previewCallCount = 0
  const harness = createHarness({
    previewHandler: request => {
      previewCallCount += 1
      return previewCallCount === 1
        ? previewResponse(['2026-10-20T09:00:00.000Z'])
        : request.deferred.promise
    },
  })
  const { apiState, page } = harness
  page.showDialog('create')
  fillForm(page.form)
  await flushVue()

  page.form.interval_seconds = 600
  await nextTick()
  const failedPreview = page.togglePreview()
  assert.equal(apiState.previewRequests.length, 2)
  assert.equal(page.previewExpanded.value, true)
  const previewFailure = new Error('manual preview failed')
  apiState.previewRequests[1].deferred.reject(previewFailure)
  await failedPreview
  await flushVue()

  assert.equal(page.previewError.value, previewFailure.message)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)
  assert.equal(page.previewExpanded.value, true)
  await page.submitForm()
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 0)
  assert.equal(page.scheduleValidationError.value, 'scheduledTasks.preview_required')

  const retry = page.togglePreview()
  assert.equal(apiState.previewRequests.length, 3)
  assert.equal(page.previewExpanded.value, true)
  apiState.previewRequests[2].deferred.resolve(previewResponse(['2026-10-26T09:00:00.000Z']))
  await retry
  await flushVue()

  assert.equal(page.previewError.value, '')
  assert.deepEqual(snapshot(page.previewTimes.value), ['2026-10-26T09:00:00.000Z'])
  assert.equal(page.previewMatches.value, true)
  assert.equal(page.previewExpanded.value, true)
  await page.togglePreview()
  assert.equal(page.previewExpanded.value, false)
})

test('invalid time rules show validation errors without requesting or retaining old preview times', async () => {
  const { apiState, page } = createHarness()
  page.showDialog('create')
  fillForm(page.form)
  await flushVue()
  await page.togglePreview()
  const previewRequestCount = apiState.previewRequests.length

  page.form.schedule_mode = 'daily'
  page.form.time_of_day = '25:00'
  await nextTick()
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)

  await page.togglePreview()
  assert.equal(apiState.previewRequests.length, previewRequestCount)
  assert.equal(page.scheduleValidationError.value, 'scheduledTasks.invalid_time')
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)
  assert.equal(page.previewExpanded.value, false)
})

test('name and message changes keep a valid schedule preview', async () => {
  const { apiState, page } = createHarness()
  page.showDialog('create')
  fillForm(page.form)
  await flushPreview(page)

  const originalKey = page.scheduleKey.value
  const originalTimes = snapshot(page.previewTimes.value)
  const previewRequestCount = apiState.previewRequests.length
  page.form.name = 'Renamed review'
  page.form.message = 'Updated instructions'
  await nextTick()

  assert.equal(apiState.previewRequests.length, previewRequestCount)
  assert.equal(page.scheduleKey.value, originalKey)
  assert.deepEqual(snapshot(page.previewTimes.value), originalTimes)
  assert.equal(page.previewMatches.value, true)
})
