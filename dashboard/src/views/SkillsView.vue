<template>
  <div v-if="userPermissions.showAdminNavigation" class="view-container">
    <BaseDataTable
      :data="skills"
      :loading="loading || !userPermissions.isSuperuser"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="''"
      :refresh-text="t('common.refresh')"
      :total-text="t('common.total_items', { total })"
      :empty-text="t('common.no_data')"
      @refresh="handleRefresh"
      @page-change="handlePageChange"
      @size-change="handleSizeChange"
    >
      <el-table-column
        :label="t('common.skills.name')"
        min-width="200"
        :resizable="false"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          {{ row.name || row.id }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.skills.description')"
        min-width="400"
        :resizable="false"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          {{ row.description || t('common.skills.no_description') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.skills.actions')"
        width="120"
        fixed="right"
        align="center"
        :resizable="false"
      >
        <template #default="{ row }">
          <el-button
            type="danger"
            size="small"
            :loading="deletingId === row.id && deletingId !== null"
            :disabled="loading || deletingId !== null || !userPermissions.isSuperuser"
            @click="handleDelete(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </BaseDataTable>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import BaseDataTable from '../components/BaseDataTable.vue'
import { systemApi } from '../api'
import { useConfirmDialog } from '../composables/useConfirmDialog.js'
import { useUserPermissionsStore } from '../composables/useUserPermissions'

const { t } = useI18n()
const userPermissions = useUserPermissionsStore()
const { confirm } = useConfirmDialog()

const skills = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const deletingId = ref(null)
let requestSequence = 0
let isActive = false

async function loadSkills() {
  if (!isActive || !userPermissions.isSuperuser) return

  const sequence = ++requestSequence
  loading.value = true
  try {
    const res = await systemApi.skills({ page: currentPage.value, size: pageSize.value })
    if (!isActive || sequence !== requestSequence || !userPermissions.isSuperuser) return

    const result = res.data.data
    const lastPage = Math.max(1, Math.ceil(result.total / pageSize.value))
    if (currentPage.value > lastPage) {
      currentPage.value = lastPage
      await loadSkills()
      return
    }

    skills.value = result.items
    total.value = result.total
  } catch (error) {
    if (isActive && sequence === requestSequence) {
      ElMessage.error(error?.message || t('common.skills.load_failed'))
    }
  } finally {
    if (isActive && sequence === requestSequence) loading.value = false
  }
}

function handleRefresh() {
  currentPage.value = 1
  loadSkills()
}

function handlePageChange(page) {
  currentPage.value = page
  loadSkills()
}

function handleSizeChange(size) {
  pageSize.value = size
  currentPage.value = 1
  loadSkills()
}

async function handleDelete(row) {
  if (!isActive || !userPermissions.isSuperuser || deletingId.value !== null || loading.value) return false

  deletingId.value = row.id
  const token = localStorage.getItem('token')
  try {
    try {
      if (
        !(await confirm({
          message: t('common.skills.delete_confirm', { name: row.name || row.id, id: row.id }),
          title: t('common.warning'),
          confirmButtonText: t('common.confirm'),
          cancelButtonText: t('common.cancel')
        }))
      ) {
        return false
      }
    } catch {
      return false
    }

    if (!isActive || !userPermissions.isSuperuser || localStorage.getItem('token') !== token) return false

    try {
      await systemApi.deleteSkill(row.id)
      ElMessage.success(t('common.skills.delete_success'))
      await loadSkills()
      return true
    } catch (error) {
      ElMessage.error(error?.message || t('common.skills.delete_failed'))
      return false
    }
  } finally {
    deletingId.value = null
  }
}

watch(
  () => userPermissions.isSuperuser,
  (isSuperuser) => {
    if (isSuperuser) {
      loadSkills()
    }
  }
)

onMounted(() => {
  isActive = true
  loadSkills()
})

onUnmounted(() => {
  isActive = false
  requestSequence += 1
})
</script>
