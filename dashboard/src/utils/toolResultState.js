import { getMessageDbId } from './assistantResponseIdentity.js'

const hasIdentity = value => value !== null && value !== undefined && value !== ''

const toIdentity = value => value === null || value === undefined || value === '' ? null : String(value)

const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value)

const parseObjectContent = content => {
  if (typeof content === 'string') {
    try {
      const parsed = JSON.parse(content)
      return isRecord(parsed) ? parsed : null
    } catch {
      return null
    }
  }
  return isRecord(content) ? content : null
}

const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key)

export const getBackgroundToolResultPayload = message => {
  if (!isRecord(message) || message.type !== 'background_result') return null

  const payload = parseObjectContent(message.content)
  const toolCall = payload?.tool_call
  if (
    !payload
    || payload.type !== 'background_tool_result'
    || !hasOwn(payload, 'task')
    || payload.task === undefined
    || !isRecord(toolCall)
    || !hasIdentity(toolCall.id)
    || String(toolCall.id).trim() === ''
  ) return null

  return payload
}

export const getOriginalToolResultMessageId = message => {
  const payload = getBackgroundToolResultPayload(message)
  return payload ? toIdentity(payload.original_tool_result_message_id) : null
}

const isToolCallMessage = (message, content) => (
  message?.type === 'tool_call'
  || message?.role === 'tool_call'
  || content?.type === 'tool_call'
  || (content?.role === 'assistant' && Array.isArray(content.tool_calls))
)

const getOrdinaryToolResultPayload = message => {
  if (!isRecord(message) || message.type === 'background_result') return null

  const parsedContent = parseObjectContent(message.content)
  if (parsedContent?.role === 'tool') {
    if (isToolCallMessage(message, parsedContent)) return null
    return { wrapper: parsedContent, result: parsedContent.content }
  }

  if (
    (message.role !== 'tool' && message.type !== 'tool_result')
    || isToolCallMessage(message, parsedContent)
  ) return null

  return { wrapper: message, result: message.content }
}

export const getToolResultCallId = message => {
  const backgroundPayload = getBackgroundToolResultPayload(message)
  if (backgroundPayload) return toIdentity(backgroundPayload.tool_call.id)
  if (message?.type === 'background_result') return null

  const payload = getOrdinaryToolResultPayload(message)
  if (!payload) return null
  const toolCallId = hasIdentity(payload.wrapper.tool_call_id)
    ? payload.wrapper.tool_call_id
    : message?.tool_call_id
  return toIdentity(toolCallId)
}

const getToolResultInfo = message => {
  const backgroundPayload = getBackgroundToolResultPayload(message)
  if (backgroundPayload) {
    return {
      kind: 'background',
      callId: toIdentity(backgroundPayload.tool_call.id),
      originalId: getOriginalToolResultMessageId(message),
      dbId: getMessageDbId(message)
    }
  }

  if (message?.type === 'background_result') return null
  const payload = getOrdinaryToolResultPayload(message)
  if (!payload) return null

  return {
    kind: 'ordinary',
    callId: getToolResultCallId(message),
    originalId: null,
    dbId: getMessageDbId(message)
  }
}

const getPersistedFieldValue = message => {
  if (hasIdentity(message?.db_id)) return message.db_id
  if (hasIdentity(message?.message_id)) return message.message_id
  return typeof message?.id === 'number' ? message.id : undefined
}

const normalizeMessage = message => {
  if (!isRecord(message)) return message

  const normalized = { ...message }
  if (!hasIdentity(normalized.db_id)) {
    const persistedField = getPersistedFieldValue(message)
    if (persistedField !== undefined) normalized.db_id = persistedField
  }
  return normalized
}

const getPersistedVersion = message => {
  const dbId = getMessageDbId(message)
  if (dbId === null) return null

  const version = Number(dbId)
  return Number.isSafeInteger(version) && version > 0 ? version : null
}

const sameResultIdentity = (left, right) => (
  (left.callId !== null && right.callId !== null && left.callId === right.callId)
  || (left.dbId !== null && right.dbId !== null && left.dbId === right.dbId)
  || (left.originalId !== null && right.originalId !== null && left.originalId === right.originalId)
  || (left.kind === 'background' && left.originalId !== null && right.dbId !== null && left.originalId === right.dbId)
  || (right.kind === 'background' && right.originalId !== null && left.dbId !== null && right.originalId === left.dbId)
)

const getNestedToolResultContent = message => {
  const payload = getOrdinaryToolResultPayload(message)
  return payload ? parseObjectContent(payload.result) : null
}

export const isToolResultPending = message => {
  const info = getToolResultInfo(message)
  if (!info || info.kind !== 'ordinary') return false

  const result = getNestedToolResultContent(message)
  return result?.status === 'queued'
    && Number.isSafeInteger(result.task_id)
    && result.task_id > 0
}

const mergeMessageFields = (localMessage, remoteMessage, preserveLocalResult = false) => {
  const merged = { ...localMessage, ...remoteMessage }

  for (const field of ['id', 'response_id', 'request_id', 'work_id', 'turn']) {
    merged[field] = localMessage?.[field] ?? remoteMessage?.[field]
  }

  if (preserveLocalResult) {
    for (const field of ['role', 'type', 'content', 'tool_call']) {
      if (hasOwn(localMessage, field)) merged[field] = localMessage[field]
    }
  }

  const localPersistedId = getPersistedFieldValue(localMessage)
  const remotePersistedId = getPersistedFieldValue(remoteMessage)
  const persistedId = preserveLocalResult
    ? localPersistedId ?? remotePersistedId
    : remotePersistedId ?? localPersistedId
  if (persistedId !== undefined) merged.db_id = persistedId

  return merged
}

const mergeNormalizedToolResultMessage = (local, remote) => {
  const localInfo = getToolResultInfo(local)
  const remoteInfo = getToolResultInfo(remote)

  if (!localInfo || !remoteInfo) return mergeMessageFields(local, remote)

  if (localInfo.kind === 'background') {
    if (remoteInfo.kind === 'background') {
      const localVersion = getPersistedVersion(local)
      const remoteVersion = getPersistedVersion(remote)
      if (remoteVersion === null || (localVersion !== null && remoteVersion <= localVersion)) return local
    } else {
      return mergeMessageFields(local, remote, true)
    }
  }

  if (remoteInfo.kind !== 'background' && isToolResultPending(remote) && !isToolResultPending(local)) {
    return mergeMessageFields(local, remote, true)
  }

  return mergeMessageFields(local, remote)
}

export const mergeToolResultMessage = (localMessage, remoteMessage) => (
  mergeNormalizedToolResultMessage(normalizeMessage(localMessage), normalizeMessage(remoteMessage))
)

export const mergeToolResultIntoList = (messages, remoteMessage) => {
  const source = Array.isArray(messages) ? messages : []
  const remote = normalizeMessage(remoteMessage)
  const remoteInfo = getToolResultInfo(remote)
  if (!remoteInfo) return [...source, remote]

  const matchedIndexes = []
  source.forEach((message, index) => {
    const info = getToolResultInfo(message)
    if (info && sameResultIdentity(info, remoteInfo)) matchedIndexes.push(index)
  })

  if (matchedIndexes.length === 0) return [...source, remote]

  const replacementIndex = matchedIndexes[0]
  let mergedMessage = normalizeMessage(source[replacementIndex])
  for (const index of matchedIndexes.slice(1)) {
    mergedMessage = mergeNormalizedToolResultMessage(mergedMessage, normalizeMessage(source[index]))
  }
  mergedMessage = mergeNormalizedToolResultMessage(mergedMessage, remote)

  const duplicateIndexes = new Set(matchedIndexes.slice(1))
  return source
    .map((message, index) => index === replacementIndex ? mergedMessage : message)
    .filter((message, index) => !duplicateIndexes.has(index))
}

export const projectToolResultMessage = message => {
  const payload = getBackgroundToolResultPayload(message)
  if (!payload) return message

  const toolCall = payload.tool_call
  return {
    ...message,
    role: 'tool',
    type: 'tool_result',
    content: JSON.stringify({
      role: 'tool',
      tool_call_id: toolCall.id,
      content: typeof payload.task === 'string' ? payload.task : JSON.stringify(payload.task)
    }),
    tool_call: toolCall
  }
}
