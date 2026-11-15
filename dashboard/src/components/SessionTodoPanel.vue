<template>
  <Teleport defer to="#chat-task-trigger-slot">
    <button
      v-if="props.sessionId"
      ref="triggerRef"
      type="button"
      class="chat-composer-action session-task-trigger"
      :aria-expanded="expanded"
      aria-controls="session-task-panel"
      :aria-label="t(expanded ? 'chat.task_panel_collapse' : 'chat.task_panel_expand')"
      :title="t(expanded ? 'chat.task_panel_collapse' : 'chat.task_panel_expand')"
      @click="handleTriggerClick"
      @keydown.esc.stop="handleEscape"
    >
      <svg
        class="session-task-panel-icon"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        aria-hidden="true"
        focusable="false"
      >
        <rect x="2.5" y="2.5" width="15" height="15" rx="1.5" />
        <path d="M13 2.5v15" />
      </svg>
      <span>{{ t('chat.task_panel_title') }}</span>
    </button>
  </Teleport>

  <Transition name="session-task-panel">
    <el-aside
      v-show="expanded"
      :inert="!expanded"
      id="session-task-panel"
      class="session-task-panel"
      width="360px"
      :aria-label="t('chat.task_panel_title')"
      @keydown.esc.stop="handleEscape"
    >
      <div class="session-task-panel-header">
        <h2 ref="panelTitleRef" tabindex="-1">{{ t('chat.task_panel_title') }}</h2>
      </div>

      <el-tabs v-model="activeTab" class="session-task-tabs">
        <el-tab-pane name="todo">
          <template #label>
            <span class="session-task-tab-label">
              <span>{{ t('chat.todo_title') }}</span>
              <span>{{ t('chat.todo_progress', { completed: summary.completed, total: summary.total }) }}</span>
            </span>
          </template>

          <div class="session-todo-tab-content">
            <div v-if="summary.total === 0" class="session-todo-empty">
              {{ t('chat.todo_empty') }}
            </div>
            <template v-else>
              <el-progress
                class="session-todo-progress"
                :percentage="summary.progressPercent"
                :show-text="false"
                :stroke-width="4"
                :aria-label="t('chat.todo_title')"
              />
              <ul class="session-todo-list">
                <li
                  v-for="(item, index) in normalizedPlan.todos"
                  :key="`${index}:${item.content}`"
                  class="session-todo-item"
                  :class="`is-${item.status}`"
                >
                  <el-icon class="session-todo-status-icon" aria-hidden="true">
                    <CircleCheckFilled v-if="item.status === 'completed'" />
                    <CaretRight v-else-if="item.status === 'in_progress'" />
                    <Clock v-else />
                  </el-icon>
                  <span class="session-todo-content">{{ item.content }}</span>
                  <span class="session-todo-status-label">{{ t(`chat.todo_status_${item.status}`) }}</span>
                </li>
              </ul>
            </template>
          </div>
        </el-tab-pane>

        <el-tab-pane name="subagents">
          <template #label>
            <span class="session-task-tab-label">
              <span>{{ t('chat.subagent_title') }}</span>
              <span>{{ activeTotal }}</span>
            </span>
          </template>

          <div ref="subagentListRef" class="session-subagent-tab-content">
            <template v-if="selectedId === null">
              <el-collapse v-model="openGroups" class="subagent-groups">
                <el-collapse-item v-for="group in groups" :key="group.key" :name="group.key">
                  <template #title>
                    <span class="subagent-group-title">{{ group.label }}</span>
                    <span class="subagent-group-count">{{ group.count }}</span>
                  </template>

                  <div class="subagent-group-content">
                    <div v-if="group.entries.length === 0" class="subagent-empty">
                      {{ group.emptyText }}
                    </div>
                    <div v-else class="subagent-card-list">
                      <button
                        v-for="entry in group.entries"
                        :key="entry.subagent_id"
                        :data-subagent-id="entry.subagent_id"
                        type="button"
                        class="subagent-card"
                        @click="handleSubagentSelect(entry.subagent_id)"
                      >
                        <span class="subagent-card-heading">
                          <strong class="subagent-card-name">{{ entry.name }}</strong>
                          <span
                            v-if="showStatus(entry.status)"
                            class="subagent-status"
                            :class="`is-${statusKey(entry.status)}`"
                          >
                            <el-icon aria-hidden="true"><component :is="statusIcon(entry.status)" /></el-icon>
                            <span>{{ statusLabel(entry.status) }}</span>
                          </span>
                        </span>
                        <span class="subagent-card-stage">{{ stageLabel(entry) }}</span>
                        <span v-if="entry.preview" class="subagent-card-preview">{{ entry.preview }}</span>
                        <span class="subagent-card-updated">
                          {{ t('chat.subagent_updated', { time: formatTime(entry.updated_at) }) }}
                        </span>
                      </button>
                    </div>

                    <div
                      v-if="group.key === 'active' && activePageSize > 0 && activeTotal > activePageSize"
                      class="subagent-recent-actions"
                    >
                      <el-pagination
                        :current-page="activePage"
                        :total="activeTotal"
                        :page-size="activePageSize"
                        layout="prev, pager, next"
                        :pager-count="5"
                        size="small"
                        @update:current-page="board.changeActivePage"
                      />
                    </div>

                    <div v-if="group.key === 'recent' && hasMoreRecent" class="subagent-recent-actions">
                      <el-button
                        v-if="recentLimit < 100"
                        text
                        size="small"
                        :loading="loading"
                        @click="board.loadMoreRecent"
                      >
                        {{ t('chat.subagent_load_more') }}
                      </el-button>
                      <span v-else class="subagent-recent-limit">
                        {{ t('chat.subagent_recent_limit', { count: recentLimit }) }}
                      </span>
                    </div>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </template>

            <div v-else class="subagent-detail-view">
              <div class="subagent-detail-header">
                <h3>
                  <button
                    ref="detailTitleRef"
                    type="button"
                    class="subagent-back-button"
                    :title="t('chat.subagent_back')"
                    @click="handleSubagentBack"
                  >
                    <el-icon aria-hidden="true"><ArrowLeft /></el-icon>
                    <span class="subagent-back-title">{{ detailName }}</span>
                  </button>
                </h3>
              </div>

              <div class="subagent-detail-content">
                <div v-if="detailFailed" class="subagent-detail-error">
                  <span>{{ t('chat.subagent_detail_failed') }}</span>
                  <el-button link size="small" :loading="detailLoading" @click="board.refreshDetail">
                    {{ t('chat.subagent_retry') }}
                  </el-button>
                </div>

                <div v-if="!detail && detailLoading" class="subagent-loading">
                  {{ t('chat.subagent_loading') }}
                </div>
                <template v-else-if="detail">
                  <el-collapse :key="`subagent-detail:${detail.work_id}`" v-model="openDetailSections" class="subagent-groups subagent-detail-collapse">
                    <el-collapse-item v-if="hasResultText" name="result">
                      <template #title>
                        <span class="subagent-group-title">{{ t('chat.subagent_result') }}</span>
                      </template>
                      <div class="subagent-group-content">
                        <p class="subagent-detail-copy" v-text="detail.result_text"></p>
                        <p v-if="detail.result_text_truncated" class="subagent-detail-notice">{{ t('chat.subagent_text_truncated') }}</p>
                      </div>
                    </el-collapse-item>
                    <el-collapse-item name="progress">
                      <template #title>
                        <span class="subagent-group-title">{{ t('chat.subagent_progress') }}</span>
                      </template>
                      <div class="subagent-group-content">
                        <ol v-if="detailPosts.length > 0" class="subagent-post-list">
                          <li v-for="post in detailPosts" :key="post.id" class="subagent-post">
                            <div class="subagent-post-meta">
                              <span>{{ post.stage || t('chat.subagent_no_progress') }}</span>
                              <time>{{ formatTime(post.created_at) }}</time>
                            </div>
                            <p class="subagent-post-content" v-text="post.content"></p>
                            <p v-if="post.truncated" class="subagent-detail-notice">{{ t('chat.subagent_text_truncated') }}</p>
                          </li>
                        </ol>
                        <div v-else class="subagent-no-progress">
                          {{ t('chat.subagent_no_progress') }}
                        </div>

                        <el-button
                          v-if="nextBeforePostId !== null"
                          text
                          size="small"
                          :loading="detailLoading"
                          @click="board.loadOlderPosts"
                        >
                          {{ t('chat.subagent_load_older') }}
                        </el-button>
                      </div>
                    </el-collapse-item>
                  </el-collapse>

                  <section v-if="hasReason" class="subagent-detail-section">
                    <h4>{{ t('chat.subagent_reason') }}</h4>
                    <p class="subagent-detail-copy" v-text="detail.reason"></p>
                    <p v-if="detail.reason_truncated" class="subagent-detail-notice">{{ t('chat.subagent_text_truncated') }}</p>
                  </section>
                  <div v-if="detail.ended_without_reply" class="subagent-detail-notice">
                    {{ t('chat.subagent_ended_without_reply') }}
                  </div>
                  <div v-if="showNoTextResult" class="subagent-detail-notice">
                    {{ t('chat.subagent_no_text_result') }}
                  </div>
                </template>
                <div v-else-if="!detailFailed" class="subagent-loading">
                  {{ t('chat.subagent_loading') }}
                </div>
              </div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-aside>
  </Transition>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ArrowLeft,
  CaretRight,
  CircleCheckFilled,
  CircleCloseFilled,
  Clock,
  Loading,
  WarningFilled
} from '@element-plus/icons-vue'
import { chatApi } from '../api/index.js'
import { createSessionSubagentBoard } from '../composables/chat/sessionSubagentBoard.js'
import { createSessionTodoOverlay } from '../composables/chat/sessionTodoOverlay.js'

const props = defineProps({
  plan: { type: Object, default: null },
  sessionId: { type: String, default: null },
  profileKey: { type: [String, Number], default: null }
})

const emit = defineEmits(['expanded-change'])
const { t, locale } = useI18n()
const triggerRef = ref(null)
const panelTitleRef = ref(null)
const detailTitleRef = ref(null)
const subagentListRef = ref(null)
const pageVisible = ref(false)
const activeTab = ref('todo')
const openGroups = ref(['active'])
const openDetailSections = ref(['progress'])

const {
  normalizedPlan,
  summary,
  expanded,
  close: closePanel,
  toggleCollapsed,
  dispose: disposeOverlay
} = createSessionTodoOverlay({ props, emit })

const board = createSessionSubagentBoard({ api: chatApi })
const {
  active,
  activePage,
  activePageSize,
  activeTotal,
  recent,
  hasMoreRecent,
  recentLimit,
  loading,
  detail,
  detailLoading,
  detailFailed,
  selectedId,
  nextBeforePostId,
  selectedEntry
} = board

watch(
  () => detail.value?.work_id,
  () => {
    openDetailSections.value = ['progress']
  },
  { flush: 'sync' }
)

const statusMap = Object.freeze({
  ready_for_llm: 'queued',
  running: 'running',
  waiting_external_work: 'waiting',
  succeeded: 'succeeded',
  failed: 'failed',
  cancelled: 'cancelled',
  execution_unknown: 'unknown'
})

const statusIcons = Object.freeze({
  queued: Clock,
  running: Loading,
  waiting: Clock,
  succeeded: CircleCheckFilled,
  failed: CircleCloseFilled,
  cancelled: CircleCloseFilled,
  unknown: WarningFilled
})

const statusKey = status => statusMap[status] || 'unknown'
const statusIcon = status => statusIcons[statusKey(status)]
const statusLabel = status => t(`chat.subagent_status_${statusKey(status)}`)
const showStatus = status => status !== null
const stageLabel = entry => {
  if (entry.stage) return entry.stage
  if (entry.completed) return t('chat.subagent_result')
  return t('chat.subagent_no_progress')
}

const groups = computed(() => [
  {
    key: 'active',
    label: t('chat.subagent_active'),
    emptyText: t('chat.subagent_empty_active'),
    count: activeTotal.value,
    entries: active.value
  },
  {
    key: 'recent',
    label: t('chat.subagent_recent'),
    emptyText: t('chat.subagent_empty_recent'),
    count: recent.value.length,
    entries: recent.value
  }
])

const detailName = computed(() => detail.value?.name || selectedEntry.value?.name || selectedId.value || '')
const detailPosts = computed(() => Array.isArray(detail.value?.posts) ? detail.value.posts : [])
const hasResultText = computed(() => typeof detail.value?.result_text === 'string' && detail.value.result_text.length > 0)
const hasReason = computed(() => typeof detail.value?.reason === 'string' && detail.value.reason.length > 0)
const showNoTextResult = computed(() => detail.value?.status === 'succeeded'
  && !hasResultText.value
  && !hasReason.value
  && !detail.value?.ended_without_reply)

const formatTime = value => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  try {
    return date.toLocaleString(locale.value || undefined)
  } catch {
    return date.toLocaleString()
  }
}

const handleTriggerClick = () => {
  const shouldFocusTitle = !expanded.value
  toggleCollapsed()
  if (!shouldFocusTitle || !expanded.value) return
  nextTick(() => panelTitleRef.value?.focus({ preventScroll: true }))
}

const handleSubagentSelect = subagentId => {
  board.selectSubagent(subagentId)
  nextTick(() => detailTitleRef.value?.focus())
}

const restoreSubagentFocus = subagentId => {
  if (!expanded.value || activeTab.value !== 'subagents') return

  const listRoot = subagentListRef.value
  const card = listRoot && [...listRoot.querySelectorAll('[data-subagent-id]')]
    .find(node => subagentId !== null
      && node.dataset.subagentId === String(subagentId)
      && node.getClientRects().length > 0)
  if (card) {
    card.focus()
    return
  }

  panelTitleRef.value?.focus()
}

const handleSubagentBack = () => {
  const subagentId = selectedId.value
  board.clearSelection()
  nextTick(() => restoreSubagentFocus(subagentId))
}

const handleEscape = () => {
  if (!expanded.value) return
  closePanel()
  triggerRef.value?.focus()
}

let panelHasOpened = false
watch(
  expanded,
  isExpanded => {
    if (!isExpanded) return
    if (!panelHasOpened && summary.value.total === 0) activeTab.value = 'subagents'
    panelHasOpened = true
  },
  { immediate: true, flush: 'sync' }
)

watch(
  [() => props.sessionId, () => props.profileKey],
  ([nextSessionId, nextProfileKey]) => board.setScope(nextSessionId, nextProfileKey),
  { immediate: true, flush: 'sync' }
)

watch(
  [expanded, pageVisible],
  ([isExpanded, isPageVisible]) => board.setEnabled(isExpanded && isPageVisible),
  { immediate: true, flush: 'sync' }
)

let pageDocument = null
const handleVisibilityChange = () => {
  if (!pageDocument) return
  pageVisible.value = !pageDocument.hidden
}

onMounted(() => {
  pageDocument = document
  pageDocument.addEventListener('visibilitychange', handleVisibilityChange)
  handleVisibilityChange()
})

onBeforeUnmount(() => {
  pageDocument?.removeEventListener('visibilitychange', handleVisibilityChange)
  board.dispose()
  disposeOverlay()
})
</script>

<style scoped lang="scss" src="../assets/css/SessionTodoPanel.scss"></style>
