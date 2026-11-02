const throwScheduleError = key => {
  throw new Error(`scheduledTasks.${key}`)
}

const isCronExpression = value => typeof value === 'string' && value !== ''

const parseCronNumber = (value, minimum, maximum) => {
  if (typeof value !== 'string' || !/^\d+$/.test(value)) return null
  const number = Number(value)
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) return null
  return number
}

const parseCronWeekdays = value => {
  if (typeof value !== 'string' || !/^\d(?:,\d)*$/.test(value)) return null
  const weekdays = value.split(',').map(day => parseCronNumber(day, 0, 6))
  if (weekdays.some(day => day === null)) return null
  return [...new Set(weekdays)].sort((left, right) => left - right)
}

const parseCronSchedule = expression => {
  if (typeof expression !== 'string') return null
  const fields = expression.trim().split(/\s+/)
  if (fields.length !== 5 || !fields[0]) return null

  const minute = parseCronNumber(fields[0], 0, 59)
  const hour = parseCronNumber(fields[1], 0, 23)
  if (minute === null || hour === null) return null

  const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  if (fields[2] === '*' && fields[3] === '*' && fields[4] === '*') {
    return { schedule_mode: 'daily', time_of_day: time }
  }

  if (fields[2] === '*' && fields[3] === '*') {
    const weekdays = parseCronWeekdays(fields[4])
    if (weekdays) return { schedule_mode: 'weekly', time_of_day: time, weekdays }
  }

  if (fields[3] === '*' && fields[4] === '*') {
    if (fields[2].toUpperCase() === 'L') {
      return { schedule_mode: 'monthly', time_of_day: time, month_last_day: true }
    }
    const day = parseCronNumber(fields[2], 1, 31)
    if (day !== null) return { schedule_mode: 'monthly', time_of_day: time, month_day: day, month_last_day: false }
  }

  if (fields[4] === '*') {
    const day = parseCronNumber(fields[2], 1, 31)
    const month = parseCronNumber(fields[3], 1, 12)
    if (day !== null && month !== null) {
      const yearDate = `2000-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
      if (parseCalendarDate(yearDate) !== null) {
        return { schedule_mode: 'yearly', time_of_day: time, year_date: yearDate }
      }
    }
  }

  return null
}

const parseTimeOfDay = value => {
  if (typeof value !== 'string') return null
  const match = /^(\d{2}):(\d{2})$/.exec(value)
  if (!match) return null
  const hour = Number(match[1])
  const minute = Number(match[2])
  if (hour > 23 || minute > 59) return null
  return { hour, minute }
}

const parseCalendarDate = value => {
  if (typeof value !== 'string') return null
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null

  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  if (year < 1 || month < 1 || month > 12) return null

  const date = new Date(0)
  date.setUTCFullYear(year, month - 1, day)
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) return null
  return { month, day }
}

const validateTimeZone = timezone => {
  if (typeof timezone !== 'string' || timezone.trim() === '') throwScheduleError('invalid_timezone')
  try {
    Intl.DateTimeFormat(undefined, { timeZone: timezone }).format()
  } catch {
    throwScheduleError('invalid_timezone')
  }
  return timezone
}

const formatCronTime = time => `${time.minute} ${time.hour}`

const describeWeekdays = (weekdays, t) => {
  if (!Array.isArray(weekdays)) return ''
  const validWeekdays = weekdays
    .filter(day => Number.isInteger(day) && day >= 0 && day <= 6)
    .filter((day, index, values) => values.indexOf(day) === index)
    .sort((left, right) => left - right)
  return validWeekdays.map(day => t(`scheduledTasks.weekday_${day}`)).join(', ')
}

const describeFormSchedule = (task, t) => {
  const mode = task.schedule_mode
  if (mode === 'interval' || mode === 'once') {
    const key = mode === 'once' ? 'scheduledTasks.summary_once' : 'scheduledTasks.summary_interval'
    return t(key, { seconds: task.interval_seconds })
  }
  if (mode === 'daily') {
    return t('scheduledTasks.summary_daily', { time: task.time_of_day })
  }
  if (mode === 'weekly') {
    return t('scheduledTasks.summary_weekly', {
      days: describeWeekdays(task.weekdays, t),
      time: task.time_of_day
    })
  }
  if (mode === 'monthly') {
    if (task.month_last_day === true) {
      return t('scheduledTasks.summary_month_end', { time: task.time_of_day })
    }
    const calendarDate = parseCalendarDate(task.month_date)
    if (calendarDate !== null) {
      return t('scheduledTasks.summary_monthly', { day: calendarDate.day, time: task.time_of_day })
    }
  }
  if (mode === 'yearly') {
    const calendarDate = parseCalendarDate(task.year_date)
    if (calendarDate !== null) {
      return t('scheduledTasks.summary_yearly', {
        month: calendarDate.month,
        day: calendarDate.day,
        time: task.time_of_day
      })
    }
  }
  if (mode === 'cron') {
    return t('scheduledTasks.summary_cron', { expression: task.cron_expression })
  }
  return null
}

export const getBrowserTimeZone = () => {
  try {
    const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone
    return typeof timeZone === 'string' ? timeZone : ''
  } catch {
    return ''
  }
}

export const getScheduleDefaults = () => ({
  schedule_mode: 'interval',
  interval_seconds: 300,
  run_once: false,
  cron_expression: '',
  timezone: getBrowserTimeZone(),
  time_of_day: '09:00',
  weekdays: [1],
  month_date: '2000-01-01',
  year_date: '2000-01-01',
  month_last_day: false
})

export const readScheduleForm = task => {
  const source = task && typeof task === 'object' ? task : {}
  const defaults = getScheduleDefaults()
  const form = { ...defaults, weekdays: [...defaults.weekdays] }

  if (typeof source.timezone === 'string' && source.timezone !== '') form.timezone = source.timezone

  if (isCronExpression(source.cron_expression)) {
    form.schedule_mode = 'cron'
    form.cron_expression = source.cron_expression
    form.run_once = false
    const parsed = parseCronSchedule(source.cron_expression)
    if (parsed) {
      form.schedule_mode = parsed.schedule_mode
      form.time_of_day = parsed.time_of_day
      if (parsed.schedule_mode === 'weekly') form.weekdays = [...parsed.weekdays]
      if (parsed.schedule_mode === 'monthly') {
        form.month_last_day = parsed.month_last_day === true
        if (parsed.month_day !== undefined) form.month_date = `2000-01-${String(parsed.month_day).padStart(2, '0')}`
      }
      if (parsed.schedule_mode === 'yearly') form.year_date = parsed.year_date
    }
    return form
  }

  form.schedule_mode = source.run_once === true ? 'once' : 'interval'
  form.run_once = source.run_once === true
  if (source.interval_seconds !== undefined && source.interval_seconds !== null) {
    form.interval_seconds = source.interval_seconds
  }
  return form
}

export const buildSchedulePayload = form => {
  const source = form && typeof form === 'object' ? form : {}
  const mode = source.schedule_mode

  if (mode === 'interval' || mode === 'once') {
    if (!Number.isInteger(source.interval_seconds) || source.interval_seconds < 60) {
      throwScheduleError('invalid_interval')
    }
    return {
      interval_seconds: source.interval_seconds,
      run_once: mode === 'once'
    }
  }

  if (mode === 'daily' || mode === 'weekly' || mode === 'monthly' || mode === 'yearly') {
    const time = parseTimeOfDay(source.time_of_day)
    if (!time) throwScheduleError('invalid_time')

    let cronDay = '*'
    let cronMonth = '*'
    let cronWeekday = '*'
    if (mode === 'weekly') {
      if (!Array.isArray(source.weekdays) || source.weekdays.length === 0) throwScheduleError('invalid_weekdays')
      const weekdays = source.weekdays.filter(day => Number.isInteger(day) && day >= 0 && day <= 6)
      if (weekdays.length !== source.weekdays.length) throwScheduleError('invalid_weekdays')
      const normalizedWeekdays = [...new Set(weekdays)].sort((left, right) => left - right)
      if (normalizedWeekdays.length === 0) throwScheduleError('invalid_weekdays')
      cronWeekday = normalizedWeekdays.join(',')
    }
    if (mode === 'monthly') {
      if (source.month_last_day === true) {
        cronDay = 'L'
      } else {
        const calendarDate = parseCalendarDate(source.month_date)
        if (calendarDate === null) throwScheduleError('invalid_month_day')
        cronDay = String(calendarDate.day)
      }
    }
    if (mode === 'yearly') {
      const calendarDate = parseCalendarDate(source.year_date)
      if (calendarDate === null) throwScheduleError('invalid_year_date')
      cronDay = String(calendarDate.day)
      cronMonth = String(calendarDate.month)
    }

    const timezone = validateTimeZone(source.timezone)
    return {
      cron_expression: `${formatCronTime(time)} ${cronDay} ${cronMonth} ${cronWeekday}`,
      timezone,
      run_once: false
    }
  }

  if (mode === 'cron') {
    if (typeof source.cron_expression !== 'string') throwScheduleError('invalid_cron')
    const cronExpression = source.cron_expression.trim()
    if (!cronExpression || cronExpression.split(/\s+/).length !== 5) throwScheduleError('invalid_cron')
    return {
      cron_expression: cronExpression,
      timezone: validateTimeZone(source.timezone),
      run_once: false
    }
  }

  throwScheduleError('invalid_schedule')
}

export const describeSchedule = (task, t) => {
  const source = task && typeof task === 'object' ? task : {}
  return describeFormSchedule(readScheduleForm(source), t)
}
