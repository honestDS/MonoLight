// 公共工具函数
import i18n from '../i18n'
import {
  findAssistantResponseReplacementIndex,
  getMessageDbId,
  isPlainAssistantResponse,
  mergeAssistantResponse,
  mergeAssistantResponseIntoList
} from './assistantResponseIdentity'

export {
  findAssistantResponseReplacementIndex,
  getMessageDbId,
  isPlainAssistantResponse,
  mergeAssistantResponse,
  mergeAssistantResponseIntoList
}

const t = (key, ...args) => i18n.global.t(key, ...args)

const formatDateObject = (date, fallback = '') => {
  try {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return fallback
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    const seconds = String(date.getSeconds()).padStart(2, '0')
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`
  } catch {
    return fallback
  }
}

export const normalizeMessageContent = (content) => {
  if (typeof content === 'string') {
    try {
      return JSON.parse(content)
    } catch {
      return content
    }
  }
  return content
}

export const getToolResultCallId = (message) => {
  const content = normalizeMessageContent(message?.content)
  const isToolResultMessage = message?.type === 'tool_result' || message?.role === 'tool' || content?.role === 'tool'
  if (!isToolResultMessage) return null
  const toolCallId = content?.tool_call_id || message?.tool_call_id
  return toolCallId === null || toolCallId === undefined || toolCallId === '' ? null : String(toolCallId)
}

export const findMessageReplacementIndex = (messages, incomingMessage) => {
  const incomingDbId = getMessageDbId(incomingMessage)
  if (incomingDbId !== null) {
    const dbIdIndex = messages.findIndex(message => getMessageDbId(message) === incomingDbId)
    if (dbIdIndex !== -1) return dbIdIndex
  }

  const incomingToolCallId = getToolResultCallId(incomingMessage)
  if (incomingToolCallId === null) return -1
  return messages.findIndex(message => getToolResultCallId(message) === incomingToolCallId)
}

export const mergeRemoteMessage = (localMessage, remoteMessage) => ({
  ...localMessage,
  ...remoteMessage,
  id: localMessage?.id ?? remoteMessage?.id,
  response_id: localMessage?.response_id ?? remoteMessage?.response_id,
  request_id: localMessage?.request_id ?? remoteMessage?.request_id,
  work_id: localMessage?.work_id ?? remoteMessage?.work_id,
  turn: localMessage?.turn ?? remoteMessage?.turn
})

export const mergeRemoteMessageIntoList = (messages, remoteMessage) => {
  const message = {
    ...remoteMessage,
    db_id: remoteMessage?.db_id ?? remoteMessage?.id
  }
  const replacementIndex = findMessageReplacementIndex(messages, message)
  if (replacementIndex === -1) return [...messages, message]
  return messages.map((item, index) => index === replacementIndex ? mergeRemoteMessage(item, message) : item)
}

const stableStringify = (value) => {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export const getMessageDedupeKeys = (message) => {
  const keys = new Set()
  const dbId = getMessageDbId(message)
  if (dbId) keys.add(`db:${dbId}`)

  const content = normalizeMessageContent(message?.content)
  const messageType = message?.type || message?.role || 'message'
  if (message?.response_id) keys.add(`response:${messageType}:${message.response_id}`)
  if (message?.work_id && message?.turn !== undefined && message?.turn !== null) {
    keys.add(`work_turn:${messageType}:${message.work_id}:${message.turn}`)
  }
  const toolCalls = content?.tool_calls || message?.tool_calls || []
  for (const toolCall of toolCalls) {
    const toolCallId = toolCall?.id || toolCall?.function?.id
    if (toolCallId) keys.add(`tool_call:${toolCallId}`)
  }

  const toolCallId = content?.tool_call_id || message?.tool_call_id
  if (toolCallId) keys.add(`tool_result:${toolCallId}`)

  if (keys.size === 0) {
    keys.add(`content:${messageType}:${stableStringify(content ?? '')}`)
  }
  return keys
}

// 时间戳格式化
export const formatTimestamp = (timestamp) => {
  if (!timestamp) return ''
  return formatDateObject(new Date(timestamp * 1000))
}

// 内容截取（获取简短预览）
export const getShortContent = (content, maxLength = 100) => {
  if (!content) return t('common.empty_content')
  return content.length > maxLength ? content.substring(0, maxLength) + '...' : content
}

// 判断是否为工具调用消息
export const isToolCall = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'object' && content !== null) {
      return content.role === 'assistant' && content.tool_calls && content.tool_calls.length > 0
    }
    if (typeof content === 'string') {
      const parsed = JSON.parse(content)
      return parsed.role === 'assistant' && parsed.tool_calls && parsed.tool_calls.length > 0
    }
    return false
  } catch {
    return false
  }
}

// 获取工具调用阶段同时返回的正文
export const getToolCallContent = (msg) => {
  try {
    const content = normalizeMessageContent(msg.content)
    if (!content || typeof content !== 'object') return ''
    return typeof content.content === 'string' ? content.content : ''
  } catch {
    return ''
  }
}

// 获取消息中的全部工具调用
export const getToolCalls = (msg) => {
  try {
    const content = normalizeMessageContent(msg?.content)
    const toolCalls = content?.tool_calls || msg?.tool_calls
    return Array.isArray(toolCalls) ? toolCalls : []
  } catch {
    return []
  }
}

export const getToolCallName = (toolCall) => {
  return toolCall?.name || toolCall?.function?.name || t('common.unknown_tool')
}

export const getToolCallArguments = (toolCall) => {
  const args = toolCall?.arguments ?? toolCall?.function?.arguments
  if (typeof args === 'string') {
    return args
  }
  return JSON.stringify(args ?? {}, null, 2)
}

// 保留单调用方法，兼容现有使用方
export const getToolName = (msg) => {
  return getToolCallName(getToolCalls(msg)[0])
}

export const getToolArguments = (msg) => {
  const toolCall = getToolCalls(msg)[0]
  return toolCall ? getToolCallArguments(toolCall) : msg?.content
}

// 判断是否为工具返回结果
export const isToolResult = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'object' && content !== null) {
      return content.role === 'tool'
    }
    if (typeof content === 'string') {
      const parsed = JSON.parse(content)
      return parsed.role === 'tool'
    }
    return false
  } catch {
    return false
  }
}

// 获取工具返回名称
export const getToolResultName = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'object' && content !== null) {
      return content.tool_call_id ? `ID: ${content.tool_call_id.substring(0, 20) + '...'}` : t('common.tool_result')
    }
    const parsed = JSON.parse(content)
    return parsed.tool_call_id ? `ID: ${parsed.tool_call_id.substring(0, 20) + '...'}` : t('common.tool_result')
  } catch {
    return t('common.tool_result')
  }
}

// 获取工具返回内容
export const getToolResultContent = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'object' && content !== null) {
      return content.content || ''
    }
    const parsed = JSON.parse(content)
    return parsed.content || ''
  } catch {
    return msg.content
  }
}

// 获取消息的时间戳（从 created_at 字段或解析的 JSON 中获取）
export const getMessageTimestamp = (msg) => {
  // 直接从消息对象获取
  if (msg.created_at) {
    return msg.created_at
  }
  // 从 JSON 解析的内容中获取
  try {
    if (typeof msg.content === 'string') {
      const parsed = JSON.parse(msg.content)
      return parsed.created_at
    }
  } catch {}
  return null
}

// 防抖函数
export const debounce = (fn, delay) => {
  let timer = null
  return function () {
    let context = this
    let args = arguments
    clearTimeout(timer)
    timer = setTimeout(function () {
      fn.apply(context, args)
    }, delay)
  }
}

// 格式化 ISO 字符串时间
export const formatTime = (isoString) => {
  if (!isoString) return ''
  return formatDateObject(new Date(isoString), isoString)
}
