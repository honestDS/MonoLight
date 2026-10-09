<template>
  <template v-if="!props.enabled">
    <slot />
  </template>

  <div
    v-else
    class="collapsible-message-text"
  >
    <div
      class="collapsible-message-text__viewport"
      :style="{ maxHeight: props.expanded ? undefined : collapsedHeight }"
    >
      <div ref="contentRef" class="collapsible-message-text__content">
        <slot />
      </div>
    </div>

    <button
      v-if="hasOverflow"
      class="collapsible-message-text__toggle"
      type="button"
      :aria-expanded="props.expanded"
      :aria-label="$t(props.expanded ? 'chat.collapse_message' : 'chat.expand_message')"
      :title="$t(props.expanded ? 'chat.collapse_message' : 'chat.expand_message')"
      @click="toggleExpanded"
    >
      <ArrowDown
        class="collapsible-message-text__icon"
        :class="{ 'is-expanded': props.expanded }"
        width="14px"
        height="14px"
        aria-hidden="true"
      />
    </button>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, onUpdated, ref, watch } from 'vue'
import { ArrowDown } from '@element-plus/icons-vue'

const COLLAPSED_LINE_COUNT = 5
const HEIGHT_TOLERANCE = 1

const props = defineProps({
  enabled: {
    type: Boolean,
    default: true,
  },
  expanded: {
    type: Boolean,
    default: false,
  },
})

const emit = defineEmits(['update:expanded'])

const contentRef = ref(null)
const collapsedHeight = ref('8.25em')
const hasOverflow = ref(false)

let mounted = false
let resizeObserver = null

function disconnectResizeObserver() {
  if (resizeObserver) {
    resizeObserver.disconnect()
  }
}

function getLineHeight(content) {
  return Number.parseFloat(window.getComputedStyle(content).lineHeight)
}

function measureContent() {
  if (!mounted || !props.enabled || !contentRef.value) {
    return
  }

  const content = contentRef.value
  const lineHeight = getLineHeight(content)

  if (!Number.isFinite(lineHeight) || lineHeight <= 0) {
    return
  }

  collapsedHeight.value = `${lineHeight * COLLAPSED_LINE_COUNT}px`
  hasOverflow.value = content.getBoundingClientRect().height
    > lineHeight * COLLAPSED_LINE_COUNT + HEIGHT_TOLERANCE
}

function bindResizeObserver() {
  disconnectResizeObserver()

  if (!mounted || !props.enabled || !contentRef.value || !resizeObserver) {
    return
  }

  resizeObserver.observe(contentRef.value)
}

function toggleExpanded() {
  emit('update:expanded', !props.expanded)
}

watch(
  () => props.enabled,
  (enabled) => {
    if (!mounted) {
      return
    }

    if (!enabled) {
      collapsedHeight.value = '8.25em'
      hasOverflow.value = false
    }

    bindResizeObserver()
    measureContent()
  },
  { flush: 'post' },
)

onMounted(() => {
  mounted = true

  if (window.ResizeObserver) {
    resizeObserver = new window.ResizeObserver(() => measureContent())
  }

  bindResizeObserver()
  measureContent()
})

onUpdated(() => {
  if (!mounted || !props.enabled) {
    return
  }

  measureContent()
})

onBeforeUnmount(() => {
  mounted = false
  disconnectResizeObserver()
  resizeObserver = null
})
</script>

<style scoped lang="scss" src="../assets/css/CollapsibleMessageText.scss"></style>
