<template>
  <section
    v-if="summary.total > 0"
    class="session-todo-anchor"
    :class="{ 'is-suppressed': props.suppressed }"
    :aria-label="$t('chat.todo_title')"
  >
    <Transition name="todo-drawer" @after-leave="handleDrawerAfterLeave">
      <div
        v-show="expanded"
        id="session-todo-list"
        class="session-todo-drawer glass-surface"
      >
        <div class="session-todo-header">
          <span class="session-todo-heading">
            <el-icon class="session-todo-heading-icon" aria-hidden="true"><List /></el-icon>
            <span class="session-todo-title">{{ $t('chat.todo_title') }}</span>
            <span class="session-todo-count">{{ $t('chat.todo_progress', { completed: summary.completed, total: summary.total }) }}</span>
          </span>
        </div>

        <div
          class="session-todo-progress"
          role="progressbar"
          :aria-label="$t('chat.todo_title')"
          :aria-valuemin="0"
          :aria-valuemax="summary.total"
          :aria-valuenow="summary.completed"
        >
          <span class="session-todo-progress-value" :style="{ width: `${summary.progressPercent}%` }"></span>
        </div>

        <div class="session-todo-body">
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
              <span class="session-todo-status-label">{{ $t(`chat.todo_status_${item.status}`) }}</span>
            </li>
          </ul>
        </div>
      </div>
    </Transition>

    <el-tooltip
      :content="$t(expanded ? 'chat.collapse_todo' : 'chat.expand_todo')"
      placement="left"
      :show-after="300"
    >
      <button
        type="button"
        class="session-todo-trigger glass-surface"
        :aria-expanded="expanded"
        :aria-label="$t(expanded ? 'chat.collapse_todo' : 'chat.expand_todo')"
        :disabled="props.suppressed"
        aria-controls="session-todo-list"
        @click="toggleCollapsed"
      >
        <el-icon class="session-todo-trigger-icon" aria-hidden="true"><List /></el-icon>
        <span class="session-todo-trigger-count" aria-hidden="true">{{ summary.completed }}/{{ summary.total }}</span>
      </button>
    </el-tooltip>
  </section>
</template>

<script setup>
import { onBeforeUnmount } from 'vue'
import { CaretRight, CircleCheckFilled, Clock, List } from '@element-plus/icons-vue'
import { createSessionTodoOverlay } from '../composables/chat/sessionTodoOverlay.js'

const props = defineProps({
  plan: { type: Object, default: null },
  suppressed: { type: Boolean, default: false }
})

const emit = defineEmits(['expanded-change'])

const {
  normalizedPlan,
  summary,
  expanded,
  handleDrawerAfterLeave,
  toggleCollapsed,
  dispose
} = createSessionTodoOverlay({ props, emit })

onBeforeUnmount(dispose)
</script>

<style scoped lang="scss" src="../assets/css/SessionTodoPanel.scss"></style>
