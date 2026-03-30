<template>
  <div class="chat-view-container">
    <!-- 左侧会话列表侧边栏 -->
    <div class="sessions-sidebar">
      <div class="sidebar-header">
        <span>会话列表</span>
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
            <div class="session-id">ID: {{ session.session_id }}</div>
            <div class="session-time">{{ session.last_active }}</div>
          </div>
          <div class="session-actions">
            <span class="delete-icon" @click.stop="handleDeleteSession(session.session_id)">删</span>
          </div>
        </div>
        <div v-if="sessions.length === 0 && !sessionsLoading" class="empty-tip">
          暂无会话
        </div>
      </div>
    </div>

    <!-- 右侧聊天区域 -->
    <div class="chat-main">
      <div class="message-list" ref="messageList">
        <div v-if="!currentSessionId && messages.length === 0" class="empty-chat">
          <p>请选择左侧会话或新建会话开始聊天</p>
        </div>
        <template v-else>
          <div v-for="msg in messages" :key="msg.id" :class="['message-item', msg.role === 'user' ? 'user' : msg.role === 'thinking' ? 'thinking' : 'ai']">
            <!-- 普通消息或工具调用消息 -->
            <template v-if="isToolCall(msg)">
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              <el-collapse v-model="activeCollapse">
                <el-collapse-item :name="msg.id">
                  <template #title>
                    <span class="tool-call-title">工具调用: {{ getToolName(msg) }}</span>
                  </template>
                  <div class="tool-call-content">
                    <pre>{{ getToolArguments(msg) }}</pre>
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
                    <span class="tool-result-title">工具返回: {{ getToolResultName(msg) }}</span>
                  </template>
                  <div class="tool-result-content">
                    <pre>{{ getToolResultContent(msg) }}</pre>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </template>
            <template v-else>
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              <div class="content">{{ msg.content }}</div>
            </template>
          </div>
        </template>
      </div>
      <div class="input-area">
        <el-button type="primary" @click="createNewSession" class="new-session-btn">
          <i class="el-icon-plus"></i> 新建会话
        </el-button>
        <span v-if="currentSessionId" class="current-session-id">
          当前会话: {{ currentSessionId.substring(0, 8) }}...
        </span>
        <div class="input-wrapper">
          <el-input
            v-model="inputMsg" 
            placeholder="输入消息..." 
            :disabled="loading"
            @keyup.enter="send"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            class="chat-input"
            :resize="'none'"
          />
          <el-button type="primary" :loading="loading" @click="send" :disabled="!inputMsg.trim()">
            发送
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted } from 'vue'
import { ElCollapse, ElCollapseItem } from 'element-plus'
import { useChatSession } from '../composables/useChatSession'

const chat = useChatSession()

// 解构状态
const {
  messages,
  inputMsg,
  loading,
  messageList,
  sessions,
  sessionsLoading,
  currentSessionId,
  activeCollapse
} = chat

// 解构方法
const {
  loadSessions,
  handleDeleteSession,
  selectSession,
  createNewSession,
  send,
  formatTimestamp,
  isToolCall,
  isToolResult,
  getToolName,
  getToolArguments,
  getToolResultName,
  getToolResultContent,
  getMessageTimestamp
} = chat

// 获取滚动处理函数
const { handleScroll } = chat

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
})
</script>

<style lang="scss">
@import "@/assets/css/chat.scss";
</style>