<template>
  <div class="base-data-table">

    <!-- 表格操作按钮 -->
    <div class="table-actions">
      <el-button v-if="resolvedCreateText" type="primary" size="default" @click="$emit('create')">{{ resolvedCreateText }}</el-button>
      <el-button v-if="resolvedRefreshText" size="default" @click="$emit('refresh')">{{ resolvedRefreshText }}</el-button>
    </div>

    <!-- 数据表格 -->
    <div class="table-content">
      <el-table
        :data="data"
        v-loading="loading"
        border
        stripe
        size="default"
        :empty-text="resolvedEmptyText">
        <slot></slot>
      </el-table>
    </div>

    <!-- 表格底部信息 -->
    <div v-if="showFooter" class="table-footer">
      <div class="pagination-info">
        <slot name="info">
          <span>{{ totalText }}</span>
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
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

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
    default: undefined
  },
  refreshText: {
    type: String,
    default: undefined
  },
  emptyText: {
    type: String,
    default: undefined
  },
  totalText: {
    type: String,
    default: ''
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

const resolvedCreateText = computed(() => props.createText === undefined ? t('common.create') : props.createText)
const resolvedRefreshText = computed(() => props.refreshText === undefined ? t('common.refresh') : props.refreshText)
const resolvedEmptyText = computed(() => props.emptyText === undefined ? t('common.no_data') : props.emptyText)

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
