import assert from 'node:assert/strict'

export const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

export const flushMicrotasks = async (turns = 8) => {
  for (let index = 0; index < turns; index += 1) {
    await Promise.resolve()
  }
}

export const makeEntry = (subagentId, workId, overrides = {}) => ({
  name: `Agent ${subagentId}`,
  subagent_id: subagentId,
  work_id: workId,
  latest_post_id: null,
  status: 'running',
  completed: false,
  created_at: '2026-11-14T12:00:00Z',
  updated_at: '2026-11-14T12:01:00Z',
  stage: 'working',
  preview: `preview-${subagentId}`,
  ...overrides
})

export const makePost = (id, workId, overrides = {}) => ({
  id,
  work_id: workId,
  stage: 'working',
  content: `post-${id}`,
  created_at: '2026-11-14T12:02:00Z',
  ...overrides
})

export const makeRound = workId => ({
  work_id: workId,
  created_at: `2026-11-14T12:${String(workId).padStart(2, '0')}:00Z`
})

const makeDetail = (overrides = {}) => {
  const workId = overrides.work_id ?? 101
  return {
    subagent_id: 'agent-a',
    name: 'Agent agent-a',
    work_id: workId,
    status: 'running',
    completed: false,
    rounds: [{ work_id: workId, created_at: '2026-11-14T12:00:00Z' }],
    posts: [],
    next_before_round_id: null,
    next_before_post_id: null,
    result_text: null,
    reason: null,
    ended_without_reply: false,
    ...overrides
  }
}

export const listResponse = ({
  profile_id: profileId = 1,
  active = [],
  recent = [],
  has_more_recent: hasMoreRecent = false,
  active_page: activePage = 1,
  active_page_size: activePageSize = 20,
  active_total: activeTotal = active.length,
  selected = null,
  lease_recheck_in_ms: leaseRecheckInMs
} = {}) => ({
  data: {
    data: {
      profile_id: profileId,
      active_page: activePage,
      active_page_size: activePageSize,
      active_total: activeTotal,
      active,
      recent,
      has_more_recent: hasMoreRecent,
      selected,
      lease_recheck_in_ms: leaseRecheckInMs
    }
  }
})

export const detailResponse = overrides => ({
  data: {
    data: makeDetail(overrides)
  }
})

export const postPageResponse = ({ work_id: workId, posts, next_before_post_id: nextBeforePostId }) => ({
  data: {
    data: {
      work_id: workId,
      posts,
      next_before_post_id: nextBeforePostId
    }
  }
})

export const roundPageResponse = ({ rounds, next_before_round_id: nextBeforeRoundId }) => ({
  data: {
    data: {
      rounds,
      next_before_round_id: nextBeforeRoundId
    }
  }
})

export const createApi = () => {
  const listQueue = []
  const detailQueue = []
  const listCalls = []
  const detailCalls = []
  let activeListRequests = 0
  let maxActiveListRequests = 0

  const take = (queue, fallback) => {
    const value = queue.length > 0 ? queue.shift() : fallback
    return value && typeof value.then === 'function' ? value : Promise.resolve(value)
  }

  const api = {
    sessionSubagents: (sessionId, params, config) => {
      const requestConfig = config || {}
      listCalls.push({
        sessionId,
        params: { ...params },
        signal: requestConfig.signal
      })
      activeListRequests += 1
      maxActiveListRequests = Math.max(maxActiveListRequests, activeListRequests)
      return take(listQueue, listResponse()).finally(() => {
        activeListRequests -= 1
      })
    },
    sessionSubagentDetail: (sessionId, subagentId, params, config) => {
      const requestConfig = config || {}
      detailCalls.push({
        sessionId,
        subagentId,
        params: { ...params },
        signal: requestConfig.signal
      })
      return take(detailQueue, detailResponse({ subagent_id: subagentId }))
    }
  }

  return {
    api,
    listCalls,
    detailCalls,
    queueList: value => listQueue.push(value),
    queueDetail: value => detailQueue.push(value),
    getMaxActiveListRequests: () => maxActiveListRequests
  }
}

export const assertScopeCleared = board => {
  assert.deepEqual(board.active.value, [])
  assert.deepEqual(board.recent.value, [])
  assert.equal(board.hasMoreRecent.value, false)
  assert.equal(board.failed.value, false)
  assert.equal(board.detail.value, null)
  assert.equal(board.detailLoading.value, false)
  assert.equal(board.detailFailed.value, false)
  assert.equal(board.selectedId.value, null)
  assert.equal(board.selectedWorkId.value, null)
  assert.equal(board.nextBeforePostId.value, null)
}
