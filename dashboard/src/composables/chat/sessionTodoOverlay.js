import { computed, ref, watch } from 'vue'
import { getClientSetting, setClientSetting } from '../../utils/clientSettings.js'
import { normalizeTodoPlan, summarizeTodoPlan } from '../../utils/todoPresentation.js'

export function createSessionTodoOverlay({ props, emit, storage }) {
  const normalizedPlan = computed(() => normalizeTodoPlan(props.plan))
  const summary = computed(() => summarizeTodoPlan(normalizedPlan.value))
  const storedCollapsed = getClientSetting('sessionTodoDrawerCollapsed', true, storage)
  const collapsed = ref(typeof storedCollapsed === 'boolean' ? storedCollapsed : true)
  const expanded = computed(() => summary.value.total > 0 && !collapsed.value && !props.suppressed)

  const stopWatch = watch(
    [expanded, () => summary.value.total, () => props.suppressed],
    ([isExpanded, total, suppressed]) => {
      if (isExpanded || total === 0 || suppressed) emit('expanded-change', isExpanded)
    },
    { immediate: true, flush: 'sync' }
  )

  const handleDrawerAfterLeave = () => {
    if (!expanded.value) emit('expanded-change', false)
  }

  const toggleCollapsed = () => {
    if (props.suppressed) return

    collapsed.value = !collapsed.value
    setClientSetting('sessionTodoDrawerCollapsed', collapsed.value, storage)
  }

  const dispose = () => {
    stopWatch()
    emit('expanded-change', false)
  }

  return {
    normalizedPlan,
    summary,
    collapsed,
    expanded,
    handleDrawerAfterLeave,
    toggleCollapsed,
    dispose
  }
}
