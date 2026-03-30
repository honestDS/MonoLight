/**
 * 通用 CRUD 操作 composable
 * 提供列表加载、创建、更新、删除的通用逻辑
 */
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { useDeleteConfirm } from './useDeleteConfirm'

/**
 * 创建通用列表加载逻辑
 * @param {Function} apiFn - API 调用函数
 * @param {Object} options - 配置选项
 * @param {string} options.errorMsg - 错误消息
 * @param {Function} options.transform - 数据转换函数
 */
export function useListLoader(apiFn, options = {}) {
  const data = ref([])
  const loading = ref(false)

  const load = async () => {
    loading.value = true
    try {
      const res = await apiFn()
      const rawData = res.data?.data || []
      data.value = options.transform ? options.transform(rawData) : rawData
    } catch (err) {
      ElMessage.error(err.message || options.errorMsg || '加载失败')
    } finally {
      loading.value = false
    }
  }

  // 刷新操作
  const handleRefresh = () => {
    load()
  }

  // 删除操作
  const { handleDelete } = useDeleteConfirm(
    options.deleteApi,
    load
  )

  return {
    data,
    loading,
    load,
    handleRefresh,
    handleDelete
  }
}

/**
 * 创建通用表单操作逻辑
 * @param {Object} defaultForm - 默认表单数据
 */
export function useForm(defaultForm) {
  const dialogVisible = ref(false)
  const dialogType = ref('create')
  const submitting = ref(false)
  const form = reactive({ ...defaultForm })

  // 显示弹窗
  const showDialog = (type, row = null) => {
    dialogType.value = type
    if (type === 'edit' && row) {
      Object.keys(defaultForm).forEach(key => {
        form[key] = row[key] !== undefined ? row[key] : defaultForm[key]
      })
    } else {
      Object.assign(form, { ...defaultForm })
    }
    dialogVisible.value = true
  }

  // 关闭弹窗
  const closeDialog = () => {
    dialogVisible.value = false
  }

  // 重置表单
  const resetForm = () => {
    Object.assign(form, { ...defaultForm })
  }

  return {
    dialogVisible,
    dialogType,
    submitting,
    form,
    showDialog,
    closeDialog,
    resetForm
  }
}

/**
 * 完整的 CRUD 组合式
 * 结合列表加载和表单操作
 */
export function useCRUD(config) {
  const {
    listApi,
    createApi,
    updateApi,
    deleteApi,
    defaultForm = {},
    formTransform = (data) => data,
    onSuccess = () => {}
  } = config

  const { data, loading, load, handleRefresh, handleDelete } = useListLoader(listApi, {
    deleteApi,
    errorMsg: '加载失败'
  })

  const {
    dialogVisible,
    dialogType,
    submitting,
    form,
    showDialog,
    closeDialog,
    resetForm
  } = useForm(defaultForm)

  // 提交表单
  const submitForm = async () => {
    submitting.value = true
    try {
      const submitData = formTransform(form)
      if (dialogType.value === 'create') {
        await createApi(submitData)
        ElMessage.success('创建成功')
      } else {
        await updateApi(form.id, submitData)
        ElMessage.success('更新成功')
      }
      closeDialog()
      resetForm()
      load()
      onSuccess()
    } catch (err) {
      ElMessage.error(err.message || '提交失败')
    } finally {
      submitting.value = false
    }
  }

  return {
    data,
    loading,
    dialogVisible,
    dialogType,
    submitting,
    form,
    load,
    handleRefresh,
    handleDelete,
    showDialog,
    closeDialog,
    submitForm
  }
}