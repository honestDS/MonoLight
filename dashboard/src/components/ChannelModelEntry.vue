<template>
  <div class="model-entry-card">
    <div class="model-entry-header">
      <span>{{ $t('channels.model_entry') }} #{{ props.index + 1 }}</span>
      <div class="model-entry-actions">
        <el-button v-if="props.entry.usage === 'CHAT'" type="text" :loading="props.testing" :disabled="props.testing" @click="emit('test-chat')">
          {{ $t('channels.test') }}
        </el-button>
        <el-button v-else-if="props.entry.usage === 'IMAGE_GENERATION'" type="text" :loading="props.testing" :disabled="props.testing" @click="emit('test-image-generation')">
          {{ $t('channels.test') }}
        </el-button>
        <el-button v-else type="text" disabled>
          {{ $t('channels.test') }}
        </el-button>
        <el-button type="text" :loading="props.detectingMetadata" :disabled="props.entry.usage !== 'CHAT' || props.metadataDetectionDisabled" @click="emit('detect-metadata')">
          {{ $t('channels.model_metadata_detect') }}
        </el-button>
        <el-button type="text" class="remove" @click="emit('remove')">
          {{ $t('channels.remove') }}
        </el-button>
      </div>
    </div>

    <div class="model-entry-fields">
      <div class="model-entry-field model-entry-field-half">
        <el-form-item :label="$t('channels.model_id_label')" :error="props.modelIdError">
          <el-input v-model="props.entry.model_id" :placeholder="$t('channels.model_id_placeholder')" @input="handleModelIdInput" />
        </el-form-item>
      </div>
      <div class="model-entry-field model-entry-field-half">
        <el-form-item :label="$t('channels.model_type_label')" >
          <el-select v-model="props.entry.usage" class="full-width-input" @change="handleUsageChange">
            <el-option v-for="item in props.modelUsages" :key="item" :label="getModelUsageLabel(item)" :value="item" />
          </el-select>
        </el-form-item>
      </div>
      <div class="model-entry-field model-entry-field-half">
        <el-form-item :label="$t('channels.model_protocol')" :error="props.protocolError">
          <el-select v-model="props.entry.protocol" class="full-width-input" @change="handleProtocolChange">
            <el-option v-for="item in props.modelProtocols" :key="item" :label="getModelProtocolLabel(item)" :value="item" />
          </el-select>
        </el-form-item>
      </div>

      <template v-if="props.entry.usage === 'CHAT'">
        <div class="model-entry-field">
          <el-form-item :label="$t('channels.temperature')" >
            <el-input-number v-model="props.entry.temperature" :min="0" :max="2" :step="0.1" controls-position="right" @change="emit('test-config-change')" />
          </el-form-item>
        </div>
        <div class="model-entry-field">
          <el-form-item :label="$t('channels.top_p')" >
            <el-input-number v-model="props.entry.top_p" :min="0" :max="1" :step="0.05" controls-position="right" @change="emit('test-config-change')" />
          </el-form-item>
        </div>
        <div class="model-entry-field">
          <el-form-item :label="$t('channels.max_tokens')">
            <el-input-number v-model="props.entry.max_tokens" :min="0" controls-position="right" @change="emit('test-config-change')" />
          </el-form-item>
        </div>
        <div class="model-entry-field">
          <el-form-item :label="$t('channels.context_window_k')">
            <el-input-number v-model="props.entry.context_window_k" :min="1" controls-position="right" />
          </el-form-item>
        </div>
        <div class="model-entry-understanding-row">
          <div class="model-entry-field model-entry-field-third">
            <el-form-item :label="$t('channels.image_understanding')">
              <el-switch v-model="props.entry.image_understanding" />
            </el-form-item>
          </div>
          <div class="model-entry-field model-entry-field-third">
            <el-form-item :label="$t('channels.audio_understanding')">
              <el-switch v-model="props.entry.audio_understanding" />
            </el-form-item>
          </div>
          <div class="model-entry-field model-entry-field-third">
            <el-form-item :label="$t('channels.video_understanding')">
              <el-switch v-model="props.entry.video_understanding" />
            </el-form-item>
          </div>
        </div>
      </template>

      <template v-if="props.entry.usage === 'EMBEDDING'">
        <div class="model-entry-field model-entry-field-half">
          <el-form-item :label="$t('channels.embedding_dimensions')">
            <div class="embedding-dimension-row">
              <el-input-number v-model="props.entry.embedding_dimensions" :min="1" controls-position="right" />
              <el-button type="primary" plain :loading="props.detectingDimension" @click="emit('detect-dimension')">
                {{ $t('channels.auto_detect') }}
              </el-button>
            </div>
          </el-form-item>
        </div>
      </template>

      <template v-if="props.entry.usage === 'IMAGE_GENERATION'">
        <div class="model-entry-field model-entry-field-half">
          <el-form-item :label="$t('channels.image_generation_size')">
            <el-select v-model="props.entry.size" class="full-width-input" @change="emit('test-config-change')">
              <el-option label="1024x1024" value="1024x1024" />
              <el-option label="1024x1536" value="1024x1536" />
              <el-option label="1536x1024" value="1536x1024" />
            </el-select>
          </el-form-item>
        </div>
        <div class="model-entry-field model-entry-field-half">
          <el-form-item :label="$t('channels.image_generation_quality')">
            <el-select v-model="props.entry.quality" class="full-width-input" @change="emit('test-config-change')">
              <el-option :label="$t('channels.image_generation_quality_auto')" value="auto" />
              <el-option :label="$t('channels.image_generation_quality_low')" value="low" />
              <el-option :label="$t('channels.image_generation_quality_medium')" value="medium" />
              <el-option :label="$t('channels.image_generation_quality_high')" value="high" />
            </el-select>
          </el-form-item>
        </div>
      </template>

      <div class="model-entry-field model-entry-field-half">
        <el-form-item :label="$t('channels.description')">
          <el-input v-model="props.entry.description" :placeholder="$t('channels.description_placeholder')" />
        </el-form-item>
      </div>
    </div>

    <el-collapse
      :model-value="props.advancedSettingsExpanded"
      class="model-entry-collapse model-entry-collapse--advanced model-entry-advanced-settings"
      @update:model-value="value => emit('update:advanced-settings-expanded', value)">
      <el-collapse-item :name="props.index">
        <template #title>
          <div class="model-collapse-title">
            <span>{{ $t('channels.advanced_settings') }}</span>
          </div>
        </template>
        <div class="custom-request-headers-heading">
          <span class="custom-request-headers-title">{{ $t('channels.custom_request_headers') }}</span>
          <el-button size="small" type="primary" plain @click="emit('fill-headers-template')">
            {{ $t('channels.fill_headers_template') }}
          </el-button>
        </div>
        <el-form-item :error="props.advancedSettingsError" class="advanced-settings-form-item">
          <el-input
            :model-value="props.advancedSettingsDraft"
            type="textarea"
            :rows="4"
            :placeholder="props.customHeadersPlaceholder"
            @input="handleAdvancedSettingsInput"
            @update:model-value="value => emit('update:advanced-settings-draft', value)" />
        </el-form-item>
      </el-collapse-item>
    </el-collapse>

    <el-collapse
      :model-value="props.testResultExpanded"
      class="model-entry-collapse model-entry-collapse--test"
      @update:model-value="value => emit('update:test-result-expanded', value)">
      <el-collapse-item name="result">
        <template #title>
          <div class="model-collapse-title">
            <span>{{ $t('channels.model_test_result') }}</span>
            <el-tag
              v-if="props.testState"
              :type="getTestStatusType(props.testState.status)"
              size="small"
              @click.stop>
              {{ getTestStatusLabel(props.testState.status) }}
            </el-tag>
          </div>
        </template>

        <div v-if="!props.testState" class="model-test-result-empty">
          {{ $t('channels.model_test_no_result') }}
        </div>

        <div v-else class="model-test-result">
          <div v-if="props.testState.status === 'running' && props.testState.kind === 'CHAT'" class="model-test-result-status">
            {{ $t('channels.chat_test_mode') }}: {{ getTestModeLabel(props.testState.testMode) }}
          </div>

          <div v-else-if="props.testState.status === 'error'" class="model-test-result-error">
            {{ props.testState.error }}
          </div>

          <div v-else-if="props.testState.status === 'success'" class="model-test-result-stats">
            <div class="model-test-result-item">
              <span class="model-test-result-label">{{ $t('channels.chat_test_model') }}</span>
              <span class="model-test-result-value">{{ props.testState.data?.model || '-' }}</span>
            </div>
            <template v-if="props.testState.kind === 'CHAT'">
              <div class="model-test-result-item model-test-result-item-wide">
                <span class="model-test-result-label">{{ $t('channels.chat_test_reply') }}</span>
                <span class="model-test-result-value model-test-result-reply">{{ props.testState.data?.reply || '-' }}</span>
              </div>
              <div class="model-test-result-item">
                <span class="model-test-result-label">{{ $t('channels.chat_test_usage') }}</span>
                <span class="model-test-result-value">{{ formatUsage(props.testState.data?.usage) }}</span>
              </div>
              <div class="model-test-result-item">
                <span class="model-test-result-label">{{ $t('channels.chat_test_mode') }}</span>
                <span class="model-test-result-value">{{ getTestModeLabel(props.testState.testMode) }}</span>
              </div>
              <div class="model-test-result-item">
                <span class="model-test-result-label">
                  {{ props.testState.testMode === 'stream' ? $t('channels.chat_test_first_char_latency') : $t('channels.chat_test_latency') }}
                </span>
                <span class="model-test-result-value">
                  {{ formatLatency(props.testState.testMode === 'stream' ? props.testState.data?.first_char_latency_ms : props.testState.data?.latency_ms) }}
                </span>
              </div>
              <div v-if="props.testState.testMode === 'stream'" class="model-test-result-item">
                <span class="model-test-result-label">{{ $t('channels.chat_test_total_latency') }}</span>
                <span class="model-test-result-value">{{ formatLatency(props.testState.data?.total_latency_ms) }}</span>
              </div>
            </template>
            <template v-else-if="props.testState.kind === 'IMAGE_GENERATION'">
              <div class="model-test-result-item">
                <span class="model-test-result-label">{{ $t('channels.chat_test_latency') }}</span>
                <span class="model-test-result-value">{{ formatLatency(props.testState.data?.latency_ms) }}</span>
              </div>
              <div v-if="getTestImageUrl(props.testState.data)" class="model-test-result-item model-test-result-item-wide">
                <img class="model-test-result-image" :src="getTestImageUrl(props.testState.data)" alt="image generation test result" />
              </div>
            </template>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<script setup>
import { useI18n } from 'vue-i18n'

const props = defineProps({
  entry: {
    type: Object,
    required: true
  },
  index: {
    type: Number,
    required: true
  },
  modelUsages: {
    type: Array,
    default: () => []
  },
  modelProtocols: {
    type: Array,
    default: () => []
  },
  modelIdError: {
    type: String,
    default: ''
  },
  protocolError: {
    type: String,
    default: ''
  },
  advancedSettingsDraft: {
    type: String,
    default: ''
  },
  advancedSettingsError: {
    type: String,
    default: ''
  },
  advancedSettingsExpanded: {
    type: Array,
    default: () => []
  },
  customHeadersPlaceholder: {
    type: String,
    default: ''
  },
  testing: Boolean,
  detectingMetadata: Boolean,
  detectingDimension: Boolean,
  metadataDetectionDisabled: Boolean,
  testState: {
    type: Object,
    default: null
  },
  testResultExpanded: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits([
  'test-chat',
  'test-image-generation',
  'detect-metadata',
  'remove',
  'model-id-input',
  'usage-change',
  'protocol-change',
  'detect-dimension',
  'fill-headers-template',
  'advanced-settings-input',
  'test-config-change',
  'update:test-result-expanded',
  'update:advanced-settings-draft',
  'update:advanced-settings-expanded'
])

const { t } = useI18n()

const handleModelIdInput = () => {
  emit('model-id-input')
  emit('test-config-change')
}

const handleUsageChange = (value) => {
  emit('usage-change', value)
  emit('test-config-change')
}

const handleProtocolChange = (value) => {
  emit('protocol-change', value)
  emit('test-config-change')
}

const handleAdvancedSettingsInput = () => {
  emit('advanced-settings-input')
  emit('test-config-change')
}

const getTestStatusType = (status) => {
  const types = {
    running: 'info',
    success: 'success',
    error: 'danger'
  }
  return types[status] || 'info'
}

const getTestStatusLabel = (status) => {
  const labels = {
    running: t('channels.model_test_running'),
    success: t('channels.model_test_success'),
    error: t('channels.model_test_failed')
  }
  return labels[status] || t('channels.model_test_running')
}

const getTestModeLabel = (testMode) => (
  testMode === 'stream' ? t('channels.chat_test_stream') : t('channels.chat_test_non_stream')
)

const formatUsage = (usage) => {
  if (!usage) return '-'
  if (typeof usage === 'string') return usage
  try {
    const keys = ['prompt_tokens', 'completion_tokens', 'total_tokens']
    const parts = keys
      .filter(key => usage[key] !== undefined && usage[key] !== null)
      .map(key => `${key}: ${usage[key]}`)
    if (parts.length > 0) return parts.join(', ')
    return JSON.stringify(usage)
  } catch {
    return String(usage)
  }
}

const formatLatency = (value) => {
  if (value === null || value === undefined || value === '') return '-'
  const latency = Number(value)
  return Number.isFinite(latency) ? `${latency.toFixed(2)} ms` : '-'
}

const getTestImageUrl = (data) => {
  const image = data?.image || {}
  return image.url || (image.b64_json ? `data:image/png;base64,${image.b64_json}` : '')
}

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
</script>
