<template>
  <div class="base-data-table">
    <!-- 表格头部信息 -->
    <div v-if="showHeader" class="table-header">
      <div class="table-info">
        <span>显示 1-{{ Math.min(dataLength, pageSize) }} 条，共 {{ dataLength }} 条</span>
      </div>
    </div>

    <!-- 表格操作按钮 -->
    <div class="table-actions">
      <el-button type="primary" size="default" @click="$emit('create')">{{ createText }}</el-button>
      <el-button size="default" @click="$emit('refresh')">{{ refreshText }}</el-button>
    </div>

    <!-- 数据表格 -->
    <el-table
      :data="data"
      v-loading="loading"
      border
      stripe
      size="default"
      :empty-text="emptyText">
      <slot></slot>
    </el-table>

    <!-- 表格底部信息 -->
    <div v-if="showHeader" class="table-footer">
      <div class="pagination-info">
        <span>显示 1-{{ Math.min(dataLength, pageSize) }} 条，共 {{ dataLength }} 条</span>
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
  showHeader: {
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
.base-data-table {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  border: 1px solid var(--color-border-light);
  border-radius: 4px;
  overflow: hidden;

  .table-header {
    background: var(--color-info);
    padding: 16px 24px;
    border-bottom: 1px solid var(--color-border-light);
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;

    .table-info {
      font-size: 13px;
      color: #FFF;
    }
  }

  .table-actions {
    display: flex;
    gap: 12px;
    padding: 16px 24px;
    background: var(--color-bg-page);
    flex-shrink: 0;
  }

  .el-table {
    flex: 1;
    min-height: 200px;
  }

  .table-footer {
    background: var(--color-info);
    padding: 16px 24px;
    border-top: 1px solid var(--color-border-light);
    flex-shrink: 0;

    .pagination-info {
      font-size: 13px;
      color: #FFF;
    }
  }
}
</style>