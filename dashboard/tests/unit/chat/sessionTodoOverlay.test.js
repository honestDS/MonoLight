import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope, reactive } from 'vue'
import { createSessionTodoOverlay } from '../../../src/composables/chat/sessionTodoOverlay.js'
import {
  CLIENT_SETTINGS_STORAGE_KEY,
  getClientSetting
} from '../../../src/utils/clientSettings.js'

const createStorage = (settings = {}, options = {}) => {
  let value = options.raw ?? JSON.stringify(settings)
  let writes = 0

  return {
    getItem: key => {
      if (options.failRead) throw new Error('storage read blocked')
      return key === CLIENT_SETTINGS_STORAGE_KEY ? value : null
    },
    setItem: (key, nextValue) => {
      if (options.failWrite) throw new Error('storage write blocked')
      if (key === CLIENT_SETTINGS_STORAGE_KEY) {
        value = nextValue
        writes += 1
      }
    },
    raw: () => value,
    writes: () => writes
  }
}

const todoPlan = (items = [{ content: 'inspect session', status: 'in_progress' }]) => ({
  revision: 1,
  todos: items
})

const createHarness = (t, options = {}) => {
  const storage = options.storage ?? createStorage(options.settings)
  const props = reactive({
    plan: options.plan ?? null,
    sessionId: options.sessionId ?? null
  })
  const expandedChanges = []
  const scope = effectScope()
  const overlay = scope.run(() => createSessionTodoOverlay({
    props,
    emit: (name, value) => {
      if (name === 'expanded-change') expandedChanges.push(value)
    },
    storage
  }))

  t.after(() => scope.stop())

  return {
    ...overlay,
    expandedChanges,
    props,
    storage
  }
}

test('restores boolean collapse preferences and defaults invalid or missing values to collapsed', t => {
  for (const [settings, expectedCollapsed] of [
    [{ sessionTodoDrawerCollapsed: true }, true],
    [{ sessionTodoDrawerCollapsed: false }, false],
    [{}, true],
    [{ sessionTodoDrawerCollapsed: 'false' }, true]
  ]) {
    const harness = createHarness(t, {
      plan: todoPlan(),
      sessionId: 'session-a',
      settings
    })

    assert.equal(harness.collapsed.value, expectedCollapsed)
    assert.equal(harness.expanded.value, !expectedCollapsed)
    assert.deepEqual(harness.expandedChanges, [!expectedCollapsed])

    if (settings.sessionTodoDrawerCollapsed === 'false') {
      assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), 'false')
      assert.equal(harness.storage.writes(), 0)
    }
  }
})

test('session presence controls expansion independently of Todo count', t => {
  const emptySession = createHarness(t, {
    plan: todoPlan([]),
    sessionId: 'session-a',
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.equal(emptySession.summary.value.total, 0)
  assert.equal(emptySession.expanded.value, true)
  assert.deepEqual(emptySession.expandedChanges, [true])

  const todosWithoutSession = createHarness(t, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.equal(todosWithoutSession.expanded.value, false)
  assert.deepEqual(todosWithoutSession.expandedChanges, [false])
  todosWithoutSession.toggleCollapsed()
  todosWithoutSession.close()
  assert.equal(todosWithoutSession.collapsed.value, false)
  assert.equal(todosWithoutSession.storage.writes(), 0)
  assert.deepEqual(todosWithoutSession.expandedChanges, [false])
})

test('close and toggle emit immediately, while repeated close writes once', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    sessionId: 'session-a',
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true])

  harness.close()
  assert.equal(harness.collapsed.value, true)
  assert.equal(harness.expanded.value, false)
  assert.deepEqual(harness.expandedChanges, [true, false])
  assert.equal(harness.storage.writes(), 1)

  harness.close()
  assert.deepEqual(harness.expandedChanges, [true, false])
  assert.equal(harness.storage.writes(), 1)

  harness.toggleCollapsed()
  assert.equal(harness.collapsed.value, false)
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true, false, true])

  harness.close()
  assert.equal(harness.expanded.value, false)
  assert.deepEqual(harness.expandedChanges, [true, false, true, false])
  assert.equal(harness.storage.writes(), 3)
  harness.close()
  assert.equal(harness.storage.writes(), 3)
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), true)
})

test('a new session restores the selected state and a missing session cannot change it', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    sessionId: 'session-a',
    settings: { sessionTodoDrawerCollapsed: true }
  })

  assert.equal(harness.expanded.value, false)
  harness.toggleCollapsed()
  assert.equal(harness.collapsed.value, false)
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [false, true])
  assert.equal(harness.storage.writes(), 1)

  harness.props.sessionId = 'session-b'
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [false, true])

  harness.props.sessionId = null
  assert.equal(harness.collapsed.value, false)
  assert.equal(harness.expanded.value, false)
  assert.deepEqual(harness.expandedChanges, [false, true, false])

  harness.toggleCollapsed()
  harness.close()
  assert.equal(harness.collapsed.value, false)
  assert.equal(harness.storage.writes(), 1)
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), false)

  harness.props.sessionId = 'session-c'
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [false, true, false, true])
})

test('Todo additions, removals, and empty plans do not auto-collapse an open session', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    sessionId: 'session-a',
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.props.plan = todoPlan([])
  assert.equal(harness.summary.value.total, 0)
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true])

  harness.props.plan = todoPlan([
    { content: 'first task', status: 'completed' },
    { content: 'second task', status: 'pending' }
  ])
  assert.equal(harness.summary.value.total, 2)
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true])

  harness.props.plan = null
  assert.equal(harness.summary.value.total, 0)
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true])
})

test('dispose stops watching expansion inputs after notifying closed', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    sessionId: 'session-a',
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.dispose()
  assert.deepEqual(harness.expandedChanges, [true, false])

  harness.props.sessionId = null
  harness.props.plan = null
  harness.props.sessionId = 'session-b'
  assert.deepEqual(harness.expandedChanges, [true, false])
})

test('normalized plans and summaries remain available for invalid plan input', t => {
  const harness = createHarness(t, {
    plan: {
      revision: 'invalid',
      todos: [
        { content: '  valid task  ', status: 'pending' },
        { content: ' ', status: 'pending' },
        { content: 'wrong status', status: 'done' },
        null,
        { content: 42, status: 'pending' }
      ]
    },
    sessionId: 'session-a',
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.deepEqual(harness.normalizedPlan.value, {
    revision: 0,
    todos: [{ content: 'valid task', status: 'pending' }]
  })
  assert.deepEqual(harness.summary.value, {
    total: 1,
    completed: 0,
    progressPercent: 0,
    currentItem: null
  })
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true])
})

test('storage read and write failures remain safe', t => {
  const readFailure = createHarness(t, {
    plan: todoPlan(),
    sessionId: 'session-a',
    storage: createStorage({}, { failRead: true })
  })

  assert.equal(readFailure.collapsed.value, true)
  assert.equal(readFailure.expanded.value, false)
  assert.deepEqual(readFailure.expandedChanges, [false])

  const writeStorage = createStorage(
    { sessionTodoDrawerCollapsed: false },
    { failWrite: true }
  )
  const writeFailure = createHarness(t, {
    plan: todoPlan(),
    sessionId: 'session-a',
    storage: writeStorage
  })

  assert.doesNotThrow(() => writeFailure.close())
  assert.equal(writeFailure.collapsed.value, true)
  assert.equal(writeFailure.expanded.value, false)
  assert.equal(writeFailure.storage.writes(), 0)
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, writeFailure.storage), false)
  assert.deepEqual(writeFailure.expandedChanges, [true, false])

  assert.doesNotThrow(() => writeFailure.toggleCollapsed())
  assert.equal(writeFailure.collapsed.value, false)
  assert.equal(writeFailure.expanded.value, true)
  assert.equal(writeFailure.storage.writes(), 0)
  assert.deepEqual(writeFailure.expandedChanges, [true, false, true])
})

test('repeated plans and session identities do not notify or persist', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    sessionId: 'session-a',
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.props.plan = todoPlan()
  harness.props.plan = todoPlan()
  harness.props.sessionId = 'session-a'
  harness.props.sessionId = 'session-a'
  harness.props.sessionId = 'session-b'
  harness.props.sessionId = 'session-b'

  assert.deepEqual(harness.expandedChanges, [true])
  assert.equal(harness.storage.writes(), 0)
})
