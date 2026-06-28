<template>
  <div class="view-container">
    <BaseDataTable
      :data="tasks"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('scheduledTasks.create_task')"
      :refresh-text="$t('scheduledTasks.refresh')"
      :total-text="$t('common.total_items', { total })"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadTasks"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="name" :label="$t('scheduledTasks.name')" min-width="140" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="session_id" :label="$t('scheduledTasks.session_id')" min-width="180" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="message" :label="$t('scheduledTasks.message')" min-width="240" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="interval_seconds" :label="$t('scheduledTasks.interval_seconds')" width="130" align="center" />
      <el-table-column :resizable="false" prop="status" :label="$t('scheduledTasks.status')" width="110" align="center">
        <template #default="scope">
          <el-switch
            :model-value="scope.row.status === 'enabled'"
            :active-text="$t('scheduledTasks.enabled')"
            :inactive-text="$t('scheduledTasks.disabled')"
            inline-prompt
            @change="toggleStatus(scope.row)" />
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="next_run_at" :label="$t('scheduledTasks.next_run_at')" width="180">
        <template #default="scope">{{ formatTime(scope.row.next_run_at) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="last_run_at" :label="$t('scheduledTasks.last_run_at')" width="180">
        <template #default="scope">{{ formatTime(scope.row.last_run_at) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="run_count" :label="$t('scheduledTasks.run_count')" width="110" align="center" />
      <el-table-column :resizable="false" :label="$t('scheduledTasks.actions')" width="180" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('scheduledTasks.edit') }}</el-button>
            <el-button type="danger" size="small" @click="deleteTask(scope.row)">{{ $t('scheduledTasks.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? $t('scheduledTasks.create_task') : $t('scheduledTasks.edit_task')" v-model="dialogVisible" width="620px" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="130px" size="default">
        <el-form-item :label="$t('scheduledTasks.name')">
          <el-input v-model="form.name" :placeholder="$t('scheduledTasks.input_name')" />
        </el-form-item>
        <el-form-item :label="$t('scheduledTasks.session_id')">
          <el-select v-model="form.session_id" :placeholder="$t('scheduledTasks.select_session')" filterable class="full-width-input" :loading="sessionsLoading">
            <el-option v-for="session in sessions" :key="session.session_id" :label="formatSessionLabel(session)" :value="session.session_id">
              <div class="session-option">
                <div class="session-option-title">{{ session.title || $t('chat.default_title') }}</div>
                <div class="session-option-meta">{{ session.session_id }} · {{ formatTime(session.created_at) }}</div>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('scheduledTasks.interval_seconds')">
          <el-input-number v-model="form.interval_seconds" :min="60" controls-position="right" class="full-width-input" />
        </el-form-item>
        <el-form-item :label="$t('scheduledTasks.status')" v-if="dialogType === 'edit'">
          <el-select v-model="form.status" class="full-width-input">
            <el-option :label="$t('scheduledTasks.enabled')" value="enabled" />
            <el-option :label="$t('scheduledTasks.disabled')" value="disabled" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('scheduledTasks.message')">
          <el-input v-model="form.message" type="textarea" :rows="8" :placeholder="$t('scheduledTasks.input_message')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ $t('scheduledTasks.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">{{ $t('scheduledTasks.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { chatApi, scheduledTaskApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'

const { t } = useI18n()

const tasks = ref([])
const loading = ref(false)
const submitting = ref(false)
const sessionsLoading = ref(false)
const total = ref(0)
const sessions = ref([])
const currentPage = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)
const dialogType = ref('create')

const form = reactive({
  id: null,
  name: '',
  session_id: '',
  interval_seconds: 300,
  status: 'enabled',
  message: ''
})

const formatTime = (timeStr) => {
  if (!timeStr) return '-'
  try {
    return new Date(timeStr).toLocaleString()
  } catch {
    return timeStr
  }
}

const loadTasks = async () => {
  loading.value = true
  try {
    const res = await scheduledTaskApi.list({ page: currentPage.value, size: pageSize.value })
    tasks.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.load_failed'))
  } finally {
    loading.value = false
  }
}

const loadSessions = async () => {
  sessionsLoading.value = true
  try {
    const res = await chatApi.sessionsList()
    sessions.value = res.data.data || []
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.load_sessions_failed'))
  } finally {
    sessionsLoading.value = false
  }
}

const formatSessionLabel = (session) => {
  const title = session.title || t('chat.default_title')
  return `${title} · ${session.session_id} · ${formatTime(session.created_at)}`
}

const resetForm = () => {
  form.id = null
  form.name = ''
  form.session_id = ''
  form.interval_seconds = 300
  form.status = 'enabled'
  form.message = ''
}

const showDialog = (type, row = null) => {
  dialogType.value = type
  resetForm()
  if (type === 'edit' && row) {
    form.id = row.id
    form.name = row.name
    form.session_id = row.session_id
    form.interval_seconds = row.interval_seconds
    form.status = row.status
    form.message = row.message || ''
  }
  dialogVisible.value = true
}

const buildPayload = () => {
  if (!form.name || !form.session_id || !form.message || !form.interval_seconds) {
    ElMessage.warning(t('scheduledTasks.fill_required'))
    return null
  }
  const payload = {
    name: form.name,
    session_id: form.session_id,
    message: form.message,
    interval_seconds: form.interval_seconds
  }
  if (dialogType.value === 'edit') payload.status = form.status
  return payload
}

const submitForm = async () => {
  const payload = buildPayload()
  if (!payload) return
  submitting.value = true
  try {
    if (dialogType.value === 'create') {
      await scheduledTaskApi.create(payload)
    } else {
      await scheduledTaskApi.update(form.id, payload)
    }
    ElMessage.success(t('scheduledTasks.save_success'))
    dialogVisible.value = false
    loadTasks()
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.submit_failed'))
  } finally {
    submitting.value = false
  }
}

const toggleStatus = async (row) => {
  const status = row.status === 'enabled' ? 'disabled' : 'enabled'
  try {
    await scheduledTaskApi.update(row.id, { status })
    row.status = status
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.status_update_failed'))
  }
}

const deleteTask = async (row) => {
  try {
    await ElMessageBox.confirm(t('scheduledTasks.delete_confirm'), t('common.warning'), {
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
      type: 'warning'
    })
    await scheduledTaskApi.delete(row.id)
    ElMessage.success(t('scheduledTasks.delete_success'))
    loadTasks()
  } catch (err) {
    if (err !== 'cancel') {
      ElMessage.error(err.message || t('scheduledTasks.delete_failed'))
    }
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  loadTasks()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadTasks()
}

onMounted(() => {
  loadSessions()
  loadTasks()
})
</script>

<style lang="scss">
@import "@/assets/css/common.scss";

.session-option {
  line-height: 1.35;
  padding: 4px 0;
}

.session-option-title {
  color: var(--el-text-color-primary);
  font-size: 14px;
}

.session-option-meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
