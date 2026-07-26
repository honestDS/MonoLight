<template>
  <div class="message-list-wrapper">
    <div
      class="llm-request-metadata"
      role="status"
      :aria-label="$t('chat.llm_request_metadata_label')"
    >
      <span class="llm-request-metadata-item">
        <span>{{ $t('chat.input_tokens') }}</span>
        <strong>{{ formatTokenCount(llmRequestMetadata?.input_tokens) }}</strong>
      </span>
      <span class="llm-request-metadata-item">
        <span>{{ $t('chat.total_output') }}</span>
        <strong>{{ formatTokenCount(llmRequestMetadata?.total_output_tokens ?? llmRequestMetadata?.output_tokens) }}</strong>
      </span>
      <span class="llm-request-metadata-item">
        <span>{{ $t('chat.cache_hit_rate') }}</span>
        <strong>{{ formatCacheHitRate(llmRequestMetadata?.cache_hit_rate) }}</strong>
      </span>
      <span class="llm-request-metadata-item">
        <span>{{ $t('chat.context_limit') }}</span>
        <strong>{{ formatTokenCount(llmRequestMetadata?.context_window_tokens) }}</strong>
      </span>
      <span class="llm-request-metadata-item">
        <span>{{ $t('chat.max_output') }}</span>
        <strong>{{ formatTokenCount(llmRequestMetadata?.max_output_tokens) }}</strong>
      </span>
    </div>
    <VList
      :key="currentSessionId || '__new_session__'"
      ref="virtualList"
      class="message-list"
      :class="{ 'is-layout-ready': messagesLayoutReady }"
      :data="displayMessages"
      :buffer-size="600"
      :shift="maintainScrollPosition"
      @scroll="handleVirtualScroll"
      @wheel="handleWheel"
      @touchmove="handleTouchMove"
      @keydown="handleScrollableKeyDown"
      @pointerdown="handlePointerDown"
    >
      <template #default="{ item: msg }">
        <div class="message-list-item" :key="getDisplayKey(msg)" :data-message-key="getDisplayKey(msg)">
          <div
            :class="['message-item', getMessageClass(msg.role), { queued: msg.status === 'queued' }]"
          >
        <template v-if="msg.type === 'tool_group'">
          <div class="message-header">
            <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
          </div>
          <div
            v-if="msg.content.trim() && currentSessionEnableMarkdown"
            class="content markdown-body tool-call-message"
            v-html="renderMarkdown(msg.content)"
          ></div>
          <div v-else-if="msg.content.trim()" class="content tool-call-message" style="white-space: pre-wrap;">
            <template v-for="(part, idx) in renderTextWithLinks(msg.content)" :key="idx">
              <el-link
                v-if="part.type === 'link'"
                :href="part.href"
                class="message-link"
                type="primary"
                target="_blank"
                rel="noopener noreferrer"
                underline="always"
              >{{ part.text }}</el-link><span v-else>{{ part.text }}</span>
            </template>
          </div>
          <el-collapse v-model="collapseModel" :class="['tool-round-collapse', getToolGroupStateClass(msg)]">
            <el-collapse-item :name="msg.id">
              <template #title>
                <span :class="['tool-round-title', getToolGroupStateClass(msg)]">{{ getToolGroupTitle(msg) }}</span>
              </template>
              <el-collapse v-model="collapseModel" class="tool-pair-collapse">
                <el-collapse-item
                  v-for="pair in msg.pairs"
                  :key="pair.id"
                  :name="`${msg.id}_${pair.id}`"
                  :class="{ 'has-result': pair.resultMessage }"
                >
                  <template #title>
                    <span :class="pair.resultMessage ? 'tool-result-title' : 'tool-call-title'">
                      {{ getToolPairTitle(pair) }}
                    </span>
                  </template>
                  <div v-if="pair.toolCall" class="tool-call-content">
                    <div class="tool-detail-title">{{ $t('chat.tool_call', { name: getToolCallName(pair.toolCall) }) }}</div>
                    <VirtualizedCode
                      :ref="el => setCodeRef(`${msg.id}_${pair.id}_call`, el)"
                      :content="getToolCallArguments(pair.toolCall)"
                      :max-height="300"
                    />
                  </div>
                  <div v-if="pair.resultMessage" class="tool-result-content">
                    <div class="tool-detail-title">{{ $t('chat.tool_result', { name: getToolPairName(pair) }) }}</div>
                    <VirtualizedCode
                      :ref="el => setCodeRef(`${msg.id}_${pair.id}_result`, el)"
                      :content="getToolResultContent(pair.resultMessage)"
                      :max-height="300"
                    />
                  </div>
                </el-collapse-item>
              </el-collapse>
            </el-collapse-item>
          </el-collapse>
        </template>
        <template v-else-if="msg.type === 'audit_confirmation'">
          <div class="message-header">
            <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
          </div>
          <div class="content audit-confirmation-card">
            <div class="audit-confirmation-title">{{ $t('chat.audit_confirmation_title') }}</div>
            <div class="audit-confirmation-summary">{{ getAuditConfirmation(msg).summary }}</div>
            <div class="audit-confirmation-meta">
              <span>{{ $t('chat.audit_status', { status: getAuditStatusLabel(msg) }) }}</span>
              <span>{{ $t('chat.audit_risk', { score: getAuditConfirmation(msg).risk }) }}</span>
              <span>{{ getAuditExpiryLabel(msg) }}</span>
            </div>
            <div class="audit-confirmation-actions">
              <el-button
                type="primary"
                :disabled="currentSessionReadOnly || isAuditDecisionPending(msg) || getAuditConfirmation(msg).status !== 'pending' || isAuditConfirmationExpired(msg)"
                @click="submitAuditDecision(msg, 'approve')"
              >{{ $t('chat.audit_approve') }}</el-button>
              <el-button
                :disabled="currentSessionReadOnly || isAuditDecisionPending(msg) || getAuditConfirmation(msg).status !== 'pending' || isAuditConfirmationExpired(msg)"
                @click="submitAuditDecision(msg, 'reject')"
              >{{ $t('chat.audit_reject') }}</el-button>
            </div>
          </div>
        </template>
        <template v-else-if="msg.role === 'background_system'">
          <div class="message-header">
            <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
          </div>
          <div class="content background-system-card">
            <div class="background-system-title">{{ getBackgroundSystemTitle(msg) }}</div>
            <div class="background-system-text">{{ getBackgroundSystemText(msg) }}</div>
          </div>
        </template>
        <template v-else>
          <div class="message-header">
            <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
          </div>

          <div v-if="msg.attachments && msg.attachments.length > 0" class="message-attachments">
            <div v-for="(att, idx) in msg.attachments" :key="idx" class="message-attachment-item">
              <el-image
                v-if="isImageFile(att)"
                :src="fileApi.getDownloadUrl(att)"
                :preview-src-list="getAttachmentImageUrls(msg)"
                preview-teleported
                :hide-on-click-modal="true"
                class="msg-attachment-image"
                @load="handleImageLoad"
              ></el-image>
              <a v-else :href="fileApi.getDownloadUrl(att)" target="_blank" class="msg-attachment-file">
                <img src="@/assets/svg/document.svg" class="icon-document" />
                <span class="file-name" :title="getFilename(att)">{{ getFilename(att) }}</span>
              </a>
            </div>
          </div>

          <div v-if="typeof getMessageText(msg) === 'string' && getMessageText(msg).trim() && currentSessionEnableMarkdown" class="content markdown-body">
            <div v-if="msg.status === 'queued'" class="queued-indicator">
              <img src="@/assets/svg/wait.svg" class="is-loading" />
            </div><div v-html="renderMarkdown(getMessageText(msg))"></div>
          </div>
          <div v-else-if="typeof getMessageText(msg) === 'string' && getMessageText(msg).trim() && !currentSessionEnableMarkdown" class="content" style="white-space: pre-wrap;">
            <div v-if="msg.status === 'queued'" class="queued-indicator">
              <img src="@/assets/svg/wait.svg" class="is-loading" />
            </div><template v-for="(part, idx) in renderTextWithLinks(getMessageText(msg))" :key="idx">
              <el-link
                v-if="part.type === 'link'"
                :href="part.href"
                class="message-link"
                type="primary"
                target="_blank"
                rel="noopener noreferrer"
                underline="always"
              >{{ part.text }}</el-link><span v-else>{{ part.text }}</span>
            </template>
          </div>
          <div v-else-if="Array.isArray(msg.content)" class="content">
            <div v-if="msg.status === 'queued'" class="queued-indicator">
              <img src="@/assets/svg/wait.svg" class="is-loading" />
            </div><div v-for="(part, idx) in msg.content" :key="idx" class="message-part">
              <div v-if="part.type === 'text'" class="text-part">
                <template v-for="(textPart, textIdx) in renderTextWithLinks(part.text)" :key="textIdx">
                  <el-link
                    v-if="textPart.type === 'link'"
                    :href="textPart.href"
                    class="message-link"
                    type="primary"
                    target="_blank"
                    rel="noopener noreferrer"
                    underline="always"
                  >{{ textPart.text }}</el-link><span v-else>{{ textPart.text }}</span>
                </template>
              </div>
              <el-image
                v-else-if="part.type === 'image_url'"
                :src="part.image_url.url"
                :preview-src-list="[part.image_url.url]"
                preview-teleported
                :hide-on-click-modal="true"
                class="msg-image"
                @load="handleImageLoad"
              ></el-image>
              <div v-else class="text-part">{{ JSON.stringify(part) }}</div>
            </div>
          </div>

          <div v-if="getMessageFiles(msg).length > 0" class="message-attachments message-sent-files">
            <div v-for="file in getMessageFiles(msg)" :key="file.id" class="message-attachment-item">
              <el-image
                v-if="isPreviewImage(file)"
                :src="getSentFileUrl(file)"
                :preview-src-list="getSentFileImageUrls(msg)"
                preview-teleported
                :hide-on-click-modal="true"
                class="msg-attachment-image"
                @load="handleImageLoad"
              ></el-image>
              <a v-else :href="getSentFileUrl(file)" target="_blank" rel="noopener noreferrer" class="msg-attachment-file">
                <img src="@/assets/svg/document.svg" class="icon-document" />
                <span class="file-name" :title="file.name">{{ file.name }}</span>
                <span class="sent-file-meta">{{ file.mime_type }} · {{ formatFileSize(file.size) }}</span>
              </a>
            </div>
          </div>
        </template>
          </div>
        </div>
      </template>
    </VList>
    <div v-if="!currentSessionId && messages.length === 0" class="empty-chat">
      <p>{{ $t('chat.empty_chat_tip') }}</p>
    </div>
    <Transition name="new-message-indicator">
      <button
        v-if="unreadMessageKeys.length > 0 && !canFollowOutput()"
        type="button"
        class="new-message-indicator"
        :aria-label="$t('chat.new_messages')"
        :title="$t('chat.new_messages')"
        @click="handleNewMessageClick"
      >
        <img src="@/assets/svg/new_msg.svg" alt="" />
      </button>
    </Transition>
    <Transition name="history-loading">
      <div v-if="historyLoading" class="history-loading-indicator" role="status" aria-live="polite">
        <span class="history-loading-spinner"></span>
        <span>{{ $t('chat.loading_history') }}</span>
      </div>
    </Transition>
    <Transition name="context-summary-notice">
      <div v-if="contextSummarizing" class="context-summary-notice" role="status" aria-live="polite">
        <span class="context-summary-notice-spinner"></span>
        <span>{{ $t('chat.context_summarizing') }}</span>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { VList } from 'virtua/vue'
import { useI18n } from 'vue-i18n'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'
import 'github-markdown-css/github-markdown.css'
import VirtualizedCode from './VirtualizedCode.vue'
import { fileApi } from '../api'
import {
  formatTimestamp,
  getMessageTimestamp,
  getToolCallArguments,
  getToolCallContent,
  getToolCallName,
  getToolCalls,
  getToolResultContent,
  getToolResultName,
  isToolCall,
  isToolResult
} from '../utils'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  currentSessionId: { type: String, default: null },
  currentSessionEnableMarkdown: { type: Boolean, default: false },
  currentSessionReadOnly: { type: Boolean, default: false },
  activeCollapse: { type: Array, default: () => [] },
  historyLoading: { type: Boolean, default: false },
  initialHistoryLoaded: { type: Boolean, default: true },
  contextSummarizing: { type: Boolean, default: false },
  llmRequestMetadata: { type: Object, default: null }
})
const emit = defineEmits(['update:activeCollapse', 'audit-decision'])
const { t } = useI18n()
const tokenNumberFormatter = new Intl.NumberFormat()
const percentNumberFormatter = new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 2 })
const formatTokenCount = value => Number.isFinite(value) ? tokenNumberFormatter.format(value) : '-'
const formatCacheHitRate = value => Number.isFinite(value) ? percentNumberFormatter.format(value) : '-'
const pendingAuditDecisions = ref(new Set())
const auditDecisionStatuses = ref(new Map())
const auditCountdownNow = ref(Date.now())
const auditCountdownTimer = window.setInterval(() => {
  auditCountdownNow.value = Date.now()
}, 1000)
const virtualList = ref(null)
const maintainScrollPosition = ref(false)
let bottomScrollRequest = 0
let programmaticScrollRequest = 0
const programmaticScrollRequests = new Set()
const programmaticScrollReleaseTasks = new Map()
let pointerScrollSession = false
let transientUserScrollCandidate = false
let transientUserScrollCandidateTimer = null
let lastVirtualScrollOffset = null
let messageListUnmounted = false
const messagesLayoutReady = ref(false)
const scrollListeners = new Set()
const codeRefs = new Map()
const collapseModel = computed({
  get: () => props.activeCollapse,
  set: value => emit('update:activeCollapse', value)
})

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
const analyzeMessage = (message) => ({
  isToolCall: isToolCall(message),
  isToolResult: isToolResult(message)
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
          created_at: message.created_at,
          content: '',
          pairs: [],
          latestEvent: null
        }
        output.push(activeToolGroup)
      }

      const toolContent = getToolCallContent(message).trim()
      if (toolContent) activeToolGroup.content = activeToolGroup.content ? `${activeToolGroup.content}\n${toolContent}` : toolContent
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

const displayMessages = computed(() => buildDisplayMessages(props.messages))
const unreadMessageKeys = ref([])
const latestLlmMessageVisible = ref(true)
const followsOutput = ref(true)
let unreadTrackingReady = false
let unreadTrackingGeneration = 0
let visibilityFrameId = null

const isIncomingMessage = message => message.type === 'tool_group' || !['user', 'thinking'].includes(message.role)
const isLlmOutputMessage = message => message.type === 'tool_group' || ['assistant', 'tool'].includes(message.role)
const OUTPUT_FOLLOW_BOTTOM_TOLERANCE = 24
let messageListAtBottom = true
const setOutputFollowState = (shouldFollow) => {
  if (shouldFollow) {
    followsOutput.value = true
    unreadMessageKeys.value = []
    return
  }
  if (!followsOutput.value) return
  followsOutput.value = false
  bottomScrollRequest += 1
}
const canFollowOutput = () => followsOutput.value && latestLlmMessageVisible.value
const getMessageListBottomDistance = (offset) => {
  const list = virtualList.value
  const currentOffset = Number.isFinite(offset) ? offset : list?.scrollOffset
  if (!list || !Number.isFinite(currentOffset)) return null
  const bottomDistance = list.scrollSize - list.viewportSize - currentOffset
  return Number.isFinite(bottomDistance) ? Math.max(0, bottomDistance) : null
}
const updateMessageListBottomState = (offset) => {
  const bottomDistance = getMessageListBottomDistance(offset)
  if (bottomDistance === null) return messageListAtBottom
  messageListAtBottom = bottomDistance <= OUTPUT_FOLLOW_BOTTOM_TOLERANCE
  return messageListAtBottom
}
const calculateLatestLlmMessageVisible = () => {
  const latestMessage = [...displayMessages.value].reverse().find(isLlmOutputMessage)
  const listElement = virtualList.value?.$el
  if (!latestMessage || !listElement) return messageListAtBottom

  const latestMessageKey = getDisplayKey(latestMessage)
  const messageElement = [...listElement.querySelectorAll('[data-message-key]')]
    .find(element => element.dataset.messageKey === latestMessageKey)
  if (!messageElement) return messageListAtBottom

  const viewportRect = listElement.getBoundingClientRect()
  const messageRect = messageElement.getBoundingClientRect()
  return messageRect.bottom > viewportRect.top && messageRect.top < viewportRect.bottom
}
const refreshLatestLlmMessageVisibility = () => {
  latestLlmMessageVisible.value = calculateLatestLlmMessageVisible()
  if (canFollowOutput()) unreadMessageKeys.value = []
  return latestLlmMessageVisible.value
}
const scheduleUnreadVisibilityCheck = () => {
  if (visibilityFrameId !== null) cancelAnimationFrame(visibilityFrameId)
  visibilityFrameId = requestAnimationFrame(() => {
    visibilityFrameId = null
    refreshLatestLlmMessageVisibility()
  })
}
const clearTransientUserScrollCandidate = () => {
  transientUserScrollCandidate = false
  if (transientUserScrollCandidateTimer !== null) {
    window.clearTimeout(transientUserScrollCandidateTimer)
    transientUserScrollCandidateTimer = null
  }
}
const createTransientUserScrollCandidate = () => {
  transientUserScrollCandidate = true
  if (transientUserScrollCandidateTimer !== null) window.clearTimeout(transientUserScrollCandidateTimer)
  transientUserScrollCandidateTimer = window.setTimeout(() => {
    transientUserScrollCandidate = false
    transientUserScrollCandidateTimer = null
  }, 250)
}
const resetUserScrollCandidates = () => {
  pointerScrollSession = false
  clearTransientUserScrollCandidate()
}

const stableSerialize = (value) => {
  if (Array.isArray(value)) return `[${value.map(stableSerialize).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableSerialize(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}
const getIncomingMessageFields = message => ({
  role: message?.role,
  type: message?.type,
  content: message?.content,
  status: message?.status,
  attachments: message?.attachments,
  files: message?.files,
  finish_reason: message?.finish_reason,
  refusal: message?.refusal
})
const getIncomingMessageSnapshot = (message) => {
  const snapshot = getIncomingMessageFields(message)
  if (message?.type === 'tool_group') {
    snapshot.pairs = (message.pairs || []).map(pair => ({
      id: pair.id,
      toolCall: pair.toolCall,
      resultMessage: pair.resultMessage ? getIncomingMessageFields(pair.resultMessage) : null
    }))
    snapshot.latestEvent = message.latestEvent
      ? { type: message.latestEvent.type, pair: message.latestEvent.pair ? { id: message.latestEvent.pair.id } : null }
      : null
  }
  return stableSerialize(snapshot)
}
let incomingMessageSnapshots = new Map()
const resetIncomingMessageSnapshots = (messages) => {
  incomingMessageSnapshots = new Map(messages.map(message => [getDisplayKey(message), getIncomingMessageSnapshot(message)]))
}
const getChangedIncomingMessages = (messages) => {
  const nextSnapshots = new Map()
  const changedMessages = []
  messages.forEach((message) => {
    const key = getDisplayKey(message)
    const snapshot = getIncomingMessageSnapshot(message)
    nextSnapshots.set(key, snapshot)
    if (isIncomingMessage(message) && (!incomingMessageSnapshots.has(key) || incomingMessageSnapshots.get(key) !== snapshot)) {
      changedMessages.push(message)
    }
  })
  incomingMessageSnapshots = nextSnapshots
  return changedMessages
}

const handleChangedIncomingMessages = async (changedMessages, replaceUnread = false) => {
  if (changedMessages.length === 0) return
  const shouldFollowOutput = canFollowOutput() && changedMessages.some(isLlmOutputMessage)
  const changedKeys = changedMessages.map(getDisplayKey)
  if (!shouldFollowOutput) {
    unreadMessageKeys.value = replaceUnread
      ? [...new Set(changedKeys)]
      : [...new Set([...unreadMessageKeys.value, ...changedKeys])]
  }

  await nextTick()
  if (shouldFollowOutput && canFollowOutput()) {
    await scrollToBottom('auto', false)
    updateMessageListBottomState()
    refreshLatestLlmMessageVisibility()
  } else {
    if (shouldFollowOutput) {
      unreadMessageKeys.value = replaceUnread
        ? [...new Set(changedKeys)]
        : [...new Set([...unreadMessageKeys.value, ...changedKeys])]
    }
    refreshLatestLlmMessageVisibility()
  }
  scheduleUnreadVisibilityCheck()
}

watch(
  () => [props.currentSessionId, props.initialHistoryLoaded],
  async ([, historyLoaded]) => {
    const generation = ++unreadTrackingGeneration
    unreadTrackingReady = false
    unreadMessageKeys.value = []
    incomingMessageSnapshots = new Map()
    if (!historyLoaded) return
    const baselineMessages = displayMessages.value
    resetIncomingMessageSnapshots(baselineMessages)
    await nextTick()
    await new Promise(resolve => requestAnimationFrame(resolve))
    if (generation !== unreadTrackingGeneration || !props.initialHistoryLoaded) return

    unreadTrackingReady = true
    const changedMessages = getChangedIncomingMessages(displayMessages.value)
    await handleChangedIncomingMessages(changedMessages, true)
  },
  { immediate: true, flush: 'post' }
)

watch(displayMessages, async (messages) => {
  if (!unreadTrackingReady) return
  if (lastDisplayChangeWasPrepend) {
    resetIncomingMessageSnapshots(messages)
    return
  }

  const changedMessages = getChangedIncomingMessages(messages)
  await handleChangedIncomingMessages(changedMessages)
}, { deep: true, flush: 'pre' })

let layoutGeneration = 0
watch(() => props.currentSessionId, () => {
  maintainScrollPosition.value = false
  bottomScrollRequest += 1
  messageListAtBottom = true
  latestLlmMessageVisible.value = true
  followsOutput.value = true
  resetUserScrollCandidates()
  lastVirtualScrollOffset = null
  layoutGeneration += 1
  if (!props.initialHistoryLoaded) {
    messagesLayoutReady.value = false
  }
}, { immediate: true, flush: 'sync' })
watch(() => props.initialHistoryLoaded, async (historyLoaded) => {
  layoutGeneration += 1
  messagesLayoutReady.value = false
  if (!historyLoaded) return

  const generation = layoutGeneration
  await nextTick()
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
  if (generation === layoutGeneration && props.initialHistoryLoaded) {
    messagesLayoutReady.value = true
  }
}, { immediate: true, flush: 'post' })

const beginProgrammaticScroll = () => {
  const request = ++programmaticScrollRequest
  programmaticScrollRequests.add(request)
  return request
}
const finishProgrammaticScroll = (request, settleDuration = 0) => {
  if (messageListUnmounted) {
    programmaticScrollRequests.delete(request)
    return
  }

  const task = { firstFrameId: null, secondFrameId: null, timeoutId: null }
  const release = () => {
    programmaticScrollRequests.delete(request)
    programmaticScrollReleaseTasks.delete(request)
  }
  task.firstFrameId = requestAnimationFrame(() => {
    task.firstFrameId = null
    task.secondFrameId = requestAnimationFrame(() => {
      task.secondFrameId = null
      if (settleDuration > 0) {
        task.timeoutId = window.setTimeout(() => {
          task.timeoutId = null
          release()
        }, settleDuration)
        return
      }
      release()
    })
  })
  programmaticScrollReleaseTasks.set(request, task)
}
const isProgrammaticScrollInProgress = () => programmaticScrollRequests.size > 0
const handleWheel = () => {
  createTransientUserScrollCandidate()
}
const handleTouchMove = () => {
  createTransientUserScrollCandidate()
}
const scrollKeys = new Set(['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' ', 'Spacebar'])
const handleScrollableKeyDown = (event) => {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || !scrollKeys.has(event.key)) return
  if (event.target instanceof Element && event.target.closest('input, textarea, select, button, [contenteditable]')) return
  createTransientUserScrollCandidate()
}
const handlePointerDown = () => {
  pointerScrollSession = true
}
const endPointerScrollSession = () => {
  pointerScrollSession = false
}
window.addEventListener('pointerup', endPointerScrollSession)
window.addEventListener('pointercancel', endPointerScrollSession)
const handleVirtualScroll = (offset) => {
  const currentOffset = Number.isFinite(offset) ? offset : virtualList.value?.scrollOffset
  const hasValidOffset = Number.isFinite(currentOffset)
  const hasScrollBaseline = Number.isFinite(lastVirtualScrollOffset)
  const offsetChanged = hasScrollBaseline && hasValidOffset && currentOffset !== lastVirtualScrollOffset
  const hasUserScrollCandidate = pointerScrollSession || transientUserScrollCandidate
  const userScrolled = hasValidOffset && (
    (hasUserScrollCandidate && (!hasScrollBaseline || offsetChanged)) ||
    (offsetChanged && !isProgrammaticScrollInProgress())
  )
  const atBottom = updateMessageListBottomState(currentOffset)
  if (userScrolled) setOutputFollowState(atBottom)
  if (hasValidOffset) lastVirtualScrollOffset = currentOffset

  refreshLatestLlmMessageVisibility()
  scrollListeners.forEach(listener => listener(offset))
  scheduleUnreadVisibilityCheck()
}
const captureScrollAnchor = () => {
  if (!virtualList.value) return null
  maintainScrollPosition.value = true
  return true
}
const restoreScrollAnchor = async (anchor) => {
  if (!anchor) return
  await nextTick()
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
  maintainScrollPosition.value = false
}
const MESSAGE_LIST_EDGE_PADDING = 20
const scrollToBottom = async (behavior = 'auto', restoreFollow = true) => {
  if (restoreFollow) setOutputFollowState(true)
  const request = ++bottomScrollRequest
  const programmaticRequest = beginProgrammaticScroll()
  const alignBottom = (smooth = false) => {
    const list = virtualList.value
    const lastIndex = displayMessages.value.length - 1
    if (!list || lastIndex < 0) return null
    list.scrollToIndex(lastIndex, {
      align: 'end',
      smooth,
      offset: MESSAGE_LIST_EDGE_PADDING
    })
    return list
  }

  try {
    await nextTick()
    if (behavior === 'smooth') {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      if (request !== bottomScrollRequest) return
      if (!alignBottom(true)) return
      await new Promise(resolve => setTimeout(resolve, 350))
      if (request === bottomScrollRequest) alignBottom(true)
      return
    }
    for (let attempt = 0; attempt < 6; attempt += 1) {
      if (request !== bottomScrollRequest) return
      const list = alignBottom()
      if (!list) return
      await new Promise(resolve => requestAnimationFrame(resolve))
      if (request !== bottomScrollRequest) return
      list.scrollTo(list.scrollSize)
    }
  } finally {
    finishProgrammaticScroll(programmaticRequest, behavior === 'smooth' ? 350 : 0)
  }
}

const getToolPairName = pair => pair.toolCall ? getToolCallName(pair.toolCall) : getToolResultName(pair.resultMessage)
const getToolPairTitle = pair => t('chat.tool', { name: getToolPairName(pair) })
const getToolGroupStateClass = group => group.latestEvent?.type === 'result' ? 'is-result' : 'is-call'
const getToolGroupTitle = (group) => {
  if (!group.latestEvent) return t('chat.tool_activity')
  return t(group.latestEvent.type === 'result' ? 'chat.tool_result' : 'chat.tool_call', { name: getToolPairName(group.latestEvent.pair) })
}

const setCodeRef = (id, element) => {
  if (element) codeRefs.set(id, element)
  else codeRefs.delete(id)
}
watch(collapseModel, (newValue, oldValue) => {
  const closedIds = oldValue.filter(id => !newValue.includes(id))
  closedIds.forEach((id) => {
    codeRefs.forEach((codeRef, refId) => {
      if ((refId === id || refId.startsWith(`${id}_`)) && typeof codeRef.reset === 'function') codeRef.reset()
    })
  })
}, { deep: true })

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
  return parsed ? parsed.text || '' : message.content
}
const getMessageFiles = (message) => message.files || parseAssistantFilesContent(message.content)?.files || []
const parseAuditConfirmation = (content) => {
  try {
    const parsed = typeof content === 'string' ? JSON.parse(content) : content
    return parsed?.type === 'audit_confirmation' ? parsed : null
  } catch {
    return null
  }
}
const getAuditConfirmation = (message) => {
  const payload = parseAuditConfirmation(message?.content) || {}
  const localStatus = auditDecisionStatuses.value.get(payload.audit_record_id)
  const terminalStatuses = new Set(['rejected', 'cancelled', 'expired', 'succeeded', 'failed', 'execution_unknown'])
  if (terminalStatuses.has(payload.status)) return payload
  if (localStatus) return { ...payload, status: localStatus }
  return payload
}
const getAuditRemainingSeconds = (message) => {
  const expiresAt = Date.parse(getAuditConfirmation(message).expires_at || '')
  if (!Number.isFinite(expiresAt)) return null
  return Math.max(0, Math.ceil((expiresAt - auditCountdownNow.value) / 1000))
}
const formatAuditCountdown = (remainingSeconds) => {
  const days = Math.floor(remainingSeconds / 86400)
  const hours = Math.floor((remainingSeconds % 86400) / 3600)
  const minutes = Math.floor((remainingSeconds % 3600) / 60)
  const seconds = remainingSeconds % 60
  if (days > 0) return t('chat.audit_countdown_days', { days, hours, minutes, seconds })
  if (hours > 0) return t('chat.audit_countdown_hours', { hours, minutes, seconds })
  if (minutes > 0) return t('chat.audit_countdown_minutes', { minutes, seconds })
  return t('chat.audit_countdown_seconds', { seconds })
}
const isAuditConfirmationExpired = message => getAuditRemainingSeconds(message) === 0
const getAuditExpiryLabel = (message) => {
  const confirmation = getAuditConfirmation(message)
  const status = confirmation.status || 'pending'
  if (status === 'expired') return t('chat.audit_status_expired')
  if (status !== 'pending') return ''
  const remainingSeconds = getAuditRemainingSeconds(message)
  if (remainingSeconds === null) return '-'
  if (remainingSeconds === 0) return t('chat.audit_status_expired')
  return t('chat.audit_expires_in', { time: formatAuditCountdown(remainingSeconds) })
}
const isAuditDecisionPending = message => pendingAuditDecisions.value.has(getAuditConfirmation(message).audit_record_id)
const getAuditStatusLabel = message => {
  const status = getAuditConfirmation(message).status || 'pending'
  return t(`chat.audit_status_${status === 'pending' && isAuditConfirmationExpired(message) ? 'expired' : status}`)
}
const submitAuditDecision = (message, decision) => {
  const auditRecordId = getAuditConfirmation(message).audit_record_id
  if (!auditRecordId || pendingAuditDecisions.value.has(auditRecordId)) return
  pendingAuditDecisions.value = new Set([...pendingAuditDecisions.value, auditRecordId])
  auditDecisionStatuses.value = new Map([
    ...auditDecisionStatuses.value,
    [auditRecordId, decision === 'approve' ? 'executing' : 'rejected']
  ])
  emit('audit-decision', { auditRecordId, decision })
}
const parseBackgroundSystemContent = (content) => {
  try {
    const parsed = typeof content === 'string' ? JSON.parse(content) : content
    return parsed?.type === 'background_tool_result' ? parsed : null
  } catch {
    return null
  }
}
const getBackgroundSystemTitle = (message) => t('chat.background_task_result', { name: parseBackgroundSystemContent(message.content)?.task?.tool_name || t('common.unknown_tool') })
const getBackgroundSystemText = (message) => {
  const parsed = parseBackgroundSystemContent(message.content)
  return parsed?.task?.summary || parsed?.task?.error || parsed?.task?.content || parsed?.instruction || ''
}

const isImageFile = (path) => ['png', 'jpg', 'jpeg', 'gif', 'webp'].includes((path || '').split('.').pop().toLowerCase())
const getFilename = (path) => {
  if (!path) return t('chat.unknown_file')
  const name = path.split(/[/\\]/).pop()
  return name.length > 9 && name[8] === '_' ? name.substring(9) : name
}
const isPreviewImage = file => file?.previewable && file?.mime_type?.startsWith('image/')
const getAttachmentImageUrls = message => (message.attachments || []).filter(isImageFile).map(fileApi.getDownloadUrl)
const getSentFileUrl = file => fileApi.resolveDownloadUrl(file?.download_url)
const getSentFileImageUrls = message => getMessageFiles(message).filter(isPreviewImage).map(getSentFileUrl)
const formatFileSize = (size) => {
  if (!Number.isFinite(size)) return '-'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
const getMessageClass = role => ({ user: 'user', thinking: 'thinking', background_system: 'background-system', err: 'error' })[role] || 'ai'
const handleNewMessageClick = async () => {
  await scrollToBottom('smooth')
  updateMessageListBottomState()
  scheduleUnreadVisibilityCheck()
}
const handleImageLoad = () => {
  scheduleUnreadVisibilityCheck()
}
onUnmounted(() => {
  messageListUnmounted = true
  window.clearInterval(auditCountdownTimer)
  if (visibilityFrameId !== null) cancelAnimationFrame(visibilityFrameId)
  window.removeEventListener('pointerup', endPointerScrollSession)
  window.removeEventListener('pointercancel', endPointerScrollSession)
  resetUserScrollCandidates()
  programmaticScrollReleaseTasks.forEach((task) => {
    if (task.firstFrameId !== null) cancelAnimationFrame(task.firstFrameId)
    if (task.secondFrameId !== null) cancelAnimationFrame(task.secondFrameId)
    if (task.timeoutId !== null) window.clearTimeout(task.timeoutId)
  })
  programmaticScrollReleaseTasks.clear()
  programmaticScrollRequests.clear()
})

const scrollTo = (options) => {
  const list = virtualList.value
  if (!list) return
  const request = beginProgrammaticScroll()
  try {
    return list.scrollTo(typeof options === 'number' ? options : options?.top || 0)
  } finally {
    finishProgrammaticScroll(request)
  }
}

defineExpose({
  captureScrollAnchor,
  restoreScrollAnchor,
  canFollowOutput,
  scrollToBottom,
  scrollTo,
  addEventListener: (type, listener) => {
    if (type === 'scroll') scrollListeners.add(listener)
  },
  removeEventListener: (type, listener) => {
    if (type === 'scroll') scrollListeners.delete(listener)
  },
  get scrollHeight() {
    return virtualList.value?.scrollSize || 0
  },
  get scrollTop() {
    return virtualList.value?.scrollOffset || 0
  }
})
</script>
