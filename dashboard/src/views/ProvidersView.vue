<template>
  <BaseDataTable
    :data="providers"
    :loading="loading"
    create-text="添加提供商"
    @create="openCreateDialog"
    @refresh="handleRefresh">

    <el-table-column :resizable="false" prop="name" label="名称" min-width="120" sortable></el-table-column>
    <el-table-column :resizable="false" prop="provider_type" label="类型" min-width="120" sortable></el-table-column>
    <el-table-column :resizable="false" prop="base_url" label="基础URL" min-width="200" sortable></el-table-column>
    <el-table-column :resizable="false" prop="is_active" label="状态" align="center" width="100" sortable>
      <template #default="scope">
        <StatusTag :status="scope.row.is_active" />
      </template>
    </el-table-column>
    <el-table-column :resizable="false" label="操作" width="280" align="center" fixed="right">
      <template #default="scope">
        <div class="action-buttons">
          <el-button type="primary" size="small" @click="handleEdit(scope.row)">编辑</el-button>
          <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">删除</el-button>
        </div>
      </template>
    </el-table-column>
  </BaseDataTable>

  <!-- 提供商编辑/创建弹窗 -->
  <el-dialog :title="isEdit ? '编辑提供商' : '添加提供商'" v-model="dialogVisible" width="50%" class="standard-dialog" center align-center>
    <el-form :model="form" label-width="100px" size="default">
      <el-form-item label="提供商名称">
        <el-input v-model="form.name" placeholder="例如: OpenAI-Official" />
      </el-form-item>
      <el-form-item label="提供商类型">
        <el-select v-model="form.provider_type" placeholder="请选择类型" class="full-width-input">
          <el-option v-for="item in providerTypes" :key="item" :label="item" :value="item" />
        </el-select>
      </el-form-item>
      <el-form-item label="API密钥">
        <el-input v-model="form.api_key" type="password" show-password placeholder="请输入API密钥" />
      </el-form-item>
      <el-form-item label="基础URL">
        <el-input v-model="form.base_url" placeholder="可选，默认为官方URL" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false" size="default">取消</el-button>
      <el-button type="primary" @click="submitForm" size="default" :loading="submitting">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, onMounted, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { providerApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'

// 数据结构定义 (基于 API.json ProviderCreate / ProviderUpdate)
const providers = ref([])
const providerTypes = ref([])
const loading = ref(false)
const submitting = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const currentId = ref(null)

const form = reactive({
  name: '',
  provider_type: 'OPENAI',
  api_key: '',
  base_url: '',
  is_active: true
})

// fetchProviders 必须在 useDeleteConfirm 之前定义
const fetchProviders = async () => {
  loading.value = true
  try {
    const res = await providerApi.list()
    providers.value = res.data.data || []
  } catch (err) {
    ElMessage.error(err.message || '获取列表失败')
  } finally {
    loading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete } = useDeleteConfirm(providerApi.delete, fetchProviders)

// 获取提供商类型列表
const fetchProviderTypes = async () => {
  try {
    const res = await providerApi.types()
    providerTypes.value = res.data.data || []
  } catch (err) {
    console.error('获取类型失败', err)
  }
}

// 刷新列表
const handleRefresh = () => {
  fetchProviders()
}

// 打开创建弹窗
const openCreateDialog = () => {
  isEdit.value = false
  currentId.value = null
  Object.assign(form, {
    name: '',
    provider_type: 'OPENAI',
    api_key: '',
    base_url: '',
    is_active: true
  })
  dialogVisible.value = true
}

// 打开编辑弹窗
const handleEdit = (row) => {
  isEdit.value = true
  currentId.value = row.id
  // 直接使用列表中的数据
  Object.assign(form, {
    name: row.name,
    provider_type: row.provider_type,
    api_key: row.api_key,
    base_url: row.base_url,
    is_active: row.is_active
  })
  dialogVisible.value = true
}

// 提交表单
const submitForm = async () => {
  if (!form.name || !form.provider_type || (!isEdit.value && !form.api_key)) {
    return ElMessage.warning('请填写必要信息')
  }
  
  submitting.value = true
  try {
    if (isEdit.value) {
      await providerApi.update(currentId.value, {
        name: form.name,
        provider_type: form.provider_type,
        api_key: form.api_key,
        base_url: form.base_url,
        is_active: form.is_active
      })
      ElMessage.success('更新成功')
    } else {
      // 创建逻辑 (ProviderCreate)
      await providerApi.create({ ...form })
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    fetchProviders()
  } catch (err) {
    ElMessage.error(err.message || '提交失败')
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  fetchProviders()
  fetchProviderTypes()
})
</script>

<style lang="scss">
@import "@/assets/css/providers.scss";
</style>