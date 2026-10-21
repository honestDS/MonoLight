<script setup>
import { computed, ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import BaseDataTable from '@/components/BaseDataTable.vue'
import KnowledgeEntryEditor from '@/components/KnowledgeEntryEditor.vue'
import KnowledgeOrganizationDialog from '@/components/KnowledgeOrganizationDialog.vue'
import { useKnowledgeBaseEntries } from '@/composables/knowledgeBase/useKnowledgeBaseEntries.js'
import { getKnowledgeBaseProfileLabel } from '@/utils/knowledgeBaseManagement.js'
import { formatTime } from '@/utils/index.js'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const knowledgeBaseId = computed(() => Number(route.params.id))
const editorRef = ref(null)
const orgRef = ref(null)

const {
  selectedKb,
  isManaged,
  loading,
  error,
  items,
  total,
  page,
  pageSize,
  query,
  entryActionId,
  scope,
  refresh,
  fetchEntries,
  schedulePolling,
  handleSearch,
  handlePageChange,
  handleSizeChange,
  getEntryName,
  getEntryStatus,
  getEntrySourceLabel,
  viewEntry,
  closeDetail,
  detailVisible,
  detailLoading,
  detailTitle,
  detailContent,
  detailError,
  showHistory,
  closeHistory,
  historyVisible,
  historyLoading,
  historyTitle,
  historyItems,
  historyError,
  deleteEntry,
  retryEntry
} = useKnowledgeBaseEntries(knowledgeBaseId)

const canLeaveView = async () => {
  if (!editorRef.value) return true
  return await editorRef.value.canLeave()
}

onBeforeRouteLeave(async () => canLeaveView())
onBeforeRouteUpdate(async (to, from) => {
  if (Number(to.params.id) === Number(from.params.id)) return true
  return canLeaveView()
})

const goBack = () => {
  const queryParams = {}
  if (route.query.page != null) queryParams.page = route.query.page
  if (route.query.size != null) queryParams.size = route.query.size
  return router.push({ path: '/knowledge-base', query: queryParams })
}

const handleSaved = async ({ created } = {}) => {
  if (created) page.value = 1
  await refresh()
}

const handleEntryAction = (command, row) => {
  if (command === 'history') return showHistory(row)
  if (command === 'retry') return retryEntry(row)
  if (command === 'delete') return deleteEntry(row)
}

const sourceReferenceFilename = row => (
  row?.source_reference?.filename || row?.filename || ''
)

const hasOtherSourceReference = row => Boolean(
  row?.source_reference && !row.source_reference.filename
)

const sourceReferenceJson = row => {
  try {
    return JSON.stringify(row?.source_reference)
  } catch (_error) {
    return ''
  }
}

const formatEntryTime = row => formatTime(row?.updated_at || row?.created_at) || '-'

const updateDetailVisibility = visible => {
  if (!visible) closeDetail()
}

const updateHistoryVisibility = visible => {
  if (!visible) closeHistory()
}
</script>

<template>
  <div class="view-container knowledge-entries-view">
    <header class="knowledge-entries-header">
      <el-button @click="goBack">{{ t('knowledgeBase.entries_back') }}</el-button>
      <h2>{{ selectedKb?.name || t('knowledgeBase.entries') }}</h2>
      <el-tag v-if="selectedKb" :type="isManaged ? 'warning' : 'info'">
        {{ t(isManaged ? 'knowledgeBase.type_managed' : 'knowledgeBase.type_user') }}
      </el-tag>
      <div class="knowledge-entries-meta">
        <span>{{ t('knowledgeBase.owner_username') }}: {{ selectedKb?.username || t('knowledgeBase.owner_unknown') }}</span>
        <span>{{ t('knowledgeBase.owner_profiles') }}: {{ getKnowledgeBaseProfileLabel(selectedKb) }}</span>
      </div>
    </header>

    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
      role="alert"
    >
      <template #default>
        <el-button link type="primary" :disabled="loading" @click="refresh">
          {{ t('knowledgeBase.refresh') }}
        </el-button>
      </template>
    </el-alert>

    <BaseDataTable
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :data="items"
      :loading="loading"
      :total="total"
      :total-text="t('common.total_items', { total })"
      :empty-text="t('knowledgeBase.entries_empty')"
      @page-change="handlePageChange"
      @size-change="handleSizeChange"
    >
      <template #actions>
        <div class="knowledge-entries-toolbar">
          <div v-if="isManaged" class="knowledge-entries-search">
            <el-input
              v-model="query"
              :placeholder="t('knowledgeBase.managed_search_placeholder')"
              :aria-label="t('knowledgeBase.managed_search_placeholder')"
              clearable
              :disabled="loading || !selectedKb"
              @keyup.enter="handleSearch"
            />
            <el-button :disabled="loading || !selectedKb" @click="handleSearch">
              {{ t('knowledgeBase.search') }}
            </el-button>
          </div>
          <div v-else class="knowledge-entries-search"></div>

          <div class="knowledge-entries-actions">
            <el-button :disabled="loading" @click="refresh">
              {{ t('knowledgeBase.refresh') }}
            </el-button>
            <el-button
              v-if="isManaged"
              :disabled="loading || entryActionId !== null || !selectedKb"
              @click="orgRef?.open()"
            >
              {{ t('knowledgeBase.organization') }}
            </el-button>
            <el-button
              type="primary"
              :disabled="loading || entryActionId !== null || !selectedKb"
              @click="editorRef?.openCreate()"
            >
              {{ t('knowledgeBase.entries_add') }}
            </el-button>
          </div>
        </div>
      </template>

      <el-table-column
        :label="t('knowledgeBase.entries_name')"
        min-width="260"
        show-overflow-tooltip
        :resizable="false"
      >
        <template #default="{ row }">
          <el-tooltip :content="getEntryName(row)" placement="top" :disabled="!getEntryName(row)">
            <div class="knowledge-entry-name">
              <strong>{{ getEntryName(row) || '-' }}</strong>
              <div v-if="isManaged">
                {{ t('knowledgeBase.managed_version') }} {{ row.version ?? '-' }}
                · {{ t('knowledgeBase.entries_maintainable') }}:
                {{ t(row.llm_maintainable ? 'knowledgeBase.yes' : 'knowledgeBase.no') }}
              </div>
            </div>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column
        :label="t('knowledgeBase.source')"
        min-width="160"
        show-overflow-tooltip
        :resizable="false"
      >
        <template #default="{ row }">
          <div class="knowledge-entry-source">
            <el-tooltip
              v-if="hasOtherSourceReference(row)"
              :content="sourceReferenceJson(row)"
              placement="top"
            >
              <span>{{ getEntrySourceLabel(row) }}</span>
            </el-tooltip>
            <span v-else>{{ getEntrySourceLabel(row) }}</span>
            <div v-if="sourceReferenceFilename(row)">{{ sourceReferenceFilename(row) }}</div>
          </div>
        </template>
      </el-table-column>

      <el-table-column
        :label="t('knowledgeBase.entries_status')"
        min-width="130"
        :resizable="false"
      >
        <template #default="{ row }">
          <div>{{ getEntryStatus(row) }}</div>
          <el-tooltip
            v-if="isManaged && row.publication_job_error"
            :content="row.publication_job_error"
            placement="top"
          >
            <div class="knowledge-entry-status-error">{{ row.publication_job_error }}</div>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column
        v-if="!isManaged"
        prop="chunk_count"
        :label="t('knowledgeBase.chunk_count')"
        width="90"
        align="center"
        :resizable="false"
      />

      <el-table-column
        :label="t('knowledgeBase.entries_update_time')"
        width="170"
        :resizable="false"
      >
        <template #default="{ row }">{{ formatEntryTime(row) }}</template>
      </el-table-column>

      <el-table-column
        :label="t('knowledgeBase.actions')"
        width="240"
        align="center"
        fixed="right"
        :resizable="false"
      >
        <template #default="{ row }">
          <el-button size="small" @click="viewEntry(row)">
            {{ t('knowledgeBase.entries_view') }}
          </el-button>
          <el-button
            v-if="isManaged"
            size="small"
            :disabled="entryActionId !== null"
            @click="editorRef?.openEdit(row)"
          >
            {{ t('knowledgeBase.entries_edit') }}
          </el-button>
          <el-dropdown
            trigger="click"
            :disabled="entryActionId !== null"
            @command="handleEntryAction($event, row)"
          >
            <el-button size="small" :disabled="entryActionId !== null">
              {{ t('knowledgeBase.more') }}
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  v-if="isManaged"
                  command="history"
                  :disabled="entryActionId !== null"
                >
                  {{ t('knowledgeBase.managed_history') }}
                </el-dropdown-item>
                <el-dropdown-item
                  v-if="isManaged && row.publication_job_status === 'failed' && row.publication_job_id != null"
                  command="retry"
                  :disabled="entryActionId !== null"
                >
                  {{ t('knowledgeBase.managed_retry') }}
                </el-dropdown-item>
                <el-dropdown-item
                  command="delete"
                  divided
                  class="danger-dropdown-item"
                  :disabled="entryActionId !== null"
                >
                  {{ t('common.delete') }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
    </BaseDataTable>

    <KnowledgeEntryEditor
      v-if="selectedKb"
      ref="editorRef"
      :knowledge-base="selectedKb"
      :scope="scope"
      @saved="handleSaved"
    />
    <KnowledgeOrganizationDialog
      v-if="selectedKb && isManaged"
      ref="orgRef"
      :knowledge-base="selectedKb"
      :scope="scope"
      :refresh-entries="fetchEntries"
      :schedule-polling="schedulePolling"
    />

    <el-dialog
      :model-value="detailVisible"
      :title="detailTitle"
      width="800px"
      class="standard-dialog dialog-with-scroll-body"
      align-center
      @update:model-value="updateDetailVisibility"
    >
      <el-skeleton v-if="detailLoading" :rows="5" animated role="status" />
      <el-alert
        v-else-if="detailError"
        :title="detailError"
        type="error"
        show-icon
        :closable="false"
        role="alert"
      />
      <pre v-else class="knowledge-entry-preview">{{ detailContent }}</pre>
    </el-dialog>

    <el-dialog
      :model-value="historyVisible"
      :title="historyTitle"
      width="980px"
      class="standard-dialog dialog-with-scroll-body"
      align-center
      @update:model-value="updateHistoryVisibility"
    >
      <el-skeleton v-if="historyLoading" :rows="5" animated role="status" />
      <el-alert
        v-else-if="historyError"
        :title="historyError"
        type="error"
        show-icon
        :closable="false"
        role="alert"
      />
      <el-empty
        v-else-if="!historyItems.length"
        :description="t('common.no_data')"
      />
      <el-table v-else :data="historyItems" row-key="id">
        <el-table-column type="expand" width="48">
          <template #default="{ row }">
            <pre class="knowledge-entry-preview">{{ row.after_snapshot?.content ?? '' }}</pre>
          </template>
        </el-table-column>
        <el-table-column
          prop="version"
          :label="t('knowledgeBase.managed_version')"
          width="80"
          align="center"
        />
        <el-table-column :label="t('knowledgeBase.managed_operation')" width="100">
          <template #default="{ row }">
            {{ t(`knowledgeBase.managed_operation_${row.operation || 'update'}`) }}
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledgeBase.managed_source')" min-width="150">
          <template #default="{ row }">
            <div class="knowledge-entry-source">
              <el-tooltip
                v-if="hasOtherSourceReference(row)"
                :content="sourceReferenceJson(row)"
                placement="top"
              >
                <span>{{ getEntrySourceLabel(row) }}</span>
              </el-tooltip>
              <span v-else>{{ getEntrySourceLabel(row) }}</span>
              <div v-if="sourceReferenceFilename(row)">{{ sourceReferenceFilename(row) }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledgeBase.managed_modified_by')" width="120">
          <template #default="{ row }">
            {{ t(`knowledgeBase.managed_actor_${row.modified_by || 'system'}`) }}
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledgeBase.entries_update_time')" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) || '-' }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<style lang="scss">
@import '@/assets/css/KnowledgeEntries.scss';
</style>
