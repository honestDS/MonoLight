import { ElMessage } from 'element-plus'
import { useConfirmDialog } from './useConfirmDialog.js'
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
  const { confirm } = useConfirmDialog()

  const handleDelete = async (id, name, extraOptions = {}) => {
    const mergedOptions = { ...options, ...extraOptions }
    const confirmMessage = mergedOptions.message || t('common.delete_named_confirm', { name })
    try {
      if (!await confirm({
        message: confirmMessage,
        title: mergedOptions.title || t('common.warning'),
        confirmButtonText: mergedOptions.confirmButtonText || t('common.confirm'),
        cancelButtonText: mergedOptions.cancelButtonText || t('common.cancel')
      })) return false
      try {
        await apiDelete(id)
        ElMessage.success(mergedOptions.successMessage || t('common.delete_success'))
        onSuccess?.()
        return true
      } catch (err) {
        ElMessage.error(err.message || mergedOptions.errorMessage || t('common.delete_failed'))
        return false
      }
    } catch (err) {
      ElMessage.error(err.message || t('common.action_failed'))
      return false
    }
  }

  return {
    handleDelete
  }
}
