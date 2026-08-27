<template>
  <div class="chat-view-container">
    <!-- 左侧会话列表侧边栏 -->
    <div class="sessions-sidebar">
      <div class="sidebar-header">
        <span>{{ $t('chat.sessions_title') }}</span>
        <div class="sidebar-actions">
          <el-icon
            class="sidebar-icon"
            :title="$t('chat.new_session_title')"
            @click.stop="createNewSession"
          ><Plus /></el-icon>
          <el-icon
            class="sidebar-icon refresh-icon"
            :class="{ loading: sessionsLoading }"
            :title="$t('chat.refresh_sessions')"
            @click.stop="loadSessions"
          ><Refresh /></el-icon>
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
          <div
            v-if="session.is_loading"
            class="session-loading-indicator"
            :title="$t('chat.session_reply_in_progress')"
            role="status"
            aria-live="polite"
          ></div>
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
        :llm-request-metadata="llmRequestMetadata"
        :current-session-info="currentSessionInfo"
        :hide-empty-tip="!currentSessionId"
        @audit-decision="handleAuditDecision"
      />

      <!-- 新建会话 / 无会话时的欢迎区 -->
      <div class="welcome-hero">
        <h1 class="welcome-greeting" :class="{ 'is-exiting': sessionEngaged }">{{ $t('chat.welcome_greeting') }}</h1>
      </div>

      <div class="input-area" :style="{ transform: !sessionEngaged ? 'translateY(-300px)' : '' }">
        <div v-if="isCurrentSessionReadOnly" class="read-only-notice">
          <span class="read-only-notice-text">{{ $t('chat.external_session_read_only') }}</span>
        </div>

        <div class="input-wrapper" :style="{ maxWidth: !sessionEngaged ? '640px' : '100%' }">
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
                      preview-teleported
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

              <div class="chat-input-row">
                <el-popover
                  v-model:visible="moreOptionsVisible"
                  placement="bottom-start"
                  :width="280"
                  :show-arrow="false"
                  popper-class="chat-more-options-popover"
                  :disabled="isCurrentSessionReadOnly"
                >
                  <template #reference>
                    <el-button
                      class="more-options-trigger"
                      :disabled="isCurrentSessionReadOnly"
                      :title="$t('chat.more_options')"
                      circle
                    >
                      <el-icon><Plus /></el-icon>
                    </el-button>
                  </template>

                  <div class="more-options-content">
                    <div
                      class="more-option-row"
                      :class="{ 'is-disabled': isCurrentSessionReadOnly }"
                      @click="!isCurrentSessionReadOnly && openUploadPicker()"
                    >
                      <el-icon class="more-option-icon"><UploadFilled /></el-icon>
                      <span class="more-option-label">{{ $t('chat.more_options_upload') }}</span>
                    </div>

                    <el-upload
                      ref="uploadTriggerRef"
                      action=""
                      :http-request="handleUpload"
                      :show-file-list="false"
                      multiple
                      :disabled="isCurrentSessionReadOnly"
                      :before-upload="() => !isCurrentSessionReadOnly"
                      class="more-option-upload-hidden"
                    />

                    <div class="more-option-divider"></div>

                    <div class="more-option-segment">
                      <span class="more-option-label">{{ $t('chat.more_options_plain') }} / {{ $t('chat.more_options_markdown') }}</span>
                      <el-radio-group
                        :model-value="currentSessionEnableMarkdown ? 'md' : 'plain'"
                        :disabled="isCurrentSessionReadOnly"
                        size="small"
                        @update:model-value="val => toggleMarkdown(val === 'md')"
                      >
                        <el-radio-button label="plain">{{ $t('chat.more_options_plain') }}</el-radio-button>
                        <el-radio-button label="md">{{ $t('chat.more_options_markdown') }}</el-radio-button>
                      </el-radio-group>
                    </div>

                    <div class="more-option-divider"></div>

                    <div class="more-option-segment">
                      <span class="more-option-label">{{ $t('chat.more_options_non_stream') }} / {{ $t('chat.more_options_stream') }}</span>
                      <el-radio-group
                        :model-value="isWsModeComputed ? 'stream' : 'non_stream'"
                        :disabled="isCurrentSessionReadOnly"
                        size="small"
                        @update:model-value="val => handleModeChange(val === 'stream')"
                      >
                        <el-radio-button label="non_stream">{{ $t('chat.more_options_non_stream') }}</el-radio-button>
                        <el-radio-button label="stream">{{ $t('chat.more_options_stream') }}</el-radio-button>
                      </el-radio-group>
                    </div>

                    <div class="more-option-divider"></div>

                    <div class="more-option-row more-option-profile">
                      <span class="more-option-label more-option-label--profile">{{ $t('chat.more_options_profile') }}</span>
                      <el-select
                        class="more-option-profile-select"
                        :model-value="currentSessionProfileDisplayId"
                        clearable
                        filterable
                        :loading="profilesLoading"
                        :disabled="isCurrentSessionReadOnly || profileSettingSubmitting"
                        :placeholder="currentSessionProfilePlaceholder"
                        @change="updateSessionProfileOverride"
                        size="small"
                      >
                        <el-option
                          v-for="profile in currentSessionProfileOptions"
                          :key="profile.id"
                          :label="formatProfileOptionLabel(profile, $t('chat.default_profile_suffix'))"
                          :value="profile.id"
                        />
                      </el-select>
                    </div>

                    <div class="more-option-divider"></div>

                    <div class="more-option-toggle">
                      <span class="more-option-label">{{ $t('chat.more_options_tool_output') }}</span>
                      <el-switch
                        :model-value="currentSessionShowToolCalls"
                        :disabled="isCurrentSessionReadOnly || toolOutputSettingSubmitting || loading"
                        @update:model-value="updateSessionShowToolCalls"
                      />
                    </div>
                  </div>
                </el-popover>

                <el-input
                  v-model="inputMsg"
                  :placeholder="isCurrentSessionReadOnly ? $t('chat.guidance_placeholder') : $t('chat.input_placeholder')"
                  :disabled="isCurrentSessionReadOnly && guidanceSubmitting"
                  :maxlength="isCurrentSessionReadOnly ? 500 : undefined"
                  :show-word-limit="false"
                  @keydown.enter="(e) => { if (e.shiftKey) return; e.preventDefault(); send(); }"
                  @paste="handlePaste"
                  type="textarea"
                  :autosize="{ minRows: 1, maxRows: 6 }"
                  class="chat-input"
                  :resize="'none'"
                />

                <div class="action-btn-container">
                  <el-button
                    type="primary"
                    @click="send"
                    :loading="isCurrentSessionReadOnly && guidanceSubmitting"
                    :disabled="isCurrentSessionReadOnly ? guidanceSubmitting || !inputMsg.trim() : !inputMsg.trim() && attachments.length === 0"
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

  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Plus, Refresh, UploadFilled } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import ChatMessageList from '../components/ChatMessageList.vue'
import { useChatSession } from '../composables/chat/useChatSession'
import { fileApi, chatApi, profileApi } from '../api'
import {
  filterProfilesByUid,
  formatProfileOptionLabel,
  resolveSessionProfileDisplayId,
  resolveSessionProfilePlaceholder,
  resolveProfileOwnerUid
} from '../utils/profileOptions'

const { t } = useI18n()

const chat = useChatSession()
const profiles = ref([])
const currentUid = ref(null)
const profilesLoading = ref(false)
const profileSettingSubmitting = ref(false)
const toolOutputSettingSubmitting = ref(false)
const moreOptionsVisible = ref(false)
const uploadTriggerRef = ref(null)

const openUploadPicker = () => {
  const triggerEl = uploadTriggerRef.value?.$el || uploadTriggerRef.value
  const input = triggerEl?.querySelector?.('input[type="file"]')
  if (input) input.click()
}

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

// 当前会话的创建/活跃时间，供 ChatMessageList 显示
const currentSessionInfo = computed(() => {
  if (!currentSessionId.value) return null
  return sessions.value.find(s => s.session_id === currentSessionId.value) || null
})

// 是否处于"会话已开始"状态：已有消息（已发送）或者正在加载（发送中）
// 让用户点击发送的瞬间就触发输入框和欢迎区的过渡动画，避免等待接口响应
const sessionEngaged = computed(() => !!currentSessionId.value || chat.loading.value || chat.messages.value.length > 0)

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
    await chatApi.updateSessionSetting(currentSessionId.value, { enable_markdown: val })
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
  currentSession,
  transportMode,
  attachments,
  isCurrentSessionReadOnly,
  externalSessionAutoPullEnabled,
  isContextSummarizing,
  llmRequestMetadata,
  historyLoading,
  initialHistoryLoaded,
  newSessionProfileOverrideId,
  currentSessionShowToolCalls
} = chat

const currentSessionProfileDisplayId = computed(() => resolveSessionProfileDisplayId(
  currentSession.value,
  newSessionProfileOverrideId.value
))
const currentSessionProfileOptions = computed(() => filterProfilesByUid(
  profiles.value,
  resolveProfileOwnerUid(currentSession.value, currentUid.value)
))
const currentSessionProfilePlaceholder = computed(() => resolveSessionProfilePlaceholder(
  currentSessionProfileOptions.value,
  isCurrentSessionReadOnly.value,
  t('chat.default_profile_suffix'),
  t('chat.inherited_profile')
))

const loadProfiles = async () => {
  profilesLoading.value = true
  try {
    const res = await profileApi.list({ page: 1, size: 1000 })
    profiles.value = res.data.data.items || []
    currentUid.value = res.data.data.meta?.current_uid || null
  } catch (error) {
    ElMessage.error(t('chat.load_profiles_failed'))
  } finally {
    profilesLoading.value = false
  }
}

const updateSessionProfileOverride = async (profileId) => {
  const sessionId = currentSessionId.value
  if (!sessionId) {
    newSessionProfileOverrideId.value = profileId ?? null
    return
  }
  if (profileSettingSubmitting.value) return

  const previousProfileOverrideId = currentSession.value?.profile_override_id ?? null
  profileSettingSubmitting.value = true
  try {
    await chatApi.updateSessionSetting(sessionId, { profile_override_id: profileId ?? null })
    const session = sessions.value.find(item => item.session_id === sessionId)
    if (session) session.profile_override_id = profileId ?? null
    ElMessage.success(t('chat.profile_setting_saved'))
  } catch (error) {
    const session = sessions.value.find(item => item.session_id === sessionId)
    if (session) session.profile_override_id = previousProfileOverrideId
    ElMessage.error(t('chat.setting_failed'))
  } finally {
    profileSettingSubmitting.value = false
  }
}

const updateSessionShowToolCalls = async (showToolCalls) => {
  if (toolOutputSettingSubmitting.value || loading.value) return

  const sessionId = currentSessionId.value
  const previousValue = currentSessionShowToolCalls.value
  currentSessionShowToolCalls.value = showToolCalls
  if (!sessionId) return

  toolOutputSettingSubmitting.value = true
  try {
    await chatApi.updateSessionSetting(sessionId, { show_tool_calls: showToolCalls })
    await reloadCurrentSessionHistory()
  } catch (error) {
    currentSessionShowToolCalls.value = previousValue
    ElMessage.error(error.message || t('chat.setting_failed'))
  } finally {
    toolOutputSettingSubmitting.value = false
  }
}

// 解构方法
const {
  loadSessions,
  handleDeleteSession,
  selectSession,
  createNewSession,
  reloadCurrentSessionHistory,
  send: originalSend,
  setTransportMode,
  disconnectWebSocket,
  handleScroll
} = chat

const guidanceSubmitting = ref(false)

// 拦截发送，发送完成后清空列表
const send = async () => {
  if (isCurrentSessionReadOnly.value) {
    const content = inputMsg.value.trim()
    const sessionId = currentSessionId.value
    if (!content || !sessionId || guidanceSubmitting.value) return

    guidanceSubmitting.value = true
    try {
      const response = await chatApi.createGuidance({ session_id: sessionId, content })
      const guidanceMessage = response.data?.data
      const guidanceMessageIds = [guidanceMessage?.id, guidanceMessage?.db_id]
        .filter(id => id !== undefined && id !== null)
        .map(String)

      if (sessionId === currentSessionId.value && guidanceMessage) {
        const exists = guidanceMessageIds.length > 0 && messages.value.some(message =>
          [message.id, message.db_id].some(id => guidanceMessageIds.includes(String(id)))
        )
        if (!exists) {
          messages.value.push({
            ...guidanceMessage,
            db_id: guidanceMessage.db_id ?? guidanceMessage.id
          })
          await nextTick()
          await messageList.value?.scrollToBottom('auto')
        }
      }
      inputMsg.value = ''
      ElMessage.success(t('chat.guidance_created'))
      void loadSessions()
    } catch (error) {
      ElMessage.error(error.message || t('chat.guidance_create_failed'))
    } finally {
      guidanceSubmitting.value = false
    }
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
  inputMsg.value = decision === 'approve'
    ? t('chat.audit_approve_word')
    : decision === 'ignore'
      ? t('chat.audit_ignore_word')
      : t('chat.audit_reject_word')
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
  loadProfiles()
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
