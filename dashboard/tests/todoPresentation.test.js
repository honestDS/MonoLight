import assert from 'node:assert/strict'
import test from 'node:test'

import {
  mergeTodoPlan,
  normalizeTodoPlan,
  readTodoPlanFromTransport,
  summarizeTodoPlan
} from '../src/utils/todoPresentation.js'

test('todo presentation normalizes valid items and ignores malformed ones', () => {
  const plan = normalizeTodoPlan({
    revision: 3,
    todos: [
      { content: '  inspect current chat layout  ', status: 'completed' },
      { content: 'build todo panel', status: 'in_progress' },
      { content: '', status: 'pending' },
      { content: 'invalid status', status: 'blocked' }
    ]
  })

  assert.deepEqual(plan, {
    revision: 3,
    todos: [
      { content: 'inspect current chat layout', status: 'completed' },
      { content: 'build todo panel', status: 'in_progress' }
    ]
  })
})

test('todo presentation reports completed progress and current item', () => {
  const summary = summarizeTodoPlan({
    revision: 2,
    todos: [
      { content: 'first', status: 'completed' },
      { content: 'second', status: 'in_progress' },
      { content: 'third', status: 'pending' },
      { content: 'fourth', status: 'completed' }
    ]
  })

  assert.deepEqual(summary, {
    total: 4,
    completed: 2,
    progressPercent: 50,
    currentItem: 'second'
  })
})

test('todo presentation returns an empty summary for missing plans', () => {
  assert.deepEqual(summarizeTodoPlan(null), {
    total: 0,
    completed: 0,
    progressPercent: 0,
    currentItem: null
  })
})

test('todo presentation ignores stale transport revisions', () => {
  const current = {
    revision: 4,
    todos: [{ content: 'current', status: 'in_progress' }]
  }

  assert.deepEqual(mergeTodoPlan(current, {
    revision: 3,
    todos: [{ content: 'stale', status: 'completed' }]
  }), current)

  assert.deepEqual(mergeTodoPlan(current, {
    revision: 5,
    todos: [{ content: 'next', status: 'completed' }]
  }), {
    revision: 5,
    todos: [{ content: 'next', status: 'completed' }]
  })
})

test('todo presentation reads stream events and non-stream responses', () => {
  assert.deepEqual(readTodoPlanFromTransport({
    type: 'todo_update',
    revision: 2,
    todos: [{ content: 'streamed', status: 'in_progress' }]
  }), {
    revision: 2,
    todos: [{ content: 'streamed', status: 'in_progress' }]
  })

  assert.deepEqual(readTodoPlanFromTransport({
    session_todo: {
      revision: 3,
      todos: [{ content: 'http', status: 'completed' }]
    }
  }), {
    revision: 3,
    todos: [{ content: 'http', status: 'completed' }]
  })
})
