import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileTemplate, parse } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import { SESSION_MAX_TURNS_UPPER_BOUND } from '../src/constants/index.js'
import enChat from '../src/i18n/locales/en/chat.js'
import zhChat from '../src/i18n/locales/zh/chat.js'

const chatViewPath = new URL('../src/views/ChatView.vue', import.meta.url)
const useChatSessionPath = new URL('../src/composables/chat/useChatSession.js', import.meta.url)

const runtime = {
  ...Vue,
  resolveComponent: (name) => ({ name }),
  resolveDirective: () => undefined,
  withDirectives: (node) => node
}

const vnodesOf = (value) => {
  if (value == null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return []
  if (typeof value === 'function') return vnodesOf(value())
  if (Array.isArray(value)) return value.flatMap(vnodesOf)
  if (!Vue.isVNode(value)) return []

  const children = []
  if (Array.isArray(value.children)) {
    children.push(...value.children.flatMap(vnodesOf))
  } else if (value.children && typeof value.children === 'object') {
    for (const child of Object.values(value.children)) children.push(...vnodesOf(child))
  }
  return [value, ...children]
}

const extractBetween = (source, startMarker, endMarker, label) => {
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker, start + startMarker.length)
  assert.ok(start >= 0, `${label} start should be found`)
  assert.ok(end > start, `${label} end should be found`)
  return source.slice(start, end)
}

let implementationPromise
const loadImplementation = () => {
  implementationPromise ||= (async () => {
    const [chatViewSource, sessionSource] = await Promise.all([
      readFile(chatViewPath, 'utf8'),
      readFile(useChatSessionPath, 'utf8')
    ])
    const parsed = parse(chatViewSource, { filename: 'ChatView.vue' })
    assert.equal(parsed.errors.length, 0, String(parsed.errors))

    const compiled = compileTemplate({
      source: parsed.descriptor.template.content,
      filename: 'ChatView.vue',
      id: 'session-agent-settings-test',
      compilerOptions: { mode: 'function' }
    })
    assert.equal(compiled.errors.length, 0, String(compiled.errors))

    const scriptSetup = parsed.descriptor.scriptSetup?.content
    assert.ok(scriptSetup, 'ChatView script setup should be available')

    const currentSessionSource = extractBetween(
      sessionSource,
      'const currentSession = computed(() =>',
      'const currentSessionShowToolCalls = computed({',
      'currentSession computed'
    )
    const defaultsSource = extractBetween(
      sessionSource,
      'const goalModeDefault = ref(true)',
      'const newSessionProfileOverrideId',
      'agent defaults'
    )
    const agentComputedSource = extractBetween(
      sessionSource,
      'const currentSessionGoalMode = computed({',
      'const currentSessionShowReasoning = computed({',
      'agent computed'
    )
    const popoverSizingSource = extractBetween(
      scriptSetup,
      'const chatInputBoxRef = ref(null)',
      'const uploadTriggerRef = ref(null)',
      'popover sizing'
    )

    return {
      render: new Function('Vue', compiled.code)(runtime),
      handlerSource: extractBetween(
        scriptSetup,
        'const updateSessionAgentSetting = async (field, value) => {',
        'const deferredContentSessionId = ref(null)',
        'ChatView agent handler'
      ),
      currentSessionSource,
      defaultsSource,
      agentComputedSource,
      popoverSizingSource
    }
  })()
  return implementationPromise
}

const createSession = (sessionId, goalMode, maxTurns) => ({
  session_id: sessionId,
  source: 'http',
  goal_mode: goalMode,
  max_turns: maxTurns
})

const createHarness = (implementation, messages, options = {}) => {
  const sessions = Vue.ref(options.sessions || [])
  const currentSessionId = Vue.ref(options.currentSessionId ?? null)
  const sessionManager = { sessions, currentSessionId }
  const currentSession = new Function(
    'computed',
    'sessionManager',
    `${implementation.currentSessionSource}\nreturn currentSession`
  )(Vue.computed, sessionManager)
  const extractedGetters = new Function(
    'ref',
    'computed',
    'currentSession',
    'sessionManager',
    'SESSION_MAX_TURNS_UPPER_BOUND',
    `${implementation.defaultsSource}\n${implementation.agentComputedSource}\nreturn { goalModeDefault, maxTurnsDefault, currentSessionGoalMode, currentSessionMaxTurns }`
  )(Vue.ref, Vue.computed, currentSession, sessionManager, SESSION_MAX_TURNS_UPPER_BOUND)

  const isCurrentSessionReadOnly = Vue.ref(false)
  const agentSettingSubmitting = Vue.ref(false)
  const loading = Vue.ref(false)
  const calls = []
  const errors = []
  const behavior = {
    update: async () => undefined
  }
  const chatApi = {
    updateSessionSetting: async (sessionId, payload) => {
      calls.push({ sessionId, payload })
      return behavior.update(sessionId, payload)
    }
  }
  const t = (key) => messages[key.slice('chat.'.length)] ?? key
  const extractedHandlers = new Function(
    'isCurrentSessionReadOnly',
    'agentSettingSubmitting',
    'loading',
    'currentSessionId',
    'sessions',
    'goalModeDefault',
    'maxTurnsDefault',
    'SESSION_MAX_TURNS_UPPER_BOUND',
    'chatApi',
    'ElMessage',
    't',
    `${implementation.handlerSource}\nreturn { updateSessionAgentSetting, updateSessionGoalMode, updateSessionMaxTurns }`
  )(
    isCurrentSessionReadOnly,
    agentSettingSubmitting,
    loading,
    currentSessionId,
    sessions,
    extractedGetters.goalModeDefault,
    extractedGetters.maxTurnsDefault,
    SESSION_MAX_TURNS_UPPER_BOUND,
    chatApi,
    { error: (message) => errors.push(message) },
    t
  )

  const currentSessionEnableMarkdown = Vue.ref(false)
  const currentSessionShowToolCalls = Vue.ref(true)
  const currentSessionShowReasoning = Vue.ref(true)
  const currentSessionInfo = Vue.computed(() => currentSession.value)
  const contextTarget = {
    $t: t,
    activeCollapse: null,
    agentSettingSubmitting,
    attachments: [],
    collapsedGroups: new Set(),
    currentSessionEnableMarkdown,
    currentSessionGoalMode: extractedGetters.currentSessionGoalMode,
    currentSessionId,
    currentSessionInfo,
    currentSessionMaxTurns: extractedGetters.currentSessionMaxTurns,
    currentSessionProfileDisplayId: null,
    currentSessionProfileOptions: [],
    currentSessionProfilePlaceholder: '',
    currentSessionShowReasoning,
    currentSessionShowToolCalls,
    currentSessionProfileOverrideId: null,
    formatProfileOptionLabel: () => '',
    formatSessionSource: () => '',
    groupedSessions: [],
    guidanceSubmitting: false,
    handleAuditDecision: () => {},
    handleCreateNewSession: () => {},
    handleDeleteSession: () => {},
    handleModeChange: () => {},
    handlePaste: () => {},
    handleRemoveCustomFile: () => {},
    handleSelectSession: () => {},
    handleSessionGroupBeforeEnter: () => {},
    handleSessionGroupBeforeLeave: () => {},
    handleSessionGroupEnter: () => {},
    handleSessionGroupLeave: () => {},
    historyLoading: false,
    inputMsg: '',
    isContextSummarizing: false,
    isCurrentSessionReadOnly,
    isWsModeComputed: Vue.computed(() => false),
    loadSessions: () => {},
    loading,
    llmRequestMetadata: null,
    messageList: null,
    messages: [],
    modeSettingSubmitting: false,
    moreOptionsVisible: false,
    chatInputBoxRef: options.chatInputBoxRef ?? null,
    moreOptionsWidth: options.moreOptionsWidth ?? 0,
    newSessionProfileOverrideId: null,
    SESSION_MAX_TURNS_UPPER_BOUND,
    openUploadPicker: () => {},
    profilesLoading: false,
    profileSettingSubmitting: false,
    reasoningSettingSubmitting: false,
    renderedInitialHistoryLoaded: false,
    renderedMessages: [],
    send: () => {},
    sessionEngaged: false,
    sessionsLoading: false,
    sessionsPanelOpen: false,
    showReasoning: true,
    toggleGroup: () => {},
    toggleMarkdown: () => {},
    toggleSessionsPanel: () => {},
    toolOutputSettingSubmitting: false,
    transportModeChangeBlocked: false,
    typingSessionId: null,
    uploadFileList: [],
    uploadTriggerRef: null,
    handleUpload: () => {},
    updateSessionGoalMode: extractedHandlers.updateSessionGoalMode,
    updateSessionMaxTurns: extractedHandlers.updateSessionMaxTurns,
    updateSessionProfileOverride: () => {},
    updateSessionShowReasoning: () => {},
    updateSessionShowToolCalls: () => {}
  }
  const context = Vue.proxyRefs(contextTarget)

  return {
    behavior,
    calls,
    context,
    currentSessionId,
    errors,
    goalModeDefault: extractedGetters.goalModeDefault,
    handlers: extractedHandlers,
    isCurrentSessionReadOnly,
    loading,
    maxTurnsDefault: extractedGetters.maxTurnsDefault,
    currentSessionMaxTurns: extractedGetters.currentSessionMaxTurns,
    sessions,
    agentSettingSubmitting
  }
}

const createPopoverSizingHarness = (implementation, options = {}) => {
  const mountedCallbacks = []
  const unmountedCallbacks = []
  const observers = []

  class MockResizeObserver {
    constructor(callback) {
      this.callback = callback
      this.observeCalls = []
      this.disconnected = false
      observers.push(this)
    }

    observe(target, options) {
      this.observeCalls.push({ target, options })
    }

    disconnect() {
      this.disconnected = true
    }
  }

  const sizing = new Function(
    'ref',
    'onMounted',
    'onUnmounted',
    'ResizeObserver',
    `${implementation.popoverSizingSource}
return { chatInputBoxRef, moreOptionsWidth }`
  )(
    Vue.ref,
    (callback) => mountedCallbacks.push(callback),
    (callback) => unmountedCallbacks.push(callback),
    MockResizeObserver
  )

  let width = options.width ?? 0
  const inputBox = options.inputBox === false
    ? null
    : Vue.markRaw({
        getBoundingClientRect: () => ({ width })
      })
  sizing.chatInputBoxRef.value = inputBox

  const harness = createHarness(implementation, enChat, {
    ...options,
    chatInputBoxRef: sizing.chatInputBoxRef,
    moreOptionsWidth: sizing.moreOptionsWidth
  })
  const renderNodes = () => vnodesOf(implementation.render(harness.context, []))
  const renderPopover = () => renderNodes().find((node) => node.type?.name === 'el-popover')
  const mount = () => {
    mountedCallbacks.forEach((callback) => callback())
    return renderPopover()
  }
  const unmount = () => {
    unmountedCallbacks.forEach((callback) => callback())
  }
  const resize = (nextWidth) => {
    width = nextWidth
    observers.at(-1)?.callback()
    return renderPopover()
  }

  return {
    harness,
    inputBox,
    mount,
    observers,
    renderNodes,
    renderPopover,
    resize,
    unmount
  }
}

const controlsFor = (render, context, messages) => {
  const root = render(context, [])
  const nodes = vnodesOf(root)
  const goalModeSwitch = nodes.find((node) => (
    node.type?.name === 'el-switch' && node.props?.['aria-label'] === messages.goal_mode
  ))
  const maxTurnsInput = nodes.find((node) => (
    node.type?.name === 'el-input-number' && node.props?.['aria-label'] === messages.max_turns
  ))
  assert.ok(goalModeSwitch, 'goal mode switch should be rendered')
  assert.ok(maxTurnsInput, 'max turns input should be rendered')
  return { goalModeSwitch, maxTurnsInput, root }
}

test('ChatView binds more options popover width to the chat input box', async () => {
  const implementation = await loadImplementation()
  const sizing = createPopoverSizingHarness(implementation, { width: 287.625 })

  const popover = sizing.mount()
  const trigger = sizing.renderNodes().find((node) => (
    node.type?.name === 'el-button' && node.props?.title === enChat.more_options
  ))

  assert.equal(popover.props.width, 287.625)
  assert.equal(popover.props['reference-el'], sizing.inputBox)
  assert.equal(popover.props.placement, 'top-start')
  assert.ok(trigger, 'more options trigger should be rendered')
  assert.notEqual(popover.props['virtual-triggering'], true)
  assert.equal(trigger.props.title, enChat.more_options)
})

test('ChatView follows more options popover width through resizes and visibility changes', async () => {
  const implementation = await loadImplementation()
  const session = createSession('session', true, 8)
  const sizing = createPopoverSizingHarness(implementation, {
    width: 312.625,
    sessions: [session]
  })

  let popover = sizing.mount()
  assert.equal(popover.props.width, 312.625)

  popover = sizing.resize(312.625)
  assert.equal(popover.props.width, 312.625)

  sizing.harness.currentSessionId.value = 'session'
  assert.equal(sizing.renderPopover().props.width, 312.625)
  popover = sizing.resize(149.75)
  assert.equal(popover.props.width, 149.75)
  assert.equal(popover.props['popper-style'].minWidth, '0')

  for (const visible of [true, false, true]) {
    sizing.harness.context.moreOptionsVisible = visible
    popover = sizing.renderPopover()
    assert.equal(popover.props.visible, visible)
    assert.equal(popover.props.width, 149.75)
  }

  popover = sizing.resize(0)
  assert.equal(popover.props.width, 0)
  popover = sizing.resize(0)
  assert.equal(popover.props.width, 0)
})

test('ChatView releases the more options resize observer on unmount', async () => {
  const implementation = await loadImplementation()
  const sizing = createPopoverSizingHarness(implementation, { width: 180.5 })

  sizing.mount()
  assert.equal(sizing.observers.length, 1)
  const observer = sizing.observers[0]
  assert.equal(observer.disconnected, false)
  assert.deepEqual(observer.observeCalls, [
    { target: sizing.inputBox, options: { box: 'border-box' } }
  ])

  sizing.unmount()
  assert.equal(observer.disconnected, true)
})

test('ChatView tolerates a missing chat input box during sizing lifecycle', async () => {
  const implementation = await loadImplementation()
  const sizing = createPopoverSizingHarness(implementation, { inputBox: false })

  assert.doesNotThrow(() => sizing.mount())
  assert.equal(sizing.observers.length, 0)
  assert.equal(sizing.renderPopover().props.width, 0)
  assert.doesNotThrow(() => sizing.unmount())
  assert.equal(sizing.observers.length, 0)
})

test('ChatView renders session agent settings and new-session defaults', async () => {
  const implementation = await loadImplementation()

  for (const messages of [zhChat, enChat]) {
    const harness = createHarness(implementation, messages)
    const initial = controlsFor(implementation.render, harness.context, messages)

    assert.equal(initial.goalModeSwitch.props['model-value'], true)
    assert.equal(initial.maxTurnsInput.props['model-value'], 5)
    assert.equal(initial.maxTurnsInput.props.min, 1)
    assert.equal(initial.maxTurnsInput.props.max, SESSION_MAX_TURNS_UPPER_BOUND)
    assert.equal(initial.goalModeSwitch.props.disabled, false)
    assert.equal(initial.maxTurnsInput.props.disabled, true)
    assert.equal(harness.calls.length, 0)

    await initial.goalModeSwitch.props['onUpdate:modelValue'](false)
    let controls = controlsFor(implementation.render, harness.context, messages)
    assert.equal(controls.goalModeSwitch.props['model-value'], false)
    assert.equal(controls.maxTurnsInput.props.disabled, false)

    for (const maxTurns of [1, 21, 1000000, SESSION_MAX_TURNS_UPPER_BOUND]) {
      await controls.maxTurnsInput.props.onChange(maxTurns)
      controls = controlsFor(implementation.render, harness.context, messages)
      assert.equal(controls.maxTurnsInput.props['model-value'], maxTurns)
      assert.equal(harness.maxTurnsDefault.value, maxTurns)
    }
    assert.equal(harness.calls.length, 0)

    await controls.goalModeSwitch.props['onUpdate:modelValue'](true)
    controls = controlsFor(implementation.render, harness.context, messages)
    assert.equal(controls.goalModeSwitch.props['model-value'], true)
    assert.equal(controls.maxTurnsInput.props['model-value'], SESSION_MAX_TURNS_UPPER_BOUND)
    assert.equal(controls.maxTurnsInput.props.disabled, true)

    await controls.goalModeSwitch.props['onUpdate:modelValue'](false)
    for (const flag of [harness.loading, harness.agentSettingSubmitting]) {
      flag.value = true
      controls = controlsFor(implementation.render, harness.context, messages)
      assert.equal(controls.goalModeSwitch.props.disabled, true)
      assert.equal(controls.maxTurnsInput.props.disabled, true)
      flag.value = false
    }
  }
})

test('ChatView keeps external session settings available while preserving restricted controls', async () => {
  const implementation = await loadImplementation()
  const session = { ...createSession('external', true, 8), source: 'weixin-openclaw' }
  const harness = createHarness(implementation, enChat, {
    sessions: [session],
    currentSessionId: 'external'
  })
  harness.isCurrentSessionReadOnly.value = true

  const initial = controlsFor(implementation.render, harness.context, enChat)
  const nodes = vnodesOf(initial.root)
  const popover = nodes.find((node) => node.type?.name === 'el-popover')
  const moreOptionsTrigger = nodes.find((node) => (
    node.type?.name === 'el-button' && node.props?.title === enChat.more_options
  ))
  const profileSelect = nodes.find((node) => node.type?.name === 'el-select')
  const auxiliarySwitches = nodes.filter((node) => (
    node.type?.name === 'el-switch' && [
      enChat.more_options_tool_output,
      enChat.show_reasoning
    ].includes(node.props?.['aria-label'])
  ))
  const uploadButton = nodes.find((node) => (
    node.type?.name === 'el-button' && node.props?.['aria-label'] === enChat.more_options_upload
  ))
  const upload = nodes.find((node) => node.type?.name === 'el-upload')
  const markdownGroup = nodes.find((node) => (
    node.type?.name === 'el-radio-group' && node.props?.['model-value'] === 'plain'
  ))
  const transportGroup = nodes.find((node) => (
    node.type?.name === 'el-radio-group' && node.props?.['model-value'] === 'non_stream'
  ))

  assert.ok(popover, 'more options popover should be rendered')
  assert.notEqual(popover.props?.disabled, true)
  assert.equal(popover.props?.placement, 'top-start')
  assert.deepEqual(popover.props?.['popper-style'], {
    maxHeight: 'min(45vh, 420px)',
    overflowY: 'auto',
    minWidth: '0'
  })
  assert.ok(moreOptionsTrigger, 'more options trigger should be rendered')
  assert.notEqual(moreOptionsTrigger.props?.disabled, true)
  assert.equal(initial.goalModeSwitch.props.disabled, false)
  assert.equal(initial.maxTurnsInput.props.disabled, true)
  assert.ok(profileSelect, 'profile select should be rendered')
  assert.equal(profileSelect.props.disabled, false)
  assert.equal(auxiliarySwitches.length, 2)
  for (const switchNode of auxiliarySwitches) {
    assert.equal(switchNode.props.disabled, false)
  }
  assert.ok(uploadButton, 'upload button should be rendered')
  assert.equal(uploadButton.props.disabled, true)
  assert.ok(upload, 'upload control should be rendered')
  assert.equal(upload.props.disabled, true)
  assert.ok(markdownGroup, 'markdown control should be rendered')
  assert.equal(markdownGroup.props.disabled, true)
  assert.ok(transportGroup, 'transport control should be rendered')
  assert.equal(transportGroup.props.disabled, true)

  await initial.goalModeSwitch.props['onUpdate:modelValue'](false)
  let controls = controlsFor(implementation.render, harness.context, enChat)
  assert.equal(controls.goalModeSwitch.props['model-value'], false)
  assert.equal(controls.maxTurnsInput.props.disabled, false)
  assert.deepEqual(harness.calls.at(-1), {
    sessionId: 'external',
    payload: { goal_mode: false }
  })
  assert.equal(session.goal_mode, false)

  await controls.maxTurnsInput.props.onChange(21)
  controls = controlsFor(implementation.render, harness.context, enChat)
  assert.equal(controls.maxTurnsInput.props['model-value'], 21)
  assert.deepEqual(harness.calls.at(-1), {
    sessionId: 'external',
    payload: { max_turns: 21 }
  })
  assert.equal(session.max_turns, 21)

  await controls.goalModeSwitch.props['onUpdate:modelValue'](true)
  controls = controlsFor(implementation.render, harness.context, enChat)
  assert.equal(controls.goalModeSwitch.props['model-value'], true)
  assert.equal(controls.maxTurnsInput.props['model-value'], 21)
  assert.equal(controls.maxTurnsInput.props.disabled, true)
  assert.deepEqual(harness.calls, [
    { sessionId: 'external', payload: { goal_mode: false } },
    { sessionId: 'external', payload: { max_turns: 21 } },
    { sessionId: 'external', payload: { goal_mode: true } }
  ])
  assert.equal(session.goal_mode, true)
  assert.equal(session.max_turns, 21)
})

test('ChatView session agent handlers isolate sessions and commit only on successful saves', async () => {
  const implementation = await loadImplementation()
  const sessionA = createSession('A', true, 7)
  const sessionB = createSession('B', false, 13)
  const harness = createHarness(implementation, enChat, {
    sessions: [sessionA, sessionB],
    currentSessionId: 'A'
  })

  let releaseSave
  const pendingSave = new Promise((resolve) => {
    releaseSave = resolve
  })
  harness.behavior.update = () => pendingSave
  const pendingUpdate = harness.handlers.updateSessionMaxTurns(21)
  await Promise.resolve()
  assert.equal(sessionA.max_turns, 7)
  assert.equal(sessionB.max_turns, 13)
  assert.equal(harness.agentSettingSubmitting.value, true)

  harness.currentSessionId.value = 'B'
  releaseSave()
  await pendingUpdate
  assert.equal(sessionA.max_turns, 21)
  assert.equal(sessionB.max_turns, 13)
  assert.equal(harness.agentSettingSubmitting.value, false)

  harness.behavior.update = async () => undefined
  harness.currentSessionId.value = 'A'
  await harness.handlers.updateSessionGoalMode(false)
  assert.equal(sessionA.goal_mode, false)
  assert.equal(sessionA.max_turns, 21)
  assert.equal(sessionB.goal_mode, false)
  assert.equal(sessionB.max_turns, 13)

  const repeatedCallStart = harness.calls.length
  await harness.handlers.updateSessionMaxTurns(21)
  await harness.handlers.updateSessionMaxTurns(21)
  assert.deepEqual(
    harness.calls.slice(repeatedCallStart),
    [
      { sessionId: 'A', payload: { max_turns: 21 } },
      { sessionId: 'A', payload: { max_turns: 21 } }
    ]
  )
  assert.equal(sessionB.max_turns, 13)

  const previousMaxTurns = sessionA.max_turns
  harness.behavior.update = async () => {
    throw new Error('save failed')
  }
  await harness.handlers.updateSessionMaxTurns(99)
  assert.equal(sessionA.max_turns, previousMaxTurns)
  assert.equal(harness.errors.at(-1), 'save failed')
  assert.equal(harness.agentSettingSubmitting.value, false)

  harness.behavior.update = async () => {
    throw new Error('')
  }
  await harness.handlers.updateSessionMaxTurns(99)
  assert.equal(sessionA.max_turns, previousMaxTurns)
  assert.equal(harness.errors.at(-1), enChat.setting_failed)
  assert.equal(harness.agentSettingSubmitting.value, false)
})

test('ChatView session agent handlers accept the max turns upper bound and reject overflow', async () => {
  const implementation = await loadImplementation()
  const session = createSession('A', true, 8)
  const harness = createHarness(implementation, enChat, {
    sessions: [session],
    currentSessionId: 'A'
  })

  await harness.handlers.updateSessionMaxTurns(SESSION_MAX_TURNS_UPPER_BOUND)
  assert.deepEqual(harness.calls, [
    { sessionId: 'A', payload: { max_turns: SESSION_MAX_TURNS_UPPER_BOUND } }
  ])
  assert.equal(session.max_turns, SESSION_MAX_TURNS_UPPER_BOUND)

  for (const value of [
    SESSION_MAX_TURNS_UPPER_BOUND + 1,
    Number.MAX_SAFE_INTEGER,
    2 ** 63
  ]) {
    const callsBefore = harness.calls.length
    await harness.handlers.updateSessionMaxTurns(value)
    assert.equal(harness.calls.length, callsBefore)
    assert.equal(session.max_turns, SESSION_MAX_TURNS_UPPER_BOUND)
    assert.equal(harness.errors.at(-1), enChat.max_turns_invalid)
  }
})

test('ChatView rejects invalid max turns and blocks session setting submissions', async () => {
  const implementation = await loadImplementation()
  const session = createSession('A', true, 8)
  const harness = createHarness(implementation, enChat, {
    sessions: [session],
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
    await harness.handlers.updateSessionMaxTurns(value)
    assert.equal(harness.calls.length, callsBefore)
    assert.equal(session.max_turns, 8)
    assert.equal(harness.errors.at(-1), enChat.max_turns_invalid)
  }

  for (const flag of [harness.loading, harness.agentSettingSubmitting]) {
    flag.value = true
    const callsBefore = harness.calls.length
    await harness.handlers.updateSessionGoalMode(false)
    await harness.handlers.updateSessionMaxTurns(9)
    assert.equal(harness.calls.length, callsBefore)
    assert.equal(session.goal_mode, true)
    assert.equal(session.max_turns, 8)
    flag.value = false
  }
})

test('useChatSession max turns computed setter accepts the upper bound and rejects overflow', async () => {
  const implementation = await loadImplementation()
  const newSessionHarness = createHarness(implementation, enChat)

  newSessionHarness.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND
  assert.equal(newSessionHarness.currentSessionMaxTurns.value, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(newSessionHarness.maxTurnsDefault.value, SESSION_MAX_TURNS_UPPER_BOUND)

  newSessionHarness.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND + 1
  assert.equal(newSessionHarness.currentSessionMaxTurns.value, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(newSessionHarness.maxTurnsDefault.value, SESSION_MAX_TURNS_UPPER_BOUND)

  const session = createSession('A', true, 8)
  const existingSessionHarness = createHarness(implementation, enChat, {
    sessions: [session],
    currentSessionId: 'A'
  })

  existingSessionHarness.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND
  assert.equal(existingSessionHarness.currentSessionMaxTurns.value, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(existingSessionHarness.sessions.value[0].max_turns, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(existingSessionHarness.calls.length, 0)

  existingSessionHarness.currentSessionMaxTurns.value = SESSION_MAX_TURNS_UPPER_BOUND + 1
  await Promise.resolve()
  assert.equal(existingSessionHarness.currentSessionMaxTurns.value, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(existingSessionHarness.sessions.value[0].max_turns, SESSION_MAX_TURNS_UPPER_BOUND)
  assert.equal(existingSessionHarness.calls.length, 0)
})
