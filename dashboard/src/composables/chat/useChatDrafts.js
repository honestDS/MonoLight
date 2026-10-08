import { onScopeDispose, watch } from 'vue'

export const CHAT_DRAFT_STORAGE_PREFIX = 'monolight_chat_draft:v1:'

const isValidUid = value => typeof value === 'string' && value.trim().length > 0

const isValidSessionId = value => (
  value === null || (typeof value === 'string' && value.trim().length > 0)
)

const isValidNonNullSessionId = value => (
  typeof value === 'string' && value.trim().length > 0
)

const normalizeUid = value => isValidUid(value) ? value : null

const resolveStorage = storage => {
  if (storage !== undefined) return storage
  try {
    return globalThis.localStorage
  } catch {
    return null
  }
}

const getDraftStorageKey = (uid, sessionId) => (
  `${CHAT_DRAFT_STORAGE_PREFIX}${JSON.stringify([uid, sessionId])}`
)

export function useChatDrafts({
  inputMsg,
  currentUid,
  currentSessionId,
  isCurrentSessionReadOnly,
  storage
}) {
  let activeUid = normalizeUid(currentUid.value)
  let activeSessionId = currentSessionId.value
  let activeReadOnly = Boolean(isCurrentSessionReadOnly.value)
  let restoring = false
  let identityResolved = isValidUid(activeUid)
  let disposed = false
  const pendingDrafts = new Map()

  const isCurrentSessionActive = () => (
    isValidSessionId(activeSessionId)
    && isValidSessionId(currentSessionId.value)
    && activeSessionId === currentSessionId.value
  )

  const persistDraft = (sessionId, text) => {
    if (disposed) return false
    if (!isValidSessionId(sessionId) || typeof text !== 'string') return false

    pendingDrafts.set(sessionId, text)
    if (!isValidUid(activeUid)) return false

    const target = resolveStorage(storage)
    if (!target) return false

    try {
      const key = getDraftStorageKey(activeUid, sessionId)
      if (text === '') {
        target.removeItem(key)
      } else {
        target.setItem(key, text)
      }
      pendingDrafts.delete(sessionId)
      return true
    } catch {
      return false
    }
  }

  const readDraft = sessionId => {
    if (!isValidSessionId(sessionId)) return ''
    if (pendingDrafts.has(sessionId)) {
      const pending = pendingDrafts.get(sessionId)
      return typeof pending === 'string' ? pending : ''
    }
    if (!isValidUid(activeUid)) return ''

    const target = resolveStorage(storage)
    if (!target) return ''

    try {
      const value = target.getItem(getDraftStorageKey(activeUid, sessionId))
      return typeof value === 'string' ? value : ''
    } catch {
      return ''
    }
  }

  const restoreBoundSession = () => {
    if (disposed) return
    restoring = true
    try {
      inputMsg.value = activeReadOnly ? '' : readDraft(activeSessionId)
    } finally {
      restoring = false
    }
  }

  const restoreSession = () => {
    if (disposed) return
    activeSessionId = currentSessionId.value
    activeReadOnly = Boolean(isCurrentSessionReadOnly.value)
    restoreBoundSession()
  }

  const removeDraft = sessionId => {
    persistDraft(sessionId, '')
  }

  const adoptSessionId = sessionId => {
    if (disposed) return
    if (!isValidNonNullSessionId(sessionId) || sessionId !== currentSessionId.value) return
    if (activeSessionId === sessionId) return

    if (activeSessionId === null) {
      const capturedInput = inputMsg.value
      removeDraft(null)
      activeSessionId = sessionId
      activeReadOnly = Boolean(isCurrentSessionReadOnly.value)
      if (!activeReadOnly && typeof capturedInput === 'string') {
        persistDraft(sessionId, capturedInput)
      }
      return
    }

    restoreSession()
  }

  watch(
    inputMsg,
    value => {
      if (
        restoring
        || activeReadOnly
        || isCurrentSessionReadOnly.value
        || !isCurrentSessionActive()
        || activeUid !== normalizeUid(currentUid.value)
        || typeof value !== 'string'
      ) return
      persistDraft(activeSessionId, value)
    },
    { flush: 'sync' }
  )

  watch(
    currentUid,
    value => {
      const nextUid = normalizeUid(value)
      if (nextUid === activeUid) return

      if (!identityResolved && isValidUid(nextUid)) {
        activeUid = nextUid
        identityResolved = true
        for (const [sessionId, text] of new Map(pendingDrafts)) {
          persistDraft(sessionId, text)
        }
        if (isCurrentSessionActive()) restoreBoundSession()
        return
      }

      pendingDrafts.clear()
      activeUid = nextUid
      if (isValidUid(nextUid)) identityResolved = true
      if (isCurrentSessionActive()) restoreBoundSession()
    },
    { flush: 'sync', immediate: true }
  )

  watch(
    isCurrentSessionReadOnly,
    value => {
      if (!isCurrentSessionActive()) return
      const nextReadOnly = Boolean(value)
      if (activeReadOnly === nextReadOnly) return
      activeReadOnly = nextReadOnly
      restoreBoundSession()
    },
    { flush: 'sync' }
  )

  onScopeDispose(() => {
    disposed = true
    pendingDrafts.clear()
  })

  restoreBoundSession()

  return {
    restoreSession,
    adoptSessionId,
    removeDraft
  }
}
