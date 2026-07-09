<template>
  <div class="view-container message-platforms-view">
    <BaseDataTable
      :data="platforms"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('messagePlatforms.create')"
      :refresh-text="$t('common.refresh')"
      :total-text="$t('common.total_items', { total })"
      @create="openCreateDialog"
      @refresh="handleRefresh"
      @page-change="fetchPlatforms"
      @size-change="handleSizeChange">
      <el-table-column prop="name" :label="$t('messagePlatforms.name')" min-width="150" show-overflow-tooltip />
      <el-table-column :label="$t('messagePlatforms.platform_type')" min-width="150">
        <template #default="{ row }">{{ typeLabel(row.platform_type) }}</template>
      </el-table-column>
      <el-table-column :label="$t('messagePlatforms.status')" width="130">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="$t('messagePlatforms.enabled')" width="100">
        <template #default="{ row }">
          <el-tag :type="row.is_enabled ? 'success' : 'info'">{{ row.is_enabled ? $t('common.status.enable') : $t('common.status.disable') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="account_id" :label="$t('messagePlatforms.account_id')" min-width="160" show-overflow-tooltip />
      <el-table-column :label="$t('messagePlatforms.uid')" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ userLabel(row.uid) }}</template>
      </el-table-column>
      <el-table-column :label="$t('messagePlatforms.last_error')" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ errorLabel(row.last_error) }}</template>
      </el-table-column>
      <el-table-column :label="$t('messagePlatforms.actions')" width="420" fixed="right">
        <template #default="{ row }">
          <div class="action-buttons">
            <el-button size="small" type="primary" @click="handleEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button size="small" type="success" @click="startLogin(row)">{{ $t('messagePlatforms.login') }}</el-button>
            <el-button v-if="row.status === 'ERROR'" size="small" type="warning" :loading="recoveringId === row.id" @click="handleRecover(row)">{{ $t('messagePlatforms.recover') }}</el-button>
            <el-button size="small" type="danger" @click="handleDelete(row.id, row.name)">{{ $t('common.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <MessagePlatformFormDialog
      v-model:visible="dialogVisible"
      :is-edit="isEdit"
      :form="form"
      :platform-types="platformTypes"
      :users="users"
      :users-loading="usersLoading"
      :type-label="typeLabel"
      :submitting="submitting"
      @submit="submitForm" />

    <WeixinOcLoginDialog
      v-model:visible="loginDialogVisible"
      :login-data="loginData"
      :qrcode-image-url="qrcodeImageUrl"
      @closed="clearLoginTimer" />
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import QRCode from 'qrcode'

import BaseDataTable from '../components/BaseDataTable.vue'
import MessagePlatformFormDialog from '../components/MessagePlatformFormDialog.vue'
import WeixinOcLoginDialog from '../components/weixin_oc/WeixinOcLoginDialog.vue'
import { adminApi, messagePlatformApi } from '../api'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'

const { t } = useI18n()

const loading = ref(false)
const submitting = ref(false)
const checkingLogin = ref(false)
const usersLoading = ref(false)
const recoveringId = ref(null)
const dialogVisible = ref(false)
const loginDialogVisible = ref(false)
const isEdit = ref(false)
const currentId = ref(null)
const currentPage = ref(1)
const pageSize = ref(10)
const total = ref(0)
const platforms = ref([])
const platformTypes = ref([])
const users = ref([])
const loginData = ref(null)
const loginPlatformId = ref(null)
const qrcodeImageUrl = ref('')
let loginTimer = null

const defaultForm = () => ({
  name: '',
  platform_type: 'WEIXIN_OPENCLAW',
  is_enabled: true,
  uid: '',
  config: {
    api_timeout_ms: 15000,
    long_poll_timeout_ms: 30000,
    poll_interval_ms: 1000,
    merge_single_poll_messages: true
  },
  state: {}
})

const form = reactive(defaultForm())

const resetForm = () => {
  Object.assign(form, defaultForm())
}

const typeLabel = (value) => t(`messagePlatforms.type_map.${value}`)
const statusLabel = (value) => t(`messagePlatforms.status_map.${value}`)
const statusTagType = (value) => ({ CONNECTED: 'success', WAITING_LOGIN: 'warning', ERROR: 'danger' }[value] || 'info')
const userLabel = (uid) => users.value.find((item) => item.uid === uid)?.username || uid || '-'
const errorLabel = (value) => {
  if (!value) return '-'
  return value.startsWith('ERR_') ? t(`messagePlatforms.error_map.${value}`, value) : value
}

const fetchPlatforms = async () => {
  loading.value = true
  try {
    const res = await messagePlatformApi.list({ page: currentPage.value, size: pageSize.value })
    const data = res.data.data
    platforms.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('messagePlatforms.load_failed'))
  } finally {
    loading.value = false
  }
}

const fetchTypes = async () => {
  try {
    const res = await messagePlatformApi.types()
    platformTypes.value = res.data.data?.platform_types || ['WEIXIN_OPENCLAW']
  } catch (err) {
    console.error(err)
  }
}

const fetchUsers = async () => {
  usersLoading.value = true
  try {
    const res = await adminApi.userList({ page: 1, size: 1000 })
    users.value = res.data.data?.items || []
  } catch (err) {
    console.error(err)
  } finally {
    usersLoading.value = false
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  fetchPlatforms()
}

const handleSizeChange = () => {
  currentPage.value = 1
  fetchPlatforms()
}

const openCreateDialog = () => {
  isEdit.value = false
  currentId.value = null
  resetForm()
  dialogVisible.value = true
}

const handleEdit = (row) => {
  isEdit.value = true
  currentId.value = row.id
  resetForm()
  form.name = row.name
  form.platform_type = row.platform_type
  form.is_enabled = row.is_enabled
  form.uid = row.uid || ''
  form.config = { ...defaultForm().config, ...(row.config || {}) }
  dialogVisible.value = true
}

const buildPayload = () => ({
  name: form.name.trim(),
  platform_type: form.platform_type,
  is_enabled: form.is_enabled,
  uid: form.uid || null,
  config: {
    api_timeout_ms: form.config.api_timeout_ms || 15000,
    long_poll_timeout_ms: form.config.long_poll_timeout_ms || 30000,
    poll_interval_ms: form.config.poll_interval_ms ?? 1000,
    merge_single_poll_messages: form.config.merge_single_poll_messages ?? true
  }
})

const submitForm = async () => {
  if (!form.name || !form.platform_type) {
    return ElMessage.warning(t('messagePlatforms.fill_required'))
  }
  if (form.is_enabled && !form.uid) {
    return ElMessage.warning(t('messagePlatforms.uid_required'))
  }
  submitting.value = true
  try {
    const payload = buildPayload()
    if (isEdit.value) {
      await messagePlatformApi.update(currentId.value, payload)
      ElMessage.success(t('messagePlatforms.update_success'))
      dialogVisible.value = false
      fetchPlatforms()
    } else {
      const res = await messagePlatformApi.create(payload)
      const platform = res.data?.data
      ElMessage.success(t('messagePlatforms.create_success'))
      dialogVisible.value = false
      await fetchPlatforms()
      if (platform?.id) {
        await startLogin(platform)
      }
    }
  } catch (err) {
    ElMessage.error(err.message || t('messagePlatforms.submit_failed'))
  } finally {
    submitting.value = false
  }
}

const clearLoginTimer = () => {
  if (loginTimer) {
    clearInterval(loginTimer)
    loginTimer = null
  }
  qrcodeImageUrl.value = ''
}

const startLogin = async (row) => {
  clearLoginTimer()
  try {
    const res = await messagePlatformApi.startWeixinLogin(row.id)
    loginData.value = res.data.data
    qrcodeImageUrl.value = await QRCode.toDataURL(loginData.value.qrcode_img_content, {
      width: 300,
      margin: 1
    })
    loginPlatformId.value = row.id
    loginDialogVisible.value = true
    ElMessage.success(t('messagePlatforms.login_started'))
    loginTimer = setInterval(checkLoginStatus, 3000)
  } catch (err) {
    ElMessage.error(err.message || t('common.action_failed'))
  }
}

const handleRecover = async (row) => {
  if (!row?.id || recoveringId.value) return
  recoveringId.value = row.id
  try {
    await messagePlatformApi.recover(row.id)
    ElMessage.success(t('messagePlatforms.recover_success'))
    await fetchPlatforms()
  } catch (err) {
    ElMessage.error(err.message || t('common.action_failed'))
  } finally {
    recoveringId.value = null
  }
}

const checkLoginStatus = async () => {
  if (!loginPlatformId.value || checkingLogin.value) return
  checkingLogin.value = true
  try {
    const res = await messagePlatformApi.getWeixinLoginStatus(loginPlatformId.value)
    const data = res.data.data
    if (data.qrcode_status === 'confirmed') {
      ElMessage.success(t('messagePlatforms.login_confirmed'))
      clearLoginTimer()
      loginDialogVisible.value = false
      fetchPlatforms()
    } else if (data.qrcode_status === 'expired') {
      ElMessage.warning(t('messagePlatforms.login_expired'))
      clearLoginTimer()
      fetchPlatforms()
    } else {
      ElMessage.info(t('messagePlatforms.login_waiting'))
    }
  } catch (err) {
    ElMessage.error(err.message || t('common.action_failed'))
  } finally {
    checkingLogin.value = false
  }
}

const { handleDelete } = useDeleteConfirm(messagePlatformApi.delete, fetchPlatforms)

onMounted(() => {
  fetchTypes()
  fetchUsers()
  fetchPlatforms()
})

onUnmounted(() => {
  clearLoginTimer()
})
</script>

<style lang="scss" scoped>
@import "../assets/css/common.scss";
</style>
