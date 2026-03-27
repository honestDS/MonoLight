<template>
  <div class="view-container">
    <div class="page-header-actions">
      <el-button type="primary" size="default" @click="$emit('create')">{{ createText }}</el-button>
      <el-button size="default" @click="$emit('refresh')">{{ refreshText }}</el-button>
    </div>

    <el-table
      :data="data"
      v-loading="loading"
      border
      stripe
      size="default"
      :empty-text="emptyText">
      <slot></slot>
    </el-table>
  </div>
</template>

<script setup>
defineProps({
  data: {
    type: Array,
    default: () => []
  },
  loading: {
    type: Boolean,
    default: false
  },
  createText: {
    type: String,
    default: '新建'
  },
  refreshText: {
    type: String,
    default: '刷新列表'
  },
  emptyText: {
    type: String,
    default: '暂无数据'
  }
})

defineEmits(['create', 'refresh'])
</script>

<style lang="scss" scoped>
.view-container {
  padding: 20px;
}

.page-header-actions {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
}
</style>