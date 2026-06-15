<template>
  <div class="chat-view-container">
    <!-- 左侧会话列表侧边栏 -->
    <div class="sessions-sidebar">
      <div class="sidebar-header">
        <span>{{ $t('chat.sessions_title') }}</span>
        <el-button type="text" size="small" @click="loadSessions">

          <i class="el-icon-refresh"></i>
        </el-button>
      </div>
      <div class="sessions-list">
        <div 
          v-for="session in sessions" 
          :key="session.session_id"
          :data-session-id="session.session_id"
          :class="['session-item', { active: currentSessionId === session.session_id }]"
          @click="selectSession(session)"
        >
          <div class="session-content">
            <div class="session-id" :title="session.session_id">
              <template v-if="typingSessionId === session.session_id">
                <span 
                  v-for="(char, index) in session.title" 
                  :key="index"
                  class="typing-char"
                >{{ char }}</span>
              </template>
              <template v-else>
                {{ session.title || $t('chat.session_prefix', { id: session.session_id.substring(0, 8) }) }}
              </template>

            </div>
            <div class="session-time">{{ session.last_active }}</div>
          </div>
          <div class="session-actions">
            <el-icon class="delete-icon" @click.stop="handleDeleteSession(session.session_id, session.title || session.session_id)"><Delete /></el-icon>
          </div>
        </div>
        <div v-if="sessions.length === 0 && !sessionsLoading" class="empty-tip">
          {{ $t('chat.no_sessions') }}
        </div>

      </div>
    </div>

    <!-- 右侧聊天区域 -->
    <div class="chat-main">
      <div class="message-list" ref="messageList">
        <div v-if="!currentSessionId && messages.length === 0" class="empty-chat">
          <p>{{ $t('chat.empty_chat_tip') }}</p>
        </div>

        <template v-else>
          <div v-for="msg in messages" :key="msg.id" :class="['message-item', getMessageClass(msg.role), { 'queued': msg.status === 'queued' }]">
            <!-- 普通消息或工具调用消息 -->
            <template v-if="isToolCall(msg)">
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              <el-collapse v-model="activeCollapse">
                <el-collapse-item :name="msg.id">
                  <template #title>
                    <span class="tool-call-title">{{ $t('chat.tool_call', { name: getToolName(msg) }) }}</span>
                  </template>
                  <div class="tool-call-content">
                    <VirtualizedCode :ref="el => setCodeRef(msg.id, el)" :content="getToolArguments(msg)" :max-height="300" />
                  </div>
                </el-collapse-item>
              </el-collapse>
            </template>
            <template v-else-if="isToolResult(msg)">
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              <el-collapse v-model="activeCollapse">
                <el-collapse-item :name="msg.id">
                  <template #title>
                    <span class="tool-result-title">{{ $t('chat.tool_result', { name: getToolResultName(msg) }) }}</span>
                  </template>
                  <div class="tool-result-content">
                    <VirtualizedCode :ref="el => setCodeRef(msg.id, el)" :content="getToolResultContent(msg)" :max-height="300" />
                  </div>
                </el-collapse-item>
              </el-collapse>
            </template>
            <template v-else>
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              
              <!-- 附件渲染区 -->
              <div class="message-attachments" v-if="msg.attachments && msg.attachments.length > 0">
                <div v-for="(att, idx) in msg.attachments" :key="idx" class="message-attachment-item">
                  <template v-if="isImageFile(att)">
                    <el-image 
                      :src="fileApi.getDownloadUrl(att)" 
                      :preview-src-list="[fileApi.getDownloadUrl(att)]"
                      :hide-on-click-modal="true"
                      class="msg-attachment-image"
                      @load="handleImageLoad"
                    ></el-image>
                  </template>
                  <template v-else>
                    <a :href="fileApi.getDownloadUrl(att)" target="_blank" class="msg-attachment-file">
                      <img src="@/assets/svg/document.svg" class="icon-document" />
                      <span class="file-name" :title="getFilename(att)">{{ getFilename(att) }}</span>
                    </a>
                  </template>
                </div>
              </div>

              <!-- 消息主体内容 -->
              <div class="content markdown-body" v-if="typeof msg.content === 'string' && msg.content.trim() && currentSessionEnableMarkdown">
                <!-- 当处于 queued 时显示 Loading 动画图标 -->
                <div class="queued-indicator" v-if="msg.status === 'queued'">
                  <img src="@/assets/svg/wait.svg" class="is-loading" />
                </div><div v-html="renderMarkdown(msg.content)"></div>
              </div>
              <div class="content" style="white-space: pre-wrap;" v-else-if="typeof msg.content === 'string' && msg.content.trim() && !currentSessionEnableMarkdown">
                <!-- 当处于 queued 时显示 Loading 动画图标 -->
                <div class="queued-indicator" v-if="msg.status === 'queued'">
                  <img src="@/assets/svg/wait.svg" class="is-loading" />
                </div><template v-for="(part, idx) in renderTextWithLinks(msg.content)" :key="idx">
                  <el-link
                    v-if="part.type === 'link'"
                    :href="part.href"
                    class="message-link"
                    type="primary"
                    target="_blank"
                    rel="noopener noreferrer"
                    underline
                  >{{ part.text }}</el-link><span v-else>{{ part.text }}</span>
                </template>
              </div>
              <div class="content" v-else-if="Array.isArray(msg.content)">
                <!-- 当处于 queued 时显示 Loading 动画图标 -->
                <div class="queued-indicator" v-if="msg.status === 'queued'">
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
                        underline
                      >{{ textPart.text }}</el-link><span v-else>{{ textPart.text }}</span>
                    </template>
                  </div>
                  <div v-else-if="part.type === 'image_url'" class="image-part">
                    <el-image 
                      :src="part.image_url.url" 
                      :preview-src-list="[part.image_url.url]"
                      :hide-on-click-modal="true"
                      class="msg-image"
                      @load="handleImageLoad"
                    ></el-image>
                  </div>
                  <div v-else class="text-part">{{ JSON.stringify(part) }}</div>
                </div>
              </div>
            </template>
          </div>
        </template>
      </div>
      <div class="input-area">
        <div class="toolbar-row">
          <el-button type="primary" @click="createNewSession" class="new-session-btn">
            <i class="el-icon-plus"></i> {{ $t('chat.new_session') }}
          </el-button>
          
          <!-- 上传按钮移到模式选择之前 -->
          <div class="upload-trigger-btn">
            <el-upload
              action=""
              :http-request="handleUpload"
              :show-file-list="false"
              multiple
              :before-upload="() => true"
            >
              <el-button :title="$t('chat.upload')">{{ $t('chat.upload') }}</el-button>
            </el-upload>
          </div>

          <div class="mode-selector">
            <button 
              type="button" 
              :class="['mode-btn', { active: !currentSessionEnableMarkdown }]"
              @click="toggleMarkdown(false)"
              :disabled="loading"
            >{{ $t('chat.plain_text') }}</button>
            <button 
              type="button" 
              :class="['mode-btn', { active: currentSessionEnableMarkdown }]"
              @click="toggleMarkdown(true)"
              :disabled="loading"
            >{{ $t('chat.md_render') }}</button>
          </div>

          <div class="mode-selector">
            <button 
              type="button" 
              :class="['mode-btn', { active: !isWsModeComputed }]"
              @click="handleModeChange(false)"
              :disabled="loading"
            >{{ $t('chat.non_stream') }}</button>
            <button 
              type="button" 
              :class="['mode-btn', { active: isWsModeComputed }]"
              @click="handleModeChange(true)"
              :disabled="loading"
            >{{ $t('chat.stream') }}</button>
          </div>
        </div>
        <div class="input-wrapper">
          <div class="input-controls">
            <div class="chat-input-box">
              <!-- 自定义附件展示区域（取代 el-upload 原生列表） -->
              <div class="upload-container" v-show="uploadFileList.length > 0">
                <div class="custom-upload-list">
                  <div class="custom-upload-item" v-for="file in uploadFileList" :key="file.uid">
                    <el-image 
                      v-if="file.url"
                      class="custom-upload-img" 
                      :src="file.url" 
                      fit="contain"
                      :preview-src-list="[file.url]"
                      :hide-on-click-modal="true"
                    />
                    <div v-else class="custom-upload-file">
                      <img src="@/assets/svg/document.svg" class="icon-document-large" />
                      <span class="file-name" :title="file.name">{{ file.name }}</span>
                    </div>
                    <!-- 右上角删除按钮 -->
                    <div class="custom-upload-remove" @click="handleRemoveCustomFile(file)">
                      <img src="@/assets/svg/close.svg" class="icon-close" />
                    </div>
                  </div>
                </div>
              </div>

              <el-input
                v-model="inputMsg" 
                :placeholder="$t('chat.input_placeholder')" 
                @keyup.enter="send"
                @paste="handlePaste"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 6 }"
                class="chat-input"
                :resize="'none'"
              />

              <div class="action-btn-container">
                <el-button 
                  type="primary" 
                  @click="send" 
                  :disabled="!inputMsg.trim() && attachments.length === 0"
                  class="action-btn"                  
                  circle
                >
                  <el-icon style="margin-left: -2px;margin-top: 2px;"><Position /></el-icon>
                </el-button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { ElCollapse, ElCollapseItem } from 'element-plus'
import VirtualizedCode from '../components/VirtualizedCode.vue'
import { useChatSession } from '../composables/chat/useChatSession'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { fileApi, chatApi } from '../api'

const { t } = useI18n()

import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'
import 'github-markdown-css/github-markdown.css'

const md = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
  highlight: function (str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return '<pre class="hljs"><code>' +
               hljs.highlight(str, { language: lang, ignoreIllegals: true }).value +
               '</code></pre>'
      } catch (__) {}
    }
    return '<pre class="hljs"><code>' + md.utils.escapeHtml(str) + '</code></pre>'
  }
})

const defaultLinkOpenRender = md.renderer.rules.link_open || ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('class', 'el-link el-link--primary is-underline message-link')
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpenRender(tokens, idx, options, env, self)
}

const renderMarkdown = (text) => {
  return md.render(text || '')
}

const renderTextWithLinks = (text) => {
  if (!text) return []

  const matches = md.linkify.match(text)
  if (!matches) {
    return [{ type: 'text', text }]
  }

  const parts = []
  let lastIndex = 0

  matches.forEach(match => {
    if (match.index > lastIndex) {
      parts.push({
        type: 'text',
        text: text.slice(lastIndex, match.index)
      })
    }

    parts.push({
      type: 'link',
      text: match.text,
      href: match.url
    })

    lastIndex = match.lastIndex
  })

  if (lastIndex < text.length) {
    parts.push({
      type: 'text',
      text: text.slice(lastIndex)
    })
  }

  return parts
}

const chat = useChatSession()

// 获取当前会话的 Markdown 开关状态
const currentSessionEnableMarkdown = computed({
  get() {
    if (!currentSessionId.value) return chat.enableMarkdownDefault.value
    const session = sessions.value.find(s => s.session_id === currentSessionId.value)
    return session ? session.enable_markdown : false
  },
  set(val) {
    if (!currentSessionId.value) {
      chat.enableMarkdownDefault.value = val
      return
    }
    const session = sessions.value.find(s => s.session_id === currentSessionId.value)
    if (session) {
      session.enable_markdown = val
    }
  }
})

// 切换 Markdown 状态
const toggleMarkdown = async (val) => {
  // 先更新本地状态
  currentSessionEnableMarkdown.value = val
  
  if (!currentSessionId.value) {
    return
  }
  
  try {
    await chatApi.updateSessionSetting(currentSessionId.value, val)
  } catch (error) {
    ElMessage.error(error.message || t('chat.setting_failed'))
    // 回退状态
    currentSessionEnableMarkdown.value = !val
  }
}

// 本地流式模式状态（从 transportMode 计算）
const isWsMode = ref(true)

// 计算属性：根据 transportMode 计算当前是否为流式模式
const isWsModeComputed = computed(() => transportMode.value === 'ws')

// 解构状态
const {
  messages,
  inputMsg,
  loading,
  messageList,
  sessions,
  sessionsLoading,
  currentSessionId,
  typingSessionId,
  activeCollapse,
  transportMode,
  wsConnected,
  sessionCreating,
  attachments
} = chat

// 用于管理 VirtualizedCode 的引用
const codeRefs = new Map()
const setCodeRef = (id, el) => {
  if (el) {
    codeRefs.set(id, el)
  } else {
    codeRefs.delete(id)
  }
}

// 监听折叠面板状态变化，当收起时重置 limit
watch(activeCollapse, (newVal, oldVal) => {
  // 找出被移除的 id (即被收起的面板)
  const closedIds = oldVal.filter(id => !newVal.includes(id))
  closedIds.forEach(id => {
    const ref = codeRefs.get(id)
    if (ref && typeof ref.reset === 'function') {
      ref.reset()
    }
  })
}, { deep: true })

// 解构方法
const {
  loadSessions,
  handleDeleteSession,
  selectSession,
  createNewSession,
  send: originalSend,
  setTransportMode,
  disconnectWebSocket,
  formatTimestamp,
  isToolCall,
  isToolResult,
  getToolName,
  getToolArguments,
  getToolResultName,
  getToolResultContent,
  getMessageTimestamp,
  handleScroll
} = chat

// 解决图片异步加载导致滚动定位不准的问题
const handleImageLoad = () => {
  if (messageList.value) {
    messageList.value.scrollTo({
      top: messageList.value.scrollHeight,
      behavior: 'smooth'
    })
  }
}

// 拦截发送，发送完成后清空列表
const send = async () => {
  if (loading.value) {
    // LLM响应中，加入前端队列并显示临时消息
    const tempMsg = inputMsg.value
    const tempAttachments = [...attachments.value]
    
    // 如果没有内容直接返回
    if (!tempMsg.trim() && tempAttachments.length === 0) return
    
    // 清空输入框和附件
    inputMsg.value = ''
    uploadFileList.value = []
    attachments.value = []
    
    // 加入队列视觉状态并直接发送
    chat.enqueueMessage(tempMsg, tempAttachments)
    return
  }

  // 正常发送
  const promise = originalSend()
  uploadFileList.value = []
  await promise
}

// 通信模式切换
const handleModeChange = async (val) => {
  isWsMode.value = val
  await setTransportMode(val ? 'ws' : 'http')
}

// 上传组件文件列表状态绑定
const uploadFileList = ref([])

// 附件上传处理
const handleUpload = async (options) => {
  const { file, onSuccess, onError } = options
  
  try {
    // 允许 session_id 为空，由后端分配未绑定的临时目录
    const res = await fileApi.upload(file, currentSessionId.value || '')
    
    // 维护后端真实路径
    attachments.value.push({
      uid: file.uid, // 用于和 el-upload 的 file_list 关联
      name: res.data?.filename || file.name,
      path: res.data?.path
    })
    
    // 通知 el-upload 组件该文件上传成功
    if (onSuccess) onSuccess(res.data)
      
    // 将文件添加到 el-upload 维护的文件列表中（如果是通过独立按钮触发的话需要手动 push）
    const isImage = file.type.startsWith('image/')
    const newFileItem = {
      uid: file.uid,
      name: file.name,
      status: 'success',
      url: isImage ? URL.createObjectURL(file) : '' // 仅图片生成本地预览图 URL
    }
    
    // 防止重复添加（el-upload 自身的 picture-card 也会触发 push，这里做去重）
    const exists = uploadFileList.value.find(f => f.uid === file.uid)
    if (!exists) {
      uploadFileList.value.push(newFileItem)
    }

    ElMessage.success(t('chat.upload_success'))
  } catch (error) {
    if (onError) onError(error)
    ElMessage.error(error.message || t('chat.upload_failed'))
  }
}

const handleRemoveCustomFile = (file) => {
  // 根据 uid 找到并移除对应的附件数据
  const attIndex = attachments.value.findIndex(a => a.uid === file.uid)
  if (attIndex !== -1) {
    attachments.value.splice(attIndex, 1)
  }
  // 从上传列表中移除
  const listIndex = uploadFileList.value.findIndex(f => f.uid === file.uid)
  if (listIndex !== -1) {
    uploadFileList.value.splice(listIndex, 1)
  }
}

// 处理粘贴上传
const handlePaste = (e) => {
  const clipboardData = e.clipboardData || window.clipboardData
  if (!clipboardData) return
  
  const items = clipboardData.items
  if (!items) return

  let hasFile = false
  for (let i = 0; i < items.length; i++) {
    if (items[i].kind === 'file') {
      const file = items[i].getAsFile()
      if (file) {
        // 拦截文件夹：如果是文件夹，在部分浏览器中其 size 为 0 或 type 为空，通常通过 webkitGetAsEntry 区分
        const entry = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) {
          continue; // 拒绝并忽略文件夹的粘贴
        }

        hasFile = true
        // 如果没有名字，通常是截图，给个默认名字
        if (file.name === 'image.png' || !file.name) {
          Object.defineProperty(file, 'name', {
            writable: true,
            value: `screenshot_${Date.now()}.png`
          })
        }
        // 复用 handleUpload 处理，手动指定一个临时 uid
        file.uid = Date.now() + i
        handleUpload({ file })
      }
    }
  }
}

// 判断是否为图片文件
const isImageFile = (path) => {
  if (!path) return false
  const ext = path.split('.').pop().toLowerCase()
  return ['png', 'jpg', 'jpeg', 'gif', 'webp'].includes(ext)
}

// 提取文件名
const getFilename = (path) => {
  if (!path) return t('chat.unknown_file')
  const name = path.split(/[/\\]/).pop()
  // 去除 8位uuid_ 前缀
  return name.length > 9 && name[8] === '_' ? name.substring(9) : name
}

// 消息角色映射到CSS类名的辅助函数
const getMessageClass = (role) => {
  const roleMap = {
    user: 'user',
    thinking: 'thinking',
    err: 'error'
  }
  return roleMap[role] || 'ai'
}

onMounted(() => {
  loadSessions()
  if (messageList.value) {
    messageList.value.addEventListener('scroll', handleScroll)
  }
})

onUnmounted(() => {
  if (messageList.value) {
    messageList.value.removeEventListener('scroll', handleScroll)
  }
  disconnectWebSocket()
})
</script>

<style lang="scss">
@import "@/assets/css/ChatView.scss";
</style>
