<template>
  <div class="session-task-center">
    <el-popover
      v-model:visible="popoverVisible"
      trigger="click"
      placement="bottom-end"
      :width="popoverWidth"
      :persistent="false"
      :show-arrow="false"
      :popper-style="{ maxWidth: 'calc(100vw - 24px)', padding: '0' }"
      popper-class="session-task-center-popper"
    >
      <template #reference>
        <button
          type="button"
          class="session-task-center-trigger"
          :aria-expanded="popoverVisible"
          aria-controls="session-task-center-popover"
          aria-labelledby="session-task-center-title"
          aria-describedby="session-task-center-summary"
          :title="summaryLabel"
          @keydown.esc.stop.prevent="closePopover"
        >
          <el-icon class="session-task-center-trigger-icon" aria-hidden="true"><Bell /></el-icon>
          <span id="session-task-center-title" class="session-task-center-label">{{ $t('chat.task_center_title') }}</span>
          <span
            id="session-task-center-summary"
            class="session-task-center-counts"
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            <span
              class="session-task-count session-task-count--running"
              :class="{ 'is-empty': runningCount === 0 }"
              aria-hidden="true"
              :title="$t('chat.task_running')"
            >
              <el-icon class="session-task-count-icon"><Clock /></el-icon>
              <span class="session-task-count-value">{{ runningCount }}</span>
            </span>
            <span
              class="session-task-count session-task-count--unread"
              :class="{ 'is-empty': unreadCount === 0 }"
              aria-hidden="true"
              :title="$t('chat.task_unread')"
            >
              <el-icon class="session-task-count-icon"><ChatDotRound /></el-icon>
              <span class="session-task-count-value">{{ unreadCount }}</span>
            </span>
            <span class="session-task-sr-only">{{ summaryLabel }}</span>
          </span>
        </button>
      </template>

      <div
        v-if="popoverVisible"
        id="session-task-center-popover"
        class="session-task-center-popover-content"
        role="dialog"
        :aria-label="$t('chat.task_center_title')"
        @keydown.esc.stop.prevent="closePopover"
      >
        <div class="session-task-center-header">
          <div class="session-task-center-header-info">
            <h2 class="session-task-center-heading">{{ $t('chat.task_center_title') }}</h2>
            <p class="session-task-center-summary">{{ summaryLabel }}</p>
          </div>
          <button
            type="button"
            class="session-task-refresh"
            :aria-label="$t('chat.task_refresh')"
            :title="$t('chat.task_refresh')"
            :aria-busy="loading"
            :disabled="loading"
            @click="handleRefresh"
          >
            <el-icon :class="{ 'is-loading': loading }" aria-hidden="true"><Refresh /></el-icon>
          </button>
        </div>

        <div v-if="error" class="session-task-center-state session-task-center-state--error" role="alert">
          <el-icon class="session-task-center-state-icon" aria-hidden="true"><Warning /></el-icon>
          <span>{{ $t('chat.task_load_failed') }}</span>
        </div>
        <div v-else-if="tasks.length === 0" class="session-task-center-state" role="status">
          <el-icon class="session-task-center-state-icon" aria-hidden="true"><Bell /></el-icon>
          <span>{{ $t('chat.task_center_empty') }}</span>
        </div>
        <div v-else class="session-task-center-list" role="list">
          <div
            v-for="task in tasks"
            :key="sessionId(task)"
            role="listitem"
          >
            <button
              type="button"
              class="session-task-item"
              :class="{
                'is-running': isTaskRunning(task),
                'is-failed': !isTaskRunning(task) && task.completed_status === 'failed'
              }"
              :aria-label="taskAccessibleLabel(task)"
              :title="$t('chat.task_open_session')"
              @click="openTask(task)"
            >
              <el-icon class="session-task-item-icon" aria-hidden="true">
                <Clock v-if="isTaskRunning(task)" />
                <CircleCloseFilled v-else-if="task.completed_status === 'failed'" />
                <CircleCheckFilled v-else />
              </el-icon>
              <span class="session-task-item-content">
                <span class="session-task-item-title">{{ taskTitle(task) }}</span>
                <span class="session-task-item-status">{{ taskStatus(task) }}</span>
              </span>
              <el-icon class="session-task-item-arrow" aria-hidden="true"><ArrowRight /></el-icon>
            </button>
          </div>
        </div>
      </div>
    </el-popover>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ArrowRight,
  Bell,
  ChatDotRound,
  CircleCheckFilled,
  CircleCloseFilled,
  Clock,
  Refresh,
  Warning
} from '@element-plus/icons-vue'

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  error: { type: Boolean, default: false },
  loading: { type: Boolean, default: false }
})

const emit = defineEmits(['open', 'refresh'])
const { t } = useI18n()

const popoverVisible = ref(false)
const popoverWidth = 'min(360px, calc(100vw - 24px))'

const sessionId = (task) => String(task?.session_id ?? '')
const isTaskRunning = (task) => task?.is_running === true
const hasUnreadResult = (task) => Boolean(task?.has_unread_result)

const runningCount = computed(() => props.tasks.filter(isTaskRunning).length)
const unreadCount = computed(() => props.tasks.filter(hasUnreadResult).length)
const summaryLabel = computed(() => t('chat.task_summary', {
  running: runningCount.value,
  unread: unreadCount.value
}))

const taskTitle = (task) => task?.title || t('chat.session_prefix', { id: sessionId(task).slice(0, 8) })

const taskStatus = (task) => {
  if (isTaskRunning(task)) {
    return hasUnreadResult(task) ? t('chat.task_running_unread') : t('chat.task_running')
  }

  return task?.completed_status === 'failed'
    ? t('chat.task_failed_unread')
    : t('chat.task_completed_unread')
}

const taskAccessibleLabel = (task) => t('chat.task_entry_label', {
  title: taskTitle(task),
  status: taskStatus(task)
})

const closePopover = () => {
  popoverVisible.value = false
}

const handleRefresh = () => {
  emit('refresh')
}

const openTask = (task) => {
  closePopover()
  emit('open', sessionId(task))
}
</script>

<style scoped lang="scss">
.session-task-center {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.session-task-center-trigger,
.session-task-refresh,
.session-task-item {
  &:focus-visible {
    outline: 2px solid var(--el-color-primary-dark-2);
    outline-offset: 2px;
  }
}

.session-task-center-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-width: 44px;
  min-height: 44px;
  max-width: calc(100vw - 24px);
  padding: 0 10px;
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--radius-10);
  background: var(--el-fill-color-light);
  color: var(--el-text-color-primary);
  cursor: pointer;
  font: inherit;
  transition: background-color 0.2s ease, border-color 0.2s ease;

  &:hover,
  &[aria-expanded='true'] {
    border-color: var(--el-color-primary-light-5);
    background: var(--el-color-primary-light-9);
  }
}

.session-task-center-trigger-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 26px;
  width: 26px;
  height: 26px;
  border-radius: var(--radius-8);
  background: var(--el-fill-color);
  color: var(--el-text-color-regular);
  font-size: 16px;
}

.session-task-center-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 600;
}

.session-task-center-counts {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.session-task-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 24px;
  height: 22px;
  padding: 0 6px;
  box-sizing: border-box;
  border-radius: var(--radius-pill);
  color: var(--el-text-color-primary);
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
}

.session-task-count--running {
  background: var(--el-color-primary-light-9);
}

.session-task-count--running .session-task-count-icon {
  color: var(--el-color-primary-dark-2);
}

.session-task-count--unread {
  background: var(--el-color-warning-light-9);
}

.session-task-count--unread .session-task-count-icon {
  color: var(--el-color-warning-dark-2);
}

.session-task-count.is-empty {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
}

.session-task-count.is-empty .session-task-count-icon {
  color: inherit;
}

.session-task-count-icon {
  flex: 0 0 auto;
  font-size: 13px;
}

.session-task-count-value {
  font-variant-numeric: tabular-nums;
}

.session-task-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.session-task-center-popover-content {
  display: flex;
  flex-direction: column;
  width: 100%;
  min-height: 0;
  max-width: min(360px, calc(100vw - 24px));
  max-height: min(70vh, 520px);
  overflow: hidden;
  color: var(--el-text-color-primary);
}

:global(.session-task-center-popper.el-popover.el-popper) {
  overflow: hidden;
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--radius-16);
  background: var(--el-bg-color-overlay);
  box-shadow: var(--chat-metadata-surface-shadow);
}

.session-task-center-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-shrink: 0;
  padding: 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-lighter);
}

.session-task-center-header-info {
  min-width: 0;
}

.session-task-center-heading {
  min-width: 0;
  margin: 0;
  color: var(--el-text-color-primary);
  font-size: 15px;
  font-weight: 600;
  line-height: 1.4;
}

.session-task-center-summary {
  margin: 4px 0 0;
  color: var(--el-text-color-regular);
  font-size: 12px;
  line-height: 1.5;
}

.session-task-refresh {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 44px;
  height: 44px;
  padding: 0;
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--radius-10);
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition: background-color 0.2s ease, border-color 0.2s ease, color 0.2s ease;

  .el-icon {
    font-size: 16px;
  }

  &:hover:not(:disabled) {
    border-color: var(--el-color-primary-light-5);
    background: var(--el-color-primary-light-9);
    color: var(--el-color-primary-dark-2);
  }

  &:disabled {
    cursor: wait;
    opacity: 0.7;
  }
}

.session-task-center-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 32px 20px;
  color: var(--el-text-color-regular);
  font-size: 13px;
  line-height: 1.6;
  text-align: center;
}

.session-task-center-state-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 48px;
  width: 48px;
  height: 48px;
  border-radius: var(--radius-circle);
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  font-size: 24px;
}

.session-task-center-state--error {
  .session-task-center-state-icon {
    background: var(--el-color-danger-light-9);
    color: var(--el-color-danger-dark-2);
  }
}

.session-task-center-list {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  gap: 8px;
  max-height: min(55vh, 420px);
  padding: 12px;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: var(--el-border-color-darker) transparent;
}

.session-task-item {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) 16px;
  align-items: center;
  gap: 12px;
  width: 100%;
  box-sizing: border-box;
  min-height: 68px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--radius-12);
  background: var(--el-fill-color-blank);
  color: var(--el-text-color-primary);
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: background-color 0.18s ease, border-color 0.18s ease;

  &:hover {
    border-color: var(--el-color-primary-light-7);
    background: var(--el-color-primary-light-9);
  }

  &:active {
    background: var(--el-fill-color);
  }
}

.session-task-item-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: var(--radius-10);
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
  font-size: 18px;
}

.session-task-item-content {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 4px;
}

.session-task-item-title {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: normal;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
}

.session-task-item-status {
  align-self: flex-start;
  max-width: 100%;
  box-sizing: border-box;
  padding: 2px 8px;
  border-radius: var(--radius-pill);
  background: var(--el-color-success-light-9);
  color: var(--el-text-color-regular);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.session-task-item.is-running {
  .session-task-item-icon {
    background: var(--el-color-primary-light-9);
    color: var(--el-color-primary-dark-2);
  }

  .session-task-item-status {
    background: var(--el-color-primary-light-9);
  }
}

.session-task-item.is-failed {
  .session-task-item-icon {
    background: var(--el-color-danger-light-9);
    color: var(--el-color-danger-dark-2);
  }

  .session-task-item-status {
    background: var(--el-color-danger-light-9);
  }
}

.session-task-item-arrow {
  color: var(--el-text-color-regular);
  font-size: 14px;
}

@media (max-width: 600px) {
  .session-task-center-label {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .session-task-center-trigger {
    gap: 6px;
    padding: 0 6px;
  }

  .session-task-count--running {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .session-task-center-trigger,
  .session-task-refresh,
  .session-task-item {
    transition: none;
  }

  .session-task-refresh .is-loading {
    animation: none;
  }
}
</style>
