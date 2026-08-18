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
        <el-button v-if="props.showRemove" type="text" class="remove" @click="emit('remove')">
          {{ $t('channels.remove') }}
        </el-button>
        <template v-if="props.testState">
          <el-button type="text" @click="emit('view-test-result')">
            {{ $t('channels.model_test_view_result') }}
          </el-button>
          <el-tag :type="getTestStatusType(props.testState.status)" size="small">
            {{ getTestStatusLabel(props.testState.status) }}
          </el-tag>
        </template>
      </div>
    </div>

    <div class="model-entry-fields">
      <div class="model-entry-field model-entry-field-half">
        <el-form-item :label="$t('channels.model_id_label')" :error="props.modelIdError" :prop="props.modelIdProp || undefined">
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
        <el-form-item :label="$t('channels.model_protocol')" :error="props.protocolError" :prop="props.protocolProp || undefined">
          <el-select v-model="props.entry.protocol" class="full-width-input" @change="handleProtocolChange">
            <el-option v-for="item in props.modelProtocols" :key="item" :label="getModelProtocolLabel(item)" :value="item" />
          </el-select>
        </el-form-item>
      </div>
      <div v-if="props.showEnabled" class="model-entry-field model-entry-field-half">
        <el-form-item :label="$t('channels.is_enabled')">
          <el-switch v-model="props.entry.is_enabled" @change="emit('test-config-change')" />
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
              <el-input-number v-model="props.entry.embedding_dimensions" :min="1" controls-position="right" @change="emit('test-config-change')" />
              <el-button type="primary" plain :loading="props.detectingDimension" @click="emit('detect-dimension')">
                {{ $t('channels.auto_detect') }}
              </el-button>
            </div>
          </el-form-item>
        </div>
        <div class="model-entry-field model-entry-field-half">
          <el-form-item :label="$t('channels.embedding_timeout')">
            <el-input-number v-model="props.entry.embedding_timeout" :min="0.1" :max="600" :step="0.1" controls-position="right" @change="emit('test-config-change')" />
          </el-form-item>
        </div>
      </template>

      <template v-if="props.entry.usage === 'RERANK'">
        <div class="model-entry-field model-entry-field-half">
          <el-form-item :label="$t('channels.rerank_timeout')">
            <el-input-number v-model="props.entry.rerank_timeout" :min="0.1" :max="120" :step="0.1" controls-position="right" @change="emit('test-config-change')" />
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
  showRemove: {
    type: Boolean,
    default: true
  },
  showEnabled: {
    type: Boolean,
    default: true
  },
  modelIdProp: {
    type: String,
    default: ''
  },
  protocolProp: {
    type: String,
    default: ''
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
  'view-test-result',
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
