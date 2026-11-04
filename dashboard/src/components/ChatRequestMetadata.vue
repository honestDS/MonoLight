<template>
  <Transition name="request-metadata-fade">
    <div
      :class="['llm-request-metadata', { 'is-collapsed': requestMetadataCollapsed }]"
    >
      <Transition name="request-metadata-panel">
        <div
          id="llm-request-metadata-content"
          class="request-metadata-details glass-surface"
          v-show="!requestMetadataCollapsed"
          role="region"
          :aria-label="$t('chat.llm_request_metadata_label')"
        >
          <div class="llm-request-metadata-content">
            <el-tooltip
              :content="$t('chat.cumulative_usage_hint')"
              placement="top"
              :show-after="300"
            >
              <span class="llm-request-metadata-item">
                <span>{{ $t('chat.input_tokens') }}</span>
                <strong>{{ formatTokenCount(metadata?.total_input_tokens) }}</strong>
              </span>
            </el-tooltip>
            <el-tooltip
              :content="$t('chat.cumulative_usage_hint')"
              placement="top"
              :show-after="300"
            >
              <span class="llm-request-metadata-item">
                <span>{{ $t('chat.total_output') }}</span>
                <strong>{{ formatTokenCount(metadata?.total_output_tokens) }}</strong>
              </span>
            </el-tooltip>
            <el-tooltip
              :content="$t('chat.cache_usage_hint')"
              placement="top"
              :show-after="300"
            >
              <span class="llm-request-metadata-item">
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

          <div v-if="hasContextSummaryProgress" class="context-usage-details">
            {{ $t('chat.context_summary_usage_details', {
              input: formatTokenCount(contextSummaryProgress.inputTokens),
              threshold: formatTokenCount(contextSummaryProgress.triggerTokens),
              remaining: formatTokenCount(contextSummaryProgress.remainingTokens)
            }) }}
          </div>

          <div class="request-metadata-detail-footer">
            <div class="request-metadata-context-note">
              <span>{{ $t('chat.context_summary_last_request') }}</span>
              <span v-if="hasContextSummaryProgress">
                {{ $t('chat.context_summary_remaining', { percent: formatPercent(contextSummaryProgress.remainingRatio) }) }}
              </span>
            </div>
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
        </div>
      </Transition>

      <section
        class="context-usage"
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
            @keydown.esc="requestMetadataCollapsed = true"
          >
            <span>{{ $t('chat.request_metadata_short_label') }}</span>
            <el-icon
              class="request-metadata-toggle-icon"
              :class="{ 'is-expanded': !requestMetadataCollapsed }"
              aria-hidden="true"
            >
              <ArrowDown />
            </el-icon>
          </button>
        </el-tooltip>
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
  </Transition>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowDown } from '@element-plus/icons-vue'

import {
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

const contextSummaryProgress = computed(() => getContextSummaryProgress(props.metadata, props.contextSummarizing))
const hasContextSummaryProgress = computed(() => (
  contextSummaryProgress.value.status === 'ready' ||
  contextSummaryProgress.value.status === 'threshold_reached'
))

const formatTokenCount = value => Number.isFinite(value) ? tokenNumberFormatter.format(value) : '-'
const formatPercent = value => percentNumberFormatter.format(value)
const formatPercentOrUnknown = value => Number.isFinite(value) ? formatPercent(value) : t('chat.usage_unknown')

const cacheHitRateText = computed(() => formatPercentOrUnknown(getSessionCacheHitRate(props.metadata)))

const contextSummaryStatusText = computed(() => {
  if (contextSummaryProgress.value.status === 'summarizing') return t('chat.context_summarizing')
  if (contextSummaryProgress.value.status === 'threshold_reached') return t('chat.context_summary_reached')
  return t('chat.context_summary_waiting')
})

const contextSummaryAriaValueText = computed(() => t('chat.context_summary_used', {
  percent: formatPercent(contextSummaryProgress.value.ratio)
}))
</script>
