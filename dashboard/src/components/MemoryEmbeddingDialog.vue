<template>
  <el-dialog
    :model-value="props.visible"
    :title="dialogTitle"
    width="min(620px, 92vw)"
    class="standard-dialog memory-embedding-dialog"
    center
    align-center
    append-to-body
    @update:model-value="handleVisibleChange"
    @closed="handleClosed"
  >
    <div class="memory-embedding-dialog__content">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        :title="t('profiles.memory_embedding_dialog_hint')"
      />

      <div class="memory-embedding-dialog__target">
        <div class="memory-embedding-dialog__field-label">
          <span>{{ t('profiles.memory_embedding_target') }}</span>
          <HelpTooltip :content="memoryEmbeddingTargetHint" />
        </div>
        <div class="memory-embedding-dialog__control-row">
          <el-select
            :model-value="props.targetKey"
            class="memory-embedding-dialog__select"
            filterable
            clearable
            :placeholder="t('profiles.memory_embedding_target_placeholder')"
            :disabled="props.previewing || props.confirming"
            @update:model-value="handleTargetKeyChange"
          >
            <el-option
              v-for="item in props.options"
              :key="item.key"
              :label="item.label"
              :value="item.key"
            />
          </el-select>

          <el-button
            type="primary"
            plain
            :loading="props.previewing"
            :disabled="!props.targetKey || props.confirming"
            @click="handleDetect"
          >
            {{ t('profiles.memory_embedding_preview') }}
          </el-button>
        </div>
      </div>

      <div v-if="props.preview" class="memory-embedding-dialog__preview">
        <el-alert
          :type="confirmationAlertType"
          :closable="false"
          show-icon
          :title="t(confirmationAlertKey)"
        />

        <el-descriptions :column="1" border class="memory-embedding-dialog__descriptions">
          <el-descriptions-item :label="t('profiles.memory_embedding_current')">
            {{ currentConfigLabel }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('profiles.memory_embedding_target')">
            {{ targetConfigLabel }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('profiles.memory_embedding_dimensions')">
            {{ dimensionsText }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="isInitialSelection || props.requiresMigration"
            :label="t('profiles.memory_embedding_estimated_records')"
          >
            {{ estimatedRecordCount }}
          </el-descriptions-item>
        </el-descriptions>

        <el-checkbox
          v-if="!sameConfiguration"
          :model-value="props.confirmationChecked"
          class="memory-embedding-dialog__confirmation-check"
          @update:model-value="handleConfirmationChange"
        >
          {{ t('profiles.memory_embedding_confirmation_check') }}
        </el-checkbox>
      </div>
    </div>

    <template #footer>
      <div class="memory-embedding-dialog__footer">
        <el-button @click="handleCancel">{{ t('profiles.cancel') }}</el-button>
        <el-button
          v-if="confirmationRequired"
          :type="confirmationButtonType"
          :loading="props.confirming"
          :disabled="!props.confirmationChecked || props.previewing"
          @click="handleConfirm"
        >
          {{ confirmationButtonText }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import HelpTooltip from './HelpTooltip.vue'

const props = defineProps({
  visible: {
    type: Boolean,
    required: true
  },
  configured: {
    type: Boolean,
    default: false
  },
  currentLabel: {
    type: String,
    default: ''
  },
  options: {
    type: Array,
    required: true
  },
  targetKey: {
    type: String,
    default: ''
  },
  preview: {
    type: Object,
    default: null
  },
  previewing: {
    type: Boolean,
    required: true
  },
  confirming: {
    type: Boolean,
    required: true
  },
  confirmationChecked: {
    type: Boolean,
    required: true
  },
  requiresMigration: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits([
  'update:visible',
  'update:targetKey',
  'update:confirmationChecked',
  'detect',
  'confirm',
  'closed'
])

const { t } = useI18n()

const dialogTitle = computed(() => t(
  props.configured
    ? 'profiles.memory_embedding_dialog_change_title'
    : 'profiles.memory_embedding_dialog_configure_title'
))

const memoryEmbeddingTargetHint = computed(() => (
  `${t('profiles.memory_embedding_target_hint')} ${t('profiles.memory_embedding_preview_hint')}`
))

const isInitialSelection = computed(() => {
  if (!props.preview) return false
  return typeof props.preview.is_initial_selection === 'boolean'
    ? props.preview.is_initial_selection
    : !props.configured
})

const sameConfiguration = computed(() => Boolean(props.preview)
  && !isInitialSelection.value
  && !props.requiresMigration)

const confirmationRequired = computed(() => Boolean(props.preview) && !sameConfiguration.value)

const confirmationAlertKey = computed(() => {
  if (isInitialSelection.value) return 'profiles.memory_embedding_confirmation_first_notice'
  if (props.requiresMigration) return 'profiles.memory_embedding_confirmation_change_notice'
  return 'profiles.memory_embedding_confirmation_same_notice'
})

const confirmationAlertType = computed(() => (
  sameConfiguration.value ? 'info' : 'warning'
))

const confirmationButtonText = computed(() => (
  isInitialSelection.value
    ? t('profiles.memory_embedding_confirm_enable')
    : t('profiles.memory_embedding_start_migration')
))

const confirmationButtonType = computed(() => (
  props.requiresMigration ? 'warning' : 'primary'
))

const selectedOption = computed(() => (
  props.options.find(item => item?.key === props.targetKey) || null
))

const currentConfigLabel = computed(() => (
  props.currentLabel || t('profiles.memory_embedding_not_configured')
))

const targetConfigLabel = computed(() => {
  const previewChannel = props.preview?.channel_name
  const previewModel = props.preview?.model_id
  if (previewChannel && previewModel) return `${previewChannel} / ${previewModel}`
  return selectedOption.value?.label || t('profiles.memory_embedding_not_configured')
})

const formatDimension = value => (
  value === null || value === undefined || value === ''
    ? t('profiles.memory_embedding_not_configured')
    : value
)

const dimensionsText = computed(() => {
  const current = formatDimension(props.preview?.current_active?.dimensions)
  const target = formatDimension(props.preview?.actual_dimensions ?? props.preview?.dimensions)
  return `${current} -> ${target}`
})

const estimatedRecordCount = computed(() => (
  props.preview?.estimated_record_count ?? '-'
))

const handleVisibleChange = value => {
  emit('update:visible', Boolean(value))
}

const handleClosed = () => {
  emit('closed')
}

const handleCancel = () => {
  emit('update:visible', false)
}

const handleTargetKeyChange = value => {
  emit('update:targetKey', typeof value === 'string' ? value : '')
}

const handleConfirmationChange = value => {
  emit('update:confirmationChecked', Boolean(value))
}

const handleDetect = () => {
  if (!props.targetKey || props.confirming) return
  emit('detect')
}

const handleConfirm = () => {
  if (!confirmationRequired.value || !props.confirmationChecked || props.confirming || props.previewing) return
  emit('confirm')
}
</script>

<style scoped lang="scss">
.memory-embedding-dialog__content {
  min-width: 0;
}

.memory-embedding-dialog__target {
  margin-top: 18px;
}

.memory-embedding-dialog__field-label {
  display: flex;
  align-items: center;
  min-height: 24px;
  margin-bottom: 8px;
  line-height: 1.4;
}

.memory-embedding-dialog__control-row {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  min-width: 0;
  gap: 8px;
}

.memory-embedding-dialog__select {
  flex: 1 1 auto;
  min-width: 0;
  width: auto;
}

.memory-embedding-dialog__control-row :deep(.el-button) {
  flex: 0 0 auto;
  margin-left: 0;
}

.memory-embedding-dialog__preview {
  min-width: 0;
  margin-top: 20px;
}

.memory-embedding-dialog__descriptions {
  margin-top: 16px;
}

.memory-embedding-dialog__confirmation-check {
  display: flex;
  align-items: flex-start;
  height: auto;
  margin-top: 16px;
  line-height: 1.5;
  white-space: normal;
}

.memory-embedding-dialog__confirmation-check :deep(.el-checkbox__label) {
  white-space: normal;
  overflow-wrap: anywhere;
}

.memory-embedding-dialog__footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.memory-embedding-dialog__footer :deep(.el-button) {
  margin-left: 0;
}

@media (max-width: 480px) {
  .memory-embedding-dialog__footer {
    justify-content: stretch;
  }

  .memory-embedding-dialog__footer :deep(.el-button) {
    flex: 1 1 auto;
  }
}
</style>
