import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { renderToString } from 'vue/server-renderer'
import {
  buildSchedulePayload,
  describeSchedule,
  getBrowserTimeZone,
  getScheduleDefaults,
  readScheduleForm,
} from '../src/utils/scheduledTaskSchedule.js'
import { loadUseConfirmDialog } from './confirmDialogHarness.js'

const viewSource = readFileSync(new URL('../src/views/ScheduledTasksView.vue', import.meta.url), 'utf8')
const scriptMatch = viewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!scriptMatch) throw new Error('ScheduledTasksView.vue script setup was not found')

const stripSingleLineImports = sourceText => sourceText
  .split(/\r?\n/)
  .filter(line => !/^\s*import\b/.test(line))
  .join('\n')

const snapshot = value => JSON.parse(JSON.stringify(value))

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const previewResponse = (nextRunTimes = [
  '2026-10-20T09:00:00.000Z',
  '2026-10-21T09:00:00.000Z',
  '2026-10-22T09:00:00.000Z',
]) => ({
  data: {
    data: { next_run_times: nextRunTimes },
  },
})

const flushVue = async () => {
  for (let index = 0; index < 4; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const flushPreview = async page => {
  await nextTick()
  await page.previewSchedule()
  await flushVue()
}

const fillForm = (form, overrides = {}) => Object.assign(form, {
  name: 'Scheduled review',
  session_id: 'session-1',
  message: 'Review the latest changes',
  interval_seconds: 300,
  schedule_mode: 'interval',
  status: 'enabled',
  ...overrides,
})

const createHarness = (options = {}) => {
  const messages = []
  const mountedCallbacks = []
  const locale = ref(options.locale ?? 'en')
  const apiState = {
    listRequests: [],
    createRequests: [],
    updateRequests: [],
    deleteRequests: [],
    previewRequests: [],
  }
  const ElMessage = Object.fromEntries(['warning', 'error', 'success'].map(type => [
    type,
    value => messages.push({ type, value }),
  ]))
  const confirmationCalls = []
  const pendingConfirmations = []
  const ElMessageBox = {
    confirm: (message, title, options) => new Promise((resolve, reject) => {
      confirmationCalls.push({ message, title, options })
      pendingConfirmations.push({ resolve, reject })
    }),
  }
  const actualUseConfirmDialog = loadUseConfirmDialog({
    t: key => key,
    ElMessageBox,
  })
  const scheduledTaskApi = {
    list: async params => {
      apiState.listRequests.push({ ...params })
      return { data: { data: { items: [], total: 0 } } }
    },
    create: async payload => {
      const request = { payload }
      apiState.createRequests.push(request)
      return options.createHandler ? options.createHandler(request) : { data: { data: {} } }
    },
    update: async (id, payload) => {
      const request = { id, payload }
      apiState.updateRequests.push(request)
      return options.updateHandler
        ? options.updateHandler(request)
        : { data: { data: { status: payload.status, next_run_at: null } } }
    },
    preview: async payload => {
      const request = { payload, deferred: createDeferred() }
      apiState.previewRequests.push(request)
      return options.previewHandler
        ? options.previewHandler(request)
        : (options.previewResponse || previewResponse())
    },
    delete: async id => {
      const request = { id }
      apiState.deleteRequests.push(request)
      return options.deleteHandler
        ? options.deleteHandler(request)
        : { data: { data: {} } }
    },
  }
  const context = vm.createContext({
    ref,
    reactive,
    computed,
    nextTick,
    watch,
    buildSchedulePayload,
    describeSchedule,
    getScheduleDefaults,
    readScheduleForm,
    useI18n: () => ({ t: key => key, locale }),
    ElMessage,
    useConfirmDialog: actualUseConfirmDialog,
    chatApi: { sessionsList: async () => ({ data: { data: [] } }) },
    scheduledTaskApi,
    onMounted: callback => mountedCallbacks.push(callback),
  })

  vm.runInContext(`${stripSingleLineImports(scriptMatch[1])}
  globalThis.__page = {
    form,
    showDialog,
    buildPayload,
    submitForm,
    toggleStatus,
    deleteTask,
    previewSchedule,
    togglePreview,
    previewTimes,
    previewLoading,
    previewMatches,
    previewError,
    scheduleValidationError,
    previewExpanded,
    scheduleKey,
    closeDialog,
    formatTime,
    dialogVisible,
    submitting
  }
`, context, { filename: 'ScheduledTasksView.vue?script-setup' })

  return {
    apiState,
    messages,
    mountedCallbacks,
    confirmationCalls,
    locale,
    page: context.__page,
    settleConfirmation: (action = 'confirm') => {
      const pending = pendingConfirmations.shift()
      assert.ok(pending, 'a confirmation must be pending')
      if (action === 'confirm') pending.resolve()
      else pending.reject(action)
    },
  }
}

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

test('deleteTask waits for confirmation, deletes the correct id, and refreshes on success', async () => {
  const { apiState, messages, confirmationCalls, page, settleConfirmation } = createHarness()
  const deletion = page.deleteTask({ id: 12, name: 'Task to delete' })

  assert.deepEqual(apiState.deleteRequests, [])
  assert.deepEqual(apiState.listRequests, [])
  assert.equal(confirmationCalls.length, 1)

  const confirmation = confirmationCalls[0]
  const html = await renderToString(confirmation.message)
  assert.match(html, /<p class="confirm-dialog-content__message">scheduledTasks\.delete_confirm<\/p>/)
  assert.equal(confirmation.title, 'common.warning')
  assert.equal(confirmation.options.confirmButtonText, 'common.confirm')
  assert.equal(confirmation.options.cancelButtonText, 'common.cancel')
  assert.equal(confirmation.options.customClass, 'app-confirm-dialog')

  settleConfirmation()
  await deletion

  assert.deepEqual(apiState.deleteRequests, [{ id: 12 }])
  assert.deepEqual(apiState.listRequests, [{ page: 1, size: 20 }])
  assert.deepEqual(messages, [{ type: 'success', value: 'scheduledTasks.delete_success' }])
})

test('cancelled or closed delete confirmations have no side effects', async () => {
  for (const action of ['cancel', 'close']) {
    const { apiState, messages, page, settleConfirmation } = createHarness()
    const deletion = page.deleteTask({ id: 13, name: `Task ${action}` })

    assert.deepEqual(apiState.deleteRequests, [])
    settleConfirmation(action)
    await deletion

    assert.deepEqual(apiState.deleteRequests, [])
    assert.deepEqual(apiState.listRequests, [])
    assert.deepEqual(messages, [])
  }
})

test('confirmation and delete API failures show errors without refreshing', async () => {
  const confirmationFailure = new Error('confirmation failed')
  const confirmationHarness = createHarness()
  const failedConfirmation = confirmationHarness.page.deleteTask({ id: 14 })
  confirmationHarness.settleConfirmation(confirmationFailure)
  await failedConfirmation

  assert.deepEqual(confirmationHarness.apiState.deleteRequests, [])
  assert.deepEqual(confirmationHarness.apiState.listRequests, [])
  assert.deepEqual(confirmationHarness.messages, [
    { type: 'error', value: confirmationFailure.message },
  ])

  const deleteFailure = new Error('delete failed')
  const deleteHarness = createHarness({
    deleteHandler: async () => { throw deleteFailure },
  })
  const failedDelete = deleteHarness.page.deleteTask({ id: 15 })
  deleteHarness.settleConfirmation()
  await failedDelete

  assert.deepEqual(deleteHarness.apiState.deleteRequests, [{ id: 15 }])
  assert.deepEqual(deleteHarness.apiState.listRequests, [])
  assert.deepEqual(deleteHarness.messages, [
    { type: 'error', value: deleteFailure.message },
  ])
})

test('toggleStatus only applies server status and next_run_at, preserving username; failures leave the row unchanged', async () => {
  for (const { id, initialStatus, expectedStatus, serverNextRunAt } of [
    {
      id: 6,
      initialStatus: 'enabled',
      expectedStatus: 'disabled',
      serverNextRunAt: '2026-10-19T16:00:00.000Z',
    },
    {
      id: 8,
      initialStatus: 'disabled',
      expectedStatus: 'enabled',
      serverNextRunAt: '2026-10-19T17:00:00.000Z',
    },
  ]) {
    const successHarness = createHarness({
      updateHandler: async () => ({
        data: { data: { status: expectedStatus, next_run_at: serverNextRunAt, username: null } },
      }),
    })
    const row = {
      id,
      name: 'Owned task',
      status: initialStatus,
      run_once: true,
      next_run_at: '2026-10-19T15:00:00.000Z',
      username: 'list-owner',
    }
    await successHarness.page.toggleStatus(row)

    assert.deepEqual(successHarness.apiState.updateRequests.map(({ id: requestId, payload }) => ({
      id: requestId,
      payload: snapshot(payload),
    })), [{ id, payload: { status: expectedStatus } }])
    assert.deepEqual(snapshot(row), {
      id,
      name: 'Owned task',
      status: expectedStatus,
      run_once: true,
      next_run_at: serverNextRunAt,
      username: 'list-owner',
    })
  }

  const failureHarness = createHarness({
    updateHandler: async () => { throw new Error('status update failed') },
  })
  const failedRow = {
    id: 7,
    status: 'enabled',
    next_run_at: 'unchanged-next-run',
    username: 'unchanged-owner',
  }
  const failedRowBefore = snapshot(failedRow)
  await failureHarness.page.toggleStatus(failedRow)

  assert.deepEqual(snapshot(failedRow), failedRowBefore)
  assert.equal(failureHarness.messages.at(-1).type, 'error')
})
