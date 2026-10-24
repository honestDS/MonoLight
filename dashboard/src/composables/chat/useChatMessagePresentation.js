import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

import {
  getToolCallContent,
  getToolCallName,
  getToolCalls,
  getToolResultName,
  getToolResultContent,
  isToolCall,
  isToolResult
} from '../../utils/index.js'
import { truncateErrorMessage } from '../../utils/errorMessage.js'
import { resolveChatActivityNotice } from '../../utils/chatPresentation.js'

export function useChatMessagePresentation({ props, t }) {
  const tokenNumberFormatter = new Intl.NumberFormat()
  const percentNumberFormatter = new Intl.NumberFormat(undefined, {
    style: 'percent',
    maximumFractionDigits: 2
  })
  const formatTokenCount = value => Number.isFinite(value) ? tokenNumberFormatter.format(value) : '-'
  const formatCacheHitRate = value => Number.isFinite(value) ? percentNumberFormatter.format(value) : '-'
  const formatSessionTooltip = (info) => {
    if (!info) return ''
    return [
      `${t('chat.session_last_active')}: ${info.last_active || '-'}`,
      `${t('chat.session_created_at')}: ${info.created_at || '-'}`
    ].join('\n')
  }

  const md = new MarkdownIt({
    html: false,
    linkify: true,
    typographer: true,
    highlight(str, lang) {
      if (lang && hljs.getLanguage(lang)) {
        try {
          return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang, ignoreIllegals: true }).value}</code></pre>`
        } catch {}
      }
      return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
    }
  })
  md.linkify.set({ fuzzyLink: false })
  const defaultLinkOpenRender = md.renderer.rules.link_open || ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))
  md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
    tokens[idx].attrSet('class', 'el-link el-link--primary is-underline message-link')
    tokens[idx].attrSet('target', '_blank')
    tokens[idx].attrSet('rel', 'noopener noreferrer')
    return defaultLinkOpenRender(tokens, idx, options, env, self)
  }

  const renderMarkdown = text => md.render(text || '')
  const renderTextWithLinks = (text) => {
    if (!text) return []
    const matches = md.linkify.match(text)
    if (!matches) return [{ type: 'text', text }]

    const parts = []
    let lastIndex = 0
    matches.forEach((match) => {
      if (match.index > lastIndex) parts.push({ type: 'text', text: text.slice(lastIndex, match.index) })
      parts.push({ type: 'link', text: match.text, href: match.url })
      lastIndex = match.lastIndex
    })
    if (lastIndex < text.length) parts.push({ type: 'text', text: text.slice(lastIndex) })
    return parts
  }

  const getToolCallId = (toolCall, fallback) => toolCall?.id || toolCall?.function?.id || fallback
  const getToolResultCallId = (message) => {
    try {
      const content = typeof message?.content === 'string' ? JSON.parse(message.content) : message?.content
      return content?.tool_call_id || message?.tool_call_id || null
    } catch {
      return message?.tool_call_id || null
    }
  }

  const fallbackDisplayKeys = new WeakMap()
  let fallbackDisplayKeyIndex = 0
  const getMessageKey = (message, index) => String(message?.id ?? `${message?.role || 'message'}_${index}`)
  const getDisplayKey = (message) => {
    if (!message || typeof message !== 'object') return String(message)
    if (message._displayKey) return message._displayKey
    if (message?.id !== undefined && message?.id !== null) return String(message.id)
    if (!fallbackDisplayKeys.has(message)) fallbackDisplayKeys.set(message, `message_${fallbackDisplayKeyIndex++}`)
    return fallbackDisplayKeys.get(message)
  }
  const parseBackgroundResult = (message) => {
    if (message?.type !== 'background_result') return null
    try {
      const payload = typeof message.content === 'string' ? JSON.parse(message.content) : message.content
      if (
        !payload
        || typeof payload !== 'object'
        || Array.isArray(payload)
        || payload.type !== 'background_tool_result'
        || !payload.tool_call
        || typeof payload.tool_call !== 'object'
        || Array.isArray(payload.tool_call)
        || !Object.prototype.hasOwnProperty.call(payload, 'task')
        || payload.task === undefined
      ) return null
      return payload
    } catch {
      return null
    }
  }
  const analyzeMessage = (message) => ({
    isToolCall: isToolCall(message),
    isToolResult: isToolResult(message),
    backgroundPayload: parseBackgroundResult(message)
  })

  let cachedSources = []
  let cachedDisplayMessages = []
  let cachedOutputCounts = []
  let lastDisplayChangeWasPrepend = false

  const buildDisplayMessages = (messages) => {
    const prependCount = messages.length - cachedSources.length
    lastDisplayChangeWasPrepend = cachedSources.length > 0 && prependCount > 0 && cachedSources.every((message, index) => messages[index + prependCount] === message)

    let firstChanged = 0
    const sharedLength = Math.min(messages.length, cachedSources.length)
    while (firstChanged < sharedLength && messages[firstChanged] === cachedSources[firstChanged]) firstChanged += 1
    if (firstChanged === messages.length && firstChanged === cachedSources.length) return cachedDisplayMessages

    let rebuildStart = firstChanged
    const changedCurrent = messages[rebuildStart]
    const changedPrevious = cachedSources[rebuildStart]
    if ((changedCurrent && (isToolCall(changedCurrent) || isToolResult(changedCurrent))) || (changedPrevious && (isToolCall(changedPrevious) || isToolResult(changedPrevious)))) {
      while (rebuildStart > 0) {
        const previousMessage = messages[rebuildStart - 1]
        if (!isToolCall(previousMessage) && !isToolResult(previousMessage)) break
        rebuildStart -= 1
      }
    }

    const prefixLength = rebuildStart > 0 ? cachedOutputCounts[rebuildStart - 1] : 0
    const output = cachedDisplayMessages.slice(0, prefixLength)
    const outputCounts = cachedOutputCounts.slice(0, rebuildStart)
    let activeToolGroup = null

    for (let index = rebuildStart; index < messages.length; index += 1) {
      const message = messages[index]
      const messageKey = getMessageKey(message, index)
      const analysis = analyzeMessage(message)

      if (analysis.backgroundPayload) {
        const payload = analysis.backgroundPayload
        const toolCall = {
          id: getToolCallId(payload.tool_call, `${messageKey}_background`),
          name: payload.tool_call.name,
          arguments: payload.tool_call.arguments
        }
        const resultMessage = {
          ...message,
          role: 'tool',
          type: 'tool_result',
          content: JSON.stringify({
            role: 'tool',
            tool_call_id: toolCall.id,
            content: typeof payload.task === 'string' ? payload.task : JSON.stringify(payload.task)
          })
        }
        const pair = { id: toolCall.id, toolCall, resultMessage }
        Object.defineProperty(pair, '_backgroundResult', { value: true })
        output.push({
          id: `tool_group_${messageKey}`,
          _displayKey: `tool_group_${messageKey}`,
          type: 'tool_group',
          role: 'assistant',
          response_id: message.response_id || null,
          request_id: message.request_id,
          work_id: message.work_id,
          turn: message.turn,
          created_at: message.created_at,
          content: '',
          reasoning_content: message.reasoning_content || '',
          pairs: [pair],
          latestEvent: { type: 'result', pair }
        })
        activeToolGroup = null
        outputCounts[index] = output.length
        continue
      }

      if (analysis.isToolCall) {
        const responseId = message.response_id || null
        if (!activeToolGroup || !responseId || activeToolGroup.response_id !== responseId) {
          activeToolGroup = {
            id: `tool_group_${responseId || messageKey}`,
            _displayKey: `tool_group_${responseId || messageKey}`,
            type: 'tool_group',
            role: 'assistant',
            response_id: responseId,
            request_id: message.request_id,
            work_id: message.work_id,
            turn: message.turn,
            created_at: message.created_at,
            content: '',
            reasoning_content: message.reasoning_content || '',
            pairs: [],
            latestEvent: null
          }
          output.push(activeToolGroup)
        }

        const toolContent = getToolCallContent(message).trim()
        if (toolContent) activeToolGroup.content = activeToolGroup.content ? `${activeToolGroup.content}\n${toolContent}` : toolContent
        if (!activeToolGroup.reasoning_content && message.reasoning_content) activeToolGroup.reasoning_content = message.reasoning_content
        getToolCalls(message).forEach((toolCall, toolIndex) => {
          const toolCallId = getToolCallId(toolCall, `${messageKey}_${toolIndex}`)
          let pair = activeToolGroup.pairs.find(item => item.id === toolCallId)
          if (!pair) {
            pair = { id: toolCallId, toolCall, resultMessage: null }
            activeToolGroup.pairs.push(pair)
          } else {
            pair.toolCall = toolCall
          }
          activeToolGroup.latestEvent = { type: 'call', pair }
        })
        outputCounts[index] = output.length
        continue
      }

      if (analysis.isToolResult) {
        const toolCallId = getToolResultCallId(message)
        let targetGroup = activeToolGroup
        let pair = targetGroup?.pairs.find(item => item.id === toolCallId)
        if (!pair) {
          targetGroup = [...output].reverse().find(item => item.type === 'tool_group' && item.pairs.some(toolPair => toolPair.id === toolCallId))
          pair = targetGroup?.pairs.find(item => item.id === toolCallId)
        }
        if (!targetGroup) {
          targetGroup = {
            id: `tool_group_${message.response_id || messageKey}`,
            _displayKey: `tool_group_${message.response_id || messageKey}`,
            type: 'tool_group',
            role: 'assistant',
            response_id: message.response_id || null,
            request_id: message.request_id,
            created_at: message.created_at,
            content: '',
            pairs: [],
            latestEvent: null
          }
          output.push(targetGroup)
        }
        if (!pair) {
          pair = { id: toolCallId || messageKey, toolCall: null, resultMessage: null }
          targetGroup.pairs.push(pair)
        }
        pair.resultMessage = message
        targetGroup.latestEvent = { type: 'result', pair }
        activeToolGroup = targetGroup
        outputCounts[index] = output.length
        continue
      }

      activeToolGroup = null
      output.push(message)
      outputCounts[index] = output.length
    }

    cachedSources = [...messages]
    cachedDisplayMessages = output
    cachedOutputCounts = outputCounts
    return output
  }

  const displayMessages = computed(() => buildDisplayMessages(props.messages).filter(message => (
    message.role !== 'thinking' && (props.currentSessionShowReasoning || message.role !== 'reasoning')
  )))
  const hasActiveThinking = computed(() => props.messages.some(message => message?.role === 'thinking'))
  const activityNotice = computed(() => {
    const type = resolveChatActivityNotice({
      contextSummarizing: props.contextSummarizing,
      thinking: hasActiveThinking.value,
      historyLoading: props.historyLoading
    })
    if (!type) return null
    const textKeys = {
      context_summary: 'chat.context_summarizing',
      thinking: 'chat.thinking_in_progress',
      history_loading: 'chat.loading_history'
    }
    return { type, text: t(textKeys[type]) }
  })

  const getToolPairName = pair => pair.toolCall ? getToolCallName(pair.toolCall) : getToolResultName(pair.resultMessage)
  const getToolPairTitle = pair => t('chat.tool', { name: getToolPairName(pair) })
  const isToolPairReturned = (pair) => {
    if (!pair?.resultMessage) return false
    if (pair._backgroundResult) return true
    let payload = getToolResultContent(pair.resultMessage)
    if (typeof payload === 'string') {
      try {
        payload = JSON.parse(payload)
      } catch {}
    }
    return !(payload && typeof payload === 'object' && payload.status === 'queued' && Number.isSafeInteger(payload.task_id) && payload.task_id > 0)
  }
  const isToolGroupReturned = group => Boolean(group?.pairs?.length) && group.pairs.every(isToolPairReturned)
  const getToolGroupStateClass = group => isToolGroupReturned(group) ? 'is-result' : 'is-call'
  const getToolGroupStatus = group => t(isToolGroupReturned(group) ? 'chat.tool_status_returned' : 'chat.tool_status_calling')

  const parseAssistantFilesContent = (content) => {
    try {
      const parsed = typeof content === 'string' ? JSON.parse(content) : content
      return parsed?.type === 'assistant_files' ? parsed : null
    } catch {
      return null
    }
  }
  const getMessageText = (message) => {
    const parsed = parseAssistantFilesContent(message.content)
    const text = parsed ? parsed.text || '' : message.content
    return message.role === 'err' ? truncateErrorMessage(text) : text
  }
  const getMessageFiles = message => message.files || parseAssistantFilesContent(message.content)?.files || []
  const getReasoningContent = message => typeof message?.reasoning_content === 'string' ? message.reasoning_content : ''
  const GUIDANCE_START_MARKER = '[系统提示信息]'
  const GUIDANCE_END_MARKER = '[系统提示信息结束]'
  const getGuidanceText = (content) => {
    if (typeof content !== 'string') return ''
    if (!content.startsWith(GUIDANCE_START_MARKER) || !content.endsWith(GUIDANCE_END_MARKER)) return content
    return content.slice(GUIDANCE_START_MARKER.length, -GUIDANCE_END_MARKER.length).trim()
  }

  const formatFileSize = (size) => {
    if (!Number.isFinite(size)) return '-'
    if (size < 1024) return `${size} B`
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
    return `${(size / 1024 / 1024).toFixed(1)} MB`
  }
  const getMessageClass = role => ({ user: 'user', err: 'error' })[role] || 'ai'
  const didLastDisplayChangePrepend = () => lastDisplayChangeWasPrepend

  return {
    formatTokenCount,
    formatCacheHitRate,
    formatSessionTooltip,
    renderMarkdown,
    renderTextWithLinks,
    getDisplayKey,
    displayMessages,
    activityNotice,
    getToolPairTitle,
    getToolGroupStateClass,
    getToolGroupStatus,
    getMessageText,
    getMessageFiles,
    getReasoningContent,
    getGuidanceText,
    formatFileSize,
    getMessageClass,
    didLastDisplayChangePrepend
  }
}
