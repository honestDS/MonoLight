import assert from 'node:assert/strict'
import test from 'node:test'
import enScheduledTasks from '../src/i18n/locales/en/scheduledTasks.js'
import zhScheduledTasks from '../src/i18n/locales/zh/scheduledTasks.js'
import {
  buildSchedulePayload,
  describeSchedule,
  getBrowserTimeZone,
  getScheduleDefaults,
  readScheduleForm,
} from '../src/utils/scheduledTaskSchedule.js'

const assertScheduleError = (operation, key) => {
  assert.throws(operation, { message: `scheduledTasks.${key}` })
}

const createTranslator = messages => (key, values = {}) => {
  const message = messages[key.slice('scheduledTasks.'.length)]
  return Object.entries(values).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    message,
  )
}

test('defaults read the browser timezone and return independent values', () => {
  const originalDateTimeFormat = Intl.DateTimeFormat
  const browserTimeZone = 'Pacific/Auckland'

  try {
    Intl.DateTimeFormat = () => ({
      resolvedOptions: () => ({ timeZone: browserTimeZone }),
    })

    const first = getScheduleDefaults()
    const second = getScheduleDefaults()

    assert.equal(getBrowserTimeZone(), browserTimeZone)
    assert.equal(first.timezone, browserTimeZone)
    assert.equal(second.timezone, browserTimeZone)
    assert.notStrictEqual(first, second)
    assert.notStrictEqual(first.weekdays, second.weekdays)
    first.weekdays.push(5)
    assert.deepEqual(second.weekdays, [1])

    const browserForm = readScheduleForm({})
    assert.equal(browserForm.timezone, browserTimeZone)

    const savedTaskForm = readScheduleForm({
      timezone: 'Asia/Tokyo',
      interval_seconds: 600,
    })
    assert.equal(savedTaskForm.timezone, 'Asia/Tokyo')
  } finally {
    Intl.DateTimeFormat = originalDateTimeFormat
  }

  assert.strictEqual(Intl.DateTimeFormat, originalDateTimeFormat)
})

test('daily schedules write a normalized cron expression and read back without stale fields', () => {
  const form = {
    schedule_mode: 'daily',
    time_of_day: '07:05',
    timezone: 'UTC',
    interval_seconds: 900,
    run_once: true,
    cron_expression: '15 6 * * 2#3',
  }
  const before = { ...form }

  const payload = buildSchedulePayload(form)

  assert.deepEqual(payload, {
    cron_expression: '5 7 * * *',
    timezone: 'UTC',
    run_once: false,
  })
  assert.deepEqual(form, before)

  const read = readScheduleForm(payload)
  assert.equal(read.schedule_mode, 'daily')
  assert.equal(read.time_of_day, '07:05')
  assert.equal(read.timezone, 'UTC')
  assert.equal(read.run_once, false)
})

test('weekly schedules deduplicate and sort days while preserving the input', () => {
  const weekdays = [5, 1, 5, 0]
  const form = {
    schedule_mode: 'weekly',
    time_of_day: '08:30',
    weekdays,
    timezone: 'UTC',
    interval_seconds: 1200,
    run_once: true,
    cron_expression: '15 6 * * 2#3',
  }

  const payload = buildSchedulePayload(form)

  assert.deepEqual(payload, {
    cron_expression: '30 8 * * 0,1,5',
    timezone: 'UTC',
    run_once: false,
  })
  assert.deepEqual(weekdays, [5, 1, 5, 0])

  const read = readScheduleForm({
    cron_expression: '30 8 * * 5,1,5,0',
    timezone: 'UTC',
  })
  assert.equal(read.schedule_mode, 'weekly')
  assert.deepEqual(read.weekdays, [0, 1, 5])
  assert.equal(read.time_of_day, '08:30')
})

test('monthly schedules preserve a valid day number and support month end', () => {
  const dayPayload = buildSchedulePayload({
    schedule_mode: 'monthly',
    time_of_day: '09:10',
    month_date: '2024-02-29',
    month_last_day: false,
    timezone: 'UTC',
  })
  assert.deepEqual(dayPayload, {
    cron_expression: '10 9 29 * *',
    timezone: 'UTC',
    run_once: false,
  })

  const dayForm = readScheduleForm(dayPayload)
  assert.equal(dayForm.schedule_mode, 'monthly')
  assert.equal(dayForm.time_of_day, '09:10')
  assert.equal(dayForm.month_date, '2000-01-29')
  assert.equal(dayForm.month_last_day, false)

  const lastDayPayload = buildSchedulePayload({
    schedule_mode: 'monthly',
    time_of_day: '23:59',
    month_date: 'not-a-date',
    month_last_day: true,
    timezone: 'UTC',
  })
  assert.deepEqual(lastDayPayload, {
    cron_expression: '59 23 L * *',
    timezone: 'UTC',
    run_once: false,
  })

  const lowerCaseLastDay = readScheduleForm({
    cron_expression: '59 23 l * *',
    timezone: 'UTC',
  })
  assert.equal(lowerCaseLastDay.schedule_mode, 'monthly')
  assert.equal(lowerCaseLastDay.time_of_day, '23:59')
  assert.equal(lowerCaseLastDay.month_last_day, true)
  assert.equal(buildSchedulePayload(lowerCaseLastDay).cron_expression, '59 23 L * *')
})

test('yearly schedules round-trip valid dates without mutating inputs', () => {
  const cases = [
    {
      yearDate: '2024-06-15',
      normalizedDate: '2000-06-15',
      timeOfDay: '09:10',
      cronExpression: '10 9 15 6 *',
    },
    {
      yearDate: '2024-12-31',
      normalizedDate: '2000-12-31',
      timeOfDay: '23:59',
      cronExpression: '59 23 31 12 *',
    },
    {
      yearDate: '2024-02-29',
      normalizedDate: '2000-02-29',
      timeOfDay: '00:05',
      cronExpression: '5 0 29 2 *',
    },
  ]

  for (const { yearDate, normalizedDate, timeOfDay, cronExpression } of cases) {
    const form = {
      schedule_mode: 'yearly',
      time_of_day: timeOfDay,
      year_date: yearDate,
      month_date: '2024-01-15',
      timezone: 'UTC',
      interval_seconds: 900,
      run_once: true,
      cron_expression: '15 6 * * 2#3',
    }
    const formBefore = { ...form }

    const payload = buildSchedulePayload(form)

    assert.deepEqual(payload, {
      cron_expression: cronExpression,
      timezone: 'UTC',
      run_once: false,
    })
    assert.deepEqual(form, formBefore)

    const payloadBefore = { ...payload }
    const read = readScheduleForm(payload)
    assert.deepEqual(payload, payloadBefore)
    assert.equal(read.schedule_mode, 'yearly')
    assert.equal(read.time_of_day, timeOfDay)
    assert.equal(read.year_date, normalizedDate)
    assert.equal(read.month_date, '2000-01-01')
    assert.deepEqual(buildSchedulePayload(read), payload)
  }
})

test('arbitrary cron expressions keep their complete raw value through read and write', () => {
  const rawCron = '15 6 * * 2#3'
  const form = readScheduleForm({
    cron_expression: rawCron,
    timezone: 'Asia/Tokyo',
    run_once: true,
    interval_seconds: 900,
  })

  assert.equal(form.schedule_mode, 'cron')
  assert.equal(form.cron_expression, rawCron)
  assert.equal(form.timezone, 'Asia/Tokyo')
  assert.equal(form.run_once, false)

  const payload = buildSchedulePayload(form)
  assert.deepEqual(payload, {
    cron_expression: rawCron,
    timezone: 'Asia/Tokyo',
    run_once: false,
  })

  const reread = readScheduleForm(payload)
  assert.equal(reread.schedule_mode, 'cron')
  assert.equal(reread.cron_expression, rawCron)
  assert.equal(reread.timezone, 'Asia/Tokyo')
})

test('complex yearly-looking cron expressions remain raw and complete', () => {
  for (const rawCron of [
    '5 7 15 1,2 *',
    '5 7 15 1-3 *',
    '5 7 15 1 2',
    '5 7 31 4 *',
  ]) {
    const form = readScheduleForm({
      cron_expression: rawCron,
      timezone: 'UTC',
      run_once: true,
    })

    assert.equal(form.schedule_mode, 'cron')
    assert.equal(form.cron_expression, rawCron)
    assert.equal(form.run_once, false)
    assert.deepEqual(buildSchedulePayload(form), {
      cron_expression: rawCron,
      timezone: 'UTC',
      run_once: false,
    })
  }
})

test('changing modes does not leak interval, cron, or run_once values', () => {
  const form = {
    schedule_mode: 'interval',
    interval_seconds: 600,
    run_once: true,
    cron_expression: '15 6 * * 2#3',
    timezone: 'UTC',
    time_of_day: '08:30',
    weekdays: [1, 3],
    month_date: '2024-01-15',
    year_date: '2024-12-31',
    month_last_day: false,
  }

  assert.deepEqual(buildSchedulePayload(form), {
    interval_seconds: 600,
    run_once: false,
  })

  form.schedule_mode = 'once'
  assert.deepEqual(buildSchedulePayload(form), {
    interval_seconds: 600,
    run_once: true,
  })

  form.schedule_mode = 'daily'
  assert.deepEqual(buildSchedulePayload(form), {
    cron_expression: '30 8 * * *',
    timezone: 'UTC',
    run_once: false,
  })

  form.schedule_mode = 'yearly'
  assert.deepEqual(buildSchedulePayload(form), {
    cron_expression: '30 8 31 12 *',
    timezone: 'UTC',
    run_once: false,
  })

  form.schedule_mode = 'monthly'
  assert.deepEqual(buildSchedulePayload(form), {
    cron_expression: '30 8 15 * *',
    timezone: 'UTC',
    run_once: false,
  })

  form.schedule_mode = 'cron'
  assert.deepEqual(buildSchedulePayload(form), {
    cron_expression: '15 6 * * 2#3',
    timezone: 'UTC',
    run_once: false,
  })

  const once = readScheduleForm({ interval_seconds: 60, run_once: true })
  assert.equal(once.schedule_mode, 'once')
  assert.equal(once.run_once, true)

  const interval = readScheduleForm({ interval_seconds: 60, run_once: false })
  assert.equal(interval.schedule_mode, 'interval')
  assert.equal(interval.run_once, false)

  const cron = readScheduleForm({
    cron_expression: '15 6 * * 2#3',
    interval_seconds: 999,
    run_once: true,
    timezone: 'UTC',
  })
  assert.equal(cron.schedule_mode, 'cron')
  assert.equal(cron.interval_seconds, 300)
  assert.equal(cron.run_once, false)
})

test('interval and once schedules accept the minimum delay and reject invalid values', () => {
  for (const scheduleMode of ['interval', 'once']) {
    assert.deepEqual(
      buildSchedulePayload({ schedule_mode: scheduleMode, interval_seconds: 60 }),
      {
        interval_seconds: 60,
        run_once: scheduleMode === 'once',
      },
    )

    for (const intervalSeconds of [null, undefined, NaN, Infinity, -1, 0, 59, 60.5, '60']) {
      assertScheduleError(
        () => buildSchedulePayload({ schedule_mode: scheduleMode, interval_seconds: intervalSeconds }),
        'invalid_interval',
      )
    }
  }
})

test('daily schedules validate HH:mm boundaries', () => {
  for (const timeOfDay of ['00:00', '23:59']) {
    assert.doesNotThrow(() => buildSchedulePayload({
      schedule_mode: 'daily',
      time_of_day: timeOfDay,
      timezone: 'UTC',
    }))
  }

  for (const timeOfDay of [
    null,
    undefined,
    '',
    '9:00',
    '09:0',
    '24:00',
    '23:60',
    '1:1',
    'aa:00',
  ]) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'daily',
        time_of_day: timeOfDay,
        timezone: 'UTC',
      }),
      'invalid_time',
    )
  }
})

test('weekly schedules reject empty or out-of-range weekday selections', () => {
  for (const weekdays of [null, undefined, [], [7], [-1], [1.5], ['1'], [NaN], [0, 7]]) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'weekly',
        time_of_day: '09:00',
        weekdays,
        timezone: 'UTC',
      }),
      'invalid_weekdays',
    )
  }
})

test('monthly schedules perform real calendar validation for the selected date', () => {
  for (const monthDate of [
    null,
    undefined,
    '',
    '2023-02-29',
    '2024-02-30',
    '2024-04-31',
    '2024-13-01',
    '0000-01-01',
    '2024-1-01',
    '2024-01-00',
    'not-a-date',
  ]) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'monthly',
        time_of_day: '09:00',
        month_date: monthDate,
        month_last_day: false,
        timezone: 'UTC',
      }),
      'invalid_month_day',
    )
  }
})

test('yearly schedules perform real calendar validation for the selected date', () => {
  for (const yearDate of [
    null,
    undefined,
    '',
    '2023-02-29',
    '2024-02-30',
    '2024-04-31',
    '2024-13-01',
    '0000-01-01',
    '2024-1-01',
    '2024-01-00',
    'not-a-date',
  ]) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'yearly',
        time_of_day: '09:00',
        year_date: yearDate,
        timezone: 'UTC',
      }),
      'invalid_year_date',
    )
  }
})

test('yearly schedules reuse time and time zone validation', () => {
  for (const timeOfDay of [null, undefined, '24:00']) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'yearly',
        time_of_day: timeOfDay,
        year_date: '2024-02-29',
        timezone: 'UTC',
      }),
      'invalid_time',
    )
  }

  for (const timezone of [null, undefined, 'Mars/Olympus']) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'yearly',
        time_of_day: '09:00',
        year_date: '2024-02-29',
        timezone,
      }),
      'invalid_timezone',
    )
  }
})

test('cron schedules require a non-empty five-field expression', () => {
  for (const cronExpression of [
    null,
    undefined,
    '',
    '   ',
    '0 9 * *',
    '0 9 * * * extra',
  ]) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'cron',
        cron_expression: cronExpression,
        timezone: 'UTC',
      }),
      'invalid_cron',
    )
  }

  assert.deepEqual(buildSchedulePayload({
    schedule_mode: 'cron',
    cron_expression: ' 15 6 * * 2#3 ',
    timezone: 'UTC',
  }), {
    cron_expression: '15 6 * * 2#3',
    timezone: 'UTC',
    run_once: false,
  })
})

test('cron schedules validate time zones', () => {
  for (const timezone of [null, undefined, '', '   ', 'Mars/Olympus', 'UTC+99']) {
    assertScheduleError(
      () => buildSchedulePayload({
        schedule_mode: 'cron',
        cron_expression: '0 9 * * *',
        timezone,
      }),
      'invalid_timezone',
    )
  }

  assert.deepEqual(buildSchedulePayload({
    schedule_mode: 'cron',
    cron_expression: '0 9 * * *',
    timezone: 'UTC',
  }), {
    cron_expression: '0 9 * * *',
    timezone: 'UTC',
    run_once: false,
  })
})

test('read and build leave input objects unchanged', () => {
  const task = {
    cron_expression: '30 8 * * 5,1,5,0',
    timezone: 'UTC',
    run_once: true,
  }
  const form = {
    schedule_mode: 'weekly',
    time_of_day: '08:30',
    weekdays: [5, 1, 5, 0],
    timezone: 'UTC',
  }
  const taskBefore = { ...task }
  const formBefore = { ...form, weekdays: [...form.weekdays] }

  readScheduleForm(task)
  buildSchedulePayload(form)

  assert.deepEqual(task, taskBefore)
  assert.deepEqual(form, formBefore)
})

test('describeSchedule interpolates the real English and Chinese locale messages', () => {
  const cases = [
    [{ interval_seconds: 300, run_once: false }, 'Every 300 seconds', '每隔 300 秒'],
    [{ interval_seconds: 60, run_once: true }, 'Once after 60 seconds', '60 秒后执行一次'],
    [{ cron_expression: '5 7 * * *' }, 'Daily at 07:05', '每天 07:05'],
    [
      { cron_expression: '30 8 * * 5,1,5' },
      'Weekly on Monday, Friday at 08:30',
      '每周 周一, 周五 08:30',
    ],
    [{ cron_expression: '0 9 29 * *' }, 'Monthly on day 29 at 09:00', '每月 29 日 09:00'],
    [{ cron_expression: '0 9 15 6 *' }, 'Yearly on 6/15 at 09:00', '每年 6 月 15 日 09:00'],
    [{ cron_expression: '0 9 29 2 *' }, 'Yearly on 2/29 at 09:00', '每年 2 月 29 日 09:00'],
    [
      { cron_expression: '0 9 L * *' },
      'On the last day of each month at 09:00',
      '每月最后一天 09:00',
    ],
    [{ cron_expression: '15 6 * * 2#3' }, 'Cron 15 6 * * 2#3', 'Cron 15 6 * * 2#3'],
  ]

  const enT = createTranslator(enScheduledTasks)
  const zhT = createTranslator(zhScheduledTasks)

  for (const [task, expectedEnglish, expectedChinese] of cases) {
    assert.equal(describeSchedule(task, enT), expectedEnglish)
    assert.equal(describeSchedule(task, zhT), expectedChinese)
  }
})
