import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileTemplate, parse } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import { SESSION_MAX_TURNS_UPPER_BOUND } from '../src/constants/index.js'
import { useSessionReasoning } from '../src/composables/chat/useSessionReasoning.js'
import enChat from '../src/i18n/locales/en/chat.js'

const chatViewPath = new URL('../src/views/ChatView.vue', import.meta.url)

const runtime = {
  ...Vue,
  resolveComponent: name => ({ name }),
  resolveDirective: () => undefined,
  withDirectives: (node, directives) => {
    node.dirs = directives
    return node
  }
}

const vnodesOf = value => {
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

const textOf = value => {
  if (value == null || typeof value === 'boolean') return ''
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  if (typeof value === 'function') return textOf(value())
  if (Array.isArray(value)) return value.map(textOf).join('')
  if (Vue.isVNode(value)) return textOf(value.children)
  if (typeof value === 'object') return Object.values(value).map(textOf).join('')
  return ''
}

const hasClass = (value, expected) => {
  if (typeof value === 'string') return value.split(/\s+/).includes(expected)
  if (Array.isArray(value)) return value.some(item => hasClass(item, expected))
  if (value && typeof value === 'object') return Boolean(value[expected])
  return false
}

const extractBetween = (source, startMarker, endMarker, label) => {
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker, start + startMarker.length)
  assert.ok(start >= 0, `${label} start should be found`)
  assert.ok(end > start, `${label} end should be found`)
  return source.slice(start, end)
}

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flushMicrotasks = async () => {
  for (let index = 0; index < 6; index += 1) await Promise.resolve()
}

const optionResponse = (options = [], defaults = []) => ({
  data: { data: { options, defaults } }
})

const createSession = (sessionId, reasoningEffort = null, overrides = {}) => ({
  session_id: sessionId,
  source: 'http',
  reasoning_effort: reasoningEffort,
  profile_override_id: null,
  enable_markdown: true,
  show_tool_calls: true,
  show_reasoning: true,
  goal_mode: true,
  max_turns: 5,
  ...overrides
})

let implementationPromise
const loadImplementation = () => {
  implementationPromise ||= (async () => {
    const source = await readFile(chatViewPath, 'utf8')
    const parsed = parse(source, { filename: 'ChatView.vue' })
    assert.equal(parsed.errors.length, 0, String(parsed.errors))

    const compiled = compileTemplate({
      source: parsed.descriptor.template.content,
      filename: 'ChatView.vue',
      id: 'chat-reasoning-overlay-test',
      compilerOptions: { mode: 'function' }
    })
    assert.equal(compiled.errors.length, 0, String(compiled.errors))

    const scriptSetup = parsed.descriptor.scriptSetup?.content
    assert.ok(scriptSetup, 'ChatView script setup should be available')

    return {
      render: new Function('Vue', compiled.code)(runtime),
      lifecycleSource: extractBetween(
        scriptSetup,
        'const chatInputBoxRef = ref(null)',
        'const uploadTriggerRef = ref(null)',
        'ChatView input lifecycle'
      ),
      reasoningSource: extractBetween(
        scriptSetup,
        'const profileOverrideId = computed(() => {',
        'const updateSessionAgentSetting = async (field, value) => {',
        'ChatView reasoning setup'
      ),
      actionButtonLabelSource: extractBetween(
        scriptSetup,
        'const actionButtonLabel = computed(() =>',
        '// 拦截发送，发送完成后清空列表',
        'ChatView action button label'
      ),
      sendSource: extractBetween(
        scriptSetup,
        'const send = async () => {',
        'const handleAuditDecision = async ({ decision }) => {',
        'ChatView send handler'
      ),
      auditSource: extractBetween(
        scriptSetup,
        'const handleAuditDecision = async ({ decision }) => {',
        '// 通信模式切换',
        'ChatView audit handler'
      ),
      navigationSource: extractBetween(
        scriptSetup,
        'const handleSelectSession = (session) => {',
        'const handleWelcomeExitTransitionEnd = async (event) => {',
        'ChatView navigation handlers'
      )
    }
  })()
  return implementationPromise
}

const createHarness = async (t, implementation, options = {}) => {
  const scope = Vue.effectScope()
  t.after(() => scope.stop())

  const mountedCallbacks = []
  const unmountedCallbacks = []
  const observers = []
  class MockResizeObserver {
    constructor(callback) {
      this.callback = callback
      this.disconnected = false
      observers.push(this)
    }

    observe() {}

    disconnect() {
      this.disconnected = true
    }
  }

  const sessions = Vue.ref(options.sessions ?? [])
  const currentSessionId = Vue.ref(options.currentSessionId ?? null)
  const currentSession = Vue.computed(() => sessions.value.find(session => (
    session && Object.is(session.session_id, currentSessionId.value)
  )) || null)
  const draftReasoningEffort = Vue.ref(options.draftReasoningEffort ?? null)
  const newSessionProfileOverrideId = Vue.ref(options.newSessionProfileOverrideId ?? null)
  const currentSessionReasoningEffort = Vue.computed({
    get() {
      if (currentSessionId.value === null || currentSessionId.value === undefined || currentSessionId.value === '') {
        return draftReasoningEffort.value
      }
      return currentSession.value?.reasoning_effort ?? null
    },
    set(value) {
      if (currentSessionId.value === null || currentSessionId.value === undefined || currentSessionId.value === '') {
        draftReasoningEffort.value = value
      }
    }
  })

  const messages = Vue.ref(options.messages ?? [])
  const inputMsg = Vue.ref(options.inputMsg ?? '')
  const loading = Vue.ref(options.loading ?? false)
  const isReplyRunning = Vue.ref(options.isReplyRunning ?? false)
  const isStopping = Vue.ref(options.isStopping ?? false)
  const messageList = Vue.ref(null)
  const sessionsLoading = Vue.ref(false)
  const typingSessionId = Vue.ref(null)
  const activeCollapse = Vue.ref(null)
  const transportMode = Vue.ref('http')
  const modeSettingSubmitting = Vue.ref(false)
  const transportModeChangeBlocked = Vue.ref(false)
  const attachments = Vue.ref(options.attachments ?? [])
  const isCurrentSessionReadOnly = Vue.ref(options.readOnly ?? false)
  const isContextSummarizing = Vue.ref(false)
  const llmRequestMetadata = Vue.ref(null)
  const historyLoading = Vue.ref(false)
  const initialHistoryLoaded = Vue.ref(false)
  const currentSessionShowToolCalls = Vue.ref(true)
  const currentSessionShowReasoning = Vue.ref(true)
  const currentSessionGoalMode = Vue.ref(true)
  const currentSessionMaxTurns = Vue.ref(5)
  const goalModeDefault = Vue.ref(true)
  const maxTurnsDefault = Vue.ref(5)
  const enableMarkdownDefault = Vue.ref(true)
  const currentTodoPlan = Vue.ref(options.todoPlan ?? null)
  const profileSettingSubmitting = Vue.ref(false)
  const reasoningSettingSubmitting = Vue.ref(false)
  const agentSettingSubmitting = Vue.ref(false)
  const toolOutputSettingSubmitting = Vue.ref(false)
  const guidanceSubmitting = Vue.ref(false)
  const sessionsPanelOpen = Vue.ref(false)
  const moreOptionsVisible = Vue.ref(false)
  const moreOptionsOverlayActive = Vue.ref(false)
  const todoExpanded = Vue.ref(false)
  const chatMainRef = Vue.ref(options.chatMainRef ?? { name: 'chat-main' })
  const deferredContentSessionId = Vue.ref(null)
  const uploadFileList = Vue.ref([])
  const profiles = Vue.ref([])
  const currentUid = Vue.ref(null)
  const profilesLoading = Vue.ref(false)

  const optionCalls = []
  const saveCalls = []
  const sendCalls = []
  const stopCalls = []
  const selectCalls = []
  const createCalls = []
  const enqueueCalls = []
  const errors = []
  const behavior = {
    options: options.optionsBehavior ?? (async () => optionResponse(
      options.reasoningOptions ?? ['low', 'high'],
      options.reasoningDefaults ?? ['high']
    )),
    save: options.saveBehavior ?? (async () => undefined),
    send: options.sendBehavior ?? (async () => undefined)
  }
  const api = {
    sessionReasoningOptions: params => {
      optionCalls.push(params)
      return behavior.options(params, optionCalls.length - 1)
    },
    updateSessionSetting: (sessionId, payload) => {
      saveCalls.push({ sessionId, payload })
      return behavior.save(sessionId, payload, saveCalls.length - 1)
    },
    createGuidance: async () => ({ data: { data: { id: 'guidance-1', db_id: 'guidance-1' } } })
  }
  const tFunction = (key, params = {}) => {
    const raw = enChat[key.startsWith('chat.') ? key.slice('chat.'.length) : key] ?? key
    return Object.entries(params).reduce(
      (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
      raw
    )
  }
  const messageApi = {
    error: message => errors.push({ type: 'error', message }),
    warning: message => errors.push({ type: 'warning', message }),
    success: message => errors.push({ type: 'success', message })
  }
  const selectSession = session => {
    selectCalls.push(session)
    currentSessionId.value = session?.session_id ?? null
  }
  const createNewSession = () => {
    createCalls.push([])
  }
  const loadSessions = () => undefined
  const stopReply = (...args) => stopCalls.push(args)
  const originalSend = (...args) => {
    sendCalls.push(args)
    return behavior.send(...args)
  }
  const chat = {
    activeCollapse,
    attachments,
    currentSession,
    currentSessionGoalMode,
    currentSessionId,
    currentSessionMaxTurns,
    currentSessionReasoningEffort,
    currentSessionShowReasoning,
    currentSessionShowToolCalls,
    currentTodoPlan,
    enableMarkdownDefault,
    enqueueMessage: (...args) => enqueueCalls.push(args),
    inputMsg,
    isCurrentSessionReadOnly,
    isReplyRunning,
    isStopping,
    loading,
    messages,
    newSessionProfileOverrideId,
    sessions,
    send: originalSend,
    stopReply
  }
  const sessionEngaged = Vue.computed(() => (
    Boolean(currentSessionId.value) || loading.value || messages.value.length > 0
  ))
  const currentSessionInfo = currentSession
  const currentSessionEnableMarkdown = Vue.ref(true)
  const currentSessionProfileDisplayId = Vue.computed(() => currentSession.value?.profile_override_id ?? null)
  const currentSessionProfileOptions = Vue.ref([])
  const currentSessionProfilePlaceholder = Vue.ref('')
  const groupedSessions = Vue.ref([])
  const isWsModeComputed = Vue.computed(() => transportMode.value === 'ws')
  const renderedMessages = messages
  const renderedInitialHistoryLoaded = Vue.ref(false)
  const chatInputBox = { getBoundingClientRect: () => ({ width: 300 }) }

  const lifecycle = scope.run(() => new Function(
    'ref',
    'onMounted',
    'onUnmounted',
    'ResizeObserver',
    `${implementation.lifecycleSource}
return { chatInputBoxRef, reasoningDropdownRef, reasoningDropdownVisible, moreOptionsWidth }`
  )(
    Vue.ref,
    callback => mountedCallbacks.push(callback),
    callback => unmountedCallbacks.push(callback),
    MockResizeObserver
  ))
  lifecycle.chatInputBoxRef.value = options.useInputBox === false ? null : Vue.markRaw(chatInputBox)
  t.after(() => unmountedCallbacks.forEach(callback => callback()))

  const reasoningState = scope.run(() => new Function(
    'computed',
    'watch',
    'useSessionReasoning',
    'currentSessionId',
    'currentSession',
    'newSessionProfileOverrideId',
    'currentSessionReasoningEffort',
    'sessions',
    'loading',
    'chatApi',
    'ElMessage',
    't',
    'reasoningDropdownRef',
    'reasoningDropdownVisible',
    'moreOptionsVisible',
    'moreOptionsOverlayActive',
    'todoExpanded',
    'profileSettingSubmitting',
    `${implementation.reasoningSource}
return {
  profileOverrideId,
  reasoningOptions,
  reasoningDefaults,
  reasoningOptionsLoading,
  reasoningOptionsFailed,
  reasoningEffortSubmitting,
  loadReasoningOptions,
  updateSessionReasoningEffort,
  reasoningDisplayValue,
  reasoningTriggerLabel,
  reasoningProfileDefaultHint,
  reasoningDropdownDisabled,
  closeReasoningDropdown,
  handleMoreOptionsAfterLeave,
  handleReasoningVisibleChange
}`
  )(
    Vue.computed,
    Vue.watch,
    useSessionReasoning,
    currentSessionId,
    currentSession,
    newSessionProfileOverrideId,
    currentSessionReasoningEffort,
    sessions,
    loading,
    api,
    messageApi,
    tFunction,
    lifecycle.reasoningDropdownRef,
    lifecycle.reasoningDropdownVisible,
    moreOptionsVisible,
    moreOptionsOverlayActive,
    todoExpanded,
    profileSettingSubmitting
  ))

  const actionButtonLabel = scope.run(() => new Function(
    'computed',
    'isStopping',
    'isReplyRunning',
    't',
    `${implementation.actionButtonLabelSource}
return actionButtonLabel`
  )(Vue.computed, isStopping, isReplyRunning, tFunction))

  const sendState = scope.run(() => new Function(
    'isStopping',
    'modeSettingSubmitting',
    'agentSettingSubmitting',
    'reasoningEffortSubmitting',
    'isCurrentSessionReadOnly',
    'inputMsg',
    'currentSessionId',
    'guidanceSubmitting',
    'chatApi',
    'messages',
    'messageList',
    'nextTick',
    'ElMessage',
    't',
    'loadSessions',
    'loading',
    'attachments',
    'uploadFileList',
    'chat',
    'originalSend',
    `${implementation.sendSource}
return { send }`
  )(
    isStopping,
    modeSettingSubmitting,
    agentSettingSubmitting,
    reasoningState.reasoningEffortSubmitting,
    isCurrentSessionReadOnly,
    inputMsg,
    currentSessionId,
    guidanceSubmitting,
    api,
    messages,
    messageList,
    Vue.nextTick,
    messageApi,
    tFunction,
    loadSessions,
    loading,
    attachments,
    uploadFileList,
    chat,
    originalSend
  ))

  const auditState = scope.run(() => new Function(
    'isCurrentSessionReadOnly',
    'loading',
    'agentSettingSubmitting',
    'reasoningEffortSubmitting',
    'inputMsg',
    'attachments',
    'originalSend',
    't',
    `${implementation.auditSource}
return { handleAuditDecision }`
  )(
    isCurrentSessionReadOnly,
    loading,
    agentSettingSubmitting,
    reasoningState.reasoningEffortSubmitting,
    inputMsg,
    attachments,
    originalSend,
    tFunction
  ))

  const closeSessionsPanel = () => {
    sessionsPanelOpen.value = false
  }
  const navigationState = scope.run(() => new Function(
    'closeSessionsPanel',
    'moreOptionsVisible',
    'closeReasoningDropdown',
    'shouldDeferChatContent',
    'sessionEngaged',
    'deferredContentSessionId',
    'selectSession',
    'createNewSession',
    `${implementation.navigationSource}
return { handleSelectSession, handleCreateNewSession }`
  )(
    closeSessionsPanel,
    moreOptionsVisible,
    reasoningState.closeReasoningDropdown,
    () => false,
    sessionEngaged,
    deferredContentSessionId,
    selectSession,
    createNewSession
  ))

  const contextTarget = {
    $t: tFunction,
    SESSION_MAX_TURNS_UPPER_BOUND,
    activeCollapse,
    actionButtonLabel,
    agentSettingSubmitting,
    attachments,
    chatInputBoxRef: lifecycle.chatInputBoxRef,
    chatMainRef,
    collapsedGroups: new Set(),
    currentSessionEnableMarkdown,
    currentSessionGoalMode,
    currentSessionId,
    currentSessionInfo,
    currentSessionMaxTurns,
    currentSessionProfileDisplayId,
    currentSessionProfileOptions,
    currentSessionProfilePlaceholder,
    currentSessionReasoningEffort,
    currentSessionShowReasoning,
    currentSessionShowToolCalls,
    currentTodoPlan,
    deferredContentSessionId,
    formatProfileOptionLabel: () => '',
    formatSessionSource: source => source,
    groupedSessions,
    guidanceSubmitting,
    handleAuditDecision: auditState.handleAuditDecision,
    handleCreateNewSession: navigationState.handleCreateNewSession,
    handleDeleteSession: () => {},
    handleModeChange: () => {},
    handleMoreOptionsAfterLeave: reasoningState.handleMoreOptionsAfterLeave,
    handlePaste: () => {},
    handleReasoningVisibleChange: reasoningState.handleReasoningVisibleChange,
    handleRemoveCustomFile: () => {},
    handleSelectSession: navigationState.handleSelectSession,
    handleSessionGroupBeforeEnter: () => {},
    handleSessionGroupBeforeLeave: () => {},
    handleSessionGroupEnter: () => {},
    handleSessionGroupLeave: () => {},
    historyLoading,
    inputMsg,
    isContextSummarizing,
    isCurrentSessionReadOnly,
    isReplyRunning,
    isStopping,
    isWsModeComputed,
    loadSessions,
    loading,
    llmRequestMetadata,
    messageList,
    messages,
    modeSettingSubmitting,
    moreOptionsOverlayActive,
    moreOptionsVisible,
    moreOptionsWidth: lifecycle.moreOptionsWidth,
    newSessionProfileOverrideId,
    openUploadPicker: () => {},
    profilesLoading,
    profileSettingSubmitting,
    reasoningDisplayValue: reasoningState.reasoningDisplayValue,
    reasoningDropdownDisabled: reasoningState.reasoningDropdownDisabled,
    reasoningDropdownRef: lifecycle.reasoningDropdownRef,
    reasoningDropdownVisible: lifecycle.reasoningDropdownVisible,
    reasoningEffortSubmitting: reasoningState.reasoningEffortSubmitting,
    reasoningOptions: reasoningState.reasoningOptions,
    reasoningOptionsFailed: reasoningState.reasoningOptionsFailed,
    reasoningOptionsLoading: reasoningState.reasoningOptionsLoading,
    reasoningProfileDefaultHint: reasoningState.reasoningProfileDefaultHint,
    reasoningSettingSubmitting,
    reasoningTriggerLabel: reasoningState.reasoningTriggerLabel,
    renderedInitialHistoryLoaded,
    renderedMessages,
    send: sendState.send,
    sessionEngaged,
    sessions,
    sessionsLoading,
    sessionsPanelOpen,
    showReasoning: currentSessionShowReasoning,
    stopReply,
    todoExpanded,
    toggleGroup: () => {},
    toggleMarkdown: () => {},
    toggleSessionsPanel: () => {},
    toolOutputSettingSubmitting,
    transportModeChangeBlocked,
    typingSessionId,
    updateSessionGoalMode: () => {},
    updateSessionMaxTurns: () => {},
    updateSessionProfileOverride: () => {},
    updateSessionReasoningEffort: reasoningState.updateSessionReasoningEffort,
    updateSessionShowReasoning: () => {},
    updateSessionShowToolCalls: () => {},
    uploadFileList,
    uploadTriggerRef: Vue.ref(null),
    handleUpload: () => {}
  }
  const context = Vue.proxyRefs(contextTarget)
  const render = () => implementation.render(context, [])
  const nodes = () => vnodesOf(render())
  const node = predicate => nodes().find(predicate)
  const reasoningDropdown = () => node(value => value.type?.name === 'el-dropdown')
  const reasoningTrigger = () => node(value => value.type === 'button' && hasClass(value.props?.class, 'session-reasoning-trigger'))
  const morePopover = () => node(value => value.type?.name === 'el-popover')
  const todoPanel = () => node(value => value.type?.name === 'SessionTodoPanel')
  const actionButton = () => node(value => value.type?.name === 'el-button' && hasClass(value.props?.class, 'action-btn'))

  return {
    actionButton,
    behavior,
    chatMainRef,
    context,
    currentSessionId,
    currentSessionReasoningEffort,
    draftReasoningEffort,
    errors,
    lifecycle,
    moreOptionsOverlayActive,
    moreOptionsVisible,
    morePopover,
    navigationState,
    node,
    nodes,
    observers,
    optionCalls,
    profileSettingSubmitting,
    reasoningDropdown,
    reasoningState,
    reasoningTrigger,
    saveCalls,
    selectCalls,
    sendCalls,
    sessions,
    stopCalls,
    todoExpanded,
    todoPanel,
    unmountedCallbacks,
    createCalls,
    inputMsg,
    isReplyRunning,
    loading,
    currentSessionShowReasoning,
    currentSessionShowToolCalls
  }
}

const dropdownItems = harness => harness.nodes().filter(node => node.type?.name === 'el-dropdown-item')

const rowSequence = harness => {
  const row = harness.node(node => node.type === 'div' && hasClass(node.props?.class, 'chat-input-row'))
  assert.ok(row, 'chat input row should be rendered')
  return row.children
    .filter(Vue.isVNode)
    .map(node => {
      if (node.type?.name === 'el-input') return 'input'
      if (node.type?.name === 'el-dropdown') return 'reasoning-dropdown'
      if (hasClass(node.props?.class, 'action-btn-container')) return 'action'
      return null
    })
    .filter(Boolean)
}

test('ChatView renders the reasoning menu in the input row with live candidates and labels', async t => {
  const implementation = await loadImplementation()
  const longEffort = 'provider-specific-long-effort'
  const harness = await createHarness(t, implementation, {
    sessions: [createSession('A', longEffort)],
    currentSessionId: 'A',
    reasoningOptions: ['low', 'high'],
    reasoningDefaults: ['high']
  })
  await flushMicrotasks()

  assert.deepEqual(rowSequence(harness), ['input', 'reasoning-dropdown', 'action'])

  const dropdown = harness.reasoningDropdown()
  const morePopover = harness.morePopover()
  assert.ok(dropdown)
  assert.ok(morePopover)
  assert.equal(dropdown.props.trigger, 'click')
  assert.equal(dropdown.props.placement, 'top-end')
  assert.equal(dropdown.props['show-arrow'], false)
  assert.deepEqual(dropdown.props['popper-options'], {
    modifiers: [{ name: 'flip', options: { fallbackPlacements: ['top-start'] } }]
  })
  assert.strictEqual(dropdown.props['append-to'], harness.chatMainRef.value)
  assert.equal(dropdown.props['popper-style'].zIndex, 11)
  assert.equal(morePopover.props['popper-style'].zIndex, 14)
  assert.strictEqual(morePopover.props['append-to'], harness.chatMainRef.value)

  assert.equal(harness.reasoningTrigger().props.title, `${enChat.reasoning_effort}: ${longEffort}`)
  assert.equal(harness.reasoningTrigger().props['aria-label'], `${enChat.reasoning_effort}: ${longEffort}`)
  assert.deepEqual(harness.reasoningState.reasoningOptions.value, ['low', 'high', longEffort])
  assert.deepEqual(harness.reasoningState.reasoningDefaults.value, ['high'])
  assert.equal(harness.currentSessionReasoningEffort.value, longEffort)

  let items = dropdownItems(harness)
  assert.equal(items[0].props.command, null)
  assert.equal(hasClass(items[0].props.class, 'is-selected'), false)
  assert.deepEqual(items.slice(1).map(item => item.props.command), ['low', 'high', longEffort])
  assert.equal(hasClass(items[1].props.class, 'is-selected'), false)
  assert.equal(hasClass(items[2].props.class, 'is-selected'), false)
  assert.equal(hasClass(items.at(-1).props.class, 'is-selected'), true)

  harness.sessions.value[0].reasoning_effort = null
  assert.equal(harness.reasoningTrigger().props.title, `${enChat.reasoning_effort}: ${enChat.reasoning_effort_follow_profile}`)
  items = dropdownItems(harness)
  assert.equal(items[0].props.command, null)
  assert.ok(hasClass(items[0].props.class, 'is-selected'))
})

test('ChatView exposes loading and failed candidate states, retries on reopen, and does not readonly-disable external selection', async t => {
  const implementation = await loadImplementation()
  const firstLoad = deferred()
  const harness = await createHarness(t, implementation, {
    sessions: [createSession('external', null, { source: 'weixin-openclaw' })],
    currentSessionId: 'external',
    readOnly: true,
    optionsBehavior: () => firstLoad.promise
  })

  assert.equal(harness.reasoningState.reasoningOptionsLoading.value, true)
  assert.match(textOf(dropdownItems(harness).at(-1)), new RegExp(enChat.reasoning_effort_loading.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  harness.reasoningState.handleReasoningVisibleChange(true)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, true)
  assert.equal(harness.optionCalls.length, 1)

  firstLoad.resolve(optionResponse(['low'], ['low']))
  await flushMicrotasks()
  assert.equal(harness.reasoningState.reasoningOptionsLoading.value, false)
  assert.equal(harness.reasoningState.reasoningOptionsFailed.value, false)
  assert.equal(harness.reasoningDropdown().props.disabled, false)
  assert.equal(harness.reasoningTrigger().props.disabled, false)

  const loadError = new Error('candidate load failed')
  harness.behavior.options = async () => {
    throw loadError
  }
  harness.reasoningState.handleReasoningVisibleChange(false)
  harness.reasoningState.handleReasoningVisibleChange(true)
  await flushMicrotasks()
  assert.equal(harness.reasoningState.reasoningOptionsFailed.value, true)
  assert.equal(harness.reasoningState.reasoningOptionsLoading.value, false)
  assert.match(textOf(dropdownItems(harness).at(-1)), new RegExp(enChat.reasoning_effort_load_failed.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  assert.equal(harness.errors.at(-1).message, loadError.message)

  harness.behavior.options = async () => optionResponse(['medium'], ['medium'])
  harness.reasoningState.handleReasoningVisibleChange(false)
  harness.reasoningState.handleReasoningVisibleChange(true)
  await flushMicrotasks()
  assert.equal(harness.reasoningState.reasoningOptionsFailed.value, false)
  assert.deepEqual(harness.reasoningState.reasoningOptions.value, ['medium'])
  assert.equal(harness.optionCalls.length, 3)
})

test('ChatView keeps thought, more-options, and todo overlays mutually exclusive through leave transitions', async t => {
  const implementation = await loadImplementation()
  const harness = await createHarness(t, implementation, {
    sessions: [createSession('A', null)],
    currentSessionId: 'A',
    todoPlan: { revision: 1, todos: [{ content: 'inspect', status: 'in_progress' }] }
  })
  await flushMicrotasks()

  let closeCalls = 0
  harness.lifecycle.reasoningDropdownRef.value = { handleClose: () => { closeCalls += 1 } }
  harness.reasoningState.handleReasoningVisibleChange(true)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, true)

  harness.moreOptionsVisible.value = true
  assert.equal(closeCalls, 1)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.moreOptionsOverlayActive.value, true)
  assert.equal(harness.reasoningTrigger().props['aria-expanded'], false)
  assert.equal(harness.reasoningDropdown().props.disabled, true)
  assert.deepEqual(harness.reasoningDropdown().props['popper-style'], {
    zIndex: 11,
    visibility: 'hidden',
    pointerEvents: 'none'
  })
  assert.equal(harness.morePopover().props.visible, true)
  assert.equal(harness.todoPanel().props.suppressed, true)

  harness.moreOptionsVisible.value = false
  assert.equal(harness.moreOptionsOverlayActive.value, true)
  assert.equal(harness.reasoningState.reasoningDropdownDisabled.value, true)
  assert.equal(harness.morePopover().props.visible, false)
  harness.reasoningState.handleMoreOptionsAfterLeave()
  assert.equal(harness.moreOptionsOverlayActive.value, false)
  assert.equal(harness.reasoningState.reasoningDropdownDisabled.value, false)

  harness.moreOptionsVisible.value = true
  harness.moreOptionsVisible.value = false
  harness.moreOptionsVisible.value = true
  harness.reasoningState.handleMoreOptionsAfterLeave()
  assert.equal(harness.moreOptionsOverlayActive.value, true)
  assert.equal(harness.moreOptionsVisible.value, true)

  const callsBeforeBlockedOpen = harness.optionCalls.length
  harness.lifecycle.reasoningDropdownVisible.value = false
  harness.reasoningState.handleReasoningVisibleChange(true)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.optionCalls.length, callsBeforeBlockedOpen)

  harness.moreOptionsVisible.value = false
  harness.reasoningState.handleMoreOptionsAfterLeave()
  const todo = harness.todoPanel()
  todo.props.onExpandedChange(true)
  assert.equal(harness.todoExpanded.value, true)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.moreOptionsVisible.value, false)
  assert.equal(harness.todoPanel().props.suppressed, false)

  harness.reasoningState.handleReasoningVisibleChange(true)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.todoExpanded.value, true)
  todo.props.onExpandedChange(false)
  assert.equal(harness.todoExpanded.value, false)
  harness.reasoningState.handleReasoningVisibleChange(true)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, true)
})

test('ChatView blocks the reasoning control while loading, saving reasoning, or saving the profile', async t => {
  const implementation = await loadImplementation()
  const harness = await createHarness(t, implementation, {
    sessions: [createSession('A', 'low')],
    currentSessionId: 'A'
  })
  await flushMicrotasks()

  for (const flag of [harness.loading, harness.reasoningState.reasoningEffortSubmitting, harness.profileSettingSubmitting]) {
    flag.value = true
    assert.equal(harness.reasoningState.reasoningDropdownDisabled.value, true)
    assert.equal(harness.reasoningDropdown().props.disabled, true)
    assert.equal(harness.reasoningTrigger().props.disabled, true)
    assert.equal(harness.reasoningDropdown().props['popper-style'].visibility, 'hidden')
    flag.value = false
    assert.equal(harness.reasoningState.reasoningDropdownDisabled.value, false)
  }
})

test('ChatView closes stale thought state on identity changes and repeated navigation', async t => {
  const implementation = await loadImplementation()
  const harness = await createHarness(t, implementation, {
    sessions: [
      createSession('A', null),
      createSession('B', null)
    ],
    currentSessionId: 'A'
  })
  await flushMicrotasks()

  harness.moreOptionsVisible.value = true
  harness.lifecycle.reasoningDropdownVisible.value = true
  harness.currentSessionId.value = 'B'
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.moreOptionsVisible.value, false)

  harness.currentSessionId.value = 'A'
  await flushMicrotasks()
  harness.moreOptionsVisible.value = true
  harness.lifecycle.reasoningDropdownVisible.value = true
  harness.sessions.value[0].profile_override_id = 'profile-2'
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.moreOptionsVisible.value, true)

  harness.lifecycle.reasoningDropdownVisible.value = true
  harness.navigationState.handleSelectSession(harness.sessions.value[0])
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.moreOptionsVisible.value, false)
  assert.equal(harness.selectCalls.length, 1)

  harness.currentSessionId.value = null
  harness.moreOptionsVisible.value = true
  harness.lifecycle.reasoningDropdownVisible.value = true
  harness.navigationState.handleCreateNewSession()
  assert.equal(harness.currentSessionId.value, null)
  assert.equal(harness.lifecycle.reasoningDropdownVisible.value, false)
  assert.equal(harness.moreOptionsVisible.value, false)
  assert.equal(harness.createCalls.length, 1)
})

test('ChatView routes dropdown commands through real saves and clears the new-session draft', async t => {
  const implementation = await loadImplementation()
  const session = createSession('A', 'low')
  const harness = await createHarness(t, implementation, {
    sessions: [session],
    currentSessionId: 'A'
  })
  await flushMicrotasks()

  const pendingSave = deferred()
  harness.behavior.save = () => pendingSave.promise
  const save = harness.reasoningDropdown().props.onCommand('high')
  await flushMicrotasks()
  assert.equal(harness.reasoningState.reasoningEffortSubmitting.value, true)
  assert.deepEqual(harness.saveCalls.at(-1), {
    sessionId: 'A',
    payload: { reasoning_effort: 'high' }
  })
  assert.equal(harness.sessions.value[0].reasoning_effort, 'low')
  pendingSave.resolve()
  await save
  assert.equal(harness.sessions.value[0].reasoning_effort, 'high')
  assert.equal(harness.currentSessionReasoningEffort.value, 'high')

  harness.behavior.save = async () => undefined
  await harness.reasoningDropdown().props.onCommand(null)
  assert.deepEqual(harness.saveCalls.at(-1), {
    sessionId: 'A',
    payload: { reasoning_effort: null }
  })
  assert.equal(harness.sessions.value[0].reasoning_effort, null)

  harness.sessions.value[0].reasoning_effort = 'medium'
  harness.behavior.save = async () => {
    throw new Error('save failed')
  }
  await harness.reasoningDropdown().props.onCommand('high')
  assert.equal(harness.sessions.value[0].reasoning_effort, 'medium')
  assert.equal(harness.currentSessionReasoningEffort.value, 'medium')
  assert.equal(harness.errors.at(-1).message, 'save failed')

  const savesBeforeDraft = harness.saveCalls.length
  harness.currentSessionId.value = null
  harness.draftReasoningEffort.value = 'draft-only'
  await flushMicrotasks()
  await harness.reasoningDropdown().props.onCommand(null)
  assert.equal(harness.draftReasoningEffort.value, null)
  assert.equal(harness.currentSessionReasoningEffort.value, null)
  assert.equal(harness.saveCalls.length, savesBeforeDraft)
})

test('ChatView guards normal and audit sends during reasoning save while keeping stop available', async t => {
  const implementation = await loadImplementation()
  const harness = await createHarness(t, implementation, {
    sessions: [createSession('A', 'low')],
    currentSessionId: 'A',
    inputMsg: 'draft message'
  })
  await flushMicrotasks()

  const pendingSave = deferred()
  harness.behavior.save = () => pendingSave.promise
  const save = harness.reasoningState.updateSessionReasoningEffort('high')
  await flushMicrotasks()
  assert.equal(harness.reasoningState.reasoningEffortSubmitting.value, true)

  await harness.context.send()
  assert.equal(harness.inputMsg.value, 'draft message')
  assert.equal(harness.sendCalls.length, 0)
  await harness.context.handleAuditDecision({ decision: 'approve' })
  assert.equal(harness.inputMsg.value, 'draft message')
  assert.equal(harness.sendCalls.length, 0)

  let actionButton = harness.actionButton()
  assert.equal(actionButton.props.disabled, true)
  harness.isReplyRunning.value = true
  actionButton = harness.actionButton()
  assert.equal(actionButton.props.disabled, false)
  actionButton.props.onClick()
  assert.equal(harness.stopCalls.length, 1)
  assert.equal(harness.sendCalls.length, 0)

  pendingSave.resolve()
  await save
  harness.isReplyRunning.value = false
  await harness.context.send()
  assert.equal(harness.sendCalls.length, 1)
  await harness.context.handleAuditDecision({ decision: 'approve' })
  assert.equal(harness.inputMsg.value, enChat.audit_approve_word)
  assert.equal(harness.sendCalls.length, 2)
})
