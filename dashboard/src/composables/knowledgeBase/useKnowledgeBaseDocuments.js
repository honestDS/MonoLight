import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'

export function useKnowledgeBaseDocuments({ t, selectedKb, scope }) {
  const importDialogVisible = ref(false)
  const importing = ref(false)
  const importFormRef = ref(null)
  const uploadFileList = ref([])
  const documentDialogVisible = ref(false)
  const documentLoading = ref(false)
  const documentList = ref([])
  const documentTotal = ref(0)
  const documentPage = ref(1)
  const documentPageSize = ref(10)
  const contentDialogVisible = ref(false)
  const documentContent = ref('')
  const contentTitle = ref('')

  const importForm = reactive({
    file: null,
    chunk_size: 1000,
    chunk_overlap: 100,
    batch_size: 16
  })

  let importFormGeneration = 0
  let documentsRequestId = 0
  let contentRequestId = 0

  const isCurrentKb = (guard, kbId) => guard() && selectedKb.value?.id === kbId
  const isCurrentImport = (guard, kbId, generation) =>
    isCurrentKb(guard, kbId) && importFormGeneration === generation

  const clearDocumentState = () => {
    documentsRequestId += 1
    contentRequestId += 1
    documentLoading.value = false
    documentList.value = []
    documentTotal.value = 0
    documentPage.value = 1
    contentDialogVisible.value = false
    documentContent.value = ''
    contentTitle.value = ''
  }

  const importRules = computed(() => ({
    file: [{ required: true, message: t('knowledgeBase.select_doc_err'), trigger: 'change' }],
    chunk_size: [{ required: true, message: t('knowledgeBase.set_chunk_size'), trigger: 'blur' }],
    chunk_overlap: [{ required: true, message: t('knowledgeBase.set_chunk_overlap'), trigger: 'blur' }],
    batch_size: [{ required: true, message: t('knowledgeBase.set_batch_size'), trigger: 'blur' }]
  }))

  const resetImportForm = () => {
    importFormGeneration += 1
    if (importFormRef.value) importFormRef.value.resetFields()
    importForm.file = null
    importForm.chunk_size = 1000
    importForm.chunk_overlap = 100
    importForm.batch_size = 16
    uploadFileList.value = []
    importing.value = false
    if (importFormRef.value) importFormRef.value.clearValidate()
  }

  const resetDocumentsState = () => {
    importDialogVisible.value = false
    documentDialogVisible.value = false
    resetImportForm()
    clearDocumentState()
    documentPageSize.value = 10
  }

  const showImportDialog = (row) => {
    const guard = scope.capture()
    if (!guard()) return
    if (selectedKb.value?.id !== row.id) clearDocumentState()
    selectedKb.value = row
    resetImportForm()
    importDialogVisible.value = true
  }

  const handleFileChange = (uploadFile, uploadFiles) => {
    const guard = scope.capture()
    if (!guard() || !selectedKb.value) return
    uploadFileList.value = uploadFiles.slice(-1)
    importForm.file = uploadFile.raw
    if (importFormRef.value) importFormRef.value.validateField('file')
  }

  const handleFileRemove = () => {
    const guard = scope.capture()
    if (!guard() || !selectedKb.value) return
    uploadFileList.value = []
    importForm.file = null
  }

  const submitImport = async () => {
    const guard = scope.capture()
    if (!guard() || !selectedKb.value) return
    if (importing.value || !importFormRef.value) return
    const kbId = selectedKb.value.id
    const generation = importFormGeneration
    if (importForm.chunk_overlap >= importForm.chunk_size) {
      if (isCurrentImport(guard, kbId, generation)) {
        ElMessage.error(t('knowledgeBase.overlap_less_than_size'))
      }
      return
    }
    const formRef = importFormRef.value
    importing.value = true
    try {
      let valid
      try {
        valid = await formRef.validate()
      } catch {
        return
      }
      if (!isCurrentImport(guard, kbId, generation) || !valid) return

      const params = {
        file: importForm.file,
        chunkSize: importForm.chunk_size,
        chunkOverlap: importForm.chunk_overlap,
        batchSize: importForm.batch_size
      }
      const formData = new FormData()
      formData.append('file', params.file)
      formData.append('chunk_size', params.chunkSize)
      formData.append('chunk_overlap', params.chunkOverlap)
      formData.append('batch_size', params.batchSize)
      await knowledgeBaseApi.importDocument(kbId, formData)
      if (!isCurrentImport(guard, kbId, generation)) return
      ElMessage.success(t('knowledgeBase.import_success'))
      importDialogVisible.value = false
      if (documentDialogVisible.value) await fetchDocuments()
    } catch (error) {
      if (isCurrentImport(guard, kbId, generation)) {
        ElMessage.error(t('knowledgeBase.import_failed') + error.message)
      }
    } finally {
      if (isCurrentImport(guard, kbId, generation)) importing.value = false
    }
  }

  const showDocumentDialog = (row) => {
    const guard = scope.capture()
    if (!guard()) return
    selectedKb.value = row
    clearDocumentState()
    documentDialogVisible.value = true
    return fetchDocuments()
  }

  const fetchDocuments = async () => {
    const guard = scope.capture()
    if (!guard() || !selectedKb.value) return
    const kbId = selectedKb.value.id
    const requestId = ++documentsRequestId
    const page = documentPage.value
    const size = documentPageSize.value
    documentLoading.value = true
    try {
      const res = await knowledgeBaseApi.documents(kbId, {
        page,
        size
      })
      if (!isCurrentKb(guard, kbId) || requestId !== documentsRequestId) return
      documentList.value = res.data.data.items || []
      documentTotal.value = res.data.data.total || 0
    } catch (error) {
      if (isCurrentKb(guard, kbId) && requestId === documentsRequestId) {
        ElMessage.error(t('knowledgeBase.fetch_doc_list_failed') + error.message)
      }
    } finally {
      if (isCurrentKb(guard, kbId) && requestId === documentsRequestId) {
        documentLoading.value = false
      }
    }
  }

  const handleDocumentSizeChange = () => {
    const guard = scope.capture()
    if (!guard() || !selectedKb.value) return
    documentPage.value = 1
    fetchDocuments()
  }

  const showContentDialog = async (row) => {
    const guard = scope.capture()
    if (!guard() || !selectedKb.value) return
    const kbId = selectedKb.value.id
    const documentId = row.id
    const filename = row.filename
    const requestId = ++contentRequestId
    try {
      const res = await knowledgeBaseApi.document(kbId, documentId)
      if (!isCurrentKb(guard, kbId) || requestId !== contentRequestId) return
      contentTitle.value = filename
      documentContent.value = res.data.data.content || ''
      contentDialogVisible.value = true
    } catch (error) {
      if (isCurrentKb(guard, kbId) && requestId === contentRequestId) {
        ElMessage.error(t('knowledgeBase.fetch_doc_content_failed') + error.message)
      }
    }
  }

  const handleDeleteDocument = async (row) => {
    const guard = scope.capture()
    if (!guard() || !selectedKb.value) return
    const kbId = selectedKb.value.id
    const documentId = row.id
    const filename = row.filename
    const confirmed = await scope.confirm(
      t('knowledgeBase.delete_doc_confirm', { filename }),
      t('knowledgeBase.prompt'),
      {
        type: 'warning',
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
        dangerouslyUseHTMLString: false
      }
    )
    if (!isCurrentKb(guard, kbId) || !confirmed) return

    try {
      await knowledgeBaseApi.deleteDocument(kbId, documentId)
    } catch (error) {
      if (isCurrentKb(guard, kbId)) {
        ElMessage.error(error?.message || t('knowledgeBase.delete_doc_failed'))
      }
      return
    }

    if (!isCurrentKb(guard, kbId)) return
    ElMessage.success(t('knowledgeBase.delete_doc_success'))
    await fetchDocuments()
  }

  return {
    importDialogVisible,
    importing,
    importFormRef,
    uploadFileList,
    importForm,
    importRules,
    documentDialogVisible,
    documentLoading,
    documentList,
    documentTotal,
    documentPage,
    documentPageSize,
    contentDialogVisible,
    documentContent,
    contentTitle,
    showImportDialog,
    handleFileChange,
    handleFileRemove,
    submitImport,
    showDocumentDialog,
    fetchDocuments,
    handleDocumentSizeChange,
    showContentDialog,
    handleDeleteDocument,
    resetDocumentsState
  }
}
