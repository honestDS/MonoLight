export function createKnowledgeBaseScope({
  isActive = () => true,
  confirm,
  closeConfirmations,
}) {
  let generation = 0
  const pending = new Set()

  function capture() {
    const capturedGeneration = generation
    return () => generation === capturedGeneration && isActive()
  }

  async function confirmInScope(message, title, options) {
    const isCurrent = capture()
    if (!isCurrent()) return false

    let cancel
    const invalidated = new Promise((resolve) => {
      cancel = () => resolve(false)
      pending.add(cancel)
    })

    try {
      await Promise.race([confirm(message, title, options), invalidated])
      return isCurrent()
    } catch (error) {
      if (!isCurrent() || error === 'cancel' || error === 'close') {
        return false
      }
      throw error
    } finally {
      pending.delete(cancel)
    }
  }

  function invalidate() {
    generation += 1
    const cancellations = [...pending]
    pending.clear()
    for (const cancel of cancellations) cancel()
    if (cancellations.length > 0) closeConfirmations()
  }

  return {
    capture,
    confirm: confirmInScope,
    invalidate,
  }
}
