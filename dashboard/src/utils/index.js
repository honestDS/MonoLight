// 公共工具函数
import i18n from '../i18n'

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

const stableStringify = (value) => {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export const getMessageDedupeKeys = (message) => {
  const keys = new Set()
  const dbId = message?.db_id || (typeof message?.id === 'number' ? message.id : null)
  if (dbId) keys.add(`db:${dbId}`)

  const content = normalizeMessageContent(message?.content)
  const toolCalls = content?.tool_calls || message?.tool_calls || []
  for (const toolCall of toolCalls) {
    const toolCallId = toolCall?.id || toolCall?.function?.id
    if (toolCallId) keys.add(`tool_call:${toolCallId}`)
  }

  const toolCallId = content?.tool_call_id || message?.tool_call_id
  if (toolCallId) keys.add(`tool_result:${toolCallId}`)

  keys.add(`content:${message?.role || ''}:${stableStringify(content ?? '')}`)
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

// 获取工具调用名称
export const getToolName = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'object' && content !== null) {
      const tc = content.tool_calls?.[0]
      return tc?.name || tc?.function?.name || t('common.unknown_tool')
    }
    const parsed = JSON.parse(content)
    const tc = parsed.tool_calls?.[0]
    return tc?.name || tc?.function?.name || t('common.unknown_tool')
  } catch {
    return t('common.unknown_tool')
  }
}

// 获取工具调用参数
export const getToolArguments = (msg) => {
  try {
    const content = msg.content
    let tc = null
    if (typeof content === 'object' && content !== null) {
      tc = content.tool_calls?.[0]
    } else {
      const parsed = JSON.parse(content)
      tc = parsed.tool_calls?.[0]
    }
    const args = tc?.arguments || tc?.function?.arguments
    if (typeof args === 'string') {
      return args
    }
    return JSON.stringify(args, null, 2)
  } catch {
    return msg.content
  }
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
