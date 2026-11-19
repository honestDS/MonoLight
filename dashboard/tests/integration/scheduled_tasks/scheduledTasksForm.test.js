import assert from 'node:assert/strict'
import test from 'node:test'
import { nextTick } from 'vue'
import { getBrowserTimeZone } from '../../../src/utils/scheduledTaskSchedule.js'
import {
  createHarness,
  fillForm,
  flushPreview,
  flushVue,
  previewResponse,
  snapshot,
} from './scheduledTasksHarness.js'

test('defaults and reset use repeating mode; edit loads one-time and legacy rows as repeating', () => {
  const { page } = createHarness()

  assert.equal(page.form.schedule_mode, 'interval')
  assert.equal(page.form.run_once, false)
  page.form.schedule_mode = 'once'
  page.showDialog('create')
  assert.equal(page.form.schedule_mode, 'interval')
  assert.equal(page.form.run_once, false)
  assert.equal(page.form.interval_seconds, 300)

  page.showDialog('edit', {
    id: 1,
    uid: 'owner-1',
    name: 'One-time task',
    session_id: 'session-1',
    interval_seconds: 60,
    run_once: true,
    status: 'disabled',
    message: 'Run once',
  })
  assert.equal(page.form.schedule_mode, 'once')
  assert.equal(page.form.run_once, true)

  page.showDialog('create')
  assert.equal(page.form.schedule_mode, 'interval')
  assert.equal(page.form.run_once, false)

  page.showDialog('edit', {
    id: 2,
    uid: 'owner-2',
    name: 'Legacy task',
    session_id: 'session-2',
    interval_seconds: 120,
    status: 'enabled',
    message: 'Legacy message',
  })
  assert.equal(page.form.schedule_mode, 'interval')
  assert.equal(page.form.run_once, false)
})

test('calendar modes build exact daily, weekly, and monthly payloads', () => {
  const { page } = createHarness()
  page.showDialog('create')

  fillForm(page.form, {
    schedule_mode: 'daily',
    time_of_day: '07:05',
    timezone: 'UTC',
  })
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    cron_expression: '5 7 * * *',
    timezone: 'UTC',
    run_once: false,
  })

  fillForm(page.form, {
    schedule_mode: 'weekly',
    time_of_day: '08:30',
    weekdays: [5, 1, 5, 0],
    timezone: 'UTC',
  })
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    cron_expression: '30 8 * * 0,1,5',
    timezone: 'UTC',
    run_once: false,
  })

  fillForm(page.form, {
    schedule_mode: 'monthly',
    time_of_day: '09:10',
    month_date: '2024-02-29',
    month_last_day: false,
    timezone: 'UTC',
  })
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    cron_expression: '10 9 29 * *',
    timezone: 'UTC',
    run_once: false,
  })

  fillForm(page.form, {
    schedule_mode: 'monthly',
    time_of_day: '23:59',
    month_date: 'not-a-date',
    month_last_day: true,
    timezone: 'UTC',
  })
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    cron_expression: '59 23 L * *',
    timezone: 'UTC',
    run_once: false,
  })
})

test('yearly create previews, toggles, and saves the encoded rule', async () => {
  const nextRunTimes = [
    '2026-12-31T00:15:00.000Z',
    '2027-12-31T00:15:00.000Z',
    '2028-12-31T00:15:00.000Z',
  ]
  const { apiState, page } = createHarness({
    previewResponse: previewResponse(nextRunTimes),
  })
  page.showDialog('create')
  fillForm(page.form, {
    schedule_mode: 'yearly',
    year_date: '2000-12-31',
    time_of_day: '09:15',
    timezone: 'Asia/Tokyo',
  })
  await flushVue()

  assert.equal(apiState.previewRequests.length, 1)
  assert.deepEqual(snapshot(apiState.previewRequests[0].payload), {
    cron_expression: '15 9 31 12 *',
    timezone: 'Asia/Tokyo',
    run_once: false,
  })
  assert.deepEqual(snapshot(page.previewTimes.value), nextRunTimes)
  assert.equal(page.previewMatches.value, true)
  assert.equal(page.previewExpanded.value, false)

  await page.togglePreview()
  assert.equal(page.previewExpanded.value, true)
  await page.togglePreview()
  assert.equal(page.previewExpanded.value, false)
  assert.equal(apiState.previewRequests.length, 1)

  await page.submitForm()
  assert.equal(apiState.createRequests.length, 1)
  assert.equal(apiState.updateRequests.length, 0)
  assert.deepEqual(snapshot(apiState.createRequests[0].payload), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    cron_expression: '15 9 31 12 *',
    timezone: 'Asia/Tokyo',
    run_once: false,
  })
})

test('yearly edit backfills the form, invalidates stale previews, and updates the saved rule', async () => {
  const initialNextRunTimes = [
    '2028-02-29T00:00:00.000Z',
    '2032-02-29T00:00:00.000Z',
    '2036-02-29T00:00:00.000Z',
  ]
  const updatedNextRunTimes = [
    '2027-03-01T00:00:00.000Z',
    '2028-03-01T00:00:00.000Z',
    '2029-03-01T00:00:00.000Z',
  ]
  const { apiState, page } = createHarness({
    previewHandler: request => request.payload.cron_expression === '0 9 29 2 *'
      ? previewResponse(initialNextRunTimes)
      : previewResponse(updatedNextRunTimes),
  })
  page.showDialog('edit', {
    id: 42,
    uid: 'owner-42',
    name: 'Leap day task',
    session_id: 'session-42',
    cron_expression: '0 9 29 2 *',
    timezone: 'Asia/Tokyo',
    status: 'disabled',
    message: 'Run on leap day',
  })

  assert.equal(page.form.schedule_mode, 'yearly')
  assert.equal(page.form.year_date, '2000-02-29')
  assert.equal(page.form.time_of_day, '09:00')
  assert.equal(page.form.timezone, 'Asia/Tokyo')

  await flushVue()
  assert.equal(apiState.previewRequests.length, 1)
  assert.deepEqual(snapshot(apiState.previewRequests[0].payload), {
    cron_expression: '0 9 29 2 *',
    timezone: 'Asia/Tokyo',
    run_once: false,
  })
  assert.deepEqual(snapshot(page.previewTimes.value), initialNextRunTimes)
  assert.equal(page.previewMatches.value, true)

  await page.togglePreview()
  assert.equal(page.previewExpanded.value, true)
  page.form.year_date = '2000-03-01'
  await nextTick()
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)

  await page.submitForm()
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 0)
  assert.equal(page.scheduleValidationError.value, 'scheduledTasks.preview_required')

  await page.togglePreview()
  assert.equal(apiState.previewRequests.length, 2)
  assert.deepEqual(snapshot(apiState.previewRequests[1].payload), {
    cron_expression: '0 9 1 3 *',
    timezone: 'Asia/Tokyo',
    run_once: false,
  })
  assert.deepEqual(snapshot(page.previewTimes.value), updatedNextRunTimes)
  assert.equal(page.previewMatches.value, true)

  await page.submitForm()
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 1)
  assert.equal(apiState.updateRequests[0].id, 42)
  assert.deepEqual(snapshot(apiState.updateRequests[0].payload), {
    name: 'Leap day task',
    session_id: 'session-42',
    message: 'Run on leap day',
    cron_expression: '0 9 1 3 *',
    timezone: 'Asia/Tokyo',
    run_once: false,
    status: 'disabled',
  })
})

test('invalid yearly dates clear old preview results and block preview, create, and update requests', async () => {
  const { apiState, page } = createHarness({
    previewResponse: previewResponse([
      '2026-12-31T00:15:00.000Z',
      '2027-12-31T00:15:00.000Z',
      '2028-12-31T00:15:00.000Z',
    ]),
  })
  page.showDialog('create')
  fillForm(page.form, {
    schedule_mode: 'yearly',
    year_date: '2000-12-31',
    time_of_day: '09:15',
    timezone: 'Asia/Tokyo',
  })
  await flushVue()
  await page.togglePreview()
  const previewRequestCount = apiState.previewRequests.length
  assert.equal(page.previewMatches.value, true)

  page.form.year_date = '2026-02-29'
  await nextTick()
  assert.equal(page.previewExpanded.value, false)
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)

  await page.togglePreview()
  assert.equal(apiState.previewRequests.length, previewRequestCount)
  assert.equal(page.scheduleValidationError.value, 'scheduledTasks.invalid_year_date')
  assert.deepEqual(snapshot(page.previewTimes.value), [])
  assert.equal(page.previewMatches.value, false)

  await page.submitForm()
  assert.equal(apiState.previewRequests.length, previewRequestCount)
  assert.equal(apiState.createRequests.length, 0)
  assert.equal(apiState.updateRequests.length, 0)
  assert.equal(page.scheduleValidationError.value, 'scheduledTasks.invalid_year_date')
})

test('uses the real browser timezone default and preserves saved timezone and unknown cron in edit mode', () => {
  const { page } = createHarness()
  page.showDialog('create')
  assert.equal(page.form.timezone, getBrowserTimeZone())

  const rawCron = '15 6 * * 2#3'
  page.showDialog('edit', {
    id: 9,
    uid: 'owner-9',
    name: 'Unknown cron',
    session_id: 'session-9',
    cron_expression: rawCron,
    timezone: 'Asia/Tokyo',
    status: 'enabled',
    message: 'Keep raw cron',
  })

  assert.equal(page.form.schedule_mode, 'cron')
  assert.equal(page.form.cron_expression, rawCron)
  assert.equal(page.form.timezone, 'Asia/Tokyo')
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Unknown cron',
    session_id: 'session-9',
    message: 'Keep raw cron',
    cron_expression: rawCron,
    timezone: 'Asia/Tokyo',
    run_once: false,
    status: 'enabled',
  })
})

test('formats the same instant by timezone and current locale and returns invalid dates unchanged', () => {
  const offsetTime = '2026-11-02T09:00:00+08:00'
  const utcTime = '2026-11-02T01:00:00Z'
  assert.equal(new Date(offsetTime).getTime(), new Date(utcTime).getTime())

  const expected = (locale, timeStr, timeZone) => new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone,
  }).format(new Date(timeStr))

  for (const locale of ['en', 'zh']) {
    const { page } = createHarness({ locale })
    const offsetUtc = page.formatTime(offsetTime, 'UTC')
    const utcUtc = page.formatTime(utcTime, 'UTC')
    const offsetTokyo = page.formatTime(offsetTime, 'Asia/Tokyo')
    const utcTokyo = page.formatTime(utcTime, 'Asia/Tokyo')

    assert.equal(offsetUtc, expected(locale, offsetTime, 'UTC'))
    assert.equal(utcUtc, expected(locale, utcTime, 'UTC'))
    assert.equal(offsetTokyo, expected(locale, offsetTime, 'Asia/Tokyo'))
    assert.equal(utcTokyo, expected(locale, utcTime, 'Asia/Tokyo'))
    assert.equal(offsetUtc, utcUtc)
    assert.equal(offsetTokyo, utcTokyo)
    assert.notEqual(offsetUtc, offsetTokyo)
    assert.equal(page.formatTime('not-a-date', 'UTC'), 'not-a-date')
  }
})

test('buildPayload preserves run_once and interval seconds and includes edit status', () => {
  const { page } = createHarness()

  page.showDialog('create')
  fillForm(page.form, { schedule_mode: 'once', interval_seconds: 125 })
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    interval_seconds: 125,
    run_once: true,
  })

  page.showDialog('edit', {
    id: 3,
    uid: 'owner-3',
    name: 'Edited task',
    session_id: 'session-3',
    interval_seconds: 600,
    run_once: false,
    status: 'disabled',
    message: 'Edited message',
  })
  page.form.interval_seconds = 1200
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Edited task',
    session_id: 'session-3',
    message: 'Edited message',
    interval_seconds: 1200,
    run_once: false,
    status: 'disabled',
  })
})

test('interval validation accepts 60 and rejects non-integer or sub-minimum values without requests', async () => {
  const { apiState, messages, page } = createHarness()
  page.showDialog('create')
  fillForm(page.form, { interval_seconds: 60 })
  assert.equal(page.buildPayload().interval_seconds, 60)

  for (const interval of [null, undefined, NaN, Infinity, 0, 59, 60.5, '60']) {
    page.form.interval_seconds = interval
    await page.submitForm()
    assert.equal(apiState.createRequests.length, 0)
    assert.equal(apiState.updateRequests.length, 0)
    assert.deepEqual(messages.at(-1), {
      type: 'warning',
      value: 'scheduledTasks.invalid_interval',
    })
  }
})

test('one-time create and edit call their respective APIs with run_once true', async () => {
  const createHarnessResult = createHarness()
  createHarnessResult.page.showDialog('create')
  fillForm(createHarnessResult.page.form, { schedule_mode: 'once', interval_seconds: 3600 })
  await flushPreview(createHarnessResult.page)
  assert.equal(createHarnessResult.page.previewTimes.value.length, 1)
  await createHarnessResult.page.submitForm()

  assert.equal(createHarnessResult.apiState.createRequests.length, 1)
  assert.equal(createHarnessResult.apiState.updateRequests.length, 0)
  assert.deepEqual(snapshot(createHarnessResult.apiState.createRequests[0].payload), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    interval_seconds: 3600,
    run_once: true,
  })
  assert.equal(createHarnessResult.page.dialogVisible.value, false)

  const editHarnessResult = createHarness()
  editHarnessResult.page.showDialog('edit', {
    id: 4,
    uid: 'owner-4',
    name: 'One-time edit',
    session_id: 'session-4',
    interval_seconds: 90,
    run_once: true,
    status: 'disabled',
    message: 'Run once after editing',
  })
  await flushPreview(editHarnessResult.page)
  await editHarnessResult.page.submitForm()

  assert.equal(editHarnessResult.apiState.createRequests.length, 0)
  assert.equal(editHarnessResult.apiState.updateRequests.length, 1)
  assert.equal(editHarnessResult.apiState.updateRequests[0].id, 4)
  assert.deepEqual(snapshot(editHarnessResult.apiState.updateRequests[0].payload), {
    name: 'One-time edit',
    session_id: 'session-4',
    message: 'Run once after editing',
    interval_seconds: 90,
    run_once: true,
    status: 'disabled',
  })
  assert.equal(editHarnessResult.page.dialogVisible.value, false)
})

test('failed create and edit retain the form and dialog and release submitting state', async () => {
  for (const type of ['create', 'edit']) {
    const { apiState, messages, page } = createHarness({
      createHandler: async () => { throw new Error('create failed') },
      updateHandler: async () => { throw new Error('edit failed') },
    })
    if (type === 'create') {
      page.showDialog('create')
      fillForm(page.form, { schedule_mode: 'once', interval_seconds: 600 })
      await flushPreview(page)
    } else {
      page.showDialog('edit', {
        id: 5,
        uid: 'owner-5',
        name: 'Edit failure',
        session_id: 'session-5',
        interval_seconds: 600,
        run_once: true,
        status: 'enabled',
        message: 'Keep this form',
      })
      await flushPreview(page)
    }
    const formBeforeSubmit = snapshot(page.form)

    await page.submitForm()

    assert.deepEqual(snapshot(page.form), formBeforeSubmit)
    assert.equal(page.dialogVisible.value, true)
    assert.equal(page.submitting.value, false)
    assert.equal(messages.at(-1).type, 'error')
    assert.equal(apiState.createRequests.length, type === 'create' ? 1 : 0)
    assert.equal(apiState.updateRequests.length, type === 'edit' ? 1 : 0)
  }
})
