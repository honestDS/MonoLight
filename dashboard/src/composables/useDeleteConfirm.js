import { ElMessageBox, ElMessage } from 'element-plus'

/**
 * 删除确认组合式函数
 * @param {Function} apiDelete - 删除API函数
 * @param {Function} onSuccess - 成功后的回调（通常是刷新列表）
 */
export function useDeleteConfirm(apiDelete, onSuccess) {
  const handleDelete = async (id, name) => {
    try {
      await ElMessageBox.confirm(
        `确定要删除 <span style="color: #F56C6C; font-weight: bold;">${name}</span> 吗？`,
        '警告',
        {
          type: 'warning',
          confirmButtonText: '确定',
          cancelButtonText: '取消',
          dangerouslyUseHTMLString: true
        }
      )
      try {
        await apiDelete(id)
        ElMessage.success('删除成功')
        onSuccess?.()
      } catch (err) {
        ElMessage.error(err.message || '删除失败')
      }
    } catch (err) {
      if (err !== 'cancel') {
        ElMessage.error(err.message || '操作失败')
      }
    }
  }

  return {
    handleDelete
  }
}