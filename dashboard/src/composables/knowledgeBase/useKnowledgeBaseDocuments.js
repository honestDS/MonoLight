import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'
import { useDeleteConfirm } from '../useDeleteConfirm.js'

export function useKnowledgeBaseDocuments({ t, selectedKb }) {
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

  const importRules = computed(() => ({
    file: [{ required: true, message: t('knowledgeBase.select_doc_err'), trigger: 'change' }],
    chunk_size: [{ required: true, message: t('knowledgeBase.set_chunk_size'), trigger: 'blur' }],
    chunk_overlap: [{ required: true, message: t('knowledgeBase.set_chunk_overlap'), trigger: 'blur' }],
    batch_size: [{ required: true, message: t('knowledgeBase.set_batch_size'), trigger: 'blur' }]
  }))

  const resetImportForm = () => {
    if (importFormRef.value) importFormRef.value.resetFields()
    importForm.file = null
    importForm.chunk_size = 1000
    importForm.chunk_overlap = 100
    importForm.batch_size = 16
    uploadFileList.value = []
  }

  const showImportDialog = (row) => {
    selectedKb.value = row
    resetImportForm()
    importDialogVisible.value = true
  }

  const handleFileChange = (uploadFile, uploadFiles) => {
    uploadFileList.value = uploadFiles.slice(-1)
    importForm.file = uploadFile.raw
    if (importFormRef.value) importFormRef.value.validateField('file')
  }

  const handleFileRemove = () => {
    uploadFileList.value = []
    importForm.file = null
  }

  const submitImport = async () => {
    if (!importFormRef.value || !selectedKb.value) return
    if (importForm.chunk_overlap >= importForm.chunk_size) {
      ElMessage.error(t('knowledgeBase.overlap_less_than_size'))
      return
    }
    await importFormRef.value.validate(async (valid) => {
      if (!valid) return
      importing.value = true
      try {
        const formData = new FormData()
        formData.append('file', importForm.file)
        formData.append('chunk_size', importForm.chunk_size)
        formData.append('chunk_overlap', importForm.chunk_overlap)
        formData.append('batch_size', importForm.batch_size)
        await knowledgeBaseApi.importDocument(selectedKb.value.id, formData)
        ElMessage.success(t('knowledgeBase.import_success'))
        importDialogVisible.value = false
        if (documentDialogVisible.value) fetchDocuments()
      } catch (error) {
        ElMessage.error(t('knowledgeBase.import_failed') + error.message)
      } finally {
        importing.value = false
      }
    })
  }

  const showDocumentDialog = (row) => {
    selectedKb.value = row
    documentPage.value = 1
    documentDialogVisible.value = true
    fetchDocuments()
  }

  const fetchDocuments = async () => {
    if (!selectedKb.value) return
    documentLoading.value = true
    try {
      const res = await knowledgeBaseApi.documents(selectedKb.value.id, {
        page: documentPage.value,
        size: documentPageSize.value
      })
      documentList.value = res.data.data.items || []
      documentTotal.value = res.data.data.total || 0
    } catch (error) {
      ElMessage.error(t('knowledgeBase.fetch_doc_list_failed') + error.message)
    } finally {
      documentLoading.value = false
    }
  }

  const handleDocumentSizeChange = () => {
    documentPage.value = 1
    fetchDocuments()
  }

  const showContentDialog = async (row) => {
    if (!selectedKb.value) return
    try {
      const res = await knowledgeBaseApi.document(selectedKb.value.id, row.id)
      contentTitle.value = row.filename
      documentContent.value = res.data.data.content || ''
      contentDialogVisible.value = true
    } catch (error) {
      ElMessage.error(t('knowledgeBase.fetch_doc_content_failed') + error.message)
    }
  }

  const deleteSelectedDocument = (documentId) => knowledgeBaseApi.deleteDocument(selectedKb.value.id, documentId)

  const { handleDelete: confirmDeleteDocument } = useDeleteConfirm(deleteSelectedDocument, fetchDocuments)

  const handleDeleteDocument = (row) => {
    if (!selectedKb.value) return
    confirmDeleteDocument(row.id, row.filename, {
      title: t('knowledgeBase.prompt'),
      message: t('knowledgeBase.delete_doc_confirm', { filename: row.filename }),
      dangerouslyUseHTMLString: false,
      successMessage: t('knowledgeBase.delete_doc_success'),
      errorMessage: t('knowledgeBase.delete_doc_failed')
    })
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
    handleDeleteDocument
  }
}
