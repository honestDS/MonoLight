import assert from 'node:assert/strict'
import test from 'node:test'
import { computed, ref } from 'vue'
import { SESSION_MAX_TURNS_UPPER_BOUND } from '../src/constants/index.js'
import {
  createSessionAgentSettingUpdater,
  createSessionAgentSettings
} from '../src/composables/chat/sessionAgentSettings.js'
import enChat from '../src/i18n/locales/en/chat.js'
import zhChat from '../src/i18n/locales/zh/chat.js'

const createSession = (sessionId, goalMode, maxTurns, source = 'http') => ({
  session_id: sessionId,
  source,
  goal_mode: goalMode,
  max_turns: maxTurns
})

const createHarness = ({
  messages = enChat,
  sessions: initialSessions = [],
  currentSessionId: initialSessionId = null
} = {}) => {
  const sessions = ref(initialSessions)
  const currentSessionId = ref(initialSessionId)
  const currentSession = computed(() => (
    sessions.value.find(session => session.session_id === currentSessionId.value)
  ))
  const settings = createSessionAgentSettings({
    sessionManager: { sessions, currentSessionId },
    currentSession
  })
  const loading = ref(false)
  const agentSettingSubmitting = ref(false)
  const calls = []
  const errors = []
  const behavior = {
    update: async () => undefined
  }
  const updateSessionSetting = async (sessionId, payload) => {
    calls.push({ sessionId, payload })
    return behavior.update(sessionId, payload)
  }
  const translate = (key) => messages[key.slice('chat.'.length)] ?? key
  const updater = createSessionAgentSettingUpdater({
    currentSessionId,
    sessions,
    goalModeDefault: settings.goalModeDefault,
    maxTurnsDefault: settings.maxTurnsDefault,
    agentSettingSubmitting,
    loading,
    updateSessionSetting,
    reportError: (message) => errors.push(message),
    translate
  })

  return {
    agentSettingSubmitting,
    behavior,
    calls,
    currentSessionId,
    errors,
    loading,
    sessions,
    settings,
    updater
  }
}

const sessionById = (harness, sessionId) => (
  harness.sessions.value.find(session => session.session_id === sessionId)
)

test('new-session settings start with true and 5 and save defaults without an API call', async () => {
  for (const messages of [zhChat, enChat]) {
    const harness = createHarness({ messages })

    assert.equal(harness.settings.goalModeDefault.value, true)
    assert.equal(harness.settings.maxTurnsDefault.value, 5)
    assert.equal(harness.settings.currentSessionGoalMode.value, true)
    assert.equal(harness.settings.currentSessionMaxTurns.value, 5)

    await harness.updater.updateSessionGoalMode(false)
    await harness.updater.updateSessionMaxTurns(21)
    assert.equal(harness.settings.goalModeDefault.value, false)
    assert.equal(harness.settings.maxTurnsDefault.value, 21)
    assert.equal(harness.settings.currentSessionGoalMode.value, false)
    assert.equal(harness.settings.currentSessionMaxTurns.value, 21)
    assert.deepEqual(harness.calls, [])

    await harness.updater.updateSessionGoalMode(true)
    assert.equal(harness.settings.goalModeDefault.value, true)
    assert.equal(harness.settings.currentSessionMaxTurns.value, 21)
  }
})

test('existing and external sessions can save goal and max-turn settings', async () => {
  for (const session of [
    createSession('existing', true, 8),
    createSession('external', true, 8, 'weixin-openclaw')
  ]) {
    const harness = createHarness({
      sessions: [session],
      currentSessionId: session.session_id
    })

    await harness.updater.updateSessionGoalMode(false)
    await harness.updater.updateSessionMaxTurns(21)
    const savedSession = sessionById(harness, session.session_id)

    assert.deepEqual(harness.calls, [
      { sessionId: session.session_id, payload: { goal_mode: false } },
      { sessionId: session.session_id, payload: { max_turns: 21 } }
    ])
    assert.equal(savedSession.goal_mode, false)
    assert.equal(savedSession.max_turns, 21)

    await harness.updater.updateSessionGoalMode(true)
    assert.equal(savedSession.goal_mode, true)
    assert.equal(savedSession.max_turns, 21)
  }
})

test('valid max-turn values save, goal changes preserve max turns, and repeats remain stable', async () => {
  const harness = createHarness({
    sessions: [createSession('A', true, 8)],
    currentSessionId: 'A'
  })
  const validValues = [1, 21, 1000000, SESSION_MAX_TURNS_UPPER_BOUND]

  for (const value of validValues) {
    await harness.updater.updateSessionMaxTurns(value)
    assert.equal(sessionById(harness, 'A').max_turns, value)
    assert.deepEqual(harness.calls.at(-1), {
      sessionId: 'A',
      payload: { max_turns: value }
    })
  }

  await harness.updater.updateSessionGoalMode(false)
  assert.equal(sessionById(harness, 'A').goal_mode, false)
  assert.equal(sessionById(harness, 'A').max_turns, SESSION_MAX_TURNS_UPPER_BOUND)
  await harness.updater.updateSessionGoalMode(true)
  assert.equal(sessionById(harness, 'A').goal_mode, true)
  assert.equal(sessionById(harness, 'A').max_turns, SESSION_MAX_TURNS_UPPER_BOUND)

  const callsBeforeRepeat = harness.calls.length
  await harness.updater.updateSessionMaxTurns(21)
  await harness.updater.updateSessionMaxTurns(21)
  assert.deepEqual(harness.calls.slice(callsBeforeRepeat), [
    { sessionId: 'A', payload: { max_turns: 21 } },
    { sessionId: 'A', payload: { max_turns: 21 } }
  ])
  assert.equal(sessionById(harness, 'A').max_turns, 21)
})

test('invalid max-turn values are rejected without an API call or mutation', async () => {
  const harness = createHarness({
    sessions: [createSession('A', true, 8)],
    currentSessionId: 'A'
  })
  const invalidValues = [
    0,
    -1,
    1.5,
    '5',
    true,
    undefined,
    NaN,
    Infinity,
    SESSION_MAX_TURNS_UPPER_BOUND + 1,
    Number.MAX_SAFE_INTEGER,
    2 ** 63
  ]

  for (const value of invalidValues) {
    const callsBefore = harness.calls.length
    await harness.updater.updateSessionMaxTurns(value)
    assert.equal(harness.calls.length, callsBefore)
    assert.equal(sessionById(harness, 'A').max_turns, 8)
    assert.equal(harness.errors.at(-1), enChat.max_turns_invalid)
  }
})

test('loading and an active submission prevent setting updates', async () => {
  const harness = createHarness({
    sessions: [createSession('A', true, 8)],
    currentSessionId: 'A'
  })

  for (const flag of [harness.loading, harness.agentSettingSubmitting]) {
    flag.value = true
    const callsBefore = harness.calls.length
    await harness.updater.updateSessionGoalMode(false)
    await harness.updater.updateSessionMaxTurns(21)
    assert.equal(harness.calls.length, callsBefore)
    assert.equal(sessionById(harness, 'A').goal_mode, true)
    assert.equal(sessionById(harness, 'A').max_turns, 8)
    flag.value = false
  }
})

test('a pending save blocks a second submission and commits to its captured session', async () => {
  const harness = createHarness({
    sessions: [
      createSession('A', true, 7),
      createSession('B', false, 13)
    ],
    currentSessionId: 'A'
  })
  let releaseSave
  const pendingSave = new Promise(resolve => {
    releaseSave = resolve
  })
  harness.behavior.update = () => pendingSave

  const firstSave = harness.updater.updateSessionMaxTurns(21)
  await Promise.resolve()
  assert.equal(harness.agentSettingSubmitting.value, true)
  assert.deepEqual(harness.calls, [
    { sessionId: 'A', payload: { max_turns: 21 } }
  ])

  harness.currentSessionId.value = 'B'
  await harness.updater.updateSessionGoalMode(true)
  assert.equal(harness.calls.length, 1)
  assert.equal(sessionById(harness, 'A').max_turns, 7)
  assert.equal(sessionById(harness, 'B').goal_mode, false)

  releaseSave()
  await firstSave
  assert.equal(sessionById(harness, 'A').max_turns, 21)
  assert.equal(sessionById(harness, 'B').max_turns, 13)
  assert.equal(sessionById(harness, 'B').goal_mode, false)
  assert.equal(harness.agentSettingSubmitting.value, false)
})

test('failed saves do not mutate sessions and use localized fallback for empty errors', async () => {
  for (const messages of [enChat, zhChat]) {
    const harness = createHarness({
      messages,
      sessions: [createSession('A', true, 8)],
      currentSessionId: 'A'
    })

    harness.behavior.update = async () => {
      throw new Error('save failed')
    }
    await harness.updater.updateSessionGoalMode(false)
    assert.equal(sessionById(harness, 'A').goal_mode, true)
    assert.equal(harness.errors.at(-1), 'save failed')
    assert.equal(harness.agentSettingSubmitting.value, false)

    harness.behavior.update = async () => {
      throw new Error('')
    }
    await harness.updater.updateSessionMaxTurns(21)
    assert.equal(sessionById(harness, 'A').max_turns, 8)
    assert.equal(harness.errors.at(-1), messages.setting_failed)
    assert.equal(harness.agentSettingSubmitting.value, false)
  }
})

test('computed setters keep new-session defaults and update only the selected existing session', () => {
  const newSession = createHarness()
  newSession.settings.currentSessionGoalMode.value = 0
  newSession.settings.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND
  assert.equal(newSession.settings.goalModeDefault.value, false)
  assert.equal(newSession.settings.maxTurnsDefault.value, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(newSession.settings.currentSessionGoalMode.value, false)
  assert.equal(newSession.settings.currentSessionMaxTurns.value, SESSION_MAX_TURNS_UPPER_BOUND)
  newSession.settings.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND + 1
  assert.equal(newSession.settings.maxTurnsDefault.value, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(newSession.settings.currentSessionMaxTurns.value, SESSION_MAX_TURNS_UPPER_BOUND)

  const existingSession = createHarness({
    sessions: [createSession('A', true, 8)],
    currentSessionId: 'A'
  })
  existingSession.settings.currentSessionGoalMode.value = false
  existingSession.settings.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND
  assert.equal(sessionById(existingSession, 'A').goal_mode, false)
  assert.equal(sessionById(existingSession, 'A').max_turns, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(existingSession.settings.goalModeDefault.value, true)
  assert.equal(existingSession.settings.maxTurnsDefault.value, 5)

  existingSession.settings.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND + 1
  assert.equal(sessionById(existingSession, 'A').max_turns, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(existingSession.calls.length, 0)
})

test('a missing selected session does not alter defaults or another session', async () => {
  const harness = createHarness({
    sessions: [createSession('other', false, 13)],
    currentSessionId: 'missing'
  })

  assert.equal(harness.settings.currentSessionGoalMode.value, true)
  assert.equal(harness.settings.currentSessionMaxTurns.value, 5)
  harness.settings.currentSessionGoalMode.value = false
  harness.settings.currentSessionMaxTurns.value = 21
  assert.equal(harness.settings.goalModeDefault.value, true)
  assert.equal(harness.settings.maxTurnsDefault.value, 5)
  assert.equal(harness.settings.currentSessionGoalMode.value, true)
  assert.equal(harness.settings.currentSessionMaxTurns.value, 5)
  assert.equal(sessionById(harness, 'other').goal_mode, false)
  assert.equal(sessionById(harness, 'other').max_turns, 13)

  await harness.updater.updateSessionGoalMode(true)
  await harness.updater.updateSessionMaxTurns(21)
  assert.deepEqual(harness.calls, [
    { sessionId: 'missing', payload: { goal_mode: true } },
    { sessionId: 'missing', payload: { max_turns: 21 } }
  ])
  assert.equal(harness.settings.goalModeDefault.value, true)
  assert.equal(harness.settings.maxTurnsDefault.value, 5)
  assert.equal(sessionById(harness, 'other').goal_mode, false)
  assert.equal(sessionById(harness, 'other').max_turns, 13)
})
