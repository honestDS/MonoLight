<template>
  <div class="view-container">
    <BaseDataTable
      :data="tableData"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('knowledgeBase.create_kb')"
      :refresh-text="$t('knowledgeBase.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @create="showDialog"
      @refresh="handleRefresh"
      @page-change="fetchData"
      @size-change="handleSizeChange"
    >
      <el-table-column :resizable="false" prop="name" :label="$t('knowledgeBase.kb_name')" min-width="150" sortable />
      <el-table-column :resizable="false" prop="description" :label="$t('knowledgeBase.description')" min-width="250" show-overflow-tooltip />
      <el-table-column :resizable="false" :label="$t('knowledgeBase.embedding_model')" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          {{ getEmbeddingModelName(row) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="created_at" :label="$t('knowledgeBase.created_at')" width="180" sortable>
        <template #default="{ row }">
          {{ formatTime(row.created_at) }}
        </template>
      </el-table-column>

      <el-table-column :resizable="false" :label="$t('knowledgeBase.actions')" width="520" align="center" fixed="right">
        <template #default="{ row }">
          <div class="action-buttons">
            <el-button type="success" size="small" @click="showImportDialog(row)">{{ $t('knowledgeBase.import_doc') }}</el-button>
            <el-button type="info" size="small" @click="showDocumentDialog(row)">{{ $t('knowledgeBase.documents') }}</el-button>
            <el-button type="warning" size="small" @click="showQueryTestDialog(row)">{{ $t('knowledgeBase.test') }}</el-button>
            <el-button type="primary" size="small" @click="showEditDialog(row)">{{ $t('knowledgeBase.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(row)">{{ $t('knowledgeBase.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <!-- 知识库弹窗 -->
    <el-dialog
      :title="isEditing ? $t('knowledgeBase.edit_kb') : $t('knowledgeBase.create_kb')"
      v-model="dialogVisible"
      width="50%"
      class="standard-dialog"
      center
      align-center
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="120px" size="default">
        <el-form-item :label="$t('knowledgeBase.name')" prop="name">
          <el-input v-model="form.name" :placeholder="$t('knowledgeBase.input_kb_name')" />
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.description')" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :placeholder="$t('knowledgeBase.input_kb_desc')"
            :rows="3"
          />
        </el-form-item>
        <el-form-item v-if="!isEditing" :label="$t('knowledgeBase.embedding_model')" prop="embedding_model_key">
          <el-select
            v-model="form.embedding_model_key"
            :placeholder="$t('knowledgeBase.select_embedding_model')"
            class="full-width-input"
            filterable
          >
            <el-option
              v-for="item in embeddingModelOptions"
              :key="item.key"
              :label="item.label"
              :value="item.key"
            />
          </el-select>
          <div class="help-text mt-5">{{ $t('knowledgeBase.embedding_model_hint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('knowledgeBase.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submitForm" size="default">{{ $t('knowledgeBase.confirm') }}</el-button>
      </template>
    </el-dialog>

    <!-- 导入文档弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.import_doc')"
      v-model="importDialogVisible"
      width="560px"
      class="standard-dialog"
      center
      align-center
    >
      <el-alert
        :title="$t('knowledgeBase.import_alert')"
        type="info"
        :closable="false"
        class="mb-15"
      />
      <el-form :model="importForm" :rules="importRules" ref="importFormRef" label-width="120px" size="default">
        <el-form-item :label="$t('knowledgeBase.target_kb')">
          <el-input :model-value="selectedKb?.name || '-'" disabled />
          <div class="help-text mt-5">{{ $t('knowledgeBase.target_kb_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.select_doc')" prop="file">
          <el-upload
            class="full-width-input"
            action=""
            :auto-upload="false"
            :limit="1"
            :file-list="uploadFileList"
            :on-change="handleFileChange"
            :on-remove="handleFileRemove"
          >
            <el-button type="primary">{{ $t('knowledgeBase.select_file') }}</el-button>
          </el-upload>
          <div class="help-text mt-5">{{ $t('knowledgeBase.select_doc_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.chunk_size')" prop="chunk_size">
          <el-input-number v-model="importForm.chunk_size" :min="100" :max="20000" :step="100" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.chunk_size_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.chunk_overlap')" prop="chunk_overlap">
          <el-input-number v-model="importForm.chunk_overlap" :min="0" :max="5000" :step="50" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.chunk_overlap_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.batch_size')" prop="batch_size">
          <el-input-number v-model="importForm.batch_size" :min="1" :max="256" :step="1" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.batch_size_hint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importDialogVisible = false" size="default">{{ $t('knowledgeBase.cancel') }}</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport" size="default">{{ $t('knowledgeBase.start_import') }}</el-button>
      </template>
    </el-dialog>

    <!-- 文档管理弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.doc_management', { name: selectedKb?.name || '' })"
      v-model="documentDialogVisible"
      width="1040px"
      class="standard-dialog"
      center
      align-center
    >
      <el-table :data="documentList" :loading="documentLoading">
        <el-table-column prop="filename" :label="$t('knowledgeBase.filename')" min-width="220" show-overflow-tooltip />
        <el-table-column prop="chunk_count" :label="$t('knowledgeBase.chunk_count')" width="90" align="center" />
        <el-table-column prop="chunk_size" :label="$t('knowledgeBase.chunk_size')" width="100" align="center" />
        <el-table-column prop="chunk_overlap" :label="$t('knowledgeBase.overlap')" width="90" align="center" />
        <el-table-column prop="batch_size" :label="$t('knowledgeBase.batch_size_col')" width="90" align="center" />
        <el-table-column :label="$t('knowledgeBase.import_time')" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.actions')" width="210" align="center" fixed="right">
          <template #default="{ row }">
            <div class="action-buttons document-action-buttons">
              <el-button type="primary" size="small" @click="showContentDialog(row)">{{ $t('knowledgeBase.original_text') }}</el-button>
              <el-button type="danger" size="small" @click="handleDeleteDocument(row)">{{ $t('knowledgeBase.delete') }}</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="document-pagination">
        <el-pagination
          v-model:current-page="documentPage"
          v-model:page-size="documentPageSize"
          :total="documentTotal"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="fetchDocuments"
          @size-change="handleDocumentSizeChange"
        />
      </div>
    </el-dialog>

    <!-- 原文查看弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.view_original', { title: contentTitle })"
      v-model="contentDialogVisible"
      width="760px"
      class="standard-dialog"
      center
      align-center
    >
      <pre class="document-content">{{ documentContent }}</pre>
    </el-dialog>

    <!-- 检索测试弹窗 -->
    <el-dialog
      :title="$t('knowledgeBase.query_test', { name: selectedKb?.name || '' })"
      v-model="queryTestDialogVisible"
      width="820px"
      class="standard-dialog"
      center
      align-center
    >
      <el-form :model="queryTestForm" :rules="queryTestRules" ref="queryTestFormRef" label-width="100px" size="default">
        <el-form-item label="TopK" prop="top_k">
          <el-input-number v-model="queryTestForm.top_k" :min="1" :max="50" :step="1" class="full-width-input" />
          <div class="help-text mt-5">{{ $t('knowledgeBase.top_k_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('knowledgeBase.query_text')" prop="query">
          <el-input
            v-model="queryTestForm.query"
            type="textarea"
            :rows="4"
            :placeholder="$t('knowledgeBase.input_query')"
          />
          <div class="help-text mt-5">{{ $t('knowledgeBase.query_hint') }}</div>
        </el-form-item>
      </el-form>
      <div class="query-test-actions">
        <el-button type="primary" :loading="queryTesting" @click="submitQueryTest">{{ $t('knowledgeBase.start_test') }}</el-button>
      </div>
      <el-alert
        v-if="queryTested && rerankError"
        type="warning"
        :closable="false"
        show-icon
        class="mt-5"
        :title="$t('knowledgeBase.downgraded_to_hybrid')"
        :description="$t('knowledgeBase.reranker_error', { error: rerankError })"
      />
      <el-alert
        v-else-if="queryTested && retrievalMode === 'hybrid_rerank'"
        type="success"
        :closable="false"
        show-icon
        class="mt-5"
        :title="$t('knowledgeBase.hybrid_rerank_enabled')"
      />
      <el-table v-if="queryTestResults.length" :data="queryTestResults" class="query-result-table">
        <el-table-column label="#" width="60" align="center">
          <template #default="{ $index }">{{ $index + 1 }}</template>
        </el-table-column>
        <el-table-column v-if="retrievalMode === 'hybrid_rerank'" :label="$t('knowledgeBase.rerank_score')" width="110" align="center">
          <template #default="{ row }">{{ formatScore(row.metadata?.rerank_score) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.distance')" width="110" align="center">
          <template #default="{ row }">{{ formatDistance(row.distance) }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.source')" width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.metadata?.filename || '-' }}</template>
        </el-table-column>
        <el-table-column :label="$t('knowledgeBase.chunk_content')" min-width="360">
          <template #default="{ row }">
            <div class="query-result-content">{{ row.content }}</div>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else-if="queryTested" :description="$t('knowledgeBase.no_results')" />

    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import BaseDataTable from '@/components/BaseDataTable.vue'
import { knowledgeBaseApi } from '@/api'
import { formatTime } from '@/utils'
import { useDeleteConfirm } from '@/composables/useDeleteConfirm'

const { t } = useI18n()

const tableData = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)
const submitting = ref(false)
const isEditing = ref(false)
const editingId = ref(null)
const embeddingModels = ref([])
const formRef = ref(null)
const importDialogVisible = ref(false)
const importing = ref(false)
const importFormRef = ref(null)
const uploadFileList = ref([])
const selectedKb = ref(null)
const documentDialogVisible = ref(false)
const documentLoading = ref(false)
const documentList = ref([])
const documentTotal = ref(0)
const documentPage = ref(1)
const documentPageSize = ref(10)
const contentDialogVisible = ref(false)
const documentContent = ref('')
const contentTitle = ref('')
const queryTestDialogVisible = ref(false)
const queryTesting = ref(false)
const queryTested = ref(false)
const queryTestFormRef = ref(null)
const queryTestResults = ref([])
const retrievalMode = ref(null)
const rerankError = ref(null)


const form = reactive({
  name: '',
  description: '',
  embedding_model_key: ''
})

const rules = computed(() => ({
  name: [{ required: true, message: t('knowledgeBase.input_kb_name'), trigger: 'blur' }],
  embedding_model_key: [{ required: !isEditing.value, message: t('knowledgeBase.select_embedding_model_err'), trigger: 'change' }]
}))

const embeddingModelOptions = computed(() => embeddingModels.value.map(item => ({
  ...item,
  key: `${item.channel_id}::${item.model_id}`,
  label: `${item.channel_name} / ${item.model_id}${item.embedding_dimensions ? ` (${item.embedding_dimensions})` : ''}`
})))

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

const queryTestForm = reactive({
  query: '',
  top_k: 5
})

const queryTestRules = computed(() => ({
  query: [{ required: true, message: t('knowledgeBase.input_query_err'), trigger: 'blur' }],
  top_k: [{ required: true, message: t('knowledgeBase.set_top_k'), trigger: 'blur' }]
}))

const fetchData = async () => {
  loading.value = true
  try {
    const res = await knowledgeBaseApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    const { items, total: totalCount, embedding_models: embeddingModelItems } = res.data.data
    tableData.value = items || []
    total.value = totalCount || 0
    embeddingModels.value = embeddingModelItems || []
  } catch (error) {
    ElMessage.error(t('knowledgeBase.fetch_list_failed') + error.message)
  } finally {
    loading.value = false
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  fetchData()
}

const handleSizeChange = () => {
  currentPage.value = 1
  fetchData()
}

const getEmbeddingModelName = (row) => {
  const option = embeddingModelOptions.value.find(item => item.channel_id === row.embedding_channel_id && item.model_id === row.embedding_model_id)
  return option?.label || row.embedding_model_id || '-'
}

const showDialog = () => {
  resetFormFields()
  isEditing.value = false
  editingId.value = null
  dialogVisible.value = true
}

const showEditDialog = (row) => {
  resetFormFields()
  isEditing.value = true
  editingId.value = row.id
  form.name = row.name
  form.description = row.description || ''
  form.embedding_model_key = `${row.embedding_channel_id}::${row.embedding_model_id}`
  dialogVisible.value = true
}

const resetFormFields = () => {
  if (formRef.value) formRef.value.resetFields()
  form.name = ''
  form.description = ''
  form.embedding_model_key = ''
}

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

const showQueryTestDialog = (row) => {
  selectedKb.value = row
  queryTestForm.query = ''
  queryTestForm.top_k = 5
  queryTestResults.value = []
  queryTested.value = false
  if (queryTestFormRef.value) queryTestFormRef.value.clearValidate()
  queryTestDialogVisible.value = true
}

const submitQueryTest = async () => {
  if (!queryTestFormRef.value || !selectedKb.value) return
  await queryTestFormRef.value.validate(async (valid) => {
    if (!valid) return
    queryTesting.value = true
    queryTested.value = false
    try {
      const res = await knowledgeBaseApi.queryTest(selectedKb.value.id, {
        query: queryTestForm.query,
        top_k: queryTestForm.top_k
      })
      queryTestResults.value = res.data.data.items || []
      retrievalMode.value = res.data.data.retrieval_mode || null
      rerankError.value = res.data.data.rerank_error || null
      queryTested.value = true

    } catch (error) {
      ElMessage.error(t('knowledgeBase.query_test_failed') + error.message)
    } finally {
      queryTesting.value = false
    }
  })
}

const formatDistance = (distance) => {
  if (distance === null || distance === undefined) return '-'
  return Number(distance).toFixed(4)
}

const formatScore = (score) => {
  if (score === null || score === undefined) return '-'
  return Number(score).toFixed(4)
}


const submitForm = async () => {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (valid) {
      submitting.value = true
      try {
        if (isEditing.value) {
          await knowledgeBaseApi.update(editingId.value, {
            name: form.name,
            description: form.description
          })
          ElMessage.success(t('knowledgeBase.update_success'))
        } else {
          const [embeddingChannelId, embeddingModelId] = form.embedding_model_key.split('::')
          await knowledgeBaseApi.create({
            name: form.name,
            description: form.description,
            embedding_channel_id: Number(embeddingChannelId),
            embedding_model_id: embeddingModelId
          })
          ElMessage.success(t('knowledgeBase.create_success'))
        }
        dialogVisible.value = false
        fetchData()
      } catch (error) {
        ElMessage.error((isEditing.value ? t('knowledgeBase.update_failed') : t('knowledgeBase.create_failed')) + error.message)
      } finally {
        submitting.value = false
      }
    }
  })
}

const { handleDelete: confirmDeleteKnowledgeBase } = useDeleteConfirm(knowledgeBaseApi.delete, fetchData)

const handleDelete = (row) => {
  confirmDeleteKnowledgeBase(row.id, row.name, {
    title: t('knowledgeBase.prompt'),
    message: t('knowledgeBase.delete_kb_confirm', { name: row.name }),
    dangerouslyUseHTMLString: false,
    errorMessage: t('knowledgeBase.delete_kb_failed')
  })
}

onMounted(() => {
  fetchData()
})
</script>

<style lang="scss">
@import "@/assets/css/KnowledgeBase.scss";
</style>
