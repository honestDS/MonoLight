import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { computed, ref } from 'vue'
import { createSessionAgentSettingUpdater } from '../src/composables/chat/sessionAgentSettings.js'
import {
  filterProfilesByUid,
  resolveProfileOwnerUid,
  resolveSessionProfileDisplayId,
  resolveSessionProfilePlaceholder
} from '../src/utils/profileOptions.js'

const stripImports = source => {
  const keptLines = []
  let inImport = false

  for (const line of source.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const settingsSource = stripImports(readFileSync(
  new URL('../src/composables/chat/useChatViewSettings.js', import.meta.url),
  'utf8'
)).replace(/\bexport\s+function\b/g, 'function')

const settingsContext = vm.createContext({
  computed,
  ref,
  createSessionAgentSettingUpdater,
  filterProfilesByUid,
  resolveProfileOwnerUid,
  resolveSessionProfileDisplayId,
  resolveSessionProfilePlaceholder
})
vm.runInContext(
  `${settingsSource}\nglobalThis.__module = { useChatViewSettings }`,
  settingsContext,
  { filename: 'useChatViewSettings.js' }
)
const { useChatViewSettings } = settingsContext.__module

const makeSession = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  uid: 'user-a',
  source: 'http',
  enable_markdown: true,
  show_tool_calls: false,
  show_reasoning: true,
  goal_mode: true,
  max_turns: 5,
  profile_override_id: null,
  profile_id: null,
  ...overrides
})

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createFixture = ({
  sessions = [],
  sessionId = null,
  currentUid = 'user-a',
  profileItems = [],
  profileResponseCurrentUid = currentUid,
  updateSessionSetting = async () => {},
  loading = false,
  readOnly = false,
  transportMode = 'http',
  modeSettingSubmitting = false,
  transportModeChangeBlocked = false,
  newSessionProfileOverrideId = null,
  enableMarkdownDefault = true,
  showToolCalls = false,
  showReasoning = true,
  goalModeDefault = true,
  maxTurnsDefault = 5
} = {}) => {
  const sessionsRef = ref(sessions)
  const currentSessionId = ref(sessionId)
  const currentSession = computed(() => (
    sessionsRef.value.find(session => session.session_id === currentSessionId.value) || null
  ))
  const currentUidRef = ref(currentUid)
  const enableMarkdownDefaultRef = ref(enableMarkdownDefault)
  const newSessionProfileOverrideIdRef = ref(newSessionProfileOverrideId)
  const isCurrentSessionReadOnly = ref(readOnly)
  const currentSessionShowToolCalls = ref(showToolCalls)
  const currentSessionShowReasoning = ref(showReasoning)
  const goalModeDefaultRef = ref(goalModeDefault)
  const maxTurnsDefaultRef = ref(maxTurnsDefault)
  const loadingRef = ref(loading)
  const transportModeRef = ref(transportMode)
  const modeSettingSubmittingRef = ref(modeSettingSubmitting)
  const transportModeChangeBlockedRef = ref(transportModeChangeBlocked)

  const updateRequests = []
  const profileListRequests = []
  const reloadCalls = []
  const modeCalls = []
  const notices = {
    warning: [],
    error: [],
    success: []
  }
  const translations = {
    'chat.external_session_read_only': 'external session is read-only',
    'chat.setting_failed': 'setting failed',
    'chat.load_profiles_failed': 'loading profiles failed',
    'chat.profile_setting_saved': 'profile setting saved',
    'chat.default_profile_suffix': ' (default)',
    'chat.inherited_profile': 'inherited profile',
    'chat.transport_change_blocked': 'transport change blocked',
    'chat.session_source_http': 'HTTP',
    'chat.session_source_ws': 'WebSocket',
    'chat.max_turns_invalid': 'max turns invalid'
  }

  const api = {
    updateSessionSetting: async (id, payload) => {
      updateRequests.push({ sessionId: id, payload: { ...payload } })
      return updateSessionSetting(id, payload)
    }
  }
  const profileApi = {
    list: async request => {
      profileListRequests.push({ ...request })
      return {
        data: {
          data: {
            items: profileItems,
            meta: { current_uid: profileResponseCurrentUid }
          }
        }
      }
    }
  }
  const notify = {
    warning: message => notices.warning.push(message),
    error: message => notices.error.push(message),
    success: message => notices.success.push(message)
  }
  const translate = key => translations[key]
  const reloadCurrentSessionHistory = async () => {
    reloadCalls.push(currentSessionId.value)
  }
  const setTransportMode = async mode => {
    modeCalls.push(mode)
    transportModeRef.value = mode
  }

  const chat = {
    enableMarkdownDefault: enableMarkdownDefaultRef,
    sessions: sessionsRef,
    currentSessionId,
    currentSession,
    newSessionProfileOverrideId: newSessionProfileOverrideIdRef,
    isCurrentSessionReadOnly,
    currentSessionShowToolCalls,
    currentSessionShowReasoning,
    goalModeDefault: goalModeDefaultRef,
    maxTurnsDefault: maxTurnsDefaultRef,
    loading: loadingRef,
    transportMode: transportModeRef,
    modeSettingSubmitting: modeSettingSubmittingRef,
    transportModeChangeBlocked: transportModeChangeBlockedRef,
    reloadCurrentSessionHistory,
    setTransportMode
  }

  return {
    chat,
    currentUid: currentUidRef,
    currentSessionId,
    sessions: sessionsRef,
    isCurrentSessionReadOnly,
    updateRequests,
    profileListRequests,
    reloadCalls,
    modeCalls,
    notices,
    transportMode: transportModeRef,
    enableMarkdownDefault: enableMarkdownDefaultRef,
    currentSessionShowToolCalls,
    currentSessionShowReasoning,
    goalModeDefault: goalModeDefaultRef,
    maxTurnsDefault: maxTurnsDefaultRef,
    loading: loadingRef,
    api,
    profileApi,
    notify,
    translate
  }
}

const createSettings = fixture => useChatViewSettings({
  chat: fixture.chat,
  currentUid: fixture.currentUid,
  api: fixture.api,
  profileApi: fixture.profileApi,
  notify: fixture.notify,
  translate: fixture.translate
})

test('updates defaults without a session and never sends a session setting request', async () => {
  const fixture = createFixture()
  const settings = createSettings(fixture)

  await settings.toggleMarkdown(false)
  await settings.updateSessionShowToolCalls(true)
  await settings.updateSessionShowReasoning(false)
  await settings.updateSessionGoalMode(false)
  await settings.updateSessionMaxTurns(8)
  await settings.updateSessionProfileOverride(42)

  assert.equal(fixture.enableMarkdownDefault.value, false)
  assert.equal(fixture.currentSessionShowToolCalls.value, true)
  assert.equal(fixture.currentSessionShowReasoning.value, false)
  assert.equal(fixture.goalModeDefault.value, false)
  assert.equal(fixture.maxTurnsDefault.value, 8)
  assert.equal(fixture.chat.newSessionProfileOverrideId.value, 42)
  assert.deepEqual(fixture.updateRequests, [])
})

test('persists existing-session settings with exact payloads and local state', async () => {
  const current = makeSession('A')
  const fixture = createFixture({
    sessions: [current],
    sessionId: 'A',
    showToolCalls: false,
    showReasoning: true
  })
  const settings = createSettings(fixture)

  await settings.toggleMarkdown(false)
  await settings.updateSessionShowToolCalls(true)
  await settings.updateSessionShowReasoning(false)
  await settings.updateSessionGoalMode(false)
  await settings.updateSessionMaxTurns(9)
  await settings.updateSessionProfileOverride(23)

  assert.deepEqual(fixture.updateRequests, [
    { sessionId: 'A', payload: { enable_markdown: false } },
    { sessionId: 'A', payload: { show_tool_calls: true } },
    { sessionId: 'A', payload: { show_reasoning: false } },
    { sessionId: 'A', payload: { goal_mode: false } },
    { sessionId: 'A', payload: { max_turns: 9 } },
    { sessionId: 'A', payload: { profile_override_id: 23 } }
  ])
  assert.equal(current.enable_markdown, false)
  assert.equal(fixture.currentSessionShowToolCalls.value, true)
  assert.equal(fixture.currentSessionShowReasoning.value, false)
  assert.equal(current.goal_mode, false)
  assert.equal(current.max_turns, 9)
  assert.equal(current.profile_override_id, 23)
  assert.deepEqual(fixture.reloadCalls, ['A'])
  assert.deepEqual(fixture.notices.success, ['profile setting saved'])
})

test('rolls back markdown, tool, reasoning, and profile state after save errors', async () => {
  const current = makeSession('A', { profile_override_id: 17 })
  const fixture = createFixture({
    sessions: [current],
    sessionId: 'A',
    updateSessionSetting: async (_sessionId, payload) => {
      throw new Error(`${Object.keys(payload)[0]} failed`)
    }
  })
  const settings = createSettings(fixture)

  await settings.toggleMarkdown(false)
  await settings.updateSessionShowToolCalls(true)
  await settings.updateSessionShowReasoning(false)
  await settings.updateSessionProfileOverride(23)

  assert.equal(current.enable_markdown, true)
  assert.equal(fixture.currentSessionShowToolCalls.value, false)
  assert.equal(fixture.currentSessionShowReasoning.value, true)
  assert.equal(current.profile_override_id, 17)
  assert.deepEqual(fixture.notices.error, [
    'enable_markdown failed',
    'show_tool_calls failed',
    'show_reasoning failed',
    'setting failed'
  ])
  assert.equal(settings.profileSettingSubmitting.value, false)
  assert.equal(settings.toolOutputSettingSubmitting.value, false)
  assert.equal(settings.reasoningSettingSubmitting.value, false)
})

test('loads profiles, captures current uid, filters by selected session uid, and inherits placeholder', async () => {
  const current = makeSession('A', {
    uid: 'session-user',
    profile_override_id: 12
  })
  const profiles = [
    { id: 11, uid: 'session-user', name: 'Session default', is_default: true },
    { id: 12, uid: 'session-user', name: 'Session custom', is_default: false },
    { id: 21, uid: 'current-user', name: 'Current default', is_default: true },
    { id: 31, uid: 'other-user', name: 'Other profile', is_default: false }
  ]
  const fixture = createFixture({
    sessions: [current],
    sessionId: 'A',
    currentUid: 'before-load',
    profileItems: profiles,
    profileResponseCurrentUid: 'current-user'
  })
  const settings = createSettings(fixture)

  await settings.loadProfiles()

  assert.deepEqual(fixture.profileListRequests, [{ page: 1, size: 1000 }])
  assert.deepEqual(settings.profiles.value.map(profile => profile.id), [11, 12, 21, 31])
  assert.equal(fixture.currentUid.value, 'current-user')
  assert.deepEqual(
    settings.currentSessionProfileOptions.value.map(profile => profile.id),
    [11, 12]
  )
  assert.equal(settings.currentSessionProfileDisplayId.value, 12)
  assert.equal(settings.currentSessionProfilePlaceholder.value, 'Session default (default)')
  assert.equal(settings.profilesLoading.value, false)

  fixture.isCurrentSessionReadOnly.value = true
  assert.equal(settings.currentSessionProfilePlaceholder.value, 'inherited profile')
})

test('a pending profile save stays bound to session A and blocks a duplicate submission', async () => {
  const sessionA = makeSession('A', { profile_override_id: 10 })
  const sessionB = makeSession('B', { profile_override_id: 20 })
  const firstSave = createDeferred()
  const fixture = createFixture({
    sessions: [sessionA, sessionB],
    sessionId: 'A',
    updateSessionSetting: async () => firstSave.promise
  })
  const settings = createSettings(fixture)

  const pending = settings.updateSessionProfileOverride(30)
  assert.equal(settings.profileSettingSubmitting.value, true)

  fixture.currentSessionId.value = 'B'
  await settings.updateSessionProfileOverride(40)
  assert.deepEqual(fixture.updateRequests, [
    { sessionId: 'A', payload: { profile_override_id: 30 } }
  ])
  assert.equal(sessionA.profile_override_id, 10)
  assert.equal(sessionB.profile_override_id, 20)

  firstSave.resolve()
  await pending

  assert.equal(sessionA.profile_override_id, 30)
  assert.equal(sessionB.profile_override_id, 20)
  assert.equal(settings.profileSettingSubmitting.value, false)
  assert.deepEqual(fixture.notices.success, ['profile setting saved'])
})

test('loading and active submission flags block tool and agent setting updates', async () => {
  const current = makeSession('A')
  const fixture = createFixture({
    sessions: [current],
    sessionId: 'A',
    loading: true
  })
  const settings = createSettings(fixture)

  await settings.updateSessionShowToolCalls(true)
  await settings.updateSessionGoalMode(false)
  await settings.updateSessionMaxTurns(8)
  assert.equal(fixture.currentSessionShowToolCalls.value, false)
  assert.equal(current.goal_mode, true)
  assert.equal(current.max_turns, 5)
  assert.deepEqual(fixture.updateRequests, [])

  fixture.loading.value = false
  settings.toolOutputSettingSubmitting.value = true
  settings.agentSettingSubmitting.value = true
  await settings.updateSessionShowToolCalls(true)
  await settings.updateSessionGoalMode(false)
  await settings.updateSessionMaxTurns(8)

  assert.equal(fixture.currentSessionShowToolCalls.value, false)
  assert.equal(current.goal_mode, true)
  assert.equal(current.max_turns, 5)
  assert.deepEqual(fixture.updateRequests, [])
})

test('mode changes warn for read-only or blocked sessions and format known sources', async () => {
  const fixture = createFixture({ readOnly: true })
  const settings = createSettings(fixture)

  await settings.handleModeChange(true)
  assert.deepEqual(fixture.modeCalls, [])
  assert.deepEqual(fixture.notices.warning, ['external session is read-only'])

  fixture.isCurrentSessionReadOnly.value = false
  fixture.chat.transportModeChangeBlocked.value = true
  await settings.handleModeChange(false)
  assert.deepEqual(fixture.modeCalls, [])
  assert.deepEqual(fixture.notices.warning, [
    'external session is read-only',
    'transport change blocked'
  ])

  fixture.chat.transportModeChangeBlocked.value = false
  await settings.handleModeChange(true)
  await settings.handleModeChange(false)
  assert.deepEqual(fixture.modeCalls, ['ws', 'http'])
  assert.equal(fixture.transportMode.value, 'http')
  assert.equal(settings.isWsModeComputed.value, false)
  assert.equal(settings.formatSessionSource('http'), 'HTTP')
  assert.equal(settings.formatSessionSource('ws'), 'WebSocket')
  assert.equal(settings.formatSessionSource('external'), 'external')
})
