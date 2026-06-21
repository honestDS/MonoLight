import { ElMessageBox, ElMessage } from 'element-plus'
import i18n from '../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)

/**
 * 删除确认组合式函数
 */
export function useDeleteConfirm(
  apiDelete, // 删除 API 函数
  onSuccess, // 成功后的回调
  options = {} // 删除确认文案配置
) {
  const handleDelete = async (id, name, extraOptions = {}) => {
    const mergedOptions = { ...options, ...extraOptions }
    const confirmMessage = mergedOptions.message || t('common.delete_named_confirm', { name })
    try {
      await ElMessageBox.confirm(
        confirmMessage,
        mergedOptions.title || t('common.warning'),
        {
          type: 'warning',
          confirmButtonText: mergedOptions.confirmButtonText || t('common.confirm'),
          cancelButtonText: mergedOptions.cancelButtonText || t('common.cancel'),
          dangerouslyUseHTMLString: mergedOptions.dangerouslyUseHTMLString ?? true
        }
      )
      try {
        await apiDelete(id)
        ElMessage.success(mergedOptions.successMessage || t('common.delete_success'))
        onSuccess?.()
      } catch (err) {
        ElMessage.error(err.message || mergedOptions.errorMessage || t('common.delete_failed'))
      }
    } catch (err) {
      if (err !== 'cancel') {
        ElMessage.error(err.message || t('common.action_failed'))
      }
    }
  }

  return {
    handleDelete
  }
}