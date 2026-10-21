import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'
import {
  createManagedKnowledgeDedupeKey,
  getManagedKnowledgeMutationFeedback
} from '../../utils/knowledgeBaseManagement.js'

export function useKnowledgeEntryEditor({ t, selectedKb, scope, onSaved = async () => {} }) {
  const visible = ref(false)
  const mode = ref('manual')
  const formRef = ref(null)
  const form = reactive({
    name: '',
    content: '',
    llm_maintainable: false,
    chunk_size: 1000,
    chunk_overlap: 100,
    batch_size: 16
  })
  const sourceFilename = ref('')
  const editingId = ref(null)
  const loading = ref(false)
  const previewing = ref(false)
  const submitting = ref(false)
  const error = ref('')
  const isManaged = computed(() => selectedKb.value?.knowledge_base_type === 'llm_managed')

  let generation = 0
  let expectedVersion = null
  const initialSnapshot = ref(null)
  let previewRequestId = 0
  let previewController = null
  let lastDedupePayload = null
  let lastDedupeKey = null

  const getSnapshot = () => ({
    name: form.name,
    content: form.content,
    llm_maintainable: form.llm_maintainable,
    chunk_size: form.chunk_size,
    chunk_overlap: form.chunk_overlap,
    batch_size: form.batch_size,
    sourceFilename: sourceFilename.value
  })

  const sameSnapshot = (left, right) => (
    left !== null
    && right !== null
    && Object.keys(left).every(key => Object.is(left[key], right[key]))
  )

  const dirty = computed(() => (
    initialSnapshot.value !== null && !sameSnapshot(getSnapshot(), initialSnapshot.value)
  ))

  const rules = computed(() => {
    const result = {
      name: [
        {
          required: true,
          whitespace: true,
          max: 255,
          message: t('knowledgeBase.entries_name_required'),
          trigger: 'blur'
        }
      ],
      content: [{
        required: true,
        whitespace: true,
        message: t('knowledgeBase.entries_content_required'),
        trigger: 'blur'
      }]
    }
    if (!isManaged.value) {
      result.chunk_size = [{
        required: true,
        message: t('knowledgeBase.set_chunk_size'),
        trigger: 'blur'
      }]
      result.chunk_overlap = [{
        required: true,
        message: t('knowledgeBase.set_chunk_overlap'),
        trigger: 'blur'
      }]
      result.batch_size = [{
        required: true,
        message: t('knowledgeBase.set_batch_size'),
        trigger: 'blur'
      }]
    }
    return result
  })

  const reset = () => {
    generation += 1
    previewRequestId += 1
    if (previewController) previewController.abort()
    previewController = null
    expectedVersion = null
    initialSnapshot.value = null
    lastDedupePayload = null
    lastDedupeKey = null
    visible.value = false
    mode.value = 'manual'
    editingId.value = null
    loading.value = false
    previewing.value = false
    submitting.value = false
    error.value = ''
    form.name = ''
    form.content = ''
    form.llm_maintainable = false
    form.chunk_size = 1000
    form.chunk_overlap = 100
    form.batch_size = 16
    sourceFilename.value = ''
    formRef.value?.clearValidate()
  }

  const isCurrent = (scopeGuard, kbId, formGeneration) => (
    scopeGuard()
    && selectedKb.value?.id === kbId
    && generation === formGeneration
  )

  const openCreate = () => {
    const scopeGuard = scope.capture()
    const kbId = selectedKb.value?.id
    if (!scopeGuard() || kbId === null || kbId === undefined) return

    reset()
    const formGeneration = generation
    if (!isCurrent(scopeGuard, kbId, formGeneration)) return
    initialSnapshot.value = getSnapshot()
    visible.value = true
  }

  const openEdit = async (row) => {
    const scopeGuard = scope.capture()
    const kbId = selectedKb.value?.id
    const itemId = row?.id
    if (
      !scopeGuard()
      || kbId === null
      || kbId === undefined
      || itemId === null
      || itemId === undefined
      || !isManaged.value
    ) return

    reset()
    const formGeneration = generation
    if (!isCurrent(scopeGuard, kbId, formGeneration)) return
    editingId.value = itemId
    visible.value = true
    loading.value = true

    try {
      const res = await knowledgeBaseApi.managedItem(kbId, itemId)
      if (!isCurrent(scopeGuard, kbId, formGeneration)) return
      const item = res.data.data
      editingId.value = item.id ?? itemId
      expectedVersion = item.version
      form.name = item.knowledge_key || ''
      form.content = item.content || ''
      form.llm_maintainable = Boolean(item.llm_maintainable)
      initialSnapshot.value = getSnapshot()
    } catch (loadError) {
      if (isCurrent(scopeGuard, kbId, formGeneration)) {
        error.value = t('knowledgeBase.entries_load_failed', {
          message: loadError?.message || ''
        })
      }
    } finally {
      if (isCurrent(scopeGuard, kbId, formGeneration)) loading.value = false
    }
  }

  const previewFile = async (file) => {
    const rawFile = file?.raw || file
    const scopeGuard = scope.capture()
    const kbId = selectedKb.value?.id
    if (
      !rawFile
      || !scopeGuard()
      || kbId === null
      || kbId === undefined
      || !visible.value
      || editingId.value !== null
    ) return

    if (previewController) previewController.abort()
    const controller = new AbortController()
    previewController = controller
    const requestId = ++previewRequestId
    const formGeneration = generation
    const inputSnapshot = getSnapshot()
    const isOperationCurrent = () => (
      isCurrent(scopeGuard, kbId, formGeneration)
      && requestId === previewRequestId
    )
    const isInputCurrent = () => (
      isOperationCurrent() && sameSnapshot(getSnapshot(), inputSnapshot)
    )
    previewing.value = true
    error.value = ''

    try {
      if (form.name || form.content) {
        let confirmed
        try {
          confirmed = await scope.confirm(
            t('knowledgeBase.entries_replace_confirm'),
            t('knowledgeBase.prompt'),
            {
              confirmButtonText: t('knowledgeBase.confirm'),
              cancelButtonText: t('knowledgeBase.cancel'),
              type: 'warning'
            }
          )
        } catch (confirmError) {
          if (confirmError === 'cancel' || confirmError === 'close') return
          throw confirmError
        }
        if (!isInputCurrent() || !confirmed) return
      }

      if (!isInputCurrent()) return
      const formData = new FormData()
      formData.append('file', rawFile)
      const res = await knowledgeBaseApi.previewEntryFile(kbId, formData, {
        signal: controller.signal
      })
      if (!isInputCurrent()) return
      const data = res.data.data
      form.name = data.filename || ''
      form.content = data.content || ''
      sourceFilename.value = data.filename || ''
    } catch (previewError) {
      if (
        isInputCurrent()
        && !controller.signal.aborted
        && previewError !== 'cancel'
        && previewError !== 'close'
      ) {
        error.value = t('knowledgeBase.entries_preview_failed', {
          message: previewError?.message || ''
        })
      }
    } finally {
      if (isOperationCurrent()) {
        previewing.value = false
        if (previewController === controller) previewController = null
      }
    }
  }

  const getDedupeKey = (payload, operation) => {
    const serializedPayload = JSON.stringify(payload)
    if (serializedPayload !== lastDedupePayload) {
      lastDedupePayload = serializedPayload
      lastDedupeKey = createManagedKnowledgeDedupeKey(operation)
    }
    return lastDedupeKey
  }

  const submit = async () => {
    const scopeGuard = scope.capture()
    const kbId = selectedKb.value?.id
    const formGeneration = generation
    const currentEditingId = editingId.value
    const formElement = formRef.value
    if (
      !scopeGuard()
      || kbId === null
      || kbId === undefined
      || !visible.value
      || !formElement
      || loading.value
      || previewing.value
      || submitting.value
      || (currentEditingId !== null && expectedVersion == null)
    ) return

    const isSubmissionCurrent = () => (
      isCurrent(scopeGuard, kbId, formGeneration)
      && editingId.value === currentEditingId
    )
    submitting.value = true
    error.value = ''

    try {
      if (!isSubmissionCurrent()) return
      if (currentEditingId === null && !isManaged.value && form.chunk_overlap >= form.chunk_size) {
        error.value = t('knowledgeBase.overlap_less_than_size')
        return
      }

      let valid
      try {
        valid = await formElement.validate()
      } catch (_validationError) {
        return
      }
      if (!isSubmissionCurrent() || !valid) return

      let res
      const operation = currentEditingId === null ? 'create' : 'update'
      if (currentEditingId !== null) {
        const payload = {
          expected_version: expectedVersion,
          knowledge_key: form.name.trim(),
          content: form.content,
          llm_maintainable: form.llm_maintainable
        }
        payload.dedupe_key = getDedupeKey(payload, operation)
        if (!isSubmissionCurrent()) return
        res = await knowledgeBaseApi.updateManagedItem(kbId, currentEditingId, payload)
      } else {
        const payload = {
          name: form.name.trim(),
          content: form.content
        }
        if (sourceFilename.value) payload.source_filename = sourceFilename.value
        if (isManaged.value) {
          payload.llm_maintainable = form.llm_maintainable
          payload.dedupe_key = getDedupeKey(payload, 'create')
        } else {
          payload.chunk_size = form.chunk_size
          payload.chunk_overlap = form.chunk_overlap
          payload.batch_size = form.batch_size
        }
        if (!isSubmissionCurrent()) return
        res = await knowledgeBaseApi.createEntry(kbId, payload)
      }

      if (!isSubmissionCurrent()) return
      const status = res.data.data.status
      if (isManaged.value) {
        const feedback = getManagedKnowledgeMutationFeedback(operation, status)
        if (['existing_key', 'existing_content'].includes(status)) {
          error.value = t(`knowledgeBase.${feedback.key}`)
          ElMessage({ type: feedback.type, message: error.value })
          return
        }
        const feedbackKey = operation === 'create' && status === 'created'
          ? 'entries_submitted'
          : feedback.key
        ElMessage({ type: feedback.type, message: t(`knowledgeBase.${feedbackKey}`) })
      } else {
        const successKey = selectedKb.value?.index_status === 'ready'
          ? 'entries_ready'
          : 'create_success'
        ElMessage.success(t(`knowledgeBase.${successKey}`))
      }

      visible.value = false
      try {
        if (!isSubmissionCurrent()) return
        await onSaved({ created: currentEditingId === null })
      } catch (savedError) {
        if (isSubmissionCurrent()) {
          ElMessage.error(t('knowledgeBase.entries_failed', {
            message: savedError?.message || ''
          }))
        }
      }
      if (isSubmissionCurrent()) reset()
    } catch (submitError) {
      if (isSubmissionCurrent()) {
        error.value = t('knowledgeBase.entries_failed', {
          message: submitError?.message || ''
        })
      }
    } finally {
      if (isSubmissionCurrent()) submitting.value = false
    }
  }

  const showBusyMessage = () => {
    error.value = t('knowledgeBase.entries_busy')
    ElMessage.warning(error.value)
  }

  const canLeave = async () => {
    const scopeGuard = scope.capture()
    const kbId = selectedKb.value?.id
    const formGeneration = generation
    const isCurrentAttempt = () => isCurrent(scopeGuard, kbId, formGeneration)
    if (!isCurrentAttempt()) return false
    if (!visible.value) return true
    if (submitting.value) {
      showBusyMessage()
      return false
    }
    if (!dirty.value) return true

    const confirmed = await scope.confirm(
      t('knowledgeBase.entries_discard_confirm'),
      t('knowledgeBase.entries_discard_title'),
      {
        confirmButtonText: t('knowledgeBase.entries_discard'),
        cancelButtonText: t('knowledgeBase.entries_continue'),
        type: 'warning'
      }
    )
    if (!isCurrentAttempt()) return false
    if (submitting.value) {
      showBusyMessage()
      return false
    }
    return Boolean(confirmed)
  }

  const requestClose = async () => {
    const scopeGuard = scope.capture()
    const kbId = selectedKb.value?.id
    const formGeneration = generation
    const isCurrentAttempt = () => isCurrent(scopeGuard, kbId, formGeneration)
    if (!isCurrentAttempt()) return false
    const allowed = await canLeave()
    if (!isCurrentAttempt() || !allowed) return false
    reset()
    return true
  }

  watch(() => selectedKb.value?.id, (nextId, previousId) => {
    if (nextId !== previousId) reset()
  }, { flush: 'sync' })
  onBeforeUnmount(reset)

  return {
    visible,
    mode,
    formRef,
    form,
    sourceFilename,
    editingId,
    loading,
    previewing,
    submitting,
    error,
    dirty,
    isManaged,
    rules,
    openCreate,
    openEdit,
    previewFile,
    submit,
    requestClose,
    canLeave,
    reset
  }
}
