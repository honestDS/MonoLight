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
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CaretRight, CircleCheckFilled, Clock, List } from '@element-plus/icons-vue'
import { getClientSetting, setClientSetting } from '../utils/clientSettings.js'
import { normalizeTodoPlan, summarizeTodoPlan } from '../utils/todoPresentation.js'

const props = defineProps({
  plan: { type: Object, default: null },
  suppressed: { type: Boolean, default: false }
})

const emit = defineEmits(['expanded-change'])

const normalizedPlan = computed(() => normalizeTodoPlan(props.plan))
const summary = computed(() => summarizeTodoPlan(normalizedPlan.value))
const storedCollapsed = getClientSetting('sessionTodoDrawerCollapsed', true)
const collapsed = ref(typeof storedCollapsed === 'boolean' ? storedCollapsed : true)
const expanded = computed(() => summary.value.total > 0 && !collapsed.value && !props.suppressed)

watch(
  [expanded, () => summary.value.total, () => props.suppressed],
  ([isExpanded, total, suppressed]) => {
    if (isExpanded || total === 0 || suppressed) emit('expanded-change', isExpanded)
  },
  { immediate: true, flush: 'sync' }
)

const handleDrawerAfterLeave = () => {
  // 退出动画完成前保持弹层占用，避免同页低优先级菜单提前互斥。
  if (!expanded.value) emit('expanded-change', false)
}

const toggleCollapsed = () => {
  if (props.suppressed) return

  collapsed.value = !collapsed.value
  setClientSetting('sessionTodoDrawerCollapsed', collapsed.value)
}

onBeforeUnmount(() => emit('expanded-change', false))
</script>

<style scoped lang="scss" src="../assets/css/SessionTodoPanel.scss"></style>
