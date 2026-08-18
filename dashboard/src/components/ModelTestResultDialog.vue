<template>
  <el-dialog
    :model-value="props.visible"
    :title="t('channels.model_test_result')"
    width="min(720px, 92vw)"
    append-to-body
    :close-on-click-modal="false"
    class="model-test-result-dialog"
    align-center
    @update:model-value="handleVisibleChange"
  >
    <div v-if="normalizedResults.length === 0" class="model-test-result-empty">
      {{ t('channels.model_test_no_result') }}
    </div>

    <template v-else>
      <el-tabs
        v-if="normalizedResults.length > 1"
        :model-value="activeResultId"
        class="model-test-result-tabs"
        @update:model-value="handleActiveIdChange"
      >
        <el-tab-pane
          v-for="result in normalizedResults"
          :key="result.id"
          :name="result.id"
          :label="result.label"
        />
      </el-tabs>

      <div v-if="activeResult" class="model-test-result-panel">
        <div class="model-test-result-heading">
          <span class="model-test-result-model-label">{{ activeResult.label }}</span>
          <el-tag :type="getStatusType(activeState.status)" size="small">
            {{ getStatusLabel(activeState.status) }}
          </el-tag>
        </div>

        <div v-if="activeState.status === 'running'" class="model-test-result-state model-test-result-state--running">
          <div class="model-test-result-state-label">
            {{ t('channels.model_test_running') }}
          </div>
          <div v-if="activeState.kind === 'CHAT'" class="model-test-result-mode">
            <span class="model-test-result-label">{{ t('channels.chat_test_mode') }}</span>
            <span class="model-test-result-value">{{ getTestModeLabel(activeState.testMode) }}</span>
          </div>
        </div>

        <div v-else-if="activeState.status === 'error'" class="model-test-result-state model-test-result-state--error">
          <div class="model-test-result-state-label">
            {{ t('channels.model_test_failed') }}
          </div>
          <div class="model-test-result-error">{{ formatDisplayValue(activeState.error) }}</div>
        </div>

        <div v-else-if="activeState.status === 'success'" class="model-test-result-stats">
          <div class="model-test-result-item">
            <span class="model-test-result-label">{{ t('channels.chat_test_model') }}</span>
            <span class="model-test-result-value">{{ formatDisplayValue(activeData.model) }}</span>
          </div>

          <template v-if="activeState.kind === 'CHAT'">
            <div class="model-test-result-item model-test-result-item-wide">
              <span class="model-test-result-label">{{ t('channels.chat_test_reply') }}</span>
              <span class="model-test-result-value model-test-result-reply">{{ formatDisplayValue(activeData.reply) }}</span>
            </div>
            <div class="model-test-result-item">
              <span class="model-test-result-label">{{ t('channels.chat_test_usage') }}</span>
              <span class="model-test-result-value">{{ formatUsage(activeData.usage) }}</span>
            </div>
            <div class="model-test-result-item">
              <span class="model-test-result-label">{{ t('channels.chat_test_mode') }}</span>
              <span class="model-test-result-value">{{ getTestModeLabel(activeState.testMode) }}</span>
            </div>
            <div class="model-test-result-item">
              <span class="model-test-result-label">
                {{ activeState.testMode === 'stream' ? t('channels.chat_test_first_char_latency') : t('channels.chat_test_latency') }}
              </span>
              <span class="model-test-result-value">
                {{ formatLatency(activeState.testMode === 'stream' ? activeData.first_char_latency_ms : activeData.latency_ms) }}
              </span>
            </div>
            <div v-if="activeState.testMode === 'stream'" class="model-test-result-item">
              <span class="model-test-result-label">{{ t('channels.chat_test_total_latency') }}</span>
              <span class="model-test-result-value">{{ formatLatency(activeData.total_latency_ms) }}</span>
            </div>
          </template>

          <template v-else-if="activeState.kind === 'IMAGE_GENERATION'">
            <div class="model-test-result-item">
              <span class="model-test-result-label">{{ t('channels.chat_test_latency') }}</span>
              <span class="model-test-result-value">{{ formatLatency(activeData.latency_ms) }}</span>
            </div>
            <div v-if="getTestImageUrl(activeData)" class="model-test-result-item model-test-result-item-wide">
              <img
                class="model-test-result-image"
                :src="getTestImageUrl(activeData)"
                :alt="t('channels.image_generation_test_result_title')"
              />
            </div>
          </template>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  results: {
    type: Array,
    default: () => []
  },
  activeId: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['update:visible', 'update:active-id'])
const { t } = useI18n()

const activeResultId = ref('')

const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value)

const createEmptyState = () => ({
  status: '',
  kind: '',
  testMode: '',
  data: {},
  error: null
})

const normalizeState = state => {
  if (!isRecord(state)) return createEmptyState()

  return {
    status: typeof state.status === 'string' ? state.status : '',
    kind: typeof state.kind === 'string' ? state.kind : '',
    testMode: typeof state.testMode === 'string' ? state.testMode : '',
    data: isRecord(state.data) ? state.data : {},
    error: state.error
  }
}

const normalizeResult = result => {
  if (!isRecord(result) || typeof result.id !== 'string' || !result.id) return null

  return {
    id: result.id,
    label: typeof result.label === 'string' && result.label.trim() ? result.label : result.id,
    state: normalizeState(result.state)
  }
}

const normalizedResults = computed(() => {
  if (!Array.isArray(props.results)) return []
  return props.results.map(normalizeResult).filter(Boolean)
})

const activeResult = computed(() => (
  normalizedResults.value.find(result => result.id === activeResultId.value)
    || normalizedResults.value[0]
    || null
))

const activeState = computed(() => activeResult.value?.state || createEmptyState())
const activeData = computed(() => (isRecord(activeState.value.data) ? activeState.value.data : {}))

const syncActiveResult = () => {
  const results = normalizedResults.value
  const requestedId = typeof props.activeId === 'string' ? props.activeId : ''
  const nextId = results.some(result => result.id === requestedId)
    ? requestedId
    : (results[0]?.id || '')

  activeResultId.value = nextId
  if (requestedId !== nextId) emit('update:active-id', nextId)
}

watch(
  [normalizedResults, () => props.activeId],
  syncActiveResult,
  { immediate: true }
)

const handleVisibleChange = value => {
  emit('update:visible', Boolean(value))
}

const handleActiveIdChange = value => {
  const nextId = typeof value === 'string' ? value : ''
  if (!normalizedResults.value.some(result => result.id === nextId)) {
    syncActiveResult()
    return
  }

  activeResultId.value = nextId
  emit('update:active-id', nextId)
}

const getStatusType = status => {
  const types = {
    running: 'info',
    success: 'success',
    error: 'danger'
  }
  return types[status] || 'info'
}

const getStatusLabel = status => {
  const labels = {
    running: t('channels.model_test_running'),
    success: t('channels.model_test_success'),
    error: t('channels.model_test_failed')
  }
  return labels[status] || t('channels.model_test_running')
}

const getTestModeLabel = testMode => (
  testMode === 'stream' ? t('channels.chat_test_stream') : t('channels.chat_test_non_stream')
)

const formatDisplayValue = value => {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'string') return value

  try {
    if (value instanceof Error && value.message) return value.message
    if (typeof value === 'object') {
      const serialized = JSON.stringify(value)
      return serialized === undefined ? '-' : serialized
    }
    return String(value)
  } catch {
    return '-'
  }
}

const formatUsage = usage => {
  if (usage === null || usage === undefined || usage === '') return '-'
  if (typeof usage === 'string') return usage

  try {
    if (typeof usage !== 'object') return String(usage)

    const keys = ['prompt_tokens', 'completion_tokens', 'total_tokens']
    const parts = keys
      .filter(key => usage[key] !== undefined && usage[key] !== null)
      .map(key => `${key}: ${formatDisplayValue(usage[key])}`)

    if (parts.length > 0) return parts.join(', ')

    const serialized = JSON.stringify(usage)
    return serialized === undefined ? '-' : serialized
  } catch {
    return '-'
  }
}

const formatLatency = value => {
  if (value === null || value === undefined || value === '') return '-'

  try {
    const latency = Number(value)
    return Number.isFinite(latency) ? `${latency.toFixed(2)} ms` : '-'
  } catch {
    return '-'
  }
}

const getTestImageUrl = data => {
  try {
    if (!isRecord(data)) return ''

    let image = data.image
    if (Array.isArray(image)) image = image[0]
    if (!isRecord(image)) {
      image = Array.isArray(data.images) ? data.images[0] : data
    }
    if (!isRecord(image)) return ''

    if (typeof image.url === 'string' && image.url.trim()) return image.url
    if (typeof image.b64_json === 'string' && image.b64_json.trim()) {
      const encoded = image.b64_json.trim()
      return encoded.startsWith('data:') ? encoded : `data:image/png;base64,${encoded}`
    }
  } catch {
    return ''
  }

  return ''
}
</script>

<style scoped>
.model-test-result-dialog :deep(.el-dialog__body) {
  min-width: 0;
}

.model-test-result-tabs {
  margin-bottom: 16px;
}

.model-test-result-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  margin-bottom: 16px;
}

.model-test-result-model-label {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--el-text-color-primary);
  font-weight: 600;
}

.model-test-result-state,
.model-test-result-empty {
  overflow-wrap: anywhere;
}

.model-test-result-state-label {
  color: var(--el-text-color-primary);
  font-weight: 600;
}

.model-test-result-mode {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 12px;
}

.model-test-result-state--error .model-test-result-state-label,
.model-test-result-error {
  color: var(--el-color-danger);
}

.model-test-result-error {
  margin-top: 8px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.model-test-result-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 16px;
}

.model-test-result-item {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 3px;
}

.model-test-result-item-wide {
  grid-column: 1 / -1;
}

.model-test-result-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.model-test-result-value {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.model-test-result-reply {
  max-height: 220px;
  overflow: auto;
}

.model-test-result-image {
  display: block;
  max-width: 100%;
  max-height: 420px;
  object-fit: contain;
  border: 1px solid var(--el-border-color-light);
}

.model-test-result-empty {
  color: var(--el-text-color-secondary);
}

@media (max-width: 640px) {
  .model-test-result-stats {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
