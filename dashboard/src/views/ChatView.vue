<template>
  <div class="chat-view-container">
    <!-- 左侧会话列表侧边栏 -->
    <div class="sessions-sidebar">
      <div class="sidebar-header">
        <span>{{ $t('chat.sessions_title') }}</span>
        <div class="sidebar-actions">
          <el-icon class="refresh-icon" :class="{ loading: sessionsLoading }" :title="$t('chat.refresh_sessions')" @click.stop="loadSessions"><Refresh /></el-icon>
        </div>
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
            <div class="session-title" :title="session.title || $t('chat.session_prefix', { id: session.session_id.substring(0, 8) })">
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
            <div class="session-meta" :title="`${$t('chat.session_created_at')}: ${session.created_at || '-'}\n${$t('chat.session_last_active')}: ${session.last_active || '-'}\n${$t('chat.session_source')}: ${session.source || '-'}`">
              <div class="session-meta-line">
                <span class="session-meta-label">{{ $t('chat.session_created_at') }}</span>
                <span class="session-meta-value">{{ session.created_at || '-' }}</span>
              </div>
              <div class="session-meta-line">
                <span class="session-meta-label">{{ $t('chat.session_last_active') }}</span>
                <span class="session-meta-value">{{ session.last_active || '-' }}</span>
              </div>
              <div class="session-meta-line">
                <span class="session-meta-label">{{ $t('chat.session_source') }}</span>
                <span class="session-meta-value">{{ session.source || '-' }}</span>
              </div>
            </div>
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
      <ChatMessageList
        ref="messageList"
        v-model:active-collapse="activeCollapse"
        :messages="messages"
        :current-session-id="currentSessionId"
        :current-session-enable-markdown="currentSessionEnableMarkdown"
        :current-session-read-only="isCurrentSessionReadOnly"
        :history-loading="historyLoading"
        :initial-history-loaded="initialHistoryLoaded"
        :context-summarizing="isContextSummarizing"
        @audit-decision="handleAuditDecision"
      />
      <div class="input-area">
        <div v-if="isCurrentSessionReadOnly" class="read-only-notice">
          {{ $t('chat.external_session_read_only') }}
        </div>
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
              :disabled="isCurrentSessionReadOnly"
              :before-upload="() => !isCurrentSessionReadOnly"
            >
              <el-button :title="$t('chat.upload')">{{ $t('chat.upload') }}</el-button>
            </el-upload>
          </div>

          <div class="mode-selector">
            <button 
              type="button" 
              :class="['mode-btn', { active: !currentSessionEnableMarkdown }]"
              @click="toggleMarkdown(false)"
              :disabled="loading || isCurrentSessionReadOnly"
            >{{ $t('chat.plain_text') }}</button>
            <button 
              type="button" 
              :class="['mode-btn', { active: currentSessionEnableMarkdown }]"
              @click="toggleMarkdown(true)"
              :disabled="loading || isCurrentSessionReadOnly"
            >{{ $t('chat.md_render') }}</button>
          </div>

          <div class="mode-selector">
            <button 
              type="button" 
              :class="['mode-btn', { active: !isWsModeComputed }]"
              @click="handleModeChange(false)"
              :disabled="loading || isCurrentSessionReadOnly"
            >{{ $t('chat.non_stream') }}</button>
            <button 
              type="button" 
              :class="['mode-btn', { active: isWsModeComputed }]"
              @click="handleModeChange(true)"
              :disabled="loading || isCurrentSessionReadOnly"
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
                :placeholder="isCurrentSessionReadOnly ? $t('chat.external_session_read_only') : $t('chat.input_placeholder')"
                :disabled="isCurrentSessionReadOnly"
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
                  :disabled="isCurrentSessionReadOnly || (!inputMsg.trim() && attachments.length === 0)"
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
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import ChatMessageList from '../components/ChatMessageList.vue'
import { useChatSession } from '../composables/chat/useChatSession'
import { fileApi, chatApi } from '../api'

const { t } = useI18n()

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
  if (isCurrentSessionReadOnly.value) {
    ElMessage.warning(t('chat.external_session_read_only'))
    return
  }

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
  attachments,
  isCurrentSessionReadOnly,
  isContextSummarizing,
  historyLoading,
  initialHistoryLoaded
} = chat

// 解构方法
const {
  loadSessions,
  handleDeleteSession,
  selectSession,
  createNewSession,
  send: originalSend,
  setTransportMode,
  disconnectWebSocket,
  handleScroll
} = chat

// 拦截发送，发送完成后清空列表
const send = async () => {
  if (isCurrentSessionReadOnly.value) {
    ElMessage.warning(t('chat.external_session_read_only'))
    return
  }

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

const handleAuditDecision = async ({ decision }) => {
  if (isCurrentSessionReadOnly.value || loading.value) return
  inputMsg.value = decision === 'approve' ? (t('chat.audit_approve_word')) : (t('chat.audit_reject_word'))
  attachments.value = []
  await originalSend()
}

// 通信模式切换
const handleModeChange = async (val) => {
  if (isCurrentSessionReadOnly.value) {
    ElMessage.warning(t('chat.external_session_read_only'))
    return
  }

  const mode = val ? 'ws' : 'http'
  isWsMode.value = val
  await setTransportMode(mode)
}

// 上传组件文件列表状态绑定
const uploadFileList = ref([])

// 附件上传处理
const handleUpload = async (options) => {
  const { file, onSuccess, onError } = options

  if (isCurrentSessionReadOnly.value) {
    if (onError) onError(new Error(t('chat.external_session_read_only')))
    ElMessage.warning(t('chat.external_session_read_only'))
    return
  }

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
  if (isCurrentSessionReadOnly.value) return

  const clipboardData = e.clipboardData || window.clipboardData
  if (!clipboardData) return
  
  const items = clipboardData.items
  if (!items) return

  for (let i = 0; i < items.length; i++) {
    if (items[i].kind === 'file') {
      const file = items[i].getAsFile()
      if (file) {
        // 拦截文件夹：如果是文件夹，在部分浏览器中其 size 为 0 或 type 为空，通常通过 webkitGetAsEntry 区分
        const entry = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) {
          continue; // 拒绝并忽略文件夹的粘贴
        }

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
