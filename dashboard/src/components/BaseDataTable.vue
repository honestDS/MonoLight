<template>
  <div class="base-data-table">

    <!-- 表格操作按钮 -->
    <div class="table-actions">
      <el-button type="primary" size="default" @click="$emit('create')">{{ createText }}</el-button>
      <el-button size="default" @click="$emit('refresh')">{{ refreshText }}</el-button>
    </div>

    <!-- 数据表格 -->
    <div class="table-content">
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

    <!-- 表格底部信息 -->
    <div v-if="showFooter || $slots.pagination" class="table-footer">
      <div v-if="showFooter" class="pagination-info">
        <slot name="info">
          <span>显示 1-{{ Math.min(dataLength, pageSize) }} 条，共 {{ dataLength }} 条</span>
        </slot>
      </div>
      <div v-if="$slots.pagination" class="pagination-wrapper">
        <slot name="pagination"></slot>
      </div>
    </div>
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
  },
  showFooter: {
    type: Boolean,
    default: true
  },
  dataLength: {
    type: Number,
    default: 0
  },
  pageSize: {
    type: Number,
    default: 10
  }
})

defineEmits(['create', 'refresh'])
</script>

<style lang="scss" scoped>
@import "../assets/css/BaseDataTable.scss";
</style>
