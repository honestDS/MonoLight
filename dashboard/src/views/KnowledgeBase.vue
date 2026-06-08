<template>
  <div class="view-container">
    <BaseDataTable
      :data="tableData"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      create-text="新建知识库"
      @create="showDialog"
      @refresh="handleRefresh"
      @page-change="fetchData"
      @size-change="handleSizeChange"
    >
      <el-table-column :resizable="false" prop="name" label="知识库名称" min-width="150" sortable />
      <el-table-column :resizable="false" prop="description" label="描述" min-width="250" show-overflow-tooltip />
      <el-table-column :resizable="false" label="配置文件" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">
          {{ getProfileName(row.profile_id) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="created_at" label="创建时间" width="180" sortable>
        <template #default="{ row }">
          {{ formatTime(row.created_at) }}
        </template>
      </el-table-column>

      <el-table-column :resizable="false" label="操作" width="520" align="center" fixed="right">
        <template #default="{ row }">
          <div class="action-buttons">
            <el-button type="success" size="small" @click="showImportDialog(row)">导入文档</el-button>
            <el-button type="info" size="small" @click="showDocumentDialog(row)">文档</el-button>
            <el-button type="warning" size="small" @click="showQueryTestDialog(row)">测试</el-button>
            <el-button type="primary" size="small" @click="showEditDialog(row)">编辑</el-button>
            <el-button type="danger" size="small" @click="handleDelete(row)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <!-- 知识库弹窗 -->
    <el-dialog
      :title="isEditing ? '编辑知识库' : '新建知识库'"
      v-model="dialogVisible"
      width="50%"
      class="standard-dialog"
      center
      align-center
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="120px" size="default">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入知识库名称" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            placeholder="请输入知识库描述"
            :rows="3"
          />
        </el-form-item>
        <el-form-item label="配置文件" prop="profile_id">
          <el-select
            v-model="form.profile_id"
            placeholder="选择绑定嵌入模型的配置"
            class="full-width-input"
            :disabled="isEditing"
          >
            <el-option
              v-for="item in availableProfileOptions"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
          <div class="help-text mt-5">
            {{ isEditing ? '配置文件用于知识库向量化，创建后不可修改。' : '绑定此配置文件后，知识库将使用该配置中定义的 Embedding 模型进行向量化。' }}
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitForm" size="default">确定</el-button>
      </template>
    </el-dialog>

    <!-- 导入文档弹窗 -->
    <el-dialog
      title="导入文档"
      v-model="importDialogVisible"
      width="560px"
      class="standard-dialog"
      center
      align-center
    >
      <el-alert
        title="当前支持 UTF-8 或 GBK 编码的文本类文档。导入后会保存原文，并按设置切分成多个片段写入向量库。"
        type="info"
        :closable="false"
        class="mb-15"
      />
      <el-form :model="importForm" :rules="importRules" ref="importFormRef" label-width="120px" size="default">
        <el-form-item label="目标知识库">
          <el-input :model-value="selectedKb?.name || '-'" disabled />
          <div class="help-text mt-5">文档会导入到这个知识库，并使用该知识库绑定配置文件里的向量模型。</div>
        </el-form-item>
        <el-form-item label="选择文档" prop="file">
          <el-upload
            class="full-width-input"
            action=""
            :auto-upload="false"
            :limit="1"
            :file-list="uploadFileList"
            :on-change="handleFileChange"
            :on-remove="handleFileRemove"
          >
            <el-button type="primary">选择文件</el-button>
          </el-upload>
          <div class="help-text mt-5">请选择需要导入的文本文件，例如 .txt、.md、.csv、.json 等。</div>
        </el-form-item>
        <el-form-item label="分块大小" prop="chunk_size">
          <el-input-number v-model="importForm.chunk_size" :min="100" :max="20000" :step="100" class="full-width-input" />
          <div class="help-text mt-5">每个片段最多包含多少字符。数值越大，单段上下文越完整；数值越小，检索越精细。</div>
        </el-form-item>
        <el-form-item label="分块重叠" prop="chunk_overlap">
          <el-input-number v-model="importForm.chunk_overlap" :min="0" :max="5000" :step="50" class="full-width-input" />
          <div class="help-text mt-5">相邻片段重复保留的字符数，用于避免句子被切断后丢失上下文，必须小于分块大小。</div>
        </el-form-item>
        <el-form-item label="批处理大小" prop="batch_size">
          <el-input-number v-model="importForm.batch_size" :min="1" :max="256" :step="1" class="full-width-input" />
          <div class="help-text mt-5">每次请求向量模型处理的片段数量。网络稳定、模型额度充足时可适当调大。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importDialogVisible = false" size="default">取消</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport" size="default">开始导入</el-button>
      </template>
    </el-dialog>

    <!-- 文档管理弹窗 -->
    <el-dialog
      :title="`文档管理 - ${selectedKb?.name || ''}`"
      v-model="documentDialogVisible"
      width="1040px"
      class="standard-dialog"
      center
      align-center
    >
      <el-table :data="documentList" :loading="documentLoading" border>
        <el-table-column prop="filename" label="文件名" min-width="220" show-overflow-tooltip />
        <el-table-column prop="chunk_count" label="分块数" width="90" align="center" />
        <el-table-column prop="chunk_size" label="分块大小" width="100" align="center" />
        <el-table-column prop="chunk_overlap" label="重叠" width="90" align="center" />
        <el-table-column prop="batch_size" label="批处理" width="90" align="center" />
        <el-table-column label="导入时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="210" align="center" fixed="right">
          <template #default="{ row }">
            <div class="action-buttons document-action-buttons">
              <el-button type="primary" size="small" @click="showContentDialog(row)">原文</el-button>
              <el-button type="danger" size="small" @click="handleDeleteDocument(row)">删除</el-button>
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
      :title="`查看原文 - ${contentTitle}`"
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
      :title="`检索测试 - ${selectedKb?.name || ''}`"
      v-model="queryTestDialogVisible"
      width="820px"
      class="standard-dialog"
      center
      align-center
    >
      <el-form :model="queryTestForm" :rules="queryTestRules" ref="queryTestFormRef" label-width="100px" size="default">
        <el-form-item label="TopK" prop="top_k">
          <el-input-number v-model="queryTestForm.top_k" :min="1" :max="50" :step="1" class="full-width-input" />
          <div class="help-text mt-5">返回最相似的片段数量。数值越大，返回结果越多，但测试耗时可能增加。</div>
        </el-form-item>
        <el-form-item label="检索词" prop="query">
          <el-input
            v-model="queryTestForm.query"
            type="textarea"
            :rows="4"
            placeholder="请输入要测试检索的关键词、问题或一段描述"
          />
          <div class="help-text mt-5">系统会将检索词向量化，并在当前知识库中查找最相似的文档片段。</div>
        </el-form-item>
      </el-form>
      <div class="query-test-actions">
        <el-button type="primary" :loading="queryTesting" @click="submitQueryTest">开始测试</el-button>
      </div>
      <el-table v-if="queryTestResults.length" :data="queryTestResults" border class="query-result-table">
        <el-table-column label="#" width="60" align="center">
          <template #default="{ $index }">{{ $index + 1 }}</template>
        </el-table-column>
        <el-table-column label="距离" width="110" align="center">
          <template #default="{ row }">{{ formatDistance(row.distance) }}</template>
        </el-table-column>
        <el-table-column label="来源" width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.metadata?.filename || '-' }}</template>
        </el-table-column>
        <el-table-column label="片段内容" min-width="360">
          <template #default="{ row }">
            <div class="query-result-content">{{ row.content }}</div>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else-if="queryTested" description="未检索到结果" />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import BaseDataTable from '@/components/BaseDataTable.vue'
import { knowledgeBaseApi } from '@/api'
import { formatTime } from '@/utils'
import { useDeleteConfirm } from '@/composables/useDeleteConfirm'

const tableData = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)
const submitting = ref(false)
const isEditing = ref(false)
const editingId = ref(null)
const allProfiles = ref([])
const profileList = ref([])
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

const form = reactive({
  name: '',
  description: '',
  profile_id: null
})

const rules = {
  name: [{ required: true, message: '请输入知识库名称', trigger: 'blur' }],
  profile_id: [{ required: true, message: '请选择配置文件', trigger: 'change' }]
}

const importForm = reactive({
  file: null,
  chunk_size: 1000,
  chunk_overlap: 100,
  batch_size: 16
})

const importRules = {
  file: [{ required: true, message: '请选择要导入的文档', trigger: 'change' }],
  chunk_size: [{ required: true, message: '请设置分块大小', trigger: 'blur' }],
  chunk_overlap: [{ required: true, message: '请设置分块重叠', trigger: 'blur' }],
  batch_size: [{ required: true, message: '请设置批处理大小', trigger: 'blur' }]
}

const queryTestForm = reactive({
  query: '',
  top_k: 5
})

const queryTestRules = {
  query: [{ required: true, message: '请输入检索词', trigger: 'blur' }],
  top_k: [{ required: true, message: '请设置 TopK', trigger: 'blur' }]
}

const fetchData = async () => {
  loading.value = true
  try {
    const res = await knowledgeBaseApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    const { items, total: totalCount, profiles, available_profiles: availableProfiles } = res.data.data
    tableData.value = items || []
    total.value = totalCount || 0
    allProfiles.value = profiles || []
    profileList.value = availableProfiles || []
  } catch (error) {
    ElMessage.error('获取列表失败: ' + error.message)
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

const getProfileName = (profileId) => {
  return allProfiles.value.find(item => item.id === profileId)?.name || '-'
}

const availableProfileOptions = computed(() => {
  if (!isEditing.value) return profileList.value

  const currentProfile = allProfiles.value.find(item => item.id === form.profile_id)
  return currentProfile ? [currentProfile] : []
})

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
  form.profile_id = row.profile_id
  dialogVisible.value = true
}

const resetFormFields = () => {
  if (formRef.value) formRef.value.resetFields()
  form.name = ''
  form.description = ''
  form.profile_id = null
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
    ElMessage.error('分块重叠必须小于分块大小')
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
      ElMessage.success('文档导入成功')
      importDialogVisible.value = false
      if (documentDialogVisible.value) fetchDocuments()
    } catch (error) {
      ElMessage.error('文档导入失败: ' + error.message)
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
    ElMessage.error('获取文档列表失败: ' + error.message)
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
    ElMessage.error('获取文档原文失败: ' + error.message)
  }
}

const deleteSelectedDocument = (documentId) => knowledgeBaseApi.deleteDocument(selectedKb.value.id, documentId)

const { handleDelete: confirmDeleteDocument } = useDeleteConfirm(deleteSelectedDocument, fetchDocuments)

const handleDeleteDocument = (row) => {
  if (!selectedKb.value) return
  confirmDeleteDocument(row.id, row.filename, {
    title: '提示',
    message: `确定要删除文档 "${row.filename}" 吗？此操作会同时删除向量库中的相关分块。`,
    dangerouslyUseHTMLString: false,
    successMessage: '文档删除成功',
    errorMessage: '文档删除失败'
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
      queryTested.value = true
    } catch (error) {
      ElMessage.error('检索测试失败: ' + error.message)
    } finally {
      queryTesting.value = false
    }
  })
}

const formatDistance = (distance) => {
  if (distance === null || distance === undefined) return '-'
  return Number(distance).toFixed(4)
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
          ElMessage.success('修改成功')
        } else {
          await knowledgeBaseApi.create(form)
          ElMessage.success('创建成功')
        }
        dialogVisible.value = false
        fetchData()
      } catch (error) {
        ElMessage.error((isEditing.value ? '修改失败: ' : '创建失败: ') + error.message)
      } finally {
        submitting.value = false
      }
    }
  })
}

const { handleDelete: confirmDeleteKnowledgeBase } = useDeleteConfirm(knowledgeBaseApi.delete, fetchData)

const handleDelete = (row) => {
  confirmDeleteKnowledgeBase(row.id, row.name, {
    title: '提示',
    message: `确定要删除知识库 "${row.name}" 吗？此操作将同时清空向量库中的相关数据且不可恢复。`,
    dangerouslyUseHTMLString: false,
    errorMessage: '删除失败'
  })
}

onMounted(() => {
  fetchData()
})
</script>

<style lang="scss">
@import "@/assets/css/KnowledgeBase.scss";
</style>
