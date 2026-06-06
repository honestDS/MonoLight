/**
 * 公共工具函数
 * 提供在各组件间复用的工具函数
 */

// 时间戳格式化
export const formatTimestamp = (timestamp) => {
  if (!timestamp) return ''
  try {
    const date = new Date(timestamp * 1000)  // 转换为毫秒
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    const seconds = String(date.getSeconds()).padStart(2, '0')
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`
  } catch {
    return ''
  }
}

// 内容截取（获取简短预览）
export const getShortContent = (content, maxLength = 100) => {
  if (!content) return '暂无内容'
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

// 获取工具调用名称
export const getToolName = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'object' && content !== null) {
      const tc = content.tool_calls?.[0]
      return tc?.name || tc?.function?.name || '未知工具'
    }
    const parsed = JSON.parse(content)
    const tc = parsed.tool_calls?.[0]
    return tc?.name || tc?.function?.name || '未知工具'
  } catch {
    return '未知工具'
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
      return content.tool_call_id ? `ID: ${content.tool_call_id.substring(0, 20) + '...'}` : '工具返回'
    }
    const parsed = JSON.parse(content)
    return parsed.tool_call_id ? `ID: ${parsed.tool_call_id.substring(0, 20) + '...'}` : '工具返回'
  } catch {
    return '工具返回'
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