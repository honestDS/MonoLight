// ResizeObserver 帧合并补丁，避免循环告警且不丢失尺寸变化
let observerPatchApplied = false

export function useResizeObserver() {
  if (observerPatchApplied || typeof window === 'undefined' || !window.ResizeObserver) return

  const NativeResizeObserver = window.ResizeObserver
  window.ResizeObserver = class ResizeObserver extends NativeResizeObserver {
    constructor(callback) {
      let frameId = null
      const pendingEntries = new Map()

      super((entries, observer) => {
        entries.forEach(entry => pendingEntries.set(entry.target, entry))
        if (frameId !== null) return

        frameId = requestAnimationFrame(() => {
          frameId = null
          const batchedEntries = [...pendingEntries.values()]
          pendingEntries.clear()
          callback(batchedEntries, observer)
        })
      })

      this.cancelPendingCallback = () => {
        if (frameId !== null) cancelAnimationFrame(frameId)
        frameId = null
        pendingEntries.clear()
      }
    }

    disconnect() {
      this.cancelPendingCallback()
      super.disconnect()
    }
  }
  observerPatchApplied = true
}