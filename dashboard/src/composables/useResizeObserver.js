/**
 * ResizeObserver 防抖补丁
 * 修复 ResizeObserver loop completed with undelivered notifications 错误
 */
import { debounce } from '../utils'

let observerPatchApplied = false

export function useResizeObserver() {
  // 确保只应用一次补丁
  if (!observerPatchApplied && typeof window !== 'undefined') {
    const _ResizeObserver = window.ResizeObserver
    window.ResizeObserver = class ResizeObserver extends _ResizeObserver {
      constructor(callback) {
        callback = debounce(callback, 16)
        super(callback)
      }
    }
    observerPatchApplied = true
  }
}