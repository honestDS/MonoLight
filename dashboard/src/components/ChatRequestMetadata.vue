<template>
  <Transition name="request-metadata-fade">
    <div
      :class="['llm-request-metadata', 'glass-surface', { 'is-collapsed': requestMetadataCollapsed }]"
    >
      <div
        id="llm-request-metadata-content"
        class="llm-request-metadata-content"
        role="status"
        :aria-hidden="requestMetadataCollapsed"
        :aria-label="$t('chat.llm_request_metadata_label')"
      >
        <el-tooltip
          :content="$t('chat.cumulative_usage_hint')"
          placement="bottom"
          :show-after="300"
        >
          <span class="llm-request-metadata-item">
            <span>{{ $t('chat.input_tokens') }}</span>
            <strong>{{ formatTokenCount(metadata?.total_input_tokens) }}</strong>
          </span>
        </el-tooltip>
        <el-tooltip
          :content="$t('chat.cumulative_usage_hint')"
          placement="bottom"
          :show-after="300"
        >
          <span class="llm-request-metadata-item">
            <span>{{ $t('chat.total_output') }}</span>
            <strong>{{ formatTokenCount(metadata?.total_output_tokens) }}</strong>
          </span>
        </el-tooltip>
        <el-tooltip
          :content="$t('chat.cache_usage_hint')"
          placement="bottom"
          :show-after="300"
        >
          <span class="llm-request-metadata-item">
            <span>{{ $t('chat.cache_hit_rate') }}</span>
            <strong>{{ cacheHitRateText }}</strong>
            <small class="cache-usage-coverage">{{ cacheCoverageText }}</small>
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

      <section
        class="context-usage"
        :class="{ 'is-near-threshold': contextSummaryProgress.nearThreshold === true }"
        role="status"
        aria-live="polite"
      >
        <div class="context-usage-heading">
          <span>{{ $t('chat.context_summary_usage') }}</span>
          <span>{{ $t('chat.context_summary_last_request') }}</span>
        </div>
        <div class="context-usage-status">
          <template v-if="hasContextSummaryProgress">
            <span>{{ $t('chat.context_summary_used', { percent: formatPercent(contextSummaryProgress.ratio) }) }}</span>
            <span>{{ $t('chat.context_summary_remaining', { percent: formatPercent(contextSummaryProgress.remainingRatio) }) }}</span>
          </template>
          <span v-else>{{ contextSummaryStatusText }}</span>
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
        <div v-if="hasContextSummaryProgress" class="context-usage-details">
          {{ $t('chat.context_summary_usage_details', {
            input: formatTokenCount(contextSummaryProgress.inputTokens),
            threshold: formatTokenCount(contextSummaryProgress.triggerTokens),
            remaining: formatTokenCount(contextSummaryProgress.remainingTokens)
          }) }}
        </div>
        <div
          v-if="contextSummaryProgress.status === 'threshold_reached'"
          class="context-usage-status"
        >
          {{ $t('chat.context_summary_reached') }}
        </div>
      </section>

      <div class="request-metadata-actions">
        <span class="request-metadata-collapsed-label" aria-hidden="true">
          {{ $t('chat.request_metadata_short_label') }}
        </span>
        <el-tooltip
          v-if="sessionInfo"
          :content="formatSessionTooltip(sessionInfo)"
          placement="bottom"
          :show-after="100"
          popper-class="session-info-tooltip"
        >
          <el-icon class="session-info-icon"><InfoFilled /></el-icon>
        </el-tooltip>
        <el-tooltip
          :content="$t(requestMetadataCollapsed ? 'chat.expand_request_metadata' : 'chat.collapse_request_metadata')"
          placement="bottom"
          :show-after="300"
        >
          <button
            type="button"
            class="request-metadata-toggle"
            :aria-label="$t(requestMetadataCollapsed ? 'chat.expand_request_metadata' : 'chat.collapse_request_metadata')"
            :aria-expanded="!requestMetadataCollapsed"
            aria-controls="llm-request-metadata-content"
            @click="requestMetadataCollapsed = !requestMetadataCollapsed"
          >
            <el-icon class="request-metadata-toggle-icon" :class="{ 'is-expanded': !requestMetadataCollapsed }">
              <ArrowDown />
            </el-icon>
          </button>
        </el-tooltip>
      </div>
    </div>
  </Transition>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowDown, InfoFilled } from '@element-plus/icons-vue'

import {
  getCacheUsagePresentation,
  getContextSummaryProgress
} from '../composables/chat/requestUsagePresentation.js'
import { getClientSetting, setClientSetting } from '../utils/clientSettings.js'

const props = defineProps({
  metadata: { type: Object, default: null },
  sessionInfo: { type: Object, default: null },
  contextSummarizing: { type: Boolean, default: false }
})

const { t } = useI18n()
const tokenNumberFormatter = new Intl.NumberFormat()
const percentNumberFormatter = new Intl.NumberFormat(undefined, {
  style: 'percent',
  maximumFractionDigits: 2
})

const storedRequestMetadataCollapsed = getClientSetting('requestMetadataCollapsed', true)
const requestMetadataCollapsed = ref(
  typeof storedRequestMetadataCollapsed === 'boolean' ? storedRequestMetadataCollapsed : true
)
watch(requestMetadataCollapsed, value => setClientSetting('requestMetadataCollapsed', value))

const cacheUsage = computed(() => getCacheUsagePresentation(props.metadata))
const contextSummaryProgress = computed(() => getContextSummaryProgress(props.metadata, props.contextSummarizing))
const hasContextSummaryProgress = computed(() => (
  contextSummaryProgress.value.status === 'ready' ||
  contextSummaryProgress.value.status === 'threshold_reached'
))

const formatTokenCount = value => Number.isFinite(value) ? tokenNumberFormatter.format(value) : '-'
const formatPercent = value => percentNumberFormatter.format(value)
const formatPercentOrUnknown = value => Number.isFinite(value) ? formatPercent(value) : t('chat.usage_unknown')

const cacheHitRateText = computed(() => formatPercentOrUnknown(cacheUsage.value.hitRate))
const cacheCoverageText = computed(() => t('chat.cache_coverage', {
  percent: formatPercentOrUnknown(cacheUsage.value.coverage)
}))

const contextSummaryStatusText = computed(() => {
  if (contextSummaryProgress.value.status === 'summarizing') return t('chat.context_summarizing')
  if (contextSummaryProgress.value.status === 'threshold_reached') return t('chat.context_summary_reached')
  return t('chat.context_summary_waiting')
})

const contextSummaryAriaValueText = computed(() => t('chat.context_summary_used', {
  percent: formatPercent(contextSummaryProgress.value.ratio)
}))

const formatSessionTooltip = (info) => {
  if (!info) return ''
  return [
    `${t('chat.session_last_active')}: ${info.last_active || '-'}`,
    `${t('chat.session_created_at')}: ${info.created_at || '-'}`
  ].join('\n')
}
</script>
