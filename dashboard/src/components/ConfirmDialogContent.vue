<template>
  <div class="confirm-dialog-content">
    <p v-if="message" class="confirm-dialog-content__message">{{ message }}</p>
    <section
      v-for="(section, index) in sections"
      :key="index"
      class="confirm-dialog-content__section"
    >
      <p v-if="section.summary" class="confirm-dialog-content__summary">{{ section.summary }}</p>
      <div v-if="section.items && section.items.length" class="confirm-dialog-content__items">
        <div v-for="(item, itemIndex) in section.items" :key="itemIndex">{{ item }}</div>
      </div>
      <p v-if="section.more" class="confirm-dialog-content__more">{{ section.more }}</p>
    </section>
  </div>
</template>

<script setup>
defineProps({
  message: {
    type: String,
    default: ''
  },
  sections: {
    type: Array,
    default: () => []
  }
})
</script>

<style lang="scss">
@import "../assets/css/ConfirmDialog.scss";
</style>
