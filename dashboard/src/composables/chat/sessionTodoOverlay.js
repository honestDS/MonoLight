import { computed, ref, watch } from 'vue'
import { getClientSetting, setClientSetting } from '../../utils/clientSettings.js'
import { normalizeTodoPlan, summarizeTodoPlan } from '../../utils/todoPresentation.js'

export function createSessionTodoOverlay({ props, emit, storage }) {
  const normalizedPlan = computed(() => normalizeTodoPlan(props.plan))
  const summary = computed(() => summarizeTodoPlan(normalizedPlan.value))
  const storedCollapsed = getClientSetting('sessionTodoDrawerCollapsed', true, storage)
  const collapsed = ref(typeof storedCollapsed === 'boolean' ? storedCollapsed : true)
  const expanded = computed(() => Boolean(props.sessionId) && !collapsed.value)

  const stopWatch = watch(
    expanded,
    isExpanded => emit('expanded-change', isExpanded),
    { immediate: true, flush: 'sync' }
  )

  const close = () => {
    if (!expanded.value) return

    collapsed.value = true
    setClientSetting('sessionTodoDrawerCollapsed', true, storage)
  }

  const toggleCollapsed = () => {
    if (!props.sessionId) return

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
    close,
    toggleCollapsed,
    dispose
  }
}
