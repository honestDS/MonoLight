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
            <el-button type="danger" size="small" @click="deletePrompt(scope.row)">{{ $t('prompts.delete') }}</el-button>
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

    <el-dialog :title="$t('prompts.reassign_prompt_title')" v-model="reassignDialogVisible" width="420px" class="standard-dialog" center align-center>
      <div class="help-text mb-12">{{ $t('prompts.reassign_prompt_hint') }}</div>
      <el-form label-width="120px" size="default">
        <el-form-item :label="$t('prompts.replacement_prompt')">
          <el-select v-model="replacementPromptId" class="full-width-input" filterable>
            <el-option v-for="item in globalPromptOptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reassignDialogVisible = false" size="default">{{ $t('prompts.cancel') }}</el-button>
        <el-button type="danger" @click="confirmReassignAndDelete" size="default" :loading="deleteSubmitting">{{ $t('prompts.confirm_reassign_delete') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { promptApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import { getShortContent } from '../utils'

const prompts = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const submitting = ref(false)
const deleteSubmitting = ref(false)
const dialogVisible = ref(false)
const reassignDialogVisible = ref(false)
const dialogType = ref('create')
const pendingDeletePrompt = ref(null)
const replacementPromptId = ref(null)
const replacementPromptOptions = ref([])

const { t } = useI18n()

const form = reactive({
  id: null,
  name: '',
  content: ''
})

const globalPromptOptions = computed(() => replacementPromptOptions.value.filter(item => item.uid === null && item.id !== pendingDeletePrompt.value?.id))

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

const loadReplacementPromptOptions = async () => {
  const res = await promptApi.list({ page: 1, size: 1000 })
  replacementPromptOptions.value = res.data.data.items || []
}

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

const deletePrompt = async (row) => {
  try {
    await ElMessageBox.confirm(t('prompts.delete_confirm', { name: row.name }), t('prompts.delete'), { type: 'warning' })
    await promptApi.delete(row.id)
    ElMessage.success(t('prompts.delete_success'))
    loadPrompts()
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    if (err.response?.status === 409 || err.response?.data?.code === 409) {
      pendingDeletePrompt.value = row
      await loadReplacementPromptOptions()
      replacementPromptId.value = globalPromptOptions.value[0]?.id || null
      reassignDialogVisible.value = true
      return
    }
    ElMessage.error(err.message || t('prompts.delete_failed'))
  }
}

const confirmReassignAndDelete = async () => {
  if (!pendingDeletePrompt.value || !replacementPromptId.value) {
    return ElMessage.warning(t('prompts.select_replacement_prompt'))
  }
  deleteSubmitting.value = true
  try {
    await promptApi.delete(pendingDeletePrompt.value.id, {
      replacement_prompt_id: replacementPromptId.value,
      confirm_reassign: true
    })
    ElMessage.success(t('prompts.delete_success'))
    reassignDialogVisible.value = false
    pendingDeletePrompt.value = null
    replacementPromptId.value = null
    loadPrompts()
  } catch (err) {
    ElMessage.error(err.message || t('prompts.delete_failed'))
  } finally {
    deleteSubmitting.value = false
  }
}

onMounted(() => {
  loadPrompts()
})
</script>

<style lang="scss">
@import "@/assets/css/common.scss";
</style>
