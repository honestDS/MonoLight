<template>
  <el-tag :type="tagType" size="default">{{ text }}</el-tag>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: {
    type: [Boolean, Number, String],
    default: false
  },
  activeText: {
    type: String,
    default: '启用'
  },
  inactiveText: {
    type: String,
    default: '禁用'
  },
  activeType: {
    type: String,
    default: 'success'
  },
  inactiveType: {
    type: String,
    default: 'info'
  }
})

const tagType = computed(() => {
  const isActive = props.status === true || props.status === 1 || props.status === 'true' || props.status === 'active'
  return isActive ? props.activeType : props.inactiveType
})

const text = computed(() => {
  const isActive = props.status === true || props.status === 1 || props.status === 'true' || props.status === 'active'
  return isActive ? props.activeText : props.inactiveText
})
</script>