// 聊天状态管理 composable
import { ref, nextTick } from 'vue'

export function useChatState() {
  // ==================== 消息状态 ====================
  
  const messages = ref([])
  const inputMsg = ref('')
  const loading = ref(false)
  const messageList = ref(null)

  // ==================== 消息操作方法 ====================
  
  // 添加消息到列表；msg 为消息对象
  const addMessage = (msg) => {
    if (msg && msg.role !== 'thinking') {
      const firstThinkingIdx = messages.value.findIndex(m => m.role === 'thinking')
      if (firstThinkingIdx !== -1) {
        messages.value.splice(firstThinkingIdx, 0, msg)
        return
      }
    }
    messages.value.push(msg)
  }

  // 根据消息 ID 查找索引，未找到返回 -1
  const findMessageIndex = (id) => {
    return messages.value.findIndex(m => m.id === id)
  }

  // 根据消息 ID 移除消息
  const removeMessage = (id) => {
    const index = findMessageIndex(id)
    if (index !== -1) {
      messages.value.splice(index, 1)
    }
  }

  // 在指定位置插入消息；clearFirst 为 true 时先清空列表
  const insertMessage = (index, msg, clearFirst = false) => {
    if (clearFirst) {
      messages.value = []
    }
    if (Array.isArray(msg)) {
      messages.value.splice(index, 0, ...msg)
    } else {
      messages.value.splice(index, 0, msg)
    }
  }

  /**
   * 清空消息列表
   */
  const clearMessages = () => {
    messages.value = []
  }

  // ==================== 滚动操作 ====================
  
  /**
   * 滚动到消息列表底部
   */
  const scrollToBottom = (behavior = 'smooth') => {
    if (messageList.value?.scrollToBottom) {
      messageList.value.scrollToBottom(behavior)
      return
    }
    if (messageList.value) {
      messageList.value.scrollTo({
        top: messageList.value.scrollHeight,
        behavior
      })
    }
  }

  // 滚动到指定消息位置
  const scrollToMessage = (msgId) => {
    // 默认滚动到底部，可扩展为滚动到指定消息
    scrollToBottom()
  }

  return {
    // 状态
    messages,
    inputMsg,
    loading,
    messageList,
    // 消息操作
    addMessage,
    findMessageIndex,
    removeMessage,
    insertMessage,
    clearMessages,
    // 滚动操作
    scrollToBottom,
    scrollToMessage
  }
}