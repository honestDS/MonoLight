<template>
  <div class="chat-view-container">
    <Transition name="sessions-panel">
      <aside
        v-show="sessionsPanelOpen"
        ref="sessionsPanelRef"
        id="chat-sessions-panel"
        class="sessions-panel"
        aria-labelledby="chat-sessions-panel-title"
        :inert="!sessionsPanelOpen"
        @keydown.esc.stop="closeSessionsPanel"
      >
        <div class="sidebar-header">
          <h2 id="chat-sessions-panel-title" ref="sessionsPanelTitleRef" tabindex="-1">
            {{ $t('chat.sessions_title') }}
          </h2>
          <div class="sidebar-actions">
            <button
              type="button"
              class="sidebar-icon"
              :aria-label="$t('chat.new_session_title')"
              :title="$t('chat.new_session_title')"
              @click.stop="handleCreateNewSession"
            >
              <el-icon aria-hidden="true"><Plus /></el-icon>
            </button>
            <button
              type="button"
              class="sidebar-icon refresh-icon"
              :class="{ loading: sessionsLoading }"
              :aria-label="$t('chat.refresh_sessions')"
              :title="$t('chat.refresh_sessions')"
              :disabled="sessionsLoading"
              :aria-busy="sessionsLoading"
              @click.stop="loadSessions"
            >
              <el-icon class="refresh-icon-glyph" aria-hidden="true"><Refresh /></el-icon>
            </button>
          </div>
        </div>
        <div class="sessions-list">
          <template v-for="group in groupedSessions" :key="group.key">
            <button
              type="button"
              :class="['session-group-title', { 'is-collapsed': collapsedGroups.has(group.key) }]"
              :aria-expanded="!collapsedGroups.has(group.key)"
              @click="toggleGroup(group.key)"
            >
              <span class="session-group-title-text" :title="group.label">{{ group.label }}</span>
              <el-icon class="session-group-chevron" aria-hidden="true"><ArrowDown /></el-icon>
            </button>
            <Transition
              @before-enter="handleSessionGroupBeforeEnter"
              @enter="handleSessionGroupEnter"
              @after-enter="resetSessionGroupTransition"
              @before-leave="handleSessionGroupBeforeLeave"
              @leave="handleSessionGroupLeave"
              @after-leave="resetSessionGroupTransition"
            >
              <div
                v-show="!collapsedGroups.has(group.key)"
                :inert="collapsedGroups.has(group.key)"
                class="session-group-body"
              >
                <div
                  v-for="session in group.sessions"
                  :key="session.session_id"
                  :data-session-id="session.session_id"
                  :class="['session-item', { active: currentSessionId === session.session_id }]"
                >
                  <button
                    type="button"
                    class="session-content"
                    :aria-current="currentSessionId === session.session_id ? 'page' : undefined"
                    @click="handleSelectSession(session)"
                  >
                    <span class="session-title" :title="session.title || $t('chat.session_prefix', { id: session.session_id.substring(0, 8) })">
                      <span :id="`chat-session-title-${session.session_id}`" class="session-title-text">
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
                      </span>
                      <span
                        v-if="session.is_loading"
                        class="session-loading-indicator"
                        :title="$t('chat.session_reply_in_progress')"
                        role="status"
                        aria-live="polite"
                      ></span>
                    </span>
                    <span class="session-meta" :title="`${$t('chat.session_source')}: ${formatSessionSource(session.source) || '-'}`">
                      <span v-if="session.source" class="session-source">{{ formatSessionSource(session.source) }}</span>
                    </span>
                  </button>
                  <div class="session-actions">
                    <button
                      type="button"
                      class="delete-icon"
                      :aria-label="$t('common.delete')"
                      :title="$t('common.delete')"
                      :aria-describedby="`chat-session-title-${session.session_id}`"
                      @click.stop="handleDeleteSession(session.session_id, session.title || session.session_id)"
                    >
                      <el-icon aria-hidden="true"><Delete /></el-icon>
                    </button>
                  </div>
                </div>
              </div>
            </Transition>
          </template>
          <div v-if="groupedSessions.length === 0 && !sessionsLoading" class="empty-tip">
            {{ $t('chat.no_sessions') }}
          </div>
        </div>
      </aside>
    </Transition>

    <!-- 聊天主区域 -->
    <div ref="chatMainRef" class="chat-main" :class="{ 'is-welcome': !sessionEngaged }">
      <ChatMessageList
        ref="messageList"
        v-model:active-collapse="activeCollapse"
        :messages="renderedMessages"
        :current-session-id="currentSessionId"
        :current-session-enable-markdown="currentSessionEnableMarkdown"
        :current-session-show-reasoning="currentSessionShowReasoning"
        :current-session-read-only="isCurrentSessionReadOnly"
        :confirmation-busy="loading || agentSettingSubmitting"
        :history-loading="historyLoading"
        :initial-history-loaded="renderedInitialHistoryLoaded"
        :context-summarizing="isContextSummarizing"
        :hide-empty-tip="!currentSessionId"
        @audit-decision="handleAuditDecision"
      />

      <div class="input-area" @transitionend.self="handleWelcomeExitTransitionEnd">
        <!-- 新建会话 / 无会话时的欢迎区 -->
        <div class="welcome-hero" :class="{ 'is-exiting': sessionEngaged }">
          <h1 class="welcome-greeting">{{ $t('chat.welcome_greeting') }}</h1>
        </div>

        <div
          v-if="isCurrentSessionReadOnly"
          id="external-session-guidance-notice"
          class="read-only-notice"
          role="note"
        >
          <el-icon class="read-only-notice-icon" aria-hidden="true"><InfoFilled /></el-icon>
          <span class="read-only-notice-text">{{ $t('chat.external_session_read_only') }}</span>
        </div>

        <div class="input-wrapper">
          <ChatRequestMetadata
            v-if="sessionEngaged"
            :metadata="llmRequestMetadata"
            :session-info="currentSessionInfo"
            :context-summarizing="isContextSummarizing"
          />

          <div class="chat-composer-actions">
            <button
              ref="sessionsTriggerRef"
              type="button"
              class="chat-composer-action"
              :aria-expanded="sessionsPanelOpen"
              aria-controls="chat-sessions-panel"
              :aria-label="$t(sessionsPanelOpen ? 'chat.collapse_sessions' : 'chat.expand_sessions')"
              :title="$t(sessionsPanelOpen ? 'chat.collapse_sessions' : 'chat.expand_sessions')"
              @click="toggleSessionsPanel"
              @keydown.esc.stop="closeSessionsPanel"
            >
              <el-icon aria-hidden="true"><ChatLineSquare /></el-icon>
              <span>{{ $t('chat.sessions_title') }}</span>
            </button>
            <button
              type="button"
              class="chat-composer-action"
              :aria-label="$t('chat.new_session_title')"
              :title="$t('chat.new_session_title')"
              @click="handleCreateNewSession"
            >
              <el-icon aria-hidden="true"><Plus /></el-icon>
              <span>{{ $t('chat.new_session_title') }}</span>
            </button>
            <div id="chat-task-trigger-slot" class="chat-composer-action-slot"></div>
            <div id="chat-request-metadata-trigger-slot" class="chat-composer-action-slot"></div>
          </div>

          <div class="input-controls">
            <div ref="chatInputBoxRef" class="chat-input-box">
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
                  placement="top-start"
                  :width="moreOptionsWidth"
                  :reference-el="chatInputBoxRef"
                  :show-arrow="false"
                  :popper-style="{ maxHeight: 'min(45vh, 420px)', overflowY: 'auto', minWidth: '0', zIndex: 14 }"
                  :append-to="chatMainRef"
                  popper-class="chat-more-options-popover"
                  @after-leave="handleMoreOptionsAfterLeave"
                >
                  <template #reference>
                    <el-button
                      class="more-options-trigger"
                      :title="$t('chat.more_options')"
                      :aria-label="$t('chat.more_options')"
                      circle
                    >
                      <el-icon><Plus /></el-icon>
                    </el-button>
                  </template>

                  <div class="more-options-content">
                    <div class="more-options-header">
                      <span class="more-options-title">{{ $t('chat.more_options_session_settings') }}</span>
                      <el-button
                        class="more-option-upload-button"
                        size="small"
                        :aria-label="$t('chat.more_options_upload')"
                        :disabled="isCurrentSessionReadOnly"
                        @click="openUploadPicker"
                      >
                        <el-icon aria-hidden="true"><UploadFilled /></el-icon>
                        {{ $t('chat.more_options_upload') }}
                      </el-button>
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

                    <div class="more-options-groups">
                      <fieldset class="more-options-group">
                        <legend class="more-options-group-title">{{ $t('chat.more_options_conversation_settings') }}</legend>

                        <div class="more-option-row more-option-profile">
                          <span class="more-option-label more-option-label--profile">{{ $t('chat.more_options_profile') }}</span>
                          <el-select
                            class="more-option-profile-select"
                            :teleported="false"
                            :model-value="currentSessionProfileDisplayId"
                            clearable
                            filterable
                            :loading="profilesLoading"
                            :disabled="profileSettingSubmitting"
                            :placeholder="currentSessionProfilePlaceholder"
                            :aria-label="$t('chat.more_options_profile')"
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

                        <div class="more-option-toggle">
                          <span class="more-option-label">
                            <span>{{ $t('chat.goal_mode') }}</span>
                            <HelpTooltip :content="$t('chat.goal_mode_hint')" />
                          </span>
                          <el-switch
                            :model-value="currentSessionGoalMode"
                            :aria-label="$t('chat.goal_mode')"
                            :disabled="loading || agentSettingSubmitting"
                            @update:model-value="updateSessionGoalMode"
                          />
                        </div>

                        <div class="more-option-segment">
                          <span class="more-option-label">
                            <span>{{ $t('chat.max_turns') }}</span>
                            <HelpTooltip :content="$t('chat.max_turns_hint')" />
                          </span>
                          <el-input-number
                            :model-value="currentSessionMaxTurns"
                            :min="1"
                            :max="SESSION_MAX_TURNS_UPPER_BOUND"
                            :step="1"
                            :precision="0"
                            controls-position="right"
                            size="small"
                            :aria-label="$t('chat.max_turns')"
                            :disabled="loading || agentSettingSubmitting || currentSessionGoalMode"
                            @change="updateSessionMaxTurns"
                          />
                        </div>

                        <div class="more-option-segment">
                          <span class="more-option-label">{{ $t('chat.more_options_response_mode') }}</span>
                          <el-radio-group
                            :model-value="isWsModeComputed ? 'stream' : 'non_stream'"
                            :aria-label="$t('chat.more_options_response_mode')"
                            :disabled="modeSettingSubmitting || transportModeChangeBlocked"
                            size="small"
                            @update:model-value="val => handleModeChange(val === 'stream')"
                          >
                            <el-radio-button label="non_stream">{{ $t('chat.more_options_non_stream') }}</el-radio-button>
                            <el-radio-button label="stream">{{ $t('chat.more_options_stream') }}</el-radio-button>
                          </el-radio-group>
                        </div>
                      </fieldset>

                      <fieldset class="more-options-group">
                        <legend class="more-options-group-title">{{ $t('chat.more_options_display_settings') }}</legend>

                        <div class="more-option-segment">
                          <span class="more-option-label">{{ $t('chat.more_options_message_format') }}</span>
                          <el-radio-group
                            :model-value="currentSessionEnableMarkdown ? 'md' : 'plain'"
                            :aria-label="$t('chat.more_options_message_format')"
                            :disabled="markdownSettingSubmitting"
                            size="small"
                            @update:model-value="val => toggleMarkdown(val === 'md')"
                          >
                            <el-radio-button label="plain">{{ $t('chat.more_options_plain') }}</el-radio-button>
                            <el-radio-button label="md">{{ $t('chat.more_options_markdown') }}</el-radio-button>
                          </el-radio-group>
                        </div>

                        <div class="more-option-toggle">
                          <span class="more-option-label">{{ $t('chat.more_options_tool_output') }}</span>
                          <el-switch
                            :model-value="currentSessionShowToolCalls"
                            :aria-label="$t('chat.more_options_tool_output')"
                            :disabled="toolOutputSettingSubmitting || loading"
                            @update:model-value="updateSessionShowToolCalls"
                          />
                        </div>

                        <div class="more-option-toggle">
                          <span class="more-option-label">{{ $t('chat.show_reasoning') }}</span>
                          <el-switch
                            :model-value="currentSessionShowReasoning"
                            :aria-label="$t('chat.show_reasoning')"
                            :disabled="reasoningSettingSubmitting || loading"
                            @update:model-value="updateSessionShowReasoning"
                          />
                        </div>
                      </fieldset>
                    </div>
                  </div>
                </el-popover>

                <el-input
                  v-model="inputMsg"
                  :placeholder="isCurrentSessionReadOnly ? $t('chat.guidance_placeholder') : $t('chat.input_placeholder')"
                  :aria-describedby="isCurrentSessionReadOnly ? 'external-session-guidance-notice' : undefined"
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
                    :type="showStopReply ? 'danger' : 'primary'"
                    @click="showStopReply ? stopReply() : send()"
                    :loading="showStopReply ? isStopping : (isCurrentSessionReadOnly && guidanceSubmitting)"
                    :disabled="showStopReply ? (isStopping || !currentSessionId) : (isCurrentSessionReadOnly ? guidanceSubmitting || agentSettingSubmitting || !inputMsg.trim() : modeSettingSubmitting || agentSettingSubmitting || (!inputMsg.trim() && attachments.length === 0))"
                    :class="{ 'is-stop-reply': showStopReply }"
                    :title="actionButtonLabel"
                    :aria-label="actionButtonLabel"
                    :aria-busy="isStopping || (isCurrentSessionReadOnly && guidanceSubmitting)"
                    class="action-btn"
                    circle
                  >
                    <span v-if="showStopReply" class="stop-reply-icon" aria-hidden="true"></span>
                    <el-icon v-else class="send-reply-icon" aria-hidden="true"><Position /></el-icon>
                  </el-button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <SessionTodoPanel
      :plan="currentTodoPlan"
      :session-id="currentSessionId"
      :profile-key="`${currentUid || ''}:${currentSessionProfileDisplayId ?? ''}`"
    />
  </div>
</template>

<script setup>
import { ref, inject } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatLineSquare, Delete, InfoFilled, Plus, Refresh, UploadFilled, ArrowDown } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import ChatMessageList from '../components/ChatMessageList.vue'
import ChatRequestMetadata from '../components/ChatRequestMetadata.vue'
import SessionTodoPanel from '../components/SessionTodoPanel.vue'
import HelpTooltip from '../components/HelpTooltip.vue'
import { useChatSession } from '../composables/chat/useChatSession'
import { SESSION_TASKS_KEY } from '../composables/chat/useSessionTasks.js'
import { useChatViewLayout } from '../composables/chat/useChatViewLayout'
import { useChatViewSettings } from '../composables/chat/useChatViewSettings'
import { useChatComposer } from '../composables/chat/useChatComposer'
import { useChatViewNavigation } from '../composables/chat/useChatViewNavigation'
import { fileApi, chatApi, profileApi } from '../api'
import { SESSION_MAX_TURNS_UPPER_BOUND } from '../constants/index.js'
import { formatProfileOptionLabel } from '../utils/profileOptions'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const currentUid = ref(null)
const chat = useChatSession({ currentUid })
const sessionTaskService = inject(SESSION_TASKS_KEY, null)

const layout = useChatViewLayout({ chat, currentUid, translate: t })
const settings = useChatViewSettings({
  chat,
  currentUid,
  api: chatApi,
  profileApi,
  notify: ElMessage,
  translate: t
})
const composer = useChatComposer({
  chat,
  agentSettingSubmitting: settings.agentSettingSubmitting,
  api: chatApi,
  fileApi,
  notify: ElMessage,
  translate: t
})
const navigation = useChatViewNavigation({
  chat,
  layout,
  route,
  router,
  sessionTaskService,
  loadProfiles: settings.loadProfiles,
  notify: ElMessage,
  translate: t
})

const {
  messages,
  inputMsg,
  loading,
  isStopping,
  messageList,
  sessions,
  sessionsLoading,
  currentSessionId,
  typingSessionId,
  activeCollapse,
  modeSettingSubmitting,
  transportModeChangeBlocked,
  attachments,
  isCurrentSessionReadOnly,
  isContextSummarizing,
  llmRequestMetadata,
  historyLoading,
  currentSessionShowToolCalls,
  currentSessionShowReasoning,
  currentSessionGoalMode,
  currentSessionMaxTurns,
  currentTodoPlan,
  loadSessions,
  handleDeleteSession,
  stopReply
} = chat

const {
  chatMainRef,
  moreOptionsVisible,
  moreOptionsOverlayActive,
  chatInputBoxRef,
  moreOptionsWidth,
  sessionsPanelOpen,
  sessionsPanelRef,
  sessionsPanelTitleRef,
  sessionsTriggerRef,
  toggleSessionsPanel,
  closeSessionsPanel,
  collapsedGroups,
  toggleGroup,
  handleSessionGroupBeforeEnter,
  handleSessionGroupEnter,
  handleSessionGroupBeforeLeave,
  handleSessionGroupLeave,
  resetSessionGroupTransition,
  groupedSessions,
  currentSessionInfo,
  sessionEngaged,
  handleMoreOptionsAfterLeave
} = layout

const {
  profilesLoading,
  profileSettingSubmitting,
  toolOutputSettingSubmitting,
  agentSettingSubmitting,
  reasoningSettingSubmitting,
  markdownSettingSubmitting,
  currentSessionEnableMarkdown,
  toggleMarkdown,
  isWsModeComputed,
  currentSessionProfileDisplayId,
  currentSessionProfileOptions,
  currentSessionProfilePlaceholder,
  updateSessionGoalMode,
  updateSessionMaxTurns,
  updateSessionProfileOverride,
  updateSessionShowToolCalls,
  updateSessionShowReasoning,
  handleModeChange,
  formatSessionSource
} = settings

const {
  uploadTriggerRef,
  openUploadPicker,
  guidanceSubmitting,
  showStopReply,
  actionButtonLabel,
  send,
  handleAuditDecision,
  uploadFileList,
  handleUpload,
  handleRemoveCustomFile,
  handlePaste
} = composer

const {
  deferredContentSessionId,
  renderedMessages,
  renderedInitialHistoryLoaded,
  handleSelectSession,
  handleCreateNewSession,
  handleWelcomeExitTransitionEnd
} = navigation
</script>

<style lang="scss">
@import "@/assets/css/ChatView.scss";
</style>
