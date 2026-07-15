<template>
  <div class="message-list-wrapper">
    <VList
      ref="virtualList"
      class="message-list"
      :class="{ 'is-layout-ready': messagesLayoutReady }"
      :data="displayMessages"
      :buffer-size="600"
      :shift="maintainScrollPosition"
      @scroll="handleVirtualScroll"
    >
      <template #default="{ item: msg }">
        <div class="message-list-item" :key="msg.id">
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
import { computed, nextTick, ref, watch } from 'vue'
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
  activeCollapse: { type: Array, default: () => [] },
  historyLoading: { type: Boolean, default: false },
  initialHistoryLoaded: { type: Boolean, default: true },
  contextSummarizing: { type: Boolean, default: false }
})
const emit = defineEmits(['update:activeCollapse'])
const { t } = useI18n()
const virtualList = ref(null)
const maintainScrollPosition = ref(false)
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

const displayMessages = computed(() => {
  const output = []
  let activeToolGroup = null

  props.messages.forEach((message) => {
    if (isToolCall(message)) {
      const responseId = message.response_id || null
      if (!activeToolGroup || !responseId || activeToolGroup.response_id !== responseId) {
        activeToolGroup = {
          id: `tool_group_${responseId || message.id}`,
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
      getToolCalls(message).forEach((toolCall, index) => {
        const toolCallId = getToolCallId(toolCall, `${message.id}_${index}`)
        let pair = activeToolGroup.pairs.find(item => item.id === toolCallId)
        if (!pair) {
          pair = { id: toolCallId, toolCall, resultMessage: null }
          activeToolGroup.pairs.push(pair)
        } else {
          pair.toolCall = toolCall
        }
        activeToolGroup.latestEvent = { type: 'call', pair }
      })
      return
    }

    if (isToolResult(message)) {
      const toolCallId = getToolResultCallId(message)
      let targetGroup = activeToolGroup
      let pair = targetGroup?.pairs.find(item => item.id === toolCallId)
      if (!pair) {
        targetGroup = [...output].reverse().find(item => item.type === 'tool_group' && item.pairs.some(toolPair => toolPair.id === toolCallId))
        pair = targetGroup?.pairs.find(item => item.id === toolCallId)
      }
      if (!targetGroup) {
        targetGroup = {
          id: `tool_group_${message.response_id || message.id}`,
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
        pair = { id: toolCallId || message.id, toolCall: null, resultMessage: null }
        targetGroup.pairs.push(pair)
      }
      pair.resultMessage = message
      targetGroup.latestEvent = { type: 'result', pair }
      activeToolGroup = targetGroup
      return
    }

    activeToolGroup = null
    output.push(message)
  })
  return output
})

let layoutGeneration = 0
watch(() => props.currentSessionId, () => {
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

const handleVirtualScroll = (offset) => {
  scrollListeners.forEach(listener => listener(offset))
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
let bottomScrollRequest = 0
const scrollToBottom = async (behavior = 'auto') => {
  const request = ++bottomScrollRequest
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

  await nextTick()
  if (behavior === 'smooth') {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    if (request !== bottomScrollRequest || !alignBottom(true)) return
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
const handleImageLoad = () => {
  const list = virtualList.value
  if (!list || list.scrollSize - list.viewportSize - list.scrollOffset > 200) return
  scrollToBottom('smooth')
}

defineExpose({
  captureScrollAnchor,
  restoreScrollAnchor,
  scrollToBottom,
  scrollTo: options => virtualList.value?.scrollTo(typeof options === 'number' ? options : options?.top || 0),
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
