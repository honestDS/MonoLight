import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import { receiveWs, selectSession } from './sessionCompositionHarness.js'

test('composed session applies only current-session todo revisions', async t => {
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', { source: 'ws' }),
      createSession('B', { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()

  const currentTodos = [
    { content: 'compose the session', status: 'in_progress' }
  ]
  const currentEvent = {
    type: 'todo_update',
    session_id: 'A',
    revision: 3,
    todos: currentTodos
  }
  await receiveWs(harness, currentEvent)

  assert.deepEqual(chat.currentTodoPlan.value?.todos, currentTodos)
  const firstTodos = JSON.stringify(chat.currentTodoPlan.value?.todos)

  await receiveWs(harness, { ...currentEvent, todos: [...currentTodos] })
  assert.equal(JSON.stringify(chat.currentTodoPlan.value?.todos), firstTodos)

  await receiveWs(harness, {
    ...currentEvent,
    revision: 2,
    todos: [{ content: 'stale', status: 'completed' }]
  })
  await receiveWs(harness, {
    ...currentEvent,
    session_id: 'B',
    revision: 99,
    todos: [{ content: 'foreign', status: 'completed' }]
  })
  assert.equal(JSON.stringify(chat.currentTodoPlan.value?.todos), firstTodos)

  await selectSession(harness, 'B')
  assert.equal(
    (chat.currentTodoPlan.value?.todos ?? []).some(todo => todo.content === 'compose the session'),
    false
  )
  await receiveWs(harness, {
    ...currentEvent,
    revision: 1,
    todos: [{ content: 'old A', status: 'in_progress' }]
  })
  assert.equal(
    (chat.currentTodoPlan.value?.todos ?? []).some(todo => todo.content === 'old A'),
    false
  )
})
