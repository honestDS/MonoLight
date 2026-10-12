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

      <el-table-column :resizable="false" prop="name" :label="$t('prompts.name')" :width="nameColumnWidth" show-overflow-tooltip sortable></el-table-column>
      <el-table-column :resizable="false" prop="username" :label="$t('prompts.owner_username')" min-width="140" show-overflow-tooltip sortable>
        <template #default="scope">
          {{ getOwnerLabel(scope.row) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('prompts.content_preview')" min-width="300">
        <template #default="scope">
          {{ getShortContent(scope.row.content) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('prompts.actions')" width="280" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button type="primary" size="small" :disabled="!canManagePrompt(scope.row)" @click="showDialog('edit', scope.row)">{{ $t('prompts.edit') }}</el-button>
            <el-button type="danger" size="small" :loading="deleteSubmitting" :disabled="deleteSubmitting || !canManagePrompt(scope.row)" @click="deletePrompt(scope.row)">{{ $t('prompts.delete') }}</el-button>
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
        <el-form-item :label="$t('prompts.owner_username')">
          <el-select
            v-if="userPermissionsStore.isSuperuser === true"
            v-model="form.uid"
            class="full-width-input"
            filterable
            :placeholder="$t('prompts.select_owner')"
            :loading="ownerUsersLoading"
            :disabled="ownerUsersLoading || submitting">
            <el-option v-for="item in ownerUsers" :key="item.uid" :label="item.username || item.uid" :value="item.uid" />
          </el-select>
          <el-input v-else :model-value="getOwnerLabel(form)" readonly></el-input>
          <div v-if="userPermissionsStore.isSuperuser === true && ownerUsersError" class="help-text">
            {{ ownerUsersError }}
            <el-button type="primary" link @click="fetchOwnerUsers">{{ $t('prompts.refresh') }}</el-button>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('prompts.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting" :disabled="submitting || (userPermissionsStore.isSuperuser === true && (ownerUsersLoading || Boolean(ownerUsersError)))">{{ $t('prompts.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog :title="$t('prompts.reassign_prompt_title')" v-model="reassignDialogVisible" width="420px" class="standard-dialog" center align-center>
      <div class="help-text mb-12">{{ $t('prompts.reassign_prompt_hint') }}</div>
      <el-form label-width="120px" size="default">
        <el-form-item :label="$t('prompts.replacement_prompt')">
          <el-select v-model="replacementPromptId" class="full-width-input" filterable>
             <el-option v-for="item in sameOwnerPromptOptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reassignDialogVisible = false" size="default">{{ $t('prompts.cancel') }}</el-button>
        <el-button type="danger" @click="confirmReassignAndDelete" size="default" :loading="deleteSubmitting" :disabled="deleteSubmitting">{{ $t('prompts.confirm_reassign_delete') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { adminApi, promptApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import { getShortContent } from '../utils'
import { useUserPermissionsStore } from '../composables/useUserPermissions'

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
const currentUid = ref(null)
const ownerUsers = ref([])
const ownerUsersLoading = ref(false)
const ownerUsersError = ref('')

const { t } = useI18n()
const userPermissionsStore = useUserPermissionsStore()

const form = reactive({
  id: null,
  name: '',
  content: '',
  uid: null,
  username: ''
})

const sameOwnerPromptOptions = computed(() => {
  const pendingPrompt = pendingDeletePrompt.value
  if (!pendingPrompt) return []
  return replacementPromptOptions.value.filter(item => item.uid === pendingPrompt.uid && item.id !== pendingPrompt.id)
})

const getOwnerLabel = (row) => row.username || row.uid || t('prompts.owner_unassigned')

const estimateTextWidth = (value) => {
  const text = typeof value === 'string' ? value : ''
  let width = 0
  for (const character of text) {
    width += character.codePointAt(0) <= 0x7f ? 8 : 15
  }
  return width
}

const nameColumnWidth = computed(() => {
  const titleWidth = estimateTextWidth(t('prompts.name')) + 46
  const contentWidth = prompts.value.reduce((width, row) => Math.max(width, estimateTextWidth(row?.name) + 24), 0)
  return Math.min(480, Math.max(120, titleWidth, contentWidth))
})

const canManagePrompt = (row) => {
  if (!row) return false
  if (userPermissionsStore.isSuperuser === true) return true
  const uid = userPermissionsStore.currentUser?.uid || currentUid.value
  return Boolean(uid) && row?.uid === uid
}

const fetchOwnerUsers = async () => {
  if (userPermissionsStore.isSuperuser !== true) return
  ownerUsersLoading.value = true
  ownerUsersError.value = ''
  ownerUsers.value = []
  try {
    const users = []
    let page = 1
    while (true) {
      const res = await adminApi.userList({ page, size: 1000 })
      const data = res.data.data
      const items = data.items || []
      users.push(...items)
      if (!items.length || users.length >= data.total) break
      page += 1
    }
    ownerUsers.value = users
  } catch (err) {
    ownerUsersError.value = t('prompts.load_users_failed')
    ElMessage.error(ownerUsersError.value)
  } finally {
    ownerUsersLoading.value = false
  }
}

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
    currentUid.value = res.data.data.meta?.current_uid ?? null
  } catch (err) {
    prompts.value = []
    total.value = 0
    currentUid.value = null
    ElMessage.error(err.message || t('prompts.load_failed'))
  } finally {
    loading.value = false
  }
}

const loadReplacementPromptOptions = async () => {
  replacementPromptOptions.value = []
  const options = []
  let page = 1
  while (true) {
    const res = await promptApi.list({ page, size: 1000 })
    const data = res.data.data
    const items = data.items || []
    options.push(...items)
    if (!items.length || options.length >= data.total) break
    page += 1
  }
  replacementPromptOptions.value = options
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
  if (type === 'edit' && !canManagePrompt(row)) {
    ElMessage.warning(t('prompts.permission_denied'))
    return
  }
  dialogType.value = type
  if (type === 'edit' && row) {
    form.id = row.id
    form.name = row.name
    form.uid = row.uid
    form.username = getOwnerLabel(row)
    form.content = row.content || ''
  } else {
    const uid = userPermissionsStore.currentUser?.uid || currentUid.value || null
    form.id = null
    form.name = ''
    form.uid = uid
    form.username = userPermissionsStore.currentUser?.username || uid || ''
    form.content = ''
  }
  if (userPermissionsStore.isSuperuser === true) fetchOwnerUsers()
  dialogVisible.value = true
}

const submitForm = async () => {
  if (submitting.value) return
  if (dialogType.value === 'edit' && !canManagePrompt(form)) {
    return ElMessage.warning(t('prompts.permission_denied'))
  }
  if (!form.name || !form.content) {
    return ElMessage.warning(t('prompts.fill_required'))
  }
  if (userPermissionsStore.isSuperuser === true) {
    if (ownerUsersLoading.value || ownerUsersError.value) return
    if (!form.uid) return ElMessage.warning(t('prompts.select_owner'))
  }
  submitting.value = true
  try {
    const payload = {
      name: form.name,
      content: form.content
    }
    if (userPermissionsStore.isSuperuser === true) payload.uid = form.uid
    if (dialogType.value === 'create') {
      await promptApi.create(payload)
    } else {
      await promptApi.update(form.id, payload)
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
  if (!canManagePrompt(row)) {
    ElMessage.warning(t('prompts.permission_denied'))
    return
  }
  if (deleteSubmitting.value) return
  deleteSubmitting.value = true
  try {
    await ElMessageBox.confirm(t('prompts.delete_confirm', { name: row.name }), t('prompts.delete'), { type: 'warning' })
    await promptApi.delete(row.id)
    ElMessage.success(t('prompts.delete_success'))
    loadPrompts()
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    if (err.response?.status === 409 || err.response?.data?.code === 409) {
      pendingDeletePrompt.value = row
      replacementPromptId.value = null
      try {
        await loadReplacementPromptOptions()
      } catch (loadErr) {
        pendingDeletePrompt.value = null
        replacementPromptId.value = null
        replacementPromptOptions.value = []
        reassignDialogVisible.value = false
        ElMessage.error(loadErr.message || t('prompts.delete_failed'))
        return
      }
      if (!sameOwnerPromptOptions.value.length) {
        pendingDeletePrompt.value = null
        replacementPromptId.value = null
        reassignDialogVisible.value = false
        ElMessage.warning(t('prompts.no_replacement_prompt'))
        return
      }
      replacementPromptId.value = sameOwnerPromptOptions.value[0].id
      reassignDialogVisible.value = true
      return
    }
    ElMessage.error(err.message || t('prompts.delete_failed'))
  } finally {
    deleteSubmitting.value = false
  }
}

const confirmReassignAndDelete = async () => {
  if (deleteSubmitting.value) return
  const pendingPrompt = pendingDeletePrompt.value
  if (!pendingPrompt || !canManagePrompt(pendingPrompt)) {
    return ElMessage.warning(t('prompts.permission_denied'))
  }
  if (!replacementPromptId.value || !sameOwnerPromptOptions.value.some(item => item.id === replacementPromptId.value)) {
    return ElMessage.warning(t('prompts.select_replacement_prompt'))
  }
  deleteSubmitting.value = true
  try {
    await promptApi.delete(pendingPrompt.id, {
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
