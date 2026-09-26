export const getLatestPersistedMessageId = messages => {
  if (!Array.isArray(messages)) return 0
  return messages.reduce((latestId, message) => {
    const messageId = Number(message?.db_id)
    return Number.isSafeInteger(messageId) && messageId > latestId
      ? messageId
      : latestId
  }, 0)
}

export const getIncrementalHistoryCursor = (messages, lastSyncedMessageId) => {
  if (lastSyncedMessageId !== undefined && lastSyncedMessageId !== null) {
    const syncedMessageId = Number(lastSyncedMessageId)
    if (Number.isSafeInteger(syncedMessageId) && syncedMessageId >= 0) {
      return syncedMessageId
    }
  }
  return getLatestPersistedMessageId(messages)
}

export const syncIncrementalHistory = async ({
  initialAfterId,
  fetchPage,
  mergePage,
  pageSize = 50,
  maxPages = 4,
  isCurrent = () => true
}) => {
  if (typeof fetchPage !== 'function' || typeof mergePage !== 'function') {
    throw new TypeError('fetchPage and mergePage must be functions')
  }
  if (!Number.isSafeInteger(pageSize) || pageSize <= 0) {
    throw new RangeError('pageSize must be a positive integer')
  }
  if (!Number.isSafeInteger(maxPages) || maxPages <= 0) {
    throw new RangeError('maxPages must be a positive integer')
  }

  let afterId = Number.isSafeInteger(initialAfterId) && initialAfterId > 0
    ? initialAfterId
    : 0
  let pagesFetched = 0

  while (pagesFetched < maxPages) {
    if (!isCurrent()) {
      return { lastMessageId: afterId, hasMore: true, pagesFetched, cancelled: true }
    }
    const page = await fetchPage({ afterId, limit: pageSize })
    if (!isCurrent()) {
      return { lastMessageId: afterId, hasMore: true, pagesFetched, cancelled: true }
    }
    const messages = Array.isArray(page) ? page : []
    pagesFetched += 1
    if (messages.length === 0) {
      return { lastMessageId: afterId, hasMore: false, pagesFetched }
    }

    let pageLastMessageId = afterId
    for (const message of messages) {
      const messageId = Number(message?.id ?? message?.db_id)
      if (Number.isSafeInteger(messageId) && messageId > pageLastMessageId) {
        pageLastMessageId = messageId
      }
    }
    if (pageLastMessageId <= afterId) {
      throw new Error('Incremental history page did not advance the message cursor')
    }

    mergePage(messages)
    afterId = pageLastMessageId

    if (messages.length < pageSize) {
      return { lastMessageId: afterId, hasMore: false, pagesFetched }
    }
  }

  return { lastMessageId: afterId, hasMore: true, pagesFetched }
}
