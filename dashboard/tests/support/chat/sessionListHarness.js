import assert from 'node:assert/strict'
import { createHttpReplyPolling } from '../../../src/composables/chat/httpReplyPolling.js'
import { getIncrementalHistoryCursor, syncIncrementalHistory } from '../../../src/composables/chat/historyIncrementalSync.js'

const loadSessionLoadingModule = () => import('../../../src/composables/chat/sessionListLoading.js')

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createManualTimerQueue = () => {
  const pending = []
  const handles = []
  const cancelled = []

  const schedule = (callback, delay) => {
    const handle = { callback, delay }
    pending.push(handle)
    handles.push(handle)
    return handle
  }

  const cancel = handle => {
    cancelled.push(handle)
    const index = pending.indexOf(handle)
    if (index !== -1) pending.splice(index, 1)
  }

  const tick = async () => {
    const handle = pending.shift()
    assert.ok(handle, 'expected a scheduled timer')
    return handle.callback()
  }

  return { pending, handles, cancelled, schedule, cancel, tick }
}

const createIncrementalHistoryMerger = ({
  fetchPage,
  pageSize = 50,
  maxPages = 4,
  initialLastMessageIds = new Map(),
  getMessages = () => []
}) => {
  const lastMessageIds = new Map(initialLastMessageIds)
  const mergedBySession = new Map()

  const mergeLatestHistory = async (sessionId, isCurrentSync) => {
    const result = await syncIncrementalHistory({
      initialAfterId: getIncrementalHistoryCursor(
        getMessages(sessionId),
        lastMessageIds.get(sessionId)
      ),
      pageSize,
      maxPages,
      fetchPage: request => fetchPage(sessionId, request),
      mergePage: page => {
        const merged = mergedBySession.get(sessionId) || []
        merged.push(...page)
        mergedBySession.set(sessionId, merged)
      },
      isCurrent: isCurrentSync
    })

    if (!result.cancelled && isCurrentSync()) {
      lastMessageIds.set(sessionId, result.lastMessageId)
    }
    return result
  }

  return { lastMessageIds, mergedBySession, mergeLatestHistory }
}

const createExternalHttpPollingFixture = ({
  currentSessionId = 'external',
  readOnly = true,
  transportMode = 'http',
  initialHistoryLoaded = true,
  loading = false,
  messages = [],
  initialLastMessageIds = new Map(),
  fetchPage = () => [],
  pageSize = 50,
  maxPages = 4,
  pendingHttpRequests = new Map()
} = {}) => {
  const state = {
    transport: { transportMode: { value: transportMode } },
    sessionManager: {
      currentSessionId: { value: currentSessionId },
      sessions: { value: [] }
    },
    isCurrentSessionReadOnly: { value: readOnly },
    chatState: {
      messages: { value: messages },
      loading: { value: loading }
    },
    initialHistoryLoaded: { value: initialHistoryLoaded },
    pendingHttpRequests
  }
  const merger = createIncrementalHistoryMerger({
    fetchPage,
    pageSize,
    maxPages,
    initialLastMessageIds,
    getMessages: () => state.chatState.messages.value
  })
  const failWritable = action => () => assert.fail(`unexpected writable HTTP action: ${action}`)
  const polling = createHttpReplyPolling({
    transport: state.transport,
    sessionManager: state.sessionManager,
    isCurrentSessionReadOnly: state.isCurrentSessionReadOnly,
    chatState: state.chatState,
    initialHistoryLoaded: state.initialHistoryLoaded,
    pendingHttpRequests: state.pendingHttpRequests,
    workLifecycleTracker: {
      finishWorkLifecycle: failWritable('finishWorkLifecycle')
    },
    applyTodoTransportPayload: failWritable('applyTodoTransportPayload'),
    updateLlmRequestMetadata: failWritable('updateLlmRequestMetadata'),
    startHttpHistoryBackgroundTaskSync: failWritable('startHttpHistoryBackgroundTaskSync'),
    applyNonStreamSessionEvents: failWritable('applyNonStreamSessionEvents'),
    shouldProcessCompletedWork: failWritable('shouldProcessCompletedWork'),
    processAiResponse: failWritable('processAiResponse'),
    messageProcessor: {
      processStreamError: failWritable('processStreamError')
    },
    api: {
      replyWorkStatus: failWritable('replyWorkStatus')
    },
    mergeLatestSessionHistory: failWritable('mergeLatestSessionHistory'),
    getHistoryCursor: sessionId => getIncrementalHistoryCursor(
      state.chatState.messages.value,
      merger.lastMessageIds.get(sessionId)
    ),
    mergeIncrementalSessionHistory: merger.mergeLatestHistory,
    reportError: failWritable('reportError'),
    translate: key => key
  })

  return { ...state, merger, polling }
}

export {
  createDeferred,
  createExternalHttpPollingFixture,
  createManualTimerQueue,
  loadSessionLoadingModule
}
