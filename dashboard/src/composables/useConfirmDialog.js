import { h } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import ConfirmDialogContent from '../components/ConfirmDialogContent.vue'

export function useConfirmDialog() {
  const { t } = useI18n()

  const confirm = async ({
    message = '',
    sections = [],
    title = t('common.warning'),
    confirmButtonText = t('common.confirm'),
    cancelButtonText = t('common.cancel'),
    type = 'warning',
    closeOnClickModal = true
  } = {}) => {
    try {
      await ElMessageBox.confirm(
        h(ConfirmDialogContent, { message, sections }),
        title,
        {
          type,
          confirmButtonText,
          cancelButtonText,
          showCancelButton: true,
          dangerouslyUseHTMLString: false,
          customClass: 'app-confirm-dialog',
          closeOnClickModal
        }
      )
      return true
    } catch (action) {
      if (action === 'cancel' || action === 'close') {
        return false
      }
      throw action
    }
  }

  return { confirm }
}
