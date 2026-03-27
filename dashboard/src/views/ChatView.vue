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
          <div v-for="msg in messages" :key="msg.id" :class="['message-item', msg.role === 'assistant' ? 'ai' : msg.role === 'thinking' ? 'thinking' : 'user']">
            <div class="content">{{ msg.content }}</div>
          </div>
        </template>
      </div>
      <div class="input-area">
        <el-button type="primary" size="small" @click="createNewSession" class="new-session-btn">
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
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../api'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'

const messages = ref([])
const inputMsg = ref('')
const loading = ref(false)
const messageList = ref(null)

// 会话相关
const sessions = ref([])
const sessionsLoading = ref(false)
const currentSessionId = ref(null)

// loadSessions 必须在 useDeleteConfirm 之前定义
const loadSessions = async () => {
  sessionsLoading.value = true
  try {
    const res = await chatApi.sessionsList()
    sessions.value = res.data.data || []
  } catch (err) {
    ElMessage.error(err.message || '获取会话列表失败')
  } finally {
    sessionsLoading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete: handleDeleteSession } = useDeleteConfirm(chatApi.deleteSession, loadSessions)

// 选择会话
const selectSession = (session) => {
  currentSessionId.value = session.session_id
  messages.value = []
  ElMessage.info('已选择会话: ' + session.session_id.substring(0, 8) + '...')
}

// 新建会话
const createNewSession = () => {
  currentSessionId.value = null
  messages.value = []
  inputMsg.value = ''
  ElMessage.info('新会话已创建，请输入消息开始聊天')
}

const send = async () => {
  if (!inputMsg.value.trim() || loading.value) return
  const text = inputMsg.value
  const userMsgId = Date.now()
  messages.value.push({ id: userMsgId, role: 'user', content: text })
  inputMsg.value = ''
  loading.value = true
  
  // 添加 Thinking 占位符
  const thinkingId = Date.now() + 1
  messages.value.push({ id: thinkingId, role: 'thinking', content: 'Thinking...' })
  
  try {
    // 调用 /chat/completions 接口，session_id为空时创建新会话
    const res = await chatApi.completions({
      message: text,
      session_id: currentSessionId.value || null,
      stream: false
    })
    // 解析返回数据 choices[0].message.content (API使用StandardResponse包装)
    const aiContent = res.data?.choices?.[0]?.message?.content || '消息解析失败'
    // 如果是新会话，保存返回的session_id
    if (!currentSessionId.value && res.data.data?.session_id) {
      currentSessionId.value = res.data.data.session_id
      console.log('新会话创建成功，session_id:', currentSessionId.value)
    }
    // 刷新会话列表
    loadSessions()
    // 更新 Thinking 占位符为实际回复
    const thinkingIndex = messages.value.findIndex(m => m.id === thinkingId)
    if (thinkingIndex !== -1) {
      messages.value[thinkingIndex] = { id: thinkingId, role: 'assistant', content: aiContent }
    }
  } catch (err) {
    // 移除 Thinking 占位符
    const thinkingIndex = messages.value.findIndex(m => m.id === thinkingId)
    if (thinkingIndex !== -1) {
      messages.value.splice(thinkingIndex, 1)
    }
    ElMessage.error(err.message || '发送失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadSessions()
})
</script>

<style lang="scss">
@import "@/assets/css/chat.scss";
</style>