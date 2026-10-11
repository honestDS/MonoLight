import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { groupChatSessions } from '../../utils/chatPresentation.js'

export function useChatViewLayout({ chat, translate, currentUid = ref(null) }) {
  const t = translate
  const { sessions, currentSessionId } = chat

  const chatMainRef = ref(null)
  const moreOptionsVisible = ref(false)
  const moreOptionsOverlayActive = ref(false)
  const chatInputBoxRef = ref(null)
  const moreOptionsWidth = ref(0)
  let chatInputResizeObserver
  const syncMoreOptionsWidth = () => {
    moreOptionsWidth.value = chatInputBoxRef.value?.getBoundingClientRect().width ?? 0
  }

  onMounted(() => {
    const inputBox = chatInputBoxRef.value
    if (!inputBox) return
    syncMoreOptionsWidth()
    chatInputResizeObserver = new ResizeObserver(syncMoreOptionsWidth)
    chatInputResizeObserver.observe(inputBox, { box: 'border-box' })
  })

  onUnmounted(() => {
    chatInputResizeObserver?.disconnect()
  })

  // 覆盖式会话面板开关状态：默认收起，选中会话、点击面板外部或按下 Esc 时收起
  const sessionsPanelOpen = ref(false)
  const toggleSessionsPanel = () => {
    sessionsPanelOpen.value = !sessionsPanelOpen.value
  }
  const closeSessionsPanel = () => {
    sessionsPanelOpen.value = false
  }

  // 按用户归属和日期划分的折叠会话分组 key
  const collapsedGroups = ref(new Set())
  const toggleGroup = (key) => {
    const next = new Set(collapsedGroups.value)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    collapsedGroups.value = next
  }

  const handleSessionGroupBeforeEnter = (element) => {
    element.style.height = '0px'
    element.style.opacity = '0'
  }

  const handleSessionGroupEnter = (element) => {
    const targetHeight = element.scrollHeight
    void element.offsetHeight
    element.style.height = `${targetHeight}px`
    element.style.opacity = '1'
  }

  const handleSessionGroupBeforeLeave = (element) => {
    element.style.height = `${element.scrollHeight}px`
    element.style.opacity = '1'
  }

  const handleSessionGroupLeave = (element) => {
    void element.offsetHeight
    element.style.height = '0px'
    element.style.opacity = '0'
  }

  const resetSessionGroupTransition = (element) => {
    element.style.height = ''
    element.style.opacity = ''
  }

  // 会话列表按用户归属和日期分组，组内按 last_active 降序
  const groupedSessions = computed(() => groupChatSessions(sessions.value, {
    currentUid: currentUid.value,
    translate: t
  }))

  // 当前会话的创建/活跃时间，供 ChatMessageList 显示
  const currentSessionInfo = computed(() => {
    if (!currentSessionId.value) return null
    return sessions.value.find(s => s.session_id === currentSessionId.value) || null
  })

  // 是否处于"会话已开始"状态：已有消息（已发送）或者正在加载（发送中）
  // 让用户点击发送的瞬间就触发输入框和欢迎区的过渡动画，避免等待接口响应
  const sessionEngaged = computed(() => !!currentSessionId.value || chat.loading.value || chat.messages.value.length > 0)

  const handleMoreOptionsAfterLeave = () => {
    if (!moreOptionsVisible.value) moreOptionsOverlayActive.value = false
  }
  watch(moreOptionsVisible, visible => {
    if (visible) moreOptionsOverlayActive.value = true
  }, { flush: 'sync' })
  watch(currentSessionId, (sessionId, previousSessionId) => {
    if (sessionId !== previousSessionId) moreOptionsVisible.value = false
  }, { flush: 'sync' })

  return {
    chatMainRef,
    moreOptionsVisible,
    moreOptionsOverlayActive,
    chatInputBoxRef,
    moreOptionsWidth,
    sessionsPanelOpen,
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
  }
}
