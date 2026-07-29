const parseContentPayload = (content) => {
  if (content && typeof content === 'object') return content
  if (typeof content !== 'string') return null

  try {
    const payload = JSON.parse(content)
    return payload && typeof payload === 'object' ? payload : null
  } catch {
    return null
  }
}

const hasToolCalls = (message) => Boolean(
  message
  && typeof message === 'object'
  && Object.prototype.hasOwnProperty.call(message, 'tool_calls')
)

const isRequiredConfirmation = (message, content) => (
  message?.type === 'audit_confirmation'
  || content?.type === 'audit_confirmation'
  || message?.confirmation_mode === 'high_risk_override'
  || content?.confirmation_mode === 'high_risk_override'
)

export const isToolOutputMessage = (message) => {
  if (!message || typeof message !== 'object') return false
  if (message.type === 'tool_call' || message.type === 'tool_result' || message.role === 'tool' || hasToolCalls(message)) {
    return true
  }

  const content = parseContentPayload(message.content)
  return content?.role === 'tool' || content?.role === 'tool_calls' || hasToolCalls(content)
}

export const filterToolOutputMessages = (messages, showToolCalls = true) => {
  if (!Array.isArray(messages) || showToolCalls) return messages || []

  return messages.filter((message) => {
    const content = parseContentPayload(message?.content)
    return isRequiredConfirmation(message, content) || !isToolOutputMessage(message)
  })
}

export const filterResponseHistoryToolOutput = (response, showToolCalls = true) => {
  if (!response || typeof response !== 'object' || showToolCalls || !Array.isArray(response.history)) return response

  return {
    ...response,
    history: filterToolOutputMessages(response.history, false)
  }
}
