export const createAbortableTaskManager = () => {
  let generation = 0
  let requestId = 0
  const activeTasks = new Map()

  const begin = (key) => {
    if (activeTasks.has(key)) return null

    const controller = new AbortController()
    const token = Object.freeze({
      key,
      generation,
      requestId: ++requestId,
      controller,
      signal: controller.signal
    })
    activeTasks.set(key, token)
    return token
  }

  const isCurrent = (token) => (
    token?.generation === generation && activeTasks.get(token.key) === token
  )

  const finish = (token) => {
    if (!isCurrent(token)) return false
    activeTasks.delete(token.key)
    return true
  }

  const cancel = (key) => {
    const token = activeTasks.get(key)
    if (!token) return false
    activeTasks.delete(key)
    token.controller.abort()
    return true
  }

  const invalidate = () => {
    generation += 1
    const tasks = [...activeTasks.values()]
    activeTasks.clear()
    for (const token of tasks) token.controller.abort()
  }

  return {
    begin,
    isCurrent,
    finish,
    cancel,
    invalidate,
    isRunning: (key) => activeTasks.has(key),
    activeCount: () => activeTasks.size
  }
}

export const createChannelTestManager = createAbortableTaskManager
