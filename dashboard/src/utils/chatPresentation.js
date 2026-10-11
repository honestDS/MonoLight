const normalizeIdentity = value => value === undefined || value === null || value === '' ? null : String(value)
const MESSAGE_LIST_BOTTOM_TOLERANCE = 24

export const getMessageListScrollState = (metrics = {}) => {
  const { scrollHeight, clientHeight, scrollTop } = metrics || {}
  if (![scrollHeight, clientHeight, scrollTop].every(Number.isFinite) || scrollHeight < 0 || clientHeight <= 0) {
    return null
  }

  const maxScrollTop = Math.max(0, scrollHeight - clientHeight)
  const bottomDistance = Math.max(0, maxScrollTop - Math.max(0, scrollTop))
  return {
    hasOverflow: maxScrollTop > 0,
    atBottom: bottomDistance <= MESSAGE_LIST_BOTTOM_TOLERANCE,
    bottomDistance,
  }
}

export const getReasoningCollapseName = (message) => {
  const responseId = normalizeIdentity(message?.response_id)
  if (responseId) return `reasoning:response:${responseId}`

  const workId = normalizeIdentity(message?.work_id)
  const turn = normalizeIdentity(message?.turn)
  if (workId && turn) return `reasoning:work:${workId}:turn:${turn}`

  const dbId = normalizeIdentity(message?.db_id ?? message?.message_id)
  if (dbId) return `reasoning:db:${dbId}`

  const messageId = normalizeIdentity(message?.id)
  return `reasoning:message:${messageId || 'unknown'}`
}

export const resolveChatActivityNotice = ({ contextSummarizing = false, thinking = false, historyLoading = false } = {}) => {
  if (contextSummarizing) return 'context_summary'
  if (thinking) return 'thinking'
  if (historyLoading) return 'history_loading'
  return null
}

export const isFollowableLlmOutput = message => (
  message?.type === 'tool_group' || ['assistant', 'tool', 'reasoning'].includes(message?.role)
)

export const groupChatSessions = (sessions, { currentUid = null, now = new Date(), translate = key => key } = {}) => {
  if (!Array.isArray(sessions)) return []

  const parseActivityTime = value => {
    if (value instanceof Date) {
      const time = value.getTime()
      return Number.isFinite(time) ? time : -Infinity
    }

    if (typeof value === 'number') {
      return Number.isFinite(value) ? value : -Infinity
    }

    if (typeof value !== 'string' || value.trim() === '') return -Infinity

    const parsed = Date.parse(value.trim().replace(/^(\d{4}-\d{2}-\d{2})\s+/, '$1T'))
    return Number.isFinite(parsed) ? parsed : -Infinity
  }

  const normalizedCurrentUid = normalizeIdentity(currentUid)
  const nowTime = now instanceof Date ? now.getTime() : NaN
  const todayStart = Number.isFinite(nowTime)
    ? new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
    : null
  const yesterdayStart = todayStart === null
    ? null
    : (() => {
        const date = new Date(todayStart)
        date.setDate(date.getDate() - 1)
        return date.getTime()
      })()

  const entries = []
  sessions.forEach((session, index) => {
    if (!session || typeof session !== 'object' || Array.isArray(session)) return
    entries.push({ session, activityTime: parseActivityTime(session.last_active), index })
  })

  entries.sort((left, right) => {
    if (left.activityTime !== right.activityTime) {
      if (left.activityTime === -Infinity) return 1
      if (right.activityTime === -Infinity) return -1
      return right.activityTime > left.activityTime ? 1 : -1
    }
    return left.index - right.index
  })

  const dateGroups = new Map([
    ['today', { key: 'today', labelKey: 'chat.session_group_today', entries: [] }],
    ['yesterday', { key: 'yesterday', labelKey: 'chat.session_group_yesterday', entries: [] }],
    ['earlier', { key: 'earlier', labelKey: 'chat.session_group_earlier', entries: [] }],
  ])
  const userGroups = new Map()

  for (const entry of entries) {
    const sessionUid = normalizeIdentity(entry.session.uid)

    if (normalizedCurrentUid !== null && sessionUid !== null && sessionUid !== normalizedCurrentUid) {
      let group = userGroups.get(sessionUid)
      if (!group) {
        group = { key: `user:${sessionUid}`, uid: sessionUid, label: sessionUid, entries: [] }
        userGroups.set(sessionUid, group)
      }

      const username = normalizeIdentity(entry.session.username)
      if (group.label === group.uid && username !== null) group.label = username
      group.entries.push(entry)
      continue
    }

    const dateKey = todayStart !== null && entry.activityTime !== -Infinity
      ? entry.activityTime >= todayStart
        ? 'today'
        : entry.activityTime >= yesterdayStart
          ? 'yesterday'
          : 'earlier'
      : 'earlier'
    dateGroups.get(dateKey).entries.push(entry)
  }

  const groups = []
  const translateLabel = typeof translate === 'function' ? translate : key => key
  for (const group of dateGroups.values()) {
    if (group.entries.length === 0) continue
    groups.push({
      key: group.key,
      label: translateLabel(group.labelKey),
      sessions: group.entries.map(entry => entry.session),
    })
  }

  for (const group of userGroups.values()) {
    groups.push({
      key: group.key,
      label: translateLabel('chat.session_group_user', { username: group.label }),
      sessions: group.entries.map(entry => entry.session),
    })
  }

  return groups
}
