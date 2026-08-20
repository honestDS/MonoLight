<script setup>
import { useI18n } from 'vue-i18n'
import UnderConstruction from '../components/UnderConstruction.vue'

const { t } = useI18n()

const props = defineProps({
  titleKey: {
    type: String,
    required: true,
  },
  descriptionKey: {
    type: String,
    required: true,
  },
})
</script>

<template>
  <UnderConstruction
    :title="t(props.titleKey)"
    :description="t(props.descriptionKey)"
    :status-text="t('common.construction.status')"
  />
</template>
