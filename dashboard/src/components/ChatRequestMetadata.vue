<template>
  <div class="llm-request-metadata">
    <section
      id="llm-request-metadata-content"
      class="request-metadata-details"
      v-show="!requestMetadataCollapsed"
      ref="detailsRef"
      tabindex="-1"
      role="region"
      :aria-label="$t('chat.llm_request_metadata_label')"
      @keydown.esc.stop="closeRequestMetadata"
    >
      <div class="llm-request-metadata-content">
        <el-tooltip
          :content="$t('chat.cumulative_usage_hint')"
          placement="top"
          :show-after="300"
          :trigger="['hover', 'focus']"
        >
          <span class="llm-request-metadata-item" tabindex="0">
            <span>{{ $t('chat.input_tokens') }}</span>
            <strong>{{ formatTokenCount(metadata?.total_input_tokens) }}</strong>
          </span>
        </el-tooltip>
        <el-tooltip
          :content="$t('chat.cumulative_usage_hint')"
          placement="top"
          :show-after="300"
          :trigger="['hover', 'focus']"
        >
          <span class="llm-request-metadata-item" tabindex="0">
            <span>{{ $t('chat.total_output') }}</span>
            <strong>{{ formatTokenCount(metadata?.total_output_tokens) }}</strong>
          </span>
        </el-tooltip>
        <el-tooltip
          :content="$t('chat.cache_usage_hint')"
          placement="top"
          :show-after="300"
          :trigger="['hover', 'focus']"
        >
          <span class="llm-request-metadata-item" tabindex="0">
            <span>{{ $t('chat.cache_hit_rate') }}</span>
            <strong>{{ cacheHitRateText }}</strong>
          </span>
        </el-tooltip>
        <span class="llm-request-metadata-item">
          <span>{{ $t('chat.context_limit') }}</span>
          <strong>{{ formatTokenCount(metadata?.context_window_tokens) }}</strong>
        </span>
        <span class="llm-request-metadata-item">
          <span>{{ $t('chat.max_output') }}</span>
          <strong>{{ formatTokenCount(metadata?.max_output_tokens) }}</strong>
        </span>
      </div>

      <div class="context-usage-details">
        <div class="context-usage-heading" role="status" aria-live="polite">
          <span class="context-usage-details-title">{{ $t('chat.context_summary_usage') }}</span>
          <template v-if="hasContextSummaryProgress">
            <strong class="context-usage-value">
              {{ formatPercent(contextSummaryProgress.ratio) }}
            </strong>
            <span>
              {{ $t('chat.context_summary_remaining', { percent: formatPercent(contextSummaryProgress.remainingRatio) }) }}
            </span>
          </template>
          <span v-if="!hasContextSummaryProgress || contextSummaryProgress.status === 'threshold_reached'">{{ contextSummaryStatusText }}</span>
        </div>
        <div v-if="hasContextSummaryProgress">
          {{ $t('chat.context_summary_usage_details', {
            input: formatTokenCount(contextSummaryProgress.inputTokens),
            threshold: formatTokenCount(contextSummaryProgress.triggerTokens),
            remaining: formatTokenCount(contextSummaryProgress.remainingTokens)
          }) }}
        </div>
      </div>

      <div class="request-metadata-detail-footer">
        <span v-if="contextSummaryProgress.status !== 'previous'">{{ $t('chat.context_summary_last_request') }}</span>
        <dl v-if="sessionInfo" class="request-metadata-session-times">
          <div class="request-metadata-session-time">
            <dt>{{ $t('chat.session_created_at') }}</dt>
            <dd>{{ sessionInfo.created_at || '-' }}</dd>
          </div>
          <div class="request-metadata-session-time">
            <dt>{{ $t('chat.session_last_active') }}</dt>
            <dd>{{ sessionInfo.last_active || '-' }}</dd>
          </div>
        </dl>
      </div>
    </section>

    <section
      class="context-usage"
      v-show="requestMetadataCollapsed"
      :class="{ 'is-near-threshold': contextSummaryProgress.nearThreshold === true }"
      role="status"
      aria-live="polite"
    >
      <div class="context-usage-heading">
        <span>{{ $t('chat.context_summary_usage') }}</span>
        <strong v-if="hasContextSummaryProgress" class="context-usage-value">
          {{ formatPercent(contextSummaryProgress.ratio) }}
        </strong>
        <span v-else>{{ contextSummaryStatusText }}</span>
      </div>
      <div
        v-if="contextSummaryProgress.status === 'threshold_reached'"
        class="context-usage-status"
      >
        {{ $t('chat.context_summary_reached') }}
      </div>
      <progress
        v-if="hasContextSummaryProgress"
        class="context-usage-progress"
        :value="contextSummaryProgress.ratio"
        max="1"
        :aria-label="$t('chat.context_summary_usage')"
        :aria-valuetext="contextSummaryAriaValueText"
      ></progress>
      <span
        v-else
        class="context-usage-placeholder"
        aria-hidden="true"
      ></span>
    </section>
  </div>

  <Teleport defer to="#chat-request-metadata-trigger-slot">
    <button
      ref="triggerRef"
      type="button"
      class="chat-composer-action"
      :aria-label="$t(requestMetadataCollapsed ? 'chat.expand_request_metadata' : 'chat.collapse_request_metadata')"
      :title="$t(requestMetadataCollapsed ? 'chat.expand_request_metadata' : 'chat.collapse_request_metadata')"
      :aria-expanded="!requestMetadataCollapsed"
      aria-controls="llm-request-metadata-content"
      @click="toggleRequestMetadata"
      @keydown.esc.stop="closeRequestMetadata"
    >
      <el-icon aria-hidden="true"><DataAnalysis /></el-icon>
      <span>{{ $t('chat.request_metadata_short_label') }}</span>
    </button>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { DataAnalysis } from '@element-plus/icons-vue'

import {
  formatTokenCount,
  getSessionCacheHitRate,
  getContextSummaryProgress
} from '../composables/chat/requestUsagePresentation.js'
import { getClientSetting, setClientSetting } from '../utils/clientSettings.js'

const props = defineProps({
  metadata: { type: Object, default: null },
  sessionInfo: { type: Object, default: null },
  contextSummarizing: { type: Boolean, default: false }
})
const { t } = useI18n()
const percentNumberFormatter = new Intl.NumberFormat(undefined, {
  style: 'percent',
  maximumFractionDigits: 2
})

const storedRequestMetadataCollapsed = getClientSetting('requestMetadataCollapsed', true)
const requestMetadataCollapsed = ref(
  typeof storedRequestMetadataCollapsed === 'boolean' ? storedRequestMetadataCollapsed : true
)
watch(requestMetadataCollapsed, value => setClientSetting('requestMetadataCollapsed', value))

const detailsRef = ref(null)
const triggerRef = ref(null)

const closeRequestMetadata = () => {
  if (requestMetadataCollapsed.value) return
  requestMetadataCollapsed.value = true
  triggerRef.value?.focus({ preventScroll: true })
}

const toggleRequestMetadata = async () => {
  if (!requestMetadataCollapsed.value) {
    closeRequestMetadata()
    return
  }
  requestMetadataCollapsed.value = false
  await nextTick()
  if (!requestMetadataCollapsed.value) {
    detailsRef.value?.focus({ preventScroll: true })
  }
}

const contextSummaryProgress = computed(() => getContextSummaryProgress(props.metadata, props.contextSummarizing))
const hasContextSummaryProgress = computed(() => (
  contextSummaryProgress.value.status === 'ready' ||
  contextSummaryProgress.value.status === 'threshold_reached' ||
  contextSummaryProgress.value.status === 'previous'
))

const formatPercent = value => percentNumberFormatter.format(value)
const formatPercentOrUnknown = value => Number.isFinite(value) ? formatPercent(value) : t('chat.usage_unknown')

const cacheHitRateText = computed(() => formatPercentOrUnknown(getSessionCacheHitRate(props.metadata)))

const contextSummaryStatusText = computed(() => {
  if (contextSummaryProgress.value.status === 'summarizing') return t('chat.context_summarizing')
  if (contextSummaryProgress.value.status === 'threshold_reached') return t('chat.context_summary_reached')
  return t('chat.context_summary_waiting')
})

const contextSummaryAriaValueText = computed(() => t(
  contextSummaryProgress.value.status === 'previous' ? 'chat.context_summary_previous_usage' : 'chat.context_summary_used',
  {
    percent: formatPercent(contextSummaryProgress.value.ratio)
  }
))
</script>
