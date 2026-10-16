import assert from 'node:assert/strict'
import test from 'node:test'

import { SESSION_MAX_TURNS_UPPER_BOUND } from '../src/constants/index.js'
import {
  buildMessagePlatformPayload,
  createMessagePlatformForm,
  isValidMessagePlatformMaxTurns,
  loadMessagePlatformForm
} from '../src/utils/messagePlatformForm.js'

test('creates complete defaults with independent stream switches and nested state', () => {
  const first = createMessagePlatformForm()
  const second = createMessagePlatformForm()

  assert.deepEqual(first.config.session_settings, {
    enable_markdown: false,
    goal_mode: true,
    max_turns: 5,
    show_tool_calls: true,
    show_reasoning: true
  })
  assert.equal(first.use_stream_dispatch, false)
  assert.equal(first.config.stream_llm_request, false)
  assert.notStrictEqual(first.config, second.config)
  assert.notStrictEqual(first.config.session_settings, second.config.session_settings)

  first.use_stream_dispatch = true
  first.config.api_timeout_ms = 0
  first.config.session_settings.goal_mode = false

  assert.equal(first.config.stream_llm_request, false)
  assert.equal(second.config.api_timeout_ms, 15000)
  assert.equal(second.config.session_settings.goal_mode, true)
})

test('keeps defaults for legacy rows without config and derives only show_tool_calls from dispatch mode', () => {
  const legacy = loadMessagePlatformForm({ name: 'legacy platform' })

  assert.deepEqual(legacy.config, {
    api_timeout_ms: 15000,
    long_poll_timeout_ms: 30000,
    poll_interval_ms: 1000,
    merge_single_poll_messages: true,
    stream_llm_request: false,
    session_settings: {
      enable_markdown: false,
      goal_mode: true,
      max_turns: 5,
      show_tool_calls: false,
      show_reasoning: true
    }
  })

  for (const [useStreamDispatch, expectedShowToolCalls] of [[true, true], [false, false]]) {
    const form = loadMessagePlatformForm({ use_stream_dispatch: useStreamDispatch })

    assert.equal(form.config.stream_llm_request, false)
    assert.equal(form.config.session_settings.show_tool_calls, expectedShowToolCalls)
  }
})

test('preserves explicitly false request and session settings over legacy dispatch mode', () => {
  const form = loadMessagePlatformForm({
    use_stream_dispatch: true,
    config: {
      stream_llm_request: false,
      session_settings: { show_tool_calls: false }
    }
  })

  assert.equal(form.config.stream_llm_request, false)
  assert.equal(form.config.session_settings.show_tool_calls, false)
})

test('does not mutate a loaded row when nested form config changes', () => {
  const row = {
    name: 'stored platform',
    config: {
      api_timeout_ms: 250,
      session_settings: {
        goal_mode: false,
        max_turns: 8
      }
    }
  }
  const before = structuredClone(row)
  const form = loadMessagePlatformForm(row)

  form.config.api_timeout_ms = 0
  form.config.session_settings.goal_mode = true
  form.config.session_settings.max_turns = 12

  assert.deepEqual(row, before)
})

test('applies uid permissions and normalizes empty profile ids to null', () => {
  const regular = createMessagePlatformForm({ uid: 'regular-user' })
  const superuser = createMessagePlatformForm({ uid: 'ignored-user', isSuperuser: true })

  assert.equal(regular.uid, 'regular-user')
  assert.equal(regular.profile_id, null)
  assert.equal(superuser.uid, '')
  assert.equal(superuser.profile_id, null)

  const regularLoaded = loadMessagePlatformForm(
    { uid: 'row-user', profile_id: '' },
    { uid: 'regular-user' }
  )
  const superuserLoaded = loadMessagePlatformForm(
    { uid: 'row-user', profile_id: null },
    { uid: 'regular-user', isSuperuser: true }
  )

  assert.equal(regularLoaded.uid, 'regular-user')
  assert.equal(regularLoaded.profile_id, null)
  assert.equal(superuserLoaded.uid, 'row-user')
  assert.equal(superuserLoaded.profile_id, null)
})

test('builds only the payload whitelist, trims name, and preserves false and zero values', () => {
  const form = {
    name: '  Platform name  ',
    platform_type: 'CUSTOM',
    language: 'en',
    use_stream_dispatch: false,
    is_enabled: false,
    uid: 'user-id',
    profile_id: null,
    config: {
      api_timeout_ms: 0,
      long_poll_timeout_ms: 0,
      poll_interval_ms: 0,
      merge_single_poll_messages: false,
      stream_llm_request: false,
      token: 'config-token',
      unknown_config: 'discard',
      session_settings: {
        enable_markdown: false,
        goal_mode: false,
        max_turns: 0,
        show_tool_calls: false,
        show_reasoning: false,
        unknown_setting: 'discard'
      }
    },
    token: 'token',
    state: { status: 'dirty' },
    unknown_field: 'discard'
  }

  const payload = buildMessagePlatformPayload(form)

  assert.deepEqual(payload, {
    name: 'Platform name',
    platform_type: 'CUSTOM',
    language: 'en',
    use_stream_dispatch: false,
    is_enabled: false,
    profile_id: null,
    config: {
      api_timeout_ms: 0,
      long_poll_timeout_ms: 0,
      poll_interval_ms: 0,
      merge_single_poll_messages: false,
      stream_llm_request: false,
      session_settings: {
        enable_markdown: false,
        goal_mode: false,
        max_turns: 0,
        show_tool_calls: false,
        show_reasoning: false
      }
    }
  })
  assert.equal(Object.hasOwn(payload, 'uid'), false)
})

test('includes uid only when requested and converts an empty uid to null', () => {
  const form = createMessagePlatformForm({ uid: 'user-id' })
  form.name = 'Platform'

  assert.equal(Object.hasOwn(buildMessagePlatformPayload(form), 'uid'), false)
  assert.equal(buildMessagePlatformPayload(form, { includeUid: true }).uid, 'user-id')

  form.uid = ''
  assert.equal(buildMessagePlatformPayload(form, { includeUid: true }).uid, null)
})

test('serializes return and request stream switches independently for all combinations', () => {
  const combinations = [
    [false, false],
    [false, true],
    [true, false],
    [true, true]
  ]

  for (const [useStreamDispatch, streamLlmRequest] of combinations) {
    const form = createMessagePlatformForm()
    form.name = 'Platform'
    form.use_stream_dispatch = useStreamDispatch
    form.config.stream_llm_request = streamLlmRequest

    const payload = buildMessagePlatformPayload(form)

    assert.equal(payload.use_stream_dispatch, useStreamDispatch)
    assert.equal(payload.config.stream_llm_request, streamLlmRequest)
  }
})

test('switching goal mode does not change max turns during serialization', () => {
  const form = createMessagePlatformForm()
  const maxTurns = Math.max(1, SESSION_MAX_TURNS_UPPER_BOUND - 1)
  form.name = 'Platform'
  form.config.session_settings.max_turns = maxTurns

  for (const goalMode of [true, false]) {
    form.config.session_settings.goal_mode = goalMode
    const payload = buildMessagePlatformPayload(form)

    assert.equal(payload.config.session_settings.goal_mode, goalMode)
    assert.equal(payload.config.session_settings.max_turns, maxTurns)
  }
})

test('validates max turns at strict integer bounds', () => {
  assert.equal(isValidMessagePlatformMaxTurns(1), true)
  assert.equal(isValidMessagePlatformMaxTurns(SESSION_MAX_TURNS_UPPER_BOUND), true)

  for (const value of [
    0,
    -1,
    1.5,
    '1',
    true,
    false,
    null,
    NaN,
    SESSION_MAX_TURNS_UPPER_BOUND + 1
  ]) {
    assert.equal(isValidMessagePlatformMaxTurns(value), false)
  }
})

test('returns equal but independent payloads for repeated builds', () => {
  const form = createMessagePlatformForm({ uid: 'user-id' })
  form.name = '  Platform  '
  form.config.session_settings.max_turns = 7
  const before = structuredClone(form)

  const first = buildMessagePlatformPayload(form, { includeUid: true })
  const second = buildMessagePlatformPayload(form, { includeUid: true })

  assert.deepEqual(first, second)
  assert.notStrictEqual(first, second)
  assert.notStrictEqual(first.config, second.config)
  assert.notStrictEqual(first.config.session_settings, second.config.session_settings)

  first.config.api_timeout_ms = 0
  first.config.session_settings.show_reasoning = false

  assert.equal(second.config.api_timeout_ms, 15000)
  assert.equal(second.config.session_settings.show_reasoning, true)
  assert.deepEqual(form, before)
})
