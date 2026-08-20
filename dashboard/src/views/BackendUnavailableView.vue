<script setup>
import { Refresh, WarningFilled } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'

import UnderConstruction from '../components/UnderConstruction.vue'

const { t } = useI18n()

function retryConnection() {
  window.location.reload()
}
</script>

<template>
  <UnderConstruction
    class="backend-unavailable-view"
    :title="t('setup.backend_unavailable_title')"
    :status-text="t('setup.backend_unavailable_status')"
    :description="t('setup.backend_unavailable_description')"
  >
    <template #icon>
      <WarningFilled />
    </template>

    <el-button type="primary" @click="retryConnection">
      <el-icon><Refresh /></el-icon>
      {{ t('setup.backend_unavailable_retry') }}
    </el-button>
  </UnderConstruction>
</template>

<style scoped lang="scss">
@import '@/assets/css/BackendUnavailableView.scss';
</style>
