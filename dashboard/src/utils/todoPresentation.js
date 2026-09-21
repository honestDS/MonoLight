const TODO_STATUSES = new Set(['pending', 'in_progress', 'completed'])

export const normalizeTodoPlan = (plan) => {
  const revision = Number.isInteger(plan?.revision) && plan.revision >= 0 ? plan.revision : 0
  const todos = Array.isArray(plan?.todos)
    ? plan.todos
      .filter(item => item && typeof item.content === 'string' && TODO_STATUSES.has(item.status))
      .map(item => ({
        content: item.content.trim(),
        status: item.status
      }))
      .filter(item => item.content)
    : []

  return { revision, todos }
}

export const mergeTodoPlan = (currentPlan, incomingPlan) => {
  const current = normalizeTodoPlan(currentPlan)
  const incoming = normalizeTodoPlan(incomingPlan)
  return incoming.revision < current.revision ? current : incoming
}

export const readTodoPlanFromTransport = (payload) => {
  if (payload?.type === 'todo_update') return normalizeTodoPlan(payload)
  if (payload?.session_todo && typeof payload.session_todo === 'object') {
    return normalizeTodoPlan(payload.session_todo)
  }
  return null
}

export const summarizeTodoPlan = (plan) => {
  const normalized = normalizeTodoPlan(plan)
  const completed = normalized.todos.filter(item => item.status === 'completed').length
  const currentItem = normalized.todos.find(item => item.status === 'in_progress')?.content || null

  return {
    total: normalized.todos.length,
    completed,
    progressPercent: normalized.todos.length > 0
      ? Math.round((completed / normalized.todos.length) * 100)
      : 0,
    currentItem
  }
}
