<template>
  <div class="view-container">
    <!-- 筛选面板 -->
    <div class="filter-card">
      <el-form :inline="true" :model="filters" size="default">
        <el-form-item label="日志级别">
          <el-select v-model="filters.level" placeholder="请选择级别" clearable class="filter-level-select">
            <el-option label="DEBUG" value="DEBUG" />
            <el-option label="INFO" value="INFO" />
            <el-option label="WARNING" value="WARNING" />
            <el-option label="ERROR" value="ERROR" />
          </el-select>
        </el-form-item>
        <el-form-item label="用户UID">
          <el-input v-model="filters.uid" placeholder="请输入UID" clearable class="filter-uid-input" />
        </el-form-item>
        <el-form-item label="会话ID">
          <el-input v-model="filters.sessionId" placeholder="请输入会话ID" clearable class="filter-session-input" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">搜索</el-button>
          <el-button @click="resetFilters">重置</el-button>
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
      refresh-text="刷新"
      @refresh="handleRefresh"
      @page-change="loadLogs"
      @size-change="handleSizeChange">
      
      <el-table-column :resizable="false" prop="created_at" label="时间" width="200" sortable>
        <template #default="scope">
          {{ formatTime(scope.row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="level" label="级别" width="100" align="center">
        <template #default="scope">
          <el-tag :type="getLevelTag(scope.row.level)" size="default">
            {{ scope.row.level }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="module" label="模块" width="180" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="message" label="消息内容" min-width="300" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="uid" label="用户UID" width="160" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="session_id" label="会话ID" width="160" show-overflow-tooltip />
    </BaseDataTable>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { systemApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'

const logs = ref([])
const loading = ref(false)
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

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
    ElMessage.error(err.message || '获取日志失败')
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
