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

<style scoped lang="scss" src="../assets/css/SessionTaskCenter.scss"></style>
