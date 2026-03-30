<template>
  <div class="view-container">
    <BaseDataTable
      :data="users"
      :loading="loading"
      :data-length="users.length"
      create-text="新建用户"
      @create="showDialog('create')"
      @refresh="handleRefresh">

      <el-table-column :resizable="false" prop="username" label="用户名" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" label="角色" min-width="100" sortable>
        <template #default="scope">
          <el-tag :type="scope.row.is_superuser ? 'danger' : 'info'" size="default">
            {{ scope.row.is_superuser ? '超级管理员' : '普通用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" label="状态" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" active-text="启用" inactive-text="禁用" />
        </template>
      </el-table-column>

      <el-table-column :resizable="false" label="操作" width="380" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button size="small" :type="scope.row.is_active ? 'warning' : 'success'" @click="handleToggleStatus(scope.row)">
              {{ scope.row.is_active ? '禁用' : '启用' }}
            </el-button>
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">编辑</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.uid, scope.row.username)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? '新建用户' : '编辑用户'" v-model="dialogVisible" width="50%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="100px" size="default">
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="请输入用户名" :disabled="dialogType === 'edit'"></el-input>
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" show-password placeholder="请输入密码"></el-input>
          <div class="help-text" v-if="dialogType === 'edit'">留空则不修改密码</div>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="form.is_active"></el-switch>
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
import { adminApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultUserForm } from '../constants'

const users = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const dialogType = ref('create')
const submitting = ref(false)

const form = reactive(defaultUserForm())

// 加载用户列表
const loadUsers = async () => {
  loading.value = true
  try {
    const res = await adminApi.userList()
    users.value = res.data.data || []
  } catch (err) {
    ElMessage.error(err.message || '加载列表失败')
  } finally {
    loading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete } = useDeleteConfirm(adminApi.userDelete, loadUsers)

const handleRefresh = () => {
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
    ElMessage.success(`用户已${newStatus ? '启用' : '禁用'}`)
  } catch (err) {
    ElMessage.error(err.message || '状态更新失败')
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
    return ElMessage.warning('请填写必要信息')
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
      ElMessage.success('用户已创建')
    } else {
      const updateData = {
        uid: form.uid,
        is_active: form.is_active
      }
      if (form.password) {
        updateData.password = form.password
      }
      await adminApi.userUpdate(updateData)
      ElMessage.success('用户已更新')
    }
    dialogVisible.value = false
    loadUsers()
  } catch (err) {
    ElMessage.error(err.message || '提交失败')
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