<template>
  <div class="view-container">
    <BaseDataTable
      :data="prompts"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('prompts.create_prompt')"
      :refresh-text="$t('prompts.refresh')"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadPrompts"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="name" :label="$t('prompts.name')" min-width="150" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('prompts.content_preview')" min-width="300">
        <template #default="scope">
          {{ getShortContent(scope.row.content) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('prompts.actions')" width="280" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('prompts.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('prompts.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? $t('prompts.create_prompt') : $t('prompts.edit_prompt')" v-model="dialogVisible" width="50%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="100px" size="default">
        <el-form-item :label="$t('prompts.prompt_name')">
          <el-input v-model="form.name" :placeholder="$t('prompts.input_name')"></el-input>
        </el-form-item>
        <el-form-item :label="$t('prompts.content')">
          <el-input v-model="form.content" type="textarea" :rows="6" :placeholder="$t('prompts.input_content')"></el-input>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('prompts.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">{{ $t('prompts.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
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

const { t } = useI18n()

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
    ElMessage.error(err.message || t('prompts.load_failed'))
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
    return ElMessage.warning(t('prompts.fill_required'))
  }
  submitting.value = true
  try {
    if (dialogType.value === 'create') {
      await promptApi.create({ ...form })
    } else {
      await promptApi.update(form.id, { ...form })
    }
    ElMessage.success(t('prompts.save_success'))
    dialogVisible.value = false
    loadPrompts()
  } catch (err) {
    ElMessage.error(err.message || t('prompts.submit_failed'))
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