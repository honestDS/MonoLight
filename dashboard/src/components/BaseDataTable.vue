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
    <div v-if="showFooter" class="table-footer">
      <div class="pagination-info">
        <slot name="info">
          <span>共 {{ total }} 条</span>
        </slot>
      </div>
      <div class="pagination-wrapper">
        <el-pagination
          :current-page="currentPage"
          :page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          :total="total"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
const props = defineProps({
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
  total: {
    type: Number,
    default: 0
  },
  pageSize: {
    type: Number,
    default: 10
  },
  currentPage: {
    type: Number,
    default: 1
  }
})

const emit = defineEmits(['create', 'refresh', 'page-change', 'size-change', 'update:currentPage', 'update:pageSize'])

const handleSizeChange = (val) => {
  emit('update:pageSize', val)
  emit('size-change', val)
}

const handleCurrentChange = (val) => {
  emit('update:currentPage', val)
  emit('page-change', val)
}
</script>

<style lang="scss" scoped>
@import "../assets/css/BaseDataTable.scss";
</style>
