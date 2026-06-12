<template>
  <div class="view-container">
    <BaseDataTable
      :data="providers"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('providers.create_provider')"
      :refresh-text="$t('providers.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @create="openCreateDialog"
      @refresh="handleRefresh"
      @page-change="fetchProviders"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="name" :label="$t('providers.name')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" prop="provider_type" :label="$t('providers.type')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('providers.model_type')" min-width="120" sortable>
        <template #default="scope">{{ getModelUsageLabel(scope.row.usage) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="base_url" :label="$t('providers.base_url')" min-width="200" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('providers.status')" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" :active-text="$t('providers.enable')" :inactive-text="$t('providers.disable')" />
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('providers.actions')" width="360" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button :type="scope.row.is_active ? 'warning' : 'success'" size="small" @click="handleToggleActive(scope.row)">{{ scope.row.is_active ? $t('providers.disable') : $t('providers.enable') }}</el-button>
            <el-button type="primary" size="small" @click="handleEdit(scope.row)">{{ $t('providers.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('providers.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>

    </BaseDataTable>

    <!-- 提供商编辑/创建弹窗 -->
    <el-dialog :title="isEdit ? $t('providers.edit_provider') : $t('providers.create_provider')" v-model="dialogVisible" width="50%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="100px" size="default">
        <el-form-item :label="$t('providers.provider_name')">
          <el-input v-model="form.name" :placeholder="$t('providers.provider_name_placeholder')" />
        </el-form-item>
        <el-form-item :label="$t('providers.provider_type')">
          <el-select v-model="form.provider_type" :placeholder="$t('providers.select_type')" class="full-width-input">
            <el-option v-for="item in providerTypes" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('providers.model_type_label')">
          <el-select v-model="form.usage" :placeholder="$t('providers.model_type_label')" class="full-width-input">
            <el-option v-for="item in modelUsages" :key="item" :label="getModelUsageLabel(item)" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('providers.api_key')">
          <el-input v-model="form.api_key" type="password" show-password :placeholder="$t('providers.api_key_placeholder')" />
        </el-form-item>
        <el-form-item :label="$t('providers.base_url')">
          <el-input v-model="form.base_url" :placeholder="$t('providers.base_url_placeholder')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('providers.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">{{ $t('providers.confirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { providerApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultProviderForm } from '../constants'

const { t } = useI18n()

// 数据结构定义 (基于 API.json ProviderCreate / ProviderUpdate)
const providers = ref([])
const providerTypes = ref([])
const modelUsages = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const submitting = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const currentId = ref(null)

// 获取模型用途中文名称
const getModelUsageLabel = (value) => {
  const map = {
    CHAT: t('providers.chat_model'),
    EMBEDDING: t('providers.embedding_model'),
    RERANK: t('providers.rerank_model')
  }
  return map[value] || value
}

const form = reactive(defaultProviderForm())

// 加载提供商列表
const fetchProviders = async () => {
  loading.value = true
  try {
    const res = await providerApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    providers.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('providers.load_failed'))
  } finally {
    loading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete } = useDeleteConfirm(providerApi.delete, fetchProviders)

// 切换提供商启用/禁用状态
const handleToggleActive = async (row) => {
  try {
    await providerApi.update(row.id, { is_active: !row.is_active })
    ElMessage.success(row.is_active ? t('providers.disabled') : t('providers.enabled'))
    fetchProviders()
  } catch (err) {
    ElMessage.error(err.message || t('providers.action_failed'))
  }
}


// 获取提供商类型列表
const fetchProviderTypes = async () => {
  try {
    const res = await providerApi.types()
    const data = res.data.data
    providerTypes.value = data?.provider_types || []
    modelUsages.value = data?.model_usages || []
  } catch (err) {
    console.error(t('providers.load_types_failed'), err)
  }
}

// 刷新列表
const handleRefresh = () => {
  currentPage.value = 1
  fetchProviders()
}

const handleSizeChange = () => {
  currentPage.value = 1
  fetchProviders()
}

// 打开创建弹窗
const openCreateDialog = () => {
  isEdit.value = false
  currentId.value = null
  Object.assign(form, defaultProviderForm())
  dialogVisible.value = true
}

// 打开编辑弹窗
const handleEdit = (row) => {
  isEdit.value = true
  currentId.value = row.id
  Object.assign(form, {
    name: row.name,
    provider_type: row.provider_type,
    usage: row.usage,
    api_key: row.api_key,
    base_url: row.base_url,
    is_active: row.is_active
  })
  dialogVisible.value = true
}

// 提交表单
const submitForm = async () => {
  if (!form.name || !form.provider_type || (!isEdit.value && !form.api_key)) {
    return ElMessage.warning(t('providers.fill_required'))
  }
  
  submitting.value = true
  try {
    if (isEdit.value) {
      await providerApi.update(currentId.value, {
        name: form.name,
        provider_type: form.provider_type,
        usage: form.usage,
        api_key: form.api_key,
        base_url: form.base_url,
        is_active: form.is_active
      })
      ElMessage.success(t('providers.update_success'))
    } else {
      // 创建逻辑 (ProviderCreate)
      await providerApi.create({ ...form })
      ElMessage.success(t('providers.create_success'))
    }
    dialogVisible.value = false
    fetchProviders()
  } catch (err) {
    ElMessage.error(err.message || t('providers.submit_failed'))
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
@import "@/assets/css/common.scss";
</style>