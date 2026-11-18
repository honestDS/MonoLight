import { ref } from 'vue'
import { createAbortableTaskManager } from '../../utils/requestTaskManager.js'

const LIST_TASK_KEY = 'session-subagent-list'
const DETAIL_TASK_KEY = 'session-subagent-detail'
const ROUND_TASK_KEY = 'session-subagent-rounds'
const DEFAULT_RECENT_LIMIT = 10
const MAX_RECENT_LIMIT = 100

const isValidRevision = revision => Number.isSafeInteger(revision) && revision >= 0
const isValidLeaseDelay = delay => Number.isSafeInteger(delay) && delay > 0
const normalizeActivityProfileId = profileId => Number.isSafeInteger(profileId) && profileId >= 0
  ? profileId
  : null

const mergePosts = (...postGroups) => {
  const postsById = new Map()
  for (const posts of postGroups) {
    for (const post of posts) postsById.set(post.id, post)
  }
  return [...postsById.values()].sort((left, right) => left.id - right.id)
}

const mergeRounds = (...roundGroups) => {
  const roundsById = new Map()
  for (const rounds of roundGroups) {
    for (const round of rounds) roundsById.set(round.work_id, round)
  }
  return [...roundsById.values()].sort((left, right) => right.work_id - left.work_id)
}

const hasPostOverlap = (oldPosts, newPosts) => {
  const oldIds = new Set(oldPosts.map(post => post.id))
  return newPosts.some(post => oldIds.has(post.id))
}

const captureDetailEntry = entry => entry
  ? {
      work_id: entry.work_id,
      latest_post_id: entry.latest_post_id,
      status: entry.status,
      completed: entry.completed
    }
  : null

const sameDetailEntry = (left, right) => Boolean(left && right)
  && left.work_id === right.work_id
  && left.latest_post_id === right.latest_post_id
  && left.status === right.status
  && left.completed === right.completed

export const createSessionSubagentBoard = ({
  api
} = {}) => {
  const active = ref([])
  const activePage = ref(1)
  const activePageSize = ref(0)
  const activeTotal = ref(0)
  const recent = ref([])
  const hasMoreRecent = ref(false)
  const recentLimit = ref(DEFAULT_RECENT_LIMIT)
  const loading = ref(false)
  const failed = ref(false)
  const detail = ref(null)
  const detailLoading = ref(false)
  const detailFailed = ref(false)
  const selectedId = ref(null)
  const selectedWorkId = ref(null)
  const selectedEntry = ref(null)
  const rounds = ref([])
  const nextBeforeRoundId = ref(null)
  const roundsLoading = ref(false)
  const roundsFailed = ref(false)
  const nextBeforePostId = ref(null)
  const taskManager = createAbortableTaskManager()
  let sessionId = null
  let profileKey = null
  let lastProfileId
  let enabled = false
  let disposed = false
  let observedRevision = null
  let observedActivityProfileId = null
  let loadedRevision = null
  let leaseRecheckAt = null
  let pendingDetailRefresh = false
  let lastDetailEntry = null
  const canRequest = () => !disposed && enabled && Boolean(sessionId)
  const activityNeedsRefresh = () => observedRevision !== loadedRevision
    || (observedActivityProfileId !== null
      && observedActivityProfileId !== (lastProfileId ?? 0))
    || failed.value
    || (leaseRecheckAt !== null && Date.now() >= leaseRecheckAt)

  const resetDetail = () => {
    taskManager.cancel(DETAIL_TASK_KEY)
    pendingDetailRefresh = false
    lastDetailEntry = null
    detail.value = null
    detailLoading.value = false
    detailFailed.value = false
    nextBeforePostId.value = null
  }
  const resetRounds = () => {
    taskManager.cancel(ROUND_TASK_KEY)
    rounds.value = []
    nextBeforeRoundId.value = null
    roundsLoading.value = false
    roundsFailed.value = false
  }
  const clearSelection = () => {
    resetDetail()
    resetRounds()
    selectedId.value = null
    selectedWorkId.value = null
    selectedEntry.value = null
  }
  const findListedSubagent = id => [...active.value, ...recent.value]
    .find(entry => entry.subagent_id === id) || null

  const applyLatestRounds = payload => {
    const incomingRounds = payload.rounds
    const incomingCursor = payload.next_before_round_id
    const currentRounds = rounds.value
    const hasGap = currentRounds.length === 0
      || (incomingCursor !== null
        && incomingRounds.at(-1)?.work_id > currentRounds[0]?.work_id)

    rounds.value = mergeRounds(currentRounds, incomingRounds)
    if (hasGap) nextBeforeRoundId.value = incomingCursor
  }

  const applyLatestDetail = payload => {
    applyLatestRounds(payload)
    const incomingCursor = payload.next_before_post_id
    const current = detail.value

    if (!current || current.work_id !== payload.work_id) {
      detail.value = {
        ...payload,
        posts: mergePosts(payload.posts),
        next_before_post_id: incomingCursor
      }
      nextBeforePostId.value = incomingCursor
      return
    }

    const currentPosts = current.posts
    const disconnected = !hasPostOverlap(currentPosts, payload.posts) && incomingCursor !== null
    let posts
    let cursor
    if (disconnected) {
      posts = mergePosts(payload.posts)
      cursor = incomingCursor
    } else {
      posts = mergePosts(currentPosts, payload.posts)
      cursor = nextBeforePostId.value
    }

    detail.value = { ...payload, posts, next_before_post_id: cursor }
    nextBeforePostId.value = cursor
  }

  const applyOlderDetail = payload => {
    const posts = mergePosts(detail.value.posts, payload.posts)
    detail.value = {
      ...detail.value,
      posts,
      next_before_post_id: payload.next_before_post_id
    }
    nextBeforePostId.value = payload.next_before_post_id
  }

  const requestDetail = async (beforePostId = null) => {
    if (!canRequest() || selectedId.value === null) return
    if (taskManager.isRunning(DETAIL_TASK_KEY)) return

    const requestSessionId = sessionId
    const requestSubagentId = selectedId.value
    const requestWorkId = selectedWorkId.value
    const requestEntry = beforePostId === null
      && (requestWorkId === null || requestWorkId === selectedEntry.value?.work_id)
      ? captureDetailEntry(selectedEntry.value)
      : null
    const detailWorkId = beforePostId !== null && requestWorkId === null
      ? detail.value.work_id
      : requestWorkId
    const params = {}
    if (detailWorkId !== null) params.work_id = detailWorkId
    if (beforePostId !== null) params.before_post_id = beforePostId

    const token = taskManager.begin(DETAIL_TASK_KEY)
    if (!token) return
    detailLoading.value = true
    detailFailed.value = false

    try {
      const response = await api.sessionSubagentDetail(requestSessionId, requestSubagentId, params, { signal: token.signal })
      if (!taskManager.isCurrent(token)) return

      const payload = response.data.data
      if (beforePostId !== null) {
        applyOlderDetail(payload)
        return
      }
      applyLatestDetail(payload)
      if (requestEntry && payload.work_id === requestEntry.work_id) {
        lastDetailEntry = requestEntry
      }
    } catch {
      if (taskManager.isCurrent(token)) detailFailed.value = true
    } finally {
      if (!taskManager.isCurrent(token)) return
      taskManager.finish(token)
      detailLoading.value = false
      const shouldRefresh = pendingDetailRefresh
      pendingDetailRefresh = false
      if (shouldRefresh) void requestDetail()
    }
  }

  const applyList = (payload, requestSelection, forceDetail) => {
    const profileChanged = lastProfileId !== undefined && lastProfileId !== payload.profile_id
    lastProfileId = payload.profile_id
    if (profileChanged) clearSelection()

    activePage.value = payload.active_page
    activePageSize.value = payload.active_page_size
    activeTotal.value = payload.active_total
    active.value = [...payload.active]
    recent.value = [...payload.recent]
    hasMoreRecent.value = payload.has_more_recent

    const selectionMatches = requestSelection !== null && selectedId.value === requestSelection
    if (!selectionMatches) return
    if (!payload.selected) {
      clearSelection()
      return
    }

    const selectionChanged = !sameDetailEntry(selectedEntry.value, payload.selected)
    selectedEntry.value = payload.selected
    const followsLatest = selectedWorkId.value === null
      || selectedWorkId.value === selectedEntry.value?.work_id
    const needsInitialDetail = detail.value === null
      && (followsLatest || !detailFailed.value)
    const latestChanged = followsLatest
      && detail.value !== null
      && !sameDetailEntry(lastDetailEntry, payload.selected)
    const supersededRunningDetail = !followsLatest
      && detail.value?.completed === false
      && !detailFailed.value
    const shouldRefresh = forceDetail
      || needsInitialDetail
      || latestChanged
      || supersededRunningDetail
      || (followsLatest && detailFailed.value)
    if (!shouldRefresh) return
    if (taskManager.isRunning(DETAIL_TASK_KEY)) {
      if (forceDetail || selectionChanged) pendingDetailRefresh = true
      return
    }
    void requestDetail()
  }

  const refresh = async ({ page = activePage.value, forceDetail = true } = {}) => {
    if (!canRequest()) {
      return
    }
    if (taskManager.isRunning(LIST_TASK_KEY)) return

    const requestRevision = observedRevision
    const requestActivityProfileId = observedActivityProfileId
    const token = taskManager.begin(LIST_TASK_KEY)
    if (!token) return
    const requestSelection = selectedId.value
    loading.value = true
    const params = {
      recent_limit: recentLimit.value,
      active_page: page
    }
    if (requestSelection !== null) params.selected_subagent_id = requestSelection

    try {
      const response = await api.sessionSubagents(sessionId, params, { signal: token.signal })
      if (!taskManager.isCurrent(token)) return
      const payload = response.data.data
      applyList(payload, requestSelection, forceDetail)
      failed.value = false
      loadedRevision = requestRevision
      const leaseDelay = payload.lease_recheck_in_ms
      leaseRecheckAt = isValidLeaseDelay(leaseDelay)
        ? Date.now() + leaseDelay
        : null
    } catch {
      if (taskManager.isCurrent(token)) failed.value = true
    } finally {
      if (!taskManager.isCurrent(token)) return
      taskManager.finish(token)
      loading.value = false
      const profileChangedInFlight = observedActivityProfileId !== requestActivityProfileId
        && observedActivityProfileId !== null
        && observedActivityProfileId !== (lastProfileId ?? 0)
      if (
        observedRevision !== requestRevision
        || profileChangedInFlight
        || params.recent_limit !== recentLimit.value
      ) {
        void refresh({ forceDetail: false })
      }
    }
  }

  const refreshDetail = () => requestDetail()

  const observeActivity = (observedSessionId, revision, activityProfileId = null) => {
    if (disposed || sessionId === null || observedSessionId !== sessionId || !isValidRevision(revision)) {
      return false
    }
    if (observedRevision !== null && revision < observedRevision) return false

    observedRevision = revision
    observedActivityProfileId = normalizeActivityProfileId(activityProfileId)
    if (!enabled) return true
    if (activityNeedsRefresh()) {
      void refresh({ forceDetail: false })
    } else if (
      detailFailed.value
      && selectedId.value !== null
      && (selectedWorkId.value === null || selectedWorkId.value === selectedEntry.value?.work_id)
    ) {
      void requestDetail()
    }
    return true
  }

  const setScope = (nextSessionId, nextProfileKey, revision = null, activityProfileId = null) => {
    if (disposed) return
    if (sessionId === nextSessionId && profileKey === nextProfileKey) return

    taskManager.invalidate()
    sessionId = nextSessionId
    profileKey = nextProfileKey
    lastProfileId = undefined
    observedRevision = isValidRevision(revision) ? revision : null
    observedActivityProfileId = normalizeActivityProfileId(activityProfileId)
    loadedRevision = null
    leaseRecheckAt = null

    active.value = []
    activePage.value = 1
    activePageSize.value = 0
    activeTotal.value = 0
    recent.value = []
    hasMoreRecent.value = false
    recentLimit.value = DEFAULT_RECENT_LIMIT
    loading.value = false
    failed.value = false
    clearSelection()

    if (enabled && sessionId) void refresh({ forceDetail: false })
  }

  const setEnabled = value => {
    if (disposed) return
    const nextEnabled = Boolean(value)
    if (enabled === nextEnabled) return

    taskManager.invalidate()
    enabled = nextEnabled
    if (!enabled) {
      pendingDetailRefresh = false
      loading.value = false
      detailLoading.value = false
      roundsLoading.value = false
      return
    }
    if (sessionId) void refresh({ forceDetail: false })
  }

  const selectSubagent = id => {
    if (disposed) return
    const entry = findListedSubagent(id)
    if (!entry) {
      clearSelection()
      return
    }

    const newSubagent = selectedId.value !== entry.subagent_id
    if (newSubagent) resetRounds()
    resetDetail()
    selectedId.value = entry.subagent_id
    selectedWorkId.value = null
    selectedEntry.value = entry
    return requestDetail()
  }

  const selectRound = workId => {
    if (disposed || selectedId.value === null) return
    resetDetail()
    selectedWorkId.value = workId
    return requestDetail()
  }

  const loadOlderPosts = () => {
    if (!canRequest() || selectedId.value === null || detail.value === null) return
    if (nextBeforePostId.value === null) return
    return requestDetail(nextBeforePostId.value)
  }

  const loadOlderRounds = async () => {
    if (!canRequest() || selectedId.value === null) return
    const requestCursor = nextBeforeRoundId.value
    if (requestCursor === null || taskManager.isRunning(ROUND_TASK_KEY)) return

    const token = taskManager.begin(ROUND_TASK_KEY)
    if (!token) return
    roundsLoading.value = true
    roundsFailed.value = false

    const requestStillApplies = () => taskManager.isCurrent(token)
      && requestCursor === nextBeforeRoundId.value

    try {
      const response = await api.sessionSubagentDetail(
        sessionId,
        selectedId.value,
        { before_round_id: requestCursor },
        { signal: token.signal }
      )
      if (!requestStillApplies()) return

      const payload = response.data.data
      rounds.value = mergeRounds(rounds.value, payload.rounds)
      nextBeforeRoundId.value = payload.next_before_round_id
      roundsFailed.value = false
    } catch {
      if (requestStillApplies()) roundsFailed.value = true
    } finally {
      if (!taskManager.isCurrent(token)) return
      taskManager.finish(token)
      roundsLoading.value = false
    }
  }

  const loadMoreRecent = () => {
    if (disposed) return
    recentLimit.value = Math.min(MAX_RECENT_LIMIT, recentLimit.value + 10)
    return refresh({ forceDetail: false })
  }

  const changeActivePage = page => {
    if (disposed) return
    taskManager.cancel(LIST_TASK_KEY)
    return refresh({ page, forceDetail: false })
  }

  const dispose = () => {
    if (disposed) return
    disposed = true
    pendingDetailRefresh = false
    taskManager.invalidate()
    loading.value = false
    detailLoading.value = false
    roundsLoading.value = false
  }

  return {
    active, activePage, activePageSize, activeTotal, recent, hasMoreRecent, recentLimit, loading, failed,
    detail, detailLoading, detailFailed, selectedId, selectedWorkId, selectedEntry,
    rounds, nextBeforeRoundId, roundsLoading, roundsFailed, nextBeforePostId,
    setScope, setEnabled, observeActivity, refresh, refreshDetail, selectSubagent, selectRound, loadOlderPosts,
    loadOlderRounds, loadMoreRecent, changeActivePage, clearSelection, dispose
  }
}
