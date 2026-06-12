<template>
  <el-tag :type="tagType" size="default">{{ text }}</el-tag>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = defineProps({
  status: {
    type: [Boolean, Number, String],
    default: false
  },
  activeText: {
    type: String,
    default: ''
  },
  inactiveText: {
    type: String,
    default: ''
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
  const defActive = t('common.status.enable')
  const defInactive = t('common.status.disable')
  return isActive ? (props.activeText || defActive) : (props.inactiveText || defInactive)
})
</script>