<template>
  <div class="view-container">
    <BaseDataTable
      :data="channels"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('channels.create_channel')"
      :refresh-text="$t('channels.refresh')"
      :total-text="$t('common.total_items', { total })"
      :empty-text="$t('common.no_data')"
      @create="openCreateDialog"
      @refresh="handleRefresh"
      @page-change="fetchChannels"
      @size-change="handleSizeChange">
      <el-table-column :resizable="false" prop="name" :label="$t('channels.name')" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('channels.models')" min-width="300" sortable>
        <template #default="scope">
          <div class="models-list" v-if="scope.row.model_ids && scope.row.model_ids.length > 0">
            <el-tag v-for="(m, idx) in scope.row.model_ids" :key="idx" class="model-tag">
              {{ m.model_id }} ({{ getModelUsageLabel(m.usage) }}<template v-if="m.protocol"> - {{ getModelProtocolLabel(m.protocol) }}</template>)
            </el-tag>
          </div>
          <span v-else class="text-muted">{{ $t('channels.no_models') }}</span>
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="base_url" :label="$t('channels.base_url')" min-width="200" sortable></el-table-column>
      <el-table-column :resizable="false" :label="$t('channels.status')" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" :active-text="$t('channels.enable')" :inactive-text="$t('channels.disable')" />
        </template>
      </el-table-column>
      <el-table-column :resizable="false" :label="$t('channels.actions')" width="360" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button :type="scope.row.is_active ? 'warning' : 'success'" size="small" @click="handleToggleActive(scope.row)">{{ scope.row.is_active ? $t('channels.disable') : $t('channels.enable') }}</el-button>
            <el-button type="primary" size="small" @click="handleEdit(scope.row)">{{ $t('channels.edit') }}</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">{{ $t('channels.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <ChannelFormDialog v-model:visible="dialogVisible" :channel="selectedChannel" @saved="fetchChannels" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { channelApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import ChannelFormDialog from '../components/ChannelFormDialog.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'

const { t } = useI18n()

const channels = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const dialogVisible = ref(false)
const selectedChannel = ref(null)

const getModelProtocolLabel = (value) => {
  const map = {
    OPENAI: 'openai-completions',
    OPENAI_RESPONSES: 'openai-responses',
    OPENAI_EMBEDDING: 'openai-embedding',
    OPENAI_IMAGE: 'openai-image',
    COHERE_RERANK: 'cohere-rerank'
  }
  return map[value] || value
}

const getModelUsageLabel = (value) => {
  const map = {
    CHAT: t('channels.chat_model'),
    EMBEDDING: t('channels.embedding_model'),
    RERANK: t('channels.rerank_model'),
    IMAGE_GENERATION: t('channels.image_generation_model')
  }
  return map[value] || value
}

const fetchChannels = async () => {
  loading.value = true
  try {
    const res = await channelApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    channels.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('channels.load_failed'))
  } finally {
    loading.value = false
  }
}

const { handleDelete } = useDeleteConfirm(channelApi.delete, fetchChannels)

const handleToggleActive = async (row) => {
  try {
    await channelApi.update(row.id, { is_active: !row.is_active })
    ElMessage.success(row.is_active ? t('channels.disabled') : t('channels.enabled'))
    fetchChannels()
  } catch (err) {
    ElMessage.error(err.message || t('channels.action_failed'))
  }
}

const openCreateDialog = () => {
  selectedChannel.value = null
  dialogVisible.value = true
}

const handleEdit = (row) => {
  selectedChannel.value = row
  dialogVisible.value = true
}

const handleRefresh = () => {
  currentPage.value = 1
  fetchChannels()
}

const handleSizeChange = () => {
  currentPage.value = 1
  fetchChannels()
}

onMounted(fetchChannels)
</script>

<style lang="scss">
@import "@/assets/css/common.scss";
</style>
