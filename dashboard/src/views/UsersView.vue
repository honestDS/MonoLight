<template>
  <div class="view-container">
    <BaseDataTable
      :data="users"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('users.create_user')"
      :refresh-text="$t('users.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadUsers"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="username" :label="$t('users.username')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('users.role')" min-width="100" sortable>
        <template #default="scope">
          <el-tag :type="scope.row.is_superuser ? 'danger' : 'info'" size="default">
            {{ scope.row.is_superuser ? $t('users.super_admin') : $t('users.normal_user') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('users.status')" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" :active-text="$t('users.enable')" :inactive-text="$t('users.disable')" />
        </template>
      </el-table-column>

      <el-table-column :resizable="false" :label="$t('users.actions')" width="380" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button size="small" :type="scope.row.is_active ? 'warning' : 'success'" @click="handleToggleStatus(scope.row)">
              {{ scope.row.is_active ? $t('users.disable') : $t('users.enable') }}
            </el-button>
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('users.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.uid, scope.row.username)">{{ $t('users.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? $t('users.create_user') : $t('users.edit_user')" v-model="dialogVisible" width="50%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="100px" size="default">
        <el-form-item :label="$t('users.username')">
          <el-input v-model="form.username" :placeholder="$t('users.input_username')" :disabled="dialogType === 'edit'"></el-input>
        </el-form-item>
        <el-form-item :label="$t('users.password')">
          <el-input v-model="form.password" type="password" show-password :placeholder="$t('users.input_password')"></el-input>
          <div class="help-text" v-if="dialogType === 'edit'">{{ $t('users.password_hint') }}</div>
        </el-form-item>
        <el-form-item :label="$t('users.status')">
          <el-switch v-model="form.is_active"></el-switch>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('users.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">{{ $t('users.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { adminApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultUserForm } from '../constants'

const users = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const dialogVisible = ref(false)
const dialogType = ref('create')
const submitting = ref(false)

const { t } = useI18n()

const form = reactive(defaultUserForm())

// 加载用户列表
const loadUsers = async () => {
  loading.value = true
  try {
    const res = await adminApi.userList({
      page: currentPage.value,
      size: pageSize.value
    })
    users.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('users.load_failed'))
  } finally {
    loading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete } = useDeleteConfirm(adminApi.userDelete, loadUsers)

const handleRefresh = () => {
  currentPage.value = 1
  loadUsers()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadUsers()
}

const handleToggleStatus = async (row) => {
  const newStatus = !row.is_active
  loading.value = true
  try {
    await adminApi.userUpdate({
      uid: row.uid,
      ...row,
      is_active: newStatus
    })
    row.is_active = newStatus
    ElMessage.success(t('users.user_status_changed', { status: newStatus ? t('users.enabled') : t('users.disabled') }))
  } catch (err) {
    ElMessage.error(err.message || t('users.status_update_failed'))
  } finally {
    loading.value = false
  }
}

const showDialog = (type, row = null) => {
  dialogType.value = type
  if (type === 'edit' && row) {
    form.uid = row.uid
    form.username = row.username
    form.password = ''
    form.is_active = row.is_active !== false
  } else {
    Object.assign(form, defaultUserForm())
  }
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.username || (!dialogType.value === 'edit' && !form.password)) {
    return ElMessage.warning(t('users.fill_required'))
  }
  submitting.value = true
  loading.value = true
  try {
    if (dialogType.value === 'create') {
      await adminApi.userAdd({
        username: form.username,
        password: form.password,
        is_active: form.is_active
      })
      ElMessage.success(t('users.create_success'))
    } else {
      const updateData = {
        uid: form.uid,
        is_active: form.is_active
      }
      if (form.password) {
        updateData.password = form.password
      }
      await adminApi.userUpdate(updateData)
      ElMessage.success(t('users.update_success'))
    }
    dialogVisible.value = false
    loadUsers()
  } catch (err) {
    ElMessage.error(err.message || t('users.submit_failed'))
  } finally {
    submitting.value = false
    loading.value = false
  }
}

onMounted(() => {
  loadUsers()
})
</script>

<style lang="scss">
@import "@/assets/css/common.scss";
</style>