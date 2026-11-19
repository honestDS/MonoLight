import * as Vue from 'vue'
import { createContextSummaryTracker } from '../../../src/composables/chat/contextSummaryTracker.js'
import { createHttpReplyPolling } from '../../../src/composables/chat/httpReplyPolling.js'
import { createReplyController } from '../../../src/composables/chat/replyControl.js'
import { createWorkLifecycleTracker } from '../../../src/composables/chat/workLifecycleTracker.js'
import { processStreamError } from '../../../src/composables/chat/terminalHistory.js'

export const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

export const createHarness = (options = {}) => {
  const sessions = Vue.ref(options.sessions ?? [])
  const currentSessionId = Vue.ref(options.currentSessionId ?? null)
  const transport = {
    transportMode: Vue.ref(options.transportMode ?? 'http')
  }
  const chatState = {
    messages: Vue.ref(options.messages ?? []),
    loading: Vue.ref(options.loading ?? false),
    inputMsg: Vue.ref(options.inputMsg ?? '')
  }
  const attachments = Vue.ref(options.attachments ?? [])
  const contextSummaryWorkKeys = Vue.ref(new Set())
  const contextSummaryRequestKeys = new Map()
  const contextSummaryTracker = createContextSummaryTracker()
  const workLifecycleTracker = createWorkLifecycleTracker()
  const initialHistoryLoaded = Vue.ref(options.initialHistoryLoaded ?? true)
  const pendingHttpRequests = new Map(options.pendingHttpRequests ?? [])
  const refreshCalls = []
  const historyMergeCalls = []
  const stopCalls = []
  const errors = []
  const streamErrors = []
  const replyWorkStatusCalls = []
  let sessionScopeActive = options.sessionScopeActive ?? true
  let stopBehavior = options.stopBehavior ?? (async () => undefined)
  let replyWorkStatusBehavior = options.replyWorkStatusBehavior ?? (() => null)

  const sessionManager = {
    sessions,
    currentSessionId,
    refreshSessionLoadingState: async () => {
      refreshCalls.push(currentSessionId.value)
    }
  }
  const currentSession = Vue.computed(() => (
    sessions.value.find(session => session.session_id === currentSessionId.value) || null
  ))
  const isCurrentSessionReadOnly = Vue.computed(() => {
    const source = currentSession.value?.source
    return Boolean(source && !['http', 'ws'].includes(source))
  })

  const reportError = message => {
    errors.push(message)
  }
  const translate = key => key
  const mergeLatestSessionHistory = async sessionId => {
    historyMergeCalls.push(sessionId)
  }
  const stopSession = sessionId => {
    stopCalls.push(sessionId)
    return typeof stopBehavior === 'function' ? stopBehavior(sessionId) : stopBehavior
  }

  const messageProcessor = {
    processStreamError: (...args) => {
      streamErrors.push(args)
      return processStreamError(...args)
    }
  }

  const api = {
    replyWorkStatus: workId => {
      replyWorkStatusCalls.push(workId)
      return typeof replyWorkStatusBehavior === 'function'
        ? replyWorkStatusBehavior(workId)
        : replyWorkStatusBehavior
    }
  }
  const httpPolling = createHttpReplyPolling({
    transport,
    sessionManager,
    isCurrentSessionReadOnly,
    chatState,
    initialHistoryLoaded,
    pendingHttpRequests,
    workLifecycleTracker,
    applyTodoTransportPayload: () => undefined,
    updateLlmRequestMetadata: () => undefined,
    startHttpHistoryBackgroundTaskSync: () => undefined,
    applyNonStreamSessionEvents: () => undefined,
    shouldProcessCompletedWork: () => false,
    processAiResponse: () => undefined,
    messageProcessor,
    api,
    mergeLatestSessionHistory,
    reportError,
    translate
  })
  const replyController = createReplyController({
    sessionManager,
    chatState,
    currentSession,
    isCurrentSessionReadOnly,
    pendingHttpRequests,
    contextSummaryWorkKeys,
    contextSummaryRequestKeys,
    workLifecycleTracker,
    contextSummaryTracker,
    stopSession,
    isSessionScopeActive: () => sessionScopeActive,
    resetHttpPollingState: httpPolling.resetHttpPollingState,
    mergeLatestSessionHistory,
    reportError,
    translate
  })

  return {
    ...replyController,
    ...httpPolling,
    attachments,
    api,
    chatState,
    contextSummaryRequestKeys,
    contextSummaryTracker,
    contextSummaryWorkKeys,
    currentSessionId,
    errors,
    historyMergeCalls,
    initialHistoryLoaded,
    isCurrentSessionReadOnly,
    messageProcessorErrors: streamErrors,
    pendingHttpRequests,
    refreshCalls,
    replyWorkStatusCalls,
    sessions,
    set replyWorkStatusBehavior(value) {
      replyWorkStatusBehavior = value
    },
    set sessionScopeActive(value) {
      sessionScopeActive = value
    },
    set stopBehavior(value) {
      stopBehavior = value
    },
    stopCalls,
    transport,
    workLifecycleTracker
  }
}
