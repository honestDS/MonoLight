import assert from 'node:assert/strict'
import test from 'node:test'
import { createI18n } from 'vue-i18n'

import enChat from '../src/i18n/locales/en/chat.js'
import zhChat from '../src/i18n/locales/zh/chat.js'

import {
  getMessageListScrollState,
  getReasoningCollapseName,
  groupChatSessions,
  isFollowableLlmOutput,
  resolveChatActivityNotice,
} from '../src/utils/chatPresentation.js'

const createChatI18n = (locale = 'en') => createI18n({
  legacy: false,
  locale,
  fallbackLocale: 'en',
  messages: {
    en: { chat: enChat },
    zh: { chat: zhChat },
  },
})

test('message list scroll state handles visible and overflowing content', () => {
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 0, clientHeight: 480, scrollTop: 0 }), {
    hasOverflow: false,
    atBottom: true,
    bottomDistance: 0,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 320, clientHeight: 480, scrollTop: 0 }), {
    hasOverflow: false,
    atBottom: true,
    bottomDistance: 0,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 480, clientHeight: 480, scrollTop: 0 }), {
    hasOverflow: false,
    atBottom: true,
    bottomDistance: 0,
  })

  const maxScrollTop = 600
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 0 }), {
    hasOverflow: true,
    atBottom: false,
    bottomDistance: maxScrollTop,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 300 }), {
    hasOverflow: true,
    atBottom: false,
    bottomDistance: 300,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: maxScrollTop }), {
    hasOverflow: true,
    atBottom: true,
    bottomDistance: 0,
  })
  assert.equal(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 576 }).atBottom, true)
  assert.equal(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 575 }).atBottom, false)
})

test('message list scroll state clamps scroll position and rejects invalid measurements', () => {
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: -50 }), {
    hasOverflow: true,
    atBottom: false,
    bottomDistance: 600,
  })
  assert.deepEqual(getMessageListScrollState({ scrollHeight: 1000, clientHeight: 400, scrollTop: 9999 }), {
    hasOverflow: true,
    atBottom: true,
    bottomDistance: 0,
  })

  const invalidMeasurements = [
    { scrollHeight: 100, clientHeight: 0, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: -1, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: Number.NaN, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: Number.POSITIVE_INFINITY, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: undefined, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: null, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: '400', scrollTop: 0 },
    { scrollHeight: Number.NaN, clientHeight: 100, scrollTop: 0 },
    { scrollHeight: 100, clientHeight: 100, scrollTop: null },
    { scrollHeight: 100, clientHeight: 100, scrollTop: '0' },
  ]
  for (const metrics of invalidMeasurements) assert.equal(getMessageListScrollState(metrics), null)
})

test('message list scroll state reflects measurement changes without retaining state', () => {
  const initiallyVisible = getMessageListScrollState({ scrollHeight: 320, clientHeight: 400, scrollTop: 0 })
  const contentGrew = getMessageListScrollState({ scrollHeight: 600, clientHeight: 400, scrollTop: 0 })
  const windowGrew = getMessageListScrollState({ scrollHeight: 600, clientHeight: 700, scrollTop: 0 })
  const contentCollapsed = getMessageListScrollState({ scrollHeight: 320, clientHeight: 400, scrollTop: 180 })

  assert.equal(initiallyVisible.hasOverflow, false)
  assert.equal(contentGrew.hasOverflow, true)
  assert.equal(contentGrew.atBottom, false)
  assert.equal(windowGrew.hasOverflow, false)
  assert.deepEqual(contentCollapsed, { hasOverflow: false, atBottom: true, bottomDistance: 0 })
  assert.deepEqual(
    getMessageListScrollState({ scrollHeight: 600, clientHeight: 400, scrollTop: 200 }),
    getMessageListScrollState({ scrollHeight: 600, clientHeight: 400, scrollTop: 200 })
  )
})

test('reasoning collapse identity survives transient reasoning becoming a tool group', () => {
  const transient = { role: 'reasoning', response_id: 'response-1', work_id: 'work-1', turn: 2 }
  const toolGroup = { type: 'tool_group', role: 'assistant', response_id: 'response-1', work_id: 'work-1', turn: 2 }

  assert.equal(getReasoningCollapseName(transient), 'reasoning:response:response-1')
  assert.equal(getReasoningCollapseName(toolGroup), getReasoningCollapseName(transient))
})

test('reasoning collapse identity falls back to work and turn when response identity is absent', () => {
  assert.equal(
    getReasoningCollapseName({ role: 'assistant', work_id: 17, turn: 3 }),
    'reasoning:work:17:turn:3'
  )
  assert.equal(getReasoningCollapseName({ db_id: 17, id: 42 }), 'reasoning:db:17')
  assert.equal(getReasoningCollapseName({}), 'reasoning:message:unknown')
})

test('chat activity notice uses one explicit priority order', () => {
  assert.equal(resolveChatActivityNotice({ contextSummarizing: true, thinking: true, historyLoading: true }), 'context_summary')
  assert.equal(resolveChatActivityNotice({ contextSummarizing: false, thinking: true, historyLoading: true }), 'thinking')
  assert.equal(resolveChatActivityNotice({ contextSummarizing: false, thinking: false, historyLoading: true }), 'history_loading')
  assert.equal(resolveChatActivityNotice({ contextSummarizing: false, thinking: false, historyLoading: false }), null)
})

test('streamed reasoning is followable output just like assistant and tool messages', () => {
  assert.equal(isFollowableLlmOutput({ role: 'assistant' }), true)
  assert.equal(isFollowableLlmOutput({ role: 'tool' }), true)
  assert.equal(isFollowableLlmOutput({ role: 'reasoning' }), true)
  assert.equal(isFollowableLlmOutput({ type: 'tool_group', role: 'assistant' }), true)
  assert.equal(isFollowableLlmOutput({ role: 'thinking' }), false)
  assert.equal(isFollowableLlmOutput({ role: 'user' }), false)
  assert.equal(isFollowableLlmOutput(), false)
  assert.equal(isFollowableLlmOutput(null), false)
})

test('chat sessions use translated local date groups in descending activity order', () => {
  const now = new Date(2026, 9, 10, 12)
  const translationKeys = []
  const sessions = [
    { session_id: 'earlier', uid: 'me', last_active: '2026-10-08 23:59:00' },
    { session_id: 'today-start', uid: 'me', last_active: '2026-10-10 00:00:00' },
    { session_id: 'yesterday-start', uid: 'me', last_active: '2026-10-09 00:00:00' },
    { session_id: 'today-late', uid: 'me', last_active: '2026-10-10 11:00:00' },
    { session_id: 'yesterday-late', uid: 'me', last_active: '2026-10-09 23:59:00' },
  ]

  const groups = groupChatSessions(sessions, {
    currentUid: 'me',
    now,
    translate: key => {
      translationKeys.push(key)
      return `translated:${key}`
    },
  })

  assert.deepEqual(groups.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id),
  })), [
    {
      key: 'today',
      label: 'translated:chat.session_group_today',
      sessionIds: ['today-late', 'today-start'],
    },
    {
      key: 'yesterday',
      label: 'translated:chat.session_group_yesterday',
      sessionIds: ['yesterday-late', 'yesterday-start'],
    },
    {
      key: 'earlier',
      label: 'translated:chat.session_group_earlier',
      sessionIds: ['earlier'],
    },
  ])
  assert.deepEqual(translationKeys, [
    'chat.session_group_today',
    'chat.session_group_yesterday',
    'chat.session_group_earlier',
  ])
})

test('admin session groups keep own date groups before users from mixed sources', () => {
  const now = new Date(2026, 9, 10, 12)
  const sessions = [
    { session_id: 'user-3-old', uid: 3, username: 'Carol', source: 'external', last_active: '2026-10-08 10:00:00' },
    { session_id: 'me-today', uid: 1, source: 'http', last_active: '2026-10-10 11:00:00' },
    { session_id: 'user-2-old', uid: '2', username: 'Bob', source: 'http', last_active: '2026-10-09 10:00:00' },
    { session_id: 'user-3-new', uid: '3', username: 'Carol', source: 'ws', last_active: '2026-10-10 09:00:00' },
    { session_id: 'me-earlier', uid: 1, source: 'http', last_active: '2026-10-08 09:00:00' },
    { session_id: 'user-2-new', uid: 2, username: 'Bob', source: 'ws', last_active: '2026-10-10 10:00:00' },
    { session_id: 'me-yesterday', uid: 1, source: 'http', last_active: '2026-10-09 09:00:00' },
  ]

  const groups = groupChatSessions(sessions, { currentUid: 1, now, translate: createChatI18n().global.t })

  assert.deepEqual(groups.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id),
  })), [
    { key: 'today', label: 'Today', sessionIds: ['me-today'] },
    { key: 'yesterday', label: 'Yesterday', sessionIds: ['me-yesterday'] },
    { key: 'earlier', label: 'Earlier', sessionIds: ['me-earlier'] },
    { key: 'user:2', label: "Bob's sessions", sessionIds: ['user-2-new', 'user-2-old'] },
    { key: 'user:3', label: "Carol's sessions", sessionIds: ['user-3-new', 'user-3-old'] },
  ])
})

test('chat session user groups normalize zero and numeric or string UIDs without merging users', () => {
  const now = new Date(2026, 9, 10, 12)
  const sessions = [
    { session_id: 'same-uid-old', uid: 7, username: 'Old name', last_active: '2026-10-10 10:00:00' },
    { session_id: 'same-uid-new', uid: '7', username: 'Renamed', last_active: '2026-10-10 11:00:00' },
    { session_id: 'same-name-other-user', uid: 8, username: 'Renamed', last_active: '2026-10-10 09:00:00' },
    { session_id: 'zero-self', uid: 0, last_active: '2026-10-10 08:00:00' },
  ]

  const groups = groupChatSessions(sessions, { currentUid: '0', now, translate: createChatI18n().global.t })

  assert.deepEqual(groups.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id),
  })), [
    { key: 'today', label: 'Today', sessionIds: ['zero-self'] },
    { key: 'user:7', label: "Renamed's sessions", sessionIds: ['same-uid-new', 'same-uid-old'] },
    { key: 'user:8', label: "Renamed's sessions", sessionIds: ['same-name-other-user'] },
  ])
})

test('chat session user labels fall back to UID and can be filled by an older named session', () => {
  const now = new Date(2026, 9, 10, 12)
  const groups = groupChatSessions([
    { session_id: 'uid-9-new', uid: 9, last_active: '2026-10-10 11:00:00' },
    { session_id: 'uid-10', uid: 10, last_active: '2026-10-10 10:00:00' },
    { session_id: 'uid-9-old', uid: 9, username: 'Nine', last_active: '2026-10-10 09:00:00' },
  ], { currentUid: 'self', now, translate: createChatI18n().global.t })

  assert.deepEqual(groups.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id),
  })), [
    { key: 'user:9', label: "Nine's sessions", sessionIds: ['uid-9-new', 'uid-9-old'] },
    { key: 'user:10', label: "10's sessions", sessionIds: ['uid-10'] },
  ])
})

test('chat session user labels preserve translated titles and usernames across locales', () => {
  const now = new Date(2026, 9, 10, 12)
  const sessions = Object.freeze([
    Object.freeze({ session_id: 'self-today', uid: 'me', last_active: '2026-10-10 11:00:00' }),
    Object.freeze({ session_id: 'zh-old', uid: 'zh', username: '张三', last_active: '2026-10-10 07:00:00' }),
    Object.freeze({ session_id: 'special-user', uid: 'special', username: "O'Neil {name}@example.com |$&", last_active: '2026-10-10 09:00:00' }),
    Object.freeze({ session_id: 'missing-user', uid: 'missing', username: '', last_active: '2026-10-10 08:00:00' }),
    Object.freeze({ session_id: 'zh-new', uid: 'zh', username: '张三', last_active: '2026-10-10 10:00:00' }),
  ])
  const originalSessions = sessions.map(session => ({ ...session }))
  const summarize = groups => groups.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id),
  }))
  const i18n = createChatI18n('zh')

  const zhGroups = groupChatSessions(sessions, {
    currentUid: 'me',
    now,
    translate: i18n.global.t,
  })
  const zhGroupsAgain = groupChatSessions(sessions, {
    currentUid: 'me',
    now,
    translate: i18n.global.t,
  })

  i18n.global.locale.value = 'en'
  const enGroups = groupChatSessions(sessions, {
    currentUid: 'me',
    now,
    translate: i18n.global.t,
  })
  const enGroupsAgain = groupChatSessions(sessions, {
    currentUid: 'me',
    now,
    translate: i18n.global.t,
  })

  assert.deepEqual(summarize(zhGroups), [
    { key: 'today', label: '今天', sessionIds: ['self-today'] },
    { key: 'user:zh', label: '张三 的会话', sessionIds: ['zh-new', 'zh-old'] },
    { key: 'user:special', label: "O'Neil {name}@example.com |$& 的会话", sessionIds: ['special-user'] },
    { key: 'user:missing', label: 'missing 的会话', sessionIds: ['missing-user'] },
  ])
  assert.deepEqual(summarize(enGroups), [
    { key: 'today', label: 'Today', sessionIds: ['self-today'] },
    { key: 'user:zh', label: "张三's sessions", sessionIds: ['zh-new', 'zh-old'] },
    { key: 'user:special', label: "O'Neil {name}@example.com |$&'s sessions", sessionIds: ['special-user'] },
    { key: 'user:missing', label: "missing's sessions", sessionIds: ['missing-user'] },
  ])
  assert.deepEqual(zhGroups, zhGroupsAgain)
  assert.deepEqual(enGroups, enGroupsAgain)
  assert.deepEqual(sessions, originalSessions)
})

test('chat session activity parsing sorts valid formats before invalid values stably', () => {
  const now = new Date(2026, 9, 10, 12)
  const sameInstant = new Date(2026, 9, 8, 12)
  const sessions = [
    { session_id: 'unknown-empty', last_active: '' },
    { session_id: 'same-backend', last_active: '2026-10-08 12:00:00' },
    { session_id: 'same-date', last_active: new Date(sameInstant.getTime()) },
    { session_id: 'invalid-date', last_active: new Date('invalid') },
    { session_id: 'later', last_active: new Date(2026, 9, 8, 13) },
    { session_id: 'same-number', last_active: sameInstant.getTime() },
    { session_id: 'same-iso', last_active: sameInstant.toISOString() },
    { session_id: 'unknown-null', last_active: null },
    { session_id: 'unknown-undefined', last_active: undefined },
    { session_id: 'unknown-nan', last_active: Number.NaN },
    { session_id: 'unknown-infinity', last_active: Number.POSITIVE_INFINITY },
  ]

  const groups = groupChatSessions(sessions, { now, currentUid: null })

  assert.deepEqual(groups.map(group => ({
    key: group.key,
    sessionIds: group.sessions.map(session => session.session_id),
  })), [{
    key: 'earlier',
    sessionIds: [
      'later',
      'same-backend',
      'same-date',
      'same-number',
      'same-iso',
      'unknown-empty',
      'invalid-date',
      'unknown-null',
      'unknown-undefined',
      'unknown-nan',
      'unknown-infinity',
    ],
  }])
})

test('chat session grouping handles unknown current users, invalid clocks, and malformed entries', () => {
  const now = new Date(2026, 9, 10, 12)
  const datedSession = { session_id: 'dated', uid: 'other', last_active: '2026-10-10 09:00:00' }

  assert.deepEqual(groupChatSessions([datedSession], { currentUid: null, now }).map(group => group.key), ['today'])

  for (const invalidNow of [new Date('invalid'), 'not-a-date', 0, null]) {
    assert.deepEqual(
      groupChatSessions([datedSession], { currentUid: 'other', now: invalidNow }).map(group => group.key),
      ['earlier']
    )
  }

  assert.deepEqual(groupChatSessions(null), [])
  assert.deepEqual(groupChatSessions({}), [])
  assert.deepEqual(
    groupChatSessions([null, undefined, 'session', 42, [], datedSession], { currentUid: 'other', now }).map(group => ({
      key: group.key,
      sessionIds: group.sessions.map(session => session.session_id),
    })),
    [{ key: 'today', sessionIds: ['dated'] }]
  )
})

test('chat session grouping preserves frozen inputs, now, references, and repeatable output', () => {
  const now = new Date(2026, 9, 10, 12)
  const originalNowTime = now.getTime()
  const earlier = Object.freeze({ session_id: 'earlier', uid: 'me', last_active: '2026-10-09 23:00:00' })
  const latest = Object.freeze({ session_id: 'latest', uid: 'me', last_active: '2026-10-10 11:00:00' })
  const sessions = Object.freeze([earlier, latest])
  const originalSessionIds = sessions.map(session => session.session_id)
  const originalSessions = sessions.map(session => ({ ...session }))

  const first = groupChatSessions(sessions, { currentUid: 'me', now })
  const second = groupChatSessions(sessions, { currentUid: 'me', now })

  assert.deepEqual(sessions.map(session => session.session_id), originalSessionIds)
  assert.deepEqual(sessions, originalSessions)
  assert.equal(now.getTime(), originalNowTime)
  assert.strictEqual(first[0].sessions[0], latest)
  assert.strictEqual(first[1].sessions[0], earlier)
  assert.deepEqual(first, second)
})

test('chat session date groups use calendar boundaries across DST', () => {
  const now = new Date(2026, 2, 9, 12)
  const groups = groupChatSessions([
    { session_id: 'before-yesterday', uid: 'me', last_active: '2026-03-07 23:59:00' },
    { session_id: 'yesterday-start', uid: 'me', last_active: '2026-03-08 00:00:00' },
    { session_id: 'today-start', uid: 'me', last_active: '2026-03-09 00:00:00' },
  ], { currentUid: 'me', now })

  assert.deepEqual(groups.map(group => ({
    key: group.key,
    sessionIds: group.sessions.map(session => session.session_id),
  })), [
    { key: 'today', sessionIds: ['today-start'] },
    { key: 'yesterday', sessionIds: ['yesterday-start'] },
    { key: 'earlier', sessionIds: ['before-yesterday'] },
  ])
})

test('chat session date groups cross month and year boundaries', () => {
  const cases = [
    {
      now: new Date(2026, 2, 1, 12),
      sessions: [
        { session_id: 'before-yesterday', uid: 'me', last_active: '2026-02-27 23:59:00' },
        { session_id: 'yesterday-start', uid: 'me', last_active: '2026-02-28 00:00:00' },
        { session_id: 'today-start', uid: 'me', last_active: '2026-03-01 00:00:00' },
        { session_id: 'yesterday-late', uid: 'me', last_active: '2026-02-28 23:59:00' },
      ],
    },
    {
      now: new Date(2028, 2, 1, 12),
      sessions: [
        { session_id: 'before-yesterday', uid: 'me', last_active: '2028-02-28 23:59:00' },
        { session_id: 'yesterday-start', uid: 'me', last_active: '2028-02-29 00:00:00' },
        { session_id: 'today-start', uid: 'me', last_active: '2028-03-01 00:00:00' },
        { session_id: 'yesterday-late', uid: 'me', last_active: '2028-02-29 23:59:00' },
      ],
    },
    {
      now: new Date(2027, 0, 1, 12),
      sessions: [
        { session_id: 'before-yesterday', uid: 'me', last_active: '2026-12-30 23:59:00' },
        { session_id: 'yesterday-start', uid: 'me', last_active: '2026-12-31 00:00:00' },
        { session_id: 'today-start', uid: 'me', last_active: '2027-01-01 00:00:00' },
        { session_id: 'yesterday-late', uid: 'me', last_active: '2026-12-31 23:59:00' },
      ],
    },
  ]

  for (const { now, sessions } of cases) {
    const groups = groupChatSessions(sessions, { currentUid: 'me', now })

    assert.deepEqual(groups.map(group => ({
      key: group.key,
      sessionIds: group.sessions.map(session => session.session_id),
    })), [
      { key: 'today', sessionIds: ['today-start'] },
      { key: 'yesterday', sessionIds: ['yesterday-late', 'yesterday-start'] },
      { key: 'earlier', sessionIds: ['before-yesterday'] },
    ])
  }
})
