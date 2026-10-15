<template>
  <div class="message-list-wrapper">
    <Transition name="request-metadata-fade">
      <div
        v-if="showRequestMetadata"
        :class="['llm-request-metadata', 'glass-surface', { 'is-collapsed': requestMetadataCollapsed }]"
      >
        <div
          id="llm-request-metadata-content"
          class="llm-request-metadata-content"
          role="status"
          :aria-hidden="requestMetadataCollapsed"
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
        <div class="request-metadata-actions">
          <span class="request-metadata-collapsed-label" aria-hidden="true">
            {{ $t('chat.request_metadata_short_label') }}
          </span>
          <el-tooltip
            v-if="currentSessionInfo"
            :content="formatSessionTooltip(currentSessionInfo)"
            placement="bottom"
            :show-after="100"
            popper-class="session-info-tooltip"
          >
            <el-icon class="session-info-icon"><InfoFilled /></el-icon>
          </el-tooltip>
          <el-tooltip
            :content="$t(requestMetadataCollapsed ? 'chat.expand_request_metadata' : 'chat.collapse_request_metadata')"
            placement="bottom"
            :show-after="300"
          >
            <button
              type="button"
              class="request-metadata-toggle"
              :aria-label="$t(requestMetadataCollapsed ? 'chat.expand_request_metadata' : 'chat.collapse_request_metadata')"
              :aria-expanded="!requestMetadataCollapsed"
              aria-controls="llm-request-metadata-content"
              @click="requestMetadataCollapsed = !requestMetadataCollapsed"
            >
              <el-icon class="request-metadata-toggle-icon" :class="{ 'is-expanded': !requestMetadataCollapsed }">
                <ArrowDown />
              </el-icon>
            </button>
          </el-tooltip>
        </div>
      </div>
    </Transition>
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
            :class="['message-item', getMessageClass(msg.role), { queued: msg.status === 'queued', guidance: msg.type === 'guidance' }]"
          >
        <template v-if="msg.type === 'guidance'">
          <div class="message-header">
            <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
          </div>
          <div class="content guidance-card">
            <div class="guidance-card-header">
              <span class="guidance-title">{{ $t('chat.guidance') }}</span>
              <span :class="['guidance-status', { 'is-used': msg.is_processed }]">
                {{ msg.is_processed ? $t('chat.guidance_used') : $t('chat.guidance_pending') }}
              </span>
            </div>
            <div class="guidance-text">{{ getGuidanceText(msg.content) }}</div>
          </div>
        </template>
        <template v-else-if="msg.type === 'tool_group'">
          <div class="message-header">
            <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
          </div>
          <ThinkingBlock
            v-if="currentSessionShowReasoning && getReasoningContent(msg)"
            v-model="reasoningCollapseModel"
            :name="getReasoningCollapseName(msg)"
            :content="getReasoningContent(msg)"
            :rendered-content="renderMarkdown(getReasoningContent(msg))"
          />
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
          <el-collapse v-model="collapseModel" class="tool-round-collapse">
            <el-collapse-item :name="msg.id">
              <template #title>
                <span class="tool-round-title">
                  <span class="tool-round-label">{{ $t('chat.tool_activity') }}</span>
                  <span v-if="msg.pairs.length > 1" class="tool-round-count">{{ $t('chat.tool_count', { count: msg.pairs.length }) }}</span>
                  <span :class="['tool-round-status', getToolGroupStateClass(msg)]">{{ getToolGroupStatus(msg) }}</span>
                </span>
              </template>
              <el-collapse v-model="collapseModel" class="tool-pair-collapse">
                <el-collapse-item
                  v-for="pair in msg.pairs"
                  :key="pair.id"
                  :name="`${msg.id}_${pair.id}`"
                >
                  <template #title>
                    <span class="tool-pair-title">
                      <span class="tool-pair-name">{{ getToolPairTitle(pair) }}</span>
                      <span :class="['tool-pair-status', pair.resultMessage ? 'is-result' : 'is-call']">
                        {{ $t(pair.resultMessage ? 'chat.tool_status_returned' : 'chat.tool_status_calling') }}
                      </span>
                    </span>
                  </template>
                  <div v-if="pair.toolCall" class="tool-call-content">
                    <div class="tool-detail-title tool-detail-title--call">{{ $t('chat.tool_arguments') }}</div>
                    <VirtualizedCode
                      :ref="el => setCodeRef(`${msg.id}_${pair.id}_call`, el)"
                      :content="getToolCallArguments(pair.toolCall)"
                      :max-height="300"
                    />
                  </div>
                  <div v-if="pair.resultMessage" class="tool-result-content">
                    <div class="tool-detail-title tool-detail-title--result">{{ $t('chat.tool_output') }}</div>
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
          <div :class="['content', 'audit-confirmation-card', { 'audit-confirmation-card--high-risk': isHighRiskAuditConfirmation(msg) }]">
            <div class="audit-confirmation-title">
              <WarningFilled v-if="isHighRiskAuditConfirmation(msg)" class="audit-confirmation-warning-icon" />
              {{ $t(isHighRiskAuditConfirmation(msg) ? 'chat.audit_high_risk_warning_title' : 'chat.audit_confirmation_title') }}
            </div>
            <div class="audit-confirmation-summary">{{ getAuditConfirmation(msg).summary }}</div>
            <div class="audit-confirmation-meta">
              <span>{{ $t('chat.audit_status', { status: getAuditStatusLabel(msg) }) }}</span>
              <span>{{ $t('chat.audit_risk', { score: getAuditConfirmation(msg).risk }) }}</span>
              <span>{{ getAuditExpiryLabel(msg) }}</span>
            </div>
            <div class="audit-confirmation-actions">
              <el-button
                :type="isHighRiskAuditConfirmation(msg) ? 'danger' : 'primary'"
                :disabled="currentSessionReadOnly || isAuditDecisionPending(msg) || getAuditConfirmation(msg).status !== 'pending' || isAuditConfirmationExpired(msg)"
                @click="submitAuditDecision(msg, isHighRiskAuditConfirmation(msg) ? 'ignore' : 'approve')"
              >{{ isHighRiskAuditConfirmation(msg) ? $t('chat.audit_ignore_and_allow') : $t('chat.audit_approve') }}</el-button>
              <el-button
                :disabled="currentSessionReadOnly || isAuditDecisionPending(msg) || getAuditConfirmation(msg).status !== 'pending' || isAuditConfirmationExpired(msg)"
                @click="submitAuditDecision(msg, 'reject')"
              >{{ $t('chat.audit_reject') }}</el-button>
            </div>
          </div>
        </template>
        <template v-else-if="msg.role === 'reasoning'">
          <ThinkingBlock
            v-if="currentSessionShowReasoning && getReasoningContent(msg)"
            v-model="reasoningCollapseModel"
            :name="getReasoningCollapseName(msg)"
            :content="getReasoningContent(msg)"
            :rendered-content="renderMarkdown(getReasoningContent(msg))"
          />
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

          <ThinkingBlock
            v-if="currentSessionShowReasoning && msg.role === 'assistant' && getReasoningContent(msg)"
            v-model="reasoningCollapseModel"
            :name="getReasoningCollapseName(msg)"
            :content="getReasoningContent(msg)"
            :rendered-content="renderMarkdown(getReasoningContent(msg))"
          />

          <div v-if="msg.attachments && msg.attachments.length > 0" class="message-attachments">
            <div v-for="(att, idx) in msg.attachments" :key="idx" class="message-attachment-item">
              <el-image
                v-if="isImageFile(att)"
                :src="getFileObjectUrl(getAttachmentSourceUrl(att))"
                :preview-src-list="getAttachmentImageUrls(msg)"
                preview-teleported
                :hide-on-click-modal="true"
                class="msg-attachment-image"
                @load="handleImageLoad"
              ></el-image>
              <a
                v-else
                :href="getFileObjectUrl(getAttachmentSourceUrl(att)) || '#'"
                class="msg-attachment-file"
                @click.prevent="handleFileDownload(getAttachmentSourceUrl(att), getFilename(att))"
              >
                <img src="@/assets/svg/document.svg" class="icon-document" />
                <span class="file-name" :title="getFilename(att)">{{ getFilename(att) }}</span>
              </a>
            </div>
          </div>

          <div v-if="typeof getMessageText(msg) === 'string' && getMessageText(msg).trim() && currentSessionEnableMarkdown" class="content markdown-body">
            <div v-if="msg.status === 'queued'" class="queued-indicator">
              <img src="@/assets/svg/wait.svg" class="is-loading" />
            </div><CollapsibleMessageText
              :enabled="msg.role === 'user'"
              :expanded="expandedUserMessages.has(getDisplayKey(msg))"
              @update:expanded="setUserMessageExpanded(msg, $event)"
            >
              <div v-html="renderMarkdown(getMessageText(msg))"></div>
            </CollapsibleMessageText>
          </div>
          <div v-else-if="typeof getMessageText(msg) === 'string' && getMessageText(msg).trim() && !currentSessionEnableMarkdown" class="content" style="white-space: pre-wrap;">
            <div v-if="msg.status === 'queued'" class="queued-indicator">
              <img src="@/assets/svg/wait.svg" class="is-loading" />
            </div><CollapsibleMessageText
              :enabled="msg.role === 'user'"
              :expanded="expandedUserMessages.has(getDisplayKey(msg))"
              @update:expanded="setUserMessageExpanded(msg, $event)"
            >
              <template v-for="(part, idx) in renderTextWithLinks(getMessageText(msg))" :key="idx">
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
            </CollapsibleMessageText>
          </div>
          <div v-else-if="Array.isArray(msg.content)" class="content">
            <div v-if="msg.status === 'queued'" class="queued-indicator">
              <img src="@/assets/svg/wait.svg" class="is-loading" />
            </div><CollapsibleMessageText
              :enabled="msg.role === 'user'"
              :expanded="expandedUserMessages.has(getDisplayKey(msg))"
              @update:expanded="setUserMessageExpanded(msg, $event)"
            >
              <div v-for="(part, idx) in msg.content" :key="idx" class="message-part">
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
            </CollapsibleMessageText>
          </div>

          <div v-if="getMessageFiles(msg).length > 0" class="message-attachments message-sent-files">
            <div v-for="file in getMessageFiles(msg)" :key="file.id" class="message-attachment-item">
              <el-image
                v-if="isPreviewImage(file)"
                :src="getFileObjectUrl(getSentFileUrl(file))"
                :preview-src-list="getSentFileImageUrls(msg)"
                preview-teleported
                :hide-on-click-modal="true"
                class="msg-attachment-image"
                @load="handleImageLoad"
              ></el-image>
              <a
                v-else
                :href="getFileObjectUrl(getSentFileUrl(file)) || '#'"
                rel="noopener noreferrer"
                class="msg-attachment-file"
                @click.prevent="handleFileDownload(getSentFileUrl(file), file.name)"
              >
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
    <div v-if="!currentSessionId && messages.length === 0 && !hideEmptyTip" class="empty-chat">
      <p>{{ $t('chat.empty_chat_tip') }}</p>
    </div>
    <Transition name="new-message-indicator">
      <button
        v-if="unreadMessageKeys.length > 0 && !canFollowOutput() && messagesLayoutReady && scrollState?.hasOverflow"
        type="button"
        class="new-message-indicator"
        :aria-label="$t('chat.new_messages')"
        :title="$t('chat.new_messages')"
        @click="handleNewMessageClick"
      >
        <img src="@/assets/svg/new_msg.svg" alt="" />
      </button>
    </Transition>
    <Transition name="activity-status-notice" mode="out-in">
      <div
        v-if="activityNotice"
        :key="activityNotice.type"
        class="activity-status-notice glass-surface"
        role="status"
        aria-live="polite"
      >
        <span class="activity-status-notice-spinner"></span>
        <span>{{ activityNotice.text }}</span>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { VList } from 'virtua/vue'
import { useI18n } from 'vue-i18n'
import { ArrowDown, InfoFilled, WarningFilled } from '@element-plus/icons-vue'
import 'highlight.js/styles/github.css'
import 'github-markdown-css/github-markdown.css'
import VirtualizedCode from './VirtualizedCode.vue'
import ThinkingBlock from './ThinkingBlock.vue'
import CollapsibleMessageText from './CollapsibleMessageText.vue'
import {
  formatTimestamp,
  getMessageTimestamp,
  getToolCallArguments,
  getToolResultContent
} from '../utils'
import { getMessageListScrollState, getReasoningCollapseName, isFollowableLlmOutput } from '../utils/chatPresentation.js'
import { getClientSetting, setClientSetting } from '../utils/clientSettings.js'
import { useChatMessagePresentation } from '../composables/chat/useChatMessagePresentation.js'
import { useChatAuditConfirmation } from '../composables/chat/useChatAuditConfirmation.js'
import { useChatMessageFiles } from '../composables/chat/useChatMessageFiles.js'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  currentSessionId: { type: String, default: null },
  currentSessionEnableMarkdown: { type: Boolean, default: false },
  currentSessionShowReasoning: { type: Boolean, default: true },
  currentSessionReadOnly: { type: Boolean, default: false },
  activeCollapse: { type: Array, default: () => [] },
  historyLoading: { type: Boolean, default: false },
  initialHistoryLoaded: { type: Boolean, default: true },
  contextSummarizing: { type: Boolean, default: false },
  llmRequestMetadata: { type: Object, default: null },
  currentSessionInfo: { type: Object, default: null },
  showRequestMetadata: { type: Boolean, default: true },
  hideEmptyTip: { type: Boolean, default: false }
})
const emit = defineEmits(['update:activeCollapse', 'audit-decision'])
const { t } = useI18n()
const {
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
  getBackgroundSystemTitle,
  getBackgroundSystemText,
  formatFileSize,
  getMessageClass,
  didLastDisplayChangePrepend
} = useChatMessagePresentation({ props, t })
const {
  getAuditConfirmation,
  isHighRiskAuditConfirmation,
  getAuditStatusLabel,
  getAuditExpiryLabel,
  isAuditDecisionPending,
  isAuditConfirmationExpired,
  submitAuditDecision
} = useChatAuditConfirmation({ props, emit, t })
const {
  isImageFile,
  getFilename,
  isPreviewImage,
  getAttachmentSourceUrl,
  getSentFileUrl,
  getFileObjectUrl,
  getAttachmentImageUrls,
  getSentFileImageUrls,
  handleFileDownload
} = useChatMessageFiles({ props, t, getMessageFiles })
const storedRequestMetadataCollapsed = getClientSetting('requestMetadataCollapsed', true)
const requestMetadataCollapsed = ref(typeof storedRequestMetadataCollapsed === 'boolean' ? storedRequestMetadataCollapsed : true)
watch(requestMetadataCollapsed, value => setClientSetting('requestMetadataCollapsed', value))
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
const scrollState = ref(null)
const scrollListeners = new Set()
const codeRefs = new Map()
const collapseModel = computed({
  get: () => props.activeCollapse,
  set: value => emit('update:activeCollapse', value)
})
const reasoningCollapseModel = ref([])
const unreadMessageKeys = ref([])
const latestLlmMessageVisible = ref(true)
const followsOutput = ref(true)
const expandedUserMessages = ref(new Set())
let unreadTrackingReady = false
let unreadTrackingGeneration = 0
let visibilityFrameId = null

const isIncomingMessage = message => message.type === 'tool_group' || (
  message.role !== 'thinking' && (message.role !== 'user' || props.currentSessionReadOnly)
)
const isFollowableIncomingMessage = message => (
  isFollowableLlmOutput(message) || (props.currentSessionReadOnly && message.role === 'user')
)
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
const canFollowOutput = () => scrollState.value?.hasOverflow === false || (followsOutput.value && latestLlmMessageVisible.value)
const updateMessageListBottomState = (offset) => {
  const listElement = virtualList.value?.$el
  const nextScrollState = getMessageListScrollState({
    scrollHeight: listElement?.scrollHeight,
    clientHeight: listElement?.clientHeight,
    scrollTop: Number.isFinite(offset) ? offset : listElement?.scrollTop
  })
  scrollState.value = nextScrollState
  if (!nextScrollState) return messageListAtBottom
  messageListAtBottom = nextScrollState.atBottom
  if (!nextScrollState.hasOverflow) setOutputFollowState(true)
  return messageListAtBottom
}
const calculateLatestLlmMessageVisible = () => {
  if (scrollState.value?.hasOverflow === false) return true
  const latestMessage = [...displayMessages.value].reverse().find(isFollowableIncomingMessage)
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
const refreshLatestLlmMessageVisibility = (offset) => {
  updateMessageListBottomState(offset)
  latestLlmMessageVisible.value = calculateLatestLlmMessageVisible()
  if (canFollowOutput()) unreadMessageKeys.value = []
  return latestLlmMessageVisible.value
}
const scheduleUnreadVisibilityCheck = () => {
  if (messageListUnmounted) return
  if (visibilityFrameId !== null) cancelAnimationFrame(visibilityFrameId)
  visibilityFrameId = requestAnimationFrame(() => {
    visibilityFrameId = null
    if (messageListUnmounted) return
    refreshLatestLlmMessageVisibility()
  })
}
const setUserMessageExpanded = (msg, expanded) => {
  const key = getDisplayKey(msg)
  if (expanded) {
    expandedUserMessages.value.add(key)
  } else {
    expandedUserMessages.value.delete(key)
  }
  scheduleUnreadVisibilityCheck()
}
let messageListResizeObserver = null
const bindMessageListResizeObserver = () => {
  if (messageListResizeObserver === null) {
    messageListResizeObserver = new ResizeObserver(() => scheduleUnreadVisibilityCheck())
  }
  messageListResizeObserver.disconnect()
  const listElement = virtualList.value?.$el
  if (messageListUnmounted || !listElement) {
    scheduleUnreadVisibilityCheck()
    return
  }
  messageListResizeObserver.observe(listElement)
  if (listElement.firstElementChild) messageListResizeObserver.observe(listElement.firstElementChild)
  scheduleUnreadVisibilityCheck()
}
watch(virtualList, bindMessageListResizeObserver, { flush: 'post' })
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
  reasoning_content: message?.reasoning_content,
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
  const shouldFollowOutput = canFollowOutput() && changedMessages.some(isFollowableIncomingMessage)
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

watch(() => props.currentSessionId, () => {
  reasoningCollapseModel.value = []
  expandedUserMessages.value.clear()
}, { flush: 'sync' })

watch(
  () => [props.currentSessionId, props.initialHistoryLoaded, messagesLayoutReady.value],
  () => scheduleUnreadVisibilityCheck(),
  { flush: 'post' }
)

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
  if (didLastDisplayChangePrepend()) {
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
  scrollState.value = null
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
const handleWindowFocus = () => scheduleUnreadVisibilityCheck()
const handleDocumentVisibilityChange = () => scheduleUnreadVisibilityCheck()
onMounted(() => {
  window.addEventListener('focus', handleWindowFocus)
  document.addEventListener('visibilitychange', handleDocumentVisibilityChange)
  bindMessageListResizeObserver()
})
window.addEventListener('pointerup', endPointerScrollSession)
window.addEventListener('pointercancel', endPointerScrollSession)
const handleVirtualScroll = (offset) => {
  const currentOffset = Number.isFinite(offset) ? offset : virtualList.value?.$el?.scrollTop
  const hasValidOffset = Number.isFinite(currentOffset)
  const hasScrollBaseline = Number.isFinite(lastVirtualScrollOffset)
  const offsetChanged = hasScrollBaseline && hasValidOffset && currentOffset !== lastVirtualScrollOffset
  const hasUserScrollCandidate = pointerScrollSession || transientUserScrollCandidate
  const userScrolled = hasValidOffset && (
    (hasUserScrollCandidate && (!hasScrollBaseline || offsetChanged)) ||
    (offsetChanged && !isProgrammaticScrollInProgress())
  )
  const atBottom = updateMessageListBottomState(currentOffset)
  if (userScrolled) {
    setOutputFollowState(atBottom)
    scrollListeners.forEach(listener => listener(offset))
  }
  if (hasValidOffset) lastVirtualScrollOffset = currentOffset

  refreshLatestLlmMessageVisibility(currentOffset)
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
  if (visibilityFrameId !== null) cancelAnimationFrame(visibilityFrameId)
  if (messageListResizeObserver !== null) messageListResizeObserver.disconnect()
  window.removeEventListener('focus', handleWindowFocus)
  document.removeEventListener('visibilitychange', handleDocumentVisibilityChange)
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
