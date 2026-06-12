<template>
  <div class="view-container">
    <!-- 筛选面板 -->
    <div class="filter-card">
      <el-form :inline="true" :model="filters" size="default">
        <el-form-item :label="$t('historyLogs.log_level')">
          <el-select v-model="filters.level" :placeholder="$t('historyLogs.select_level')" clearable class="filter-level-select">
            <el-option label="DEBUG" value="DEBUG" />
            <el-option label="INFO" value="INFO" />
            <el-option label="WARNING" value="WARNING" />
            <el-option label="ERROR" value="ERROR" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('historyLogs.uid')">
          <el-input v-model="filters.uid" :placeholder="$t('historyLogs.input_uid')" clearable class="filter-uid-input" />
        </el-form-item>
        <el-form-item :label="$t('historyLogs.session_id')">
          <el-input v-model="filters.sessionId" :placeholder="$t('historyLogs.input_session_id')" clearable class="filter-session-input" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">{{ $t('historyLogs.search') }}</el-button>
          <el-button @click="resetFilters">{{ $t('historyLogs.reset') }}</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 数据表格 -->
    <BaseDataTable
      :data="logs"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="''"
      :refresh-text="$t('historyLogs.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @refresh="handleRefresh"
      @page-change="loadLogs"
      @size-change="handleSizeChange">
      
      <el-table-column :resizable="false" prop="created_at" :label="$t('historyLogs.time')" width="200" sortable>
        <template #default="scope">
          {{ formatTime(scope.row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="level" :label="$t('historyLogs.level')" width="100" align="center">
        <template #default="scope">
          <el-tag :type="getLevelTag(scope.row.level)" size="default">
            {{ scope.row.level }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="module" :label="$t('historyLogs.module')" width="180" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="message" :label="$t('historyLogs.message')" min-width="300" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="uid" :label="$t('historyLogs.uid')" width="160" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="session_id" :label="$t('historyLogs.session_id')" width="160" show-overflow-tooltip />
    </BaseDataTable>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { systemApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'

const logs = ref([])
const loading = ref(false)
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

const { t } = useI18n()

const filters = reactive({
  level: '',
  uid: '',
  sessionId: ''
})

const formatTime = (timeStr) => {
  if (!timeStr) return '-'
  try {
    const d = new Date(timeStr)
    return d.toLocaleString()
  } catch {
    return timeStr
  }
}

const getLevelTag = (level) => {
  switch (level) {
    case 'DEBUG': return 'info'
    case 'INFO': return 'success'
    case 'WARNING':
    case 'WARN': return 'warning'
    case 'ERROR':
    case 'CRITICAL': return 'danger'
    default: return ''
  }
}

const loadLogs = async () => {
  loading.value = true
  try {
    const params = {
      page: currentPage.value,
      size: pageSize.value,
    }
    if (filters.level) params.level = filters.level
    if (filters.uid) params.uid = filters.uid
    if (filters.sessionId) params.session_id = filters.sessionId

    const res = await systemApi.logsHistory(params)
    logs.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('historyLogs.load_failed'))
  } finally {
    loading.value = false
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  loadLogs()
}

const handleSearch = () => {
  currentPage.value = 1
  loadLogs()
}

const resetFilters = () => {
  filters.level = ''
  filters.uid = ''
  filters.sessionId = ''
  currentPage.value = 1
  loadLogs()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadLogs()
}

onMounted(() => {
  loadLogs()
})
</script>

<style scoped lang="scss">
@import "../assets/css/HistoryLogs.scss";
</style>
