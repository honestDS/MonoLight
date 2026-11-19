<template>
  <div v-if="userPermissions.showAdminNavigation" class="view-container skills-view">
    <section
      class="skill-marketplace"
      aria-labelledby="skill-marketplace-title"
      :aria-busy="restoring"
    >
      <div class="skill-marketplace__icon" aria-hidden="true">
        <el-icon :size="24"><Shop /></el-icon>
      </div>
      <div class="skill-marketplace__body">
        <div class="skill-marketplace__heading">
          <h2 id="skill-marketplace-title">{{ t('common.skills.marketplace_title') }}</h2>
          <el-tag v-if="marketplaceInstalled !== null || loading">
            <span role="status" aria-live="polite" aria-atomic="true">
              <template v-if="marketplaceInstalled === true">
                {{ t('common.skills.marketplace_installed') }}
              </template>
              <template v-else-if="marketplaceInstalled === false">
                {{ t('common.skills.marketplace_available') }}
              </template>
              <template v-else>
                {{ t('common.skills.marketplace_loading') }}
              </template>
            </span>
          </el-tag>
        </div>
        <p id="skill-marketplace-description" class="skill-marketplace__description">
          {{ t('common.skills.marketplace_description') }}
        </p>
        <p id="skill-marketplace-hint" class="skill-marketplace__hint">
          {{ t('common.skills.marketplace_hint') }}
        </p>
      </div>
      <el-button
        class="skill-marketplace__restore"
        type="primary"
        :loading="restoring"
        :disabled="loading || deletingId !== null || !userPermissions.isSuperuser || marketplaceInstalled !== false"
        aria-describedby="skill-marketplace-description skill-marketplace-hint"
        @click="handleRestoreMarketplace"
      >
        <el-icon v-if="!restoring" aria-hidden="true"><RefreshLeft /></el-icon>
        {{ t(restoring ? 'common.skills.marketplace_restoring' : 'common.skills.marketplace_restore') }}
      </el-button>
    </section>
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
            :disabled="loading || restoring || deletingId !== null || !userPermissions.isSuperuser"
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
import { Shop, RefreshLeft } from '@element-plus/icons-vue'
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
const marketplaceInstalled = ref(null)
const restoring = ref(false)
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
    marketplaceInstalled.value = result.meta.marketplace_installed
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

async function handleRestoreMarketplace() {
  if (
    !isActive ||
    !userPermissions.isSuperuser ||
    restoring.value ||
    deletingId.value !== null ||
    loading.value ||
    marketplaceInstalled.value !== false
  ) {
    return false
  }

  restoring.value = true
  const token = localStorage.getItem('token')
  try {
    await systemApi.restoreSkillMarketplace()
    if (!isActive || !userPermissions.isSuperuser || localStorage.getItem('token') !== token) return false

    marketplaceInstalled.value = true
    ElMessage.success(t('common.skills.marketplace_restore_success'))
    currentPage.value = 1
    await loadSkills()
    return true
  } catch (error) {
    if (isActive && userPermissions.isSuperuser && localStorage.getItem('token') === token) {
      ElMessage.error(error?.message || t('common.skills.marketplace_restore_failed'))
    }
    return false
  } finally {
    restoring.value = false
  }
}

async function handleDelete(row) {
  if (
    !isActive ||
    !userPermissions.isSuperuser ||
    restoring.value ||
    deletingId.value !== null ||
    loading.value
  ) return false

  deletingId.value = row.id
  const token = localStorage.getItem('token')
  let confirmationKey = 'common.skills.delete_confirm'
  if (row.id === 'skillhub-manual-installer') confirmationKey = 'common.skills.delete_marketplace_confirm'
  try {
    try {
      if (
        !(await confirm({
          message: t(confirmationKey, { name: row.name || row.id, id: row.id }),
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

<style lang="scss" scoped>
@import '../assets/css/SkillsView.scss';
</style>
