import { computed, nextTick, ref } from 'vue'

export function useChatComposer({
  chat,
  agentSettingSubmitting,
  api,
  fileApi,
  notify,
  translate
}) {
  const {
    isStopping,
    isReplyRunning,
    modeSettingSubmitting,
    isCurrentSessionReadOnly,
    inputMsg,
    currentSessionId,
    messages,
    messageList,
    loading,
    attachments,
    loadSessions,
    send: originalSend
  } = chat
  const t = translate

  const uploadTriggerRef = ref(null)

  const openUploadPicker = () => {
    const triggerEl = uploadTriggerRef.value?.$el || uploadTriggerRef.value
    const input = triggerEl?.querySelector?.('input[type="file"]')
    if (input) input.click()
  }

  const guidanceSubmitting = ref(false)

  const actionButtonLabel = computed(() => t(
    isStopping.value
      ? 'chat.stopping_reply'
      : isReplyRunning.value
        ? 'chat.stop_reply'
        : 'chat.send_message'
  ))

  // 上传组件文件列表状态绑定
  const uploadFileList = ref([])

  // 拦截发送，发送完成后清空列表
  const send = async () => {
    if (isStopping.value) return

    if (modeSettingSubmitting.value || agentSettingSubmitting.value) return

    if (isCurrentSessionReadOnly.value) {
      const content = inputMsg.value.trim()
      const sessionId = currentSessionId.value
      if (!content || !sessionId || guidanceSubmitting.value) return

      guidanceSubmitting.value = true
      try {
        const response = await api.createGuidance({ session_id: sessionId, content })
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
        if (
          sessionId === currentSessionId.value &&
          isCurrentSessionReadOnly.value &&
          inputMsg.value.trim() === content
        ) {
          inputMsg.value = ''
        }
        notify.success(t('chat.guidance_created'))
        void loadSessions()
      } catch (error) {
        notify.error(error.message || t('chat.guidance_create_failed'))
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
    if (isCurrentSessionReadOnly.value || loading.value || agentSettingSubmitting.value) return
    inputMsg.value = decision === 'approve'
      ? t('chat.audit_approve_word')
      : decision === 'ignore'
        ? t('chat.audit_ignore_word')
        : t('chat.audit_reject_word')
    attachments.value = []
    await originalSend()
  }

  // 附件上传处理
  const handleUpload = async (options) => {
    const { file, onSuccess, onError } = options

    if (isCurrentSessionReadOnly.value) {
      if (onError) onError(new Error(t('chat.external_session_read_only')))
      notify.warning(t('chat.external_session_read_only'))
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

      notify.success(t('chat.upload_success'))
    } catch (error) {
      if (onError) onError(error)
      notify.error(error.message || t('chat.upload_failed'))
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
            continue // 拒绝并忽略文件夹的粘贴
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

  return {
    uploadTriggerRef,
    openUploadPicker,
    guidanceSubmitting,
    actionButtonLabel,
    send,
    handleAuditDecision,
    uploadFileList,
    handleUpload,
    handleRemoveCustomFile,
    handlePaste
  }
}
