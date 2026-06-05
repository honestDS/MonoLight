<template>
  <div class="view-container">
    <BaseDataTable
      :data="prompts"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      create-text="新建提示词"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadPrompts"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="name" label="名称" min-width="150" sortable></el-table-column>
      <el-table-column :resizable="false" label="内容预览" min-width="300">
        <template #default="scope">
          {{ getShortContent(scope.row.content) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" label="操作" width="280" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">编辑</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? '新建提示词' : '编辑提示词'" v-model="dialogVisible" width="50%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="100px" size="default">
        <el-form-item label="提示词名称">
          <el-input v-model="form.name" placeholder="请输入提示词名称"></el-input>
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="form.content" type="textarea" :rows="6" placeholder="请输入提示词内容"></el-input>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">取消</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { promptApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { getShortContent } from '../utils'

const prompts = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const submitting = ref(false)
const dialogVisible = ref(false)
const dialogType = ref('create')

const form = reactive({
  id: null,
  name: '',
  content: ''
})

// 加载提示词列表
const loadPrompts = async () => {
  loading.value = true
  try {
    const res = await promptApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    prompts.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || '加载列表失败')
  } finally {
    loading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete } = useDeleteConfirm(promptApi.delete, loadPrompts)

const handleRefresh = () => {
  currentPage.value = 1
  loadPrompts()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadPrompts()
}

const showDialog = (type, row = null) => {
  dialogType.value = type
  if (type === 'edit' && row) {
    form.id = row.id
    form.name = row.name
    form.content = row.content || ''
  } else {
    form.id = null
    form.name = ''
    form.description = ''
    form.content = ''
  }
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name || !form.content) {
    return ElMessage.warning('请填写名称和内容')
  }
  submitting.value = true
  try {
    if (dialogType.value === 'create') {
      await promptApi.create({ ...form })
    } else {
      await promptApi.update(form.id, { ...form })
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    loadPrompts()
  } catch (err) {
    ElMessage.error(err.message || '提交失败')
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadPrompts()
})
</script>

<style lang="scss">
@import "@/assets/css/common.scss";
</style>