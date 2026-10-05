import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope, reactive } from 'vue'
import { createSessionTodoOverlay } from '../src/composables/chat/sessionTodoOverlay.js'
import {
  CLIENT_SETTINGS_STORAGE_KEY,
  getClientSetting
} from '../src/utils/clientSettings.js'

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
    suppressed: options.suppressed ?? false
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

test('SessionTodoPanel restores a boolean collapsed preference', t => {
  for (const [storedCollapsed, expectedExpanded] of [[true, false], [false, true]]) {
    const harness = createHarness(t, {
      plan: todoPlan(),
      settings: { sessionTodoDrawerCollapsed: storedCollapsed }
    })

    assert.equal(harness.collapsed.value, storedCollapsed)
    assert.equal(harness.expanded.value, expectedExpanded)
    assert.deepEqual(harness.expandedChanges, expectedExpanded ? [true] : [])
  }
})

test('SessionTodoPanel releases collapse occupancy only after leave and ignores a stale leave after reopening', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true])

  harness.toggleCollapsed()
  assert.equal(harness.collapsed.value, true)
  assert.equal(harness.expanded.value, false)
  assert.deepEqual(harness.expandedChanges, [true])
  assert.equal(harness.storage.writes(), 1)

  const leaveAfterCollapse = harness.handleDrawerAfterLeave
  leaveAfterCollapse()
  assert.deepEqual(harness.expandedChanges, [true, false])

  harness.toggleCollapsed()
  assert.equal(harness.collapsed.value, false)
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true, false, true])

  const staleLeave = harness.handleDrawerAfterLeave
  staleLeave()
  assert.deepEqual(harness.expandedChanges, [true, false, true])
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), false)
  assert.equal(harness.storage.writes(), 2)
})

test('SessionTodoPanel suppresses immediately without changing the stored preference or allowing a toggle', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.props.suppressed = true
  assert.equal(harness.expanded.value, false)
  assert.deepEqual(harness.expandedChanges, [true, false])

  const writesBeforeToggle = harness.storage.writes()
  harness.toggleCollapsed()
  assert.equal(harness.collapsed.value, false)
  assert.equal(harness.storage.writes(), writesBeforeToggle)
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), false)

  harness.props.suppressed = true
  assert.deepEqual(harness.expandedChanges, [true, false])
  harness.props.suppressed = false
  assert.equal(harness.expanded.value, true)
  assert.deepEqual(harness.expandedChanges, [true, false, true])
})

test('SessionTodoPanel releases immediately for a missing plan or empty todos', t => {
  const missingPlan = createHarness(t, {
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.equal(missingPlan.expanded.value, false)
  assert.equal(missingPlan.summary.value.total, 0)
  assert.deepEqual(missingPlan.expandedChanges, [false])

  const harness = createHarness(t, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.deepEqual(harness.expandedChanges, [true])
  harness.props.plan = todoPlan([])
  assert.equal(harness.expanded.value, false)
  assert.equal(harness.summary.value.total, 0)
  assert.deepEqual(harness.expandedChanges, [true, false])
})

test('SessionTodoPanel emits false on dispose and stops watching props', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.dispose()
  assert.deepEqual(harness.expandedChanges, [true, false])

  harness.props.suppressed = true
  harness.props.plan = null
  assert.deepEqual(harness.expandedChanges, [true, false])
})

test('SessionTodoPanel falls back to collapsed for an invalid stored preference', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: 'false' }
  })

  assert.equal(harness.collapsed.value, true)
  assert.equal(harness.expanded.value, false)
  assert.deepEqual(harness.expandedChanges, [])
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), 'false')
  assert.equal(harness.storage.writes(), 0)
})

test('SessionTodoPanel exposes the normalized plan and summary for invalid plan input', t => {
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

test('SessionTodoPanel tolerates storage read and write failures', t => {
  const readFailure = createHarness(t, {
    plan: todoPlan(),
    storage: createStorage({}, { failRead: true })
  })

  assert.equal(readFailure.collapsed.value, true)
  assert.equal(readFailure.expanded.value, false)
  assert.deepEqual(readFailure.expandedChanges, [])

  const writeStorage = createStorage(
    { sessionTodoDrawerCollapsed: false },
    { failWrite: true }
  )
  const writeFailure = createHarness(t, {
    plan: todoPlan(),
    storage: writeStorage
  })

  assert.doesNotThrow(() => writeFailure.toggleCollapsed())
  assert.equal(writeFailure.collapsed.value, true)
  assert.equal(writeFailure.expanded.value, false)
  assert.equal(writeFailure.storage.writes(), 0)
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, writeFailure.storage), false)
  assert.deepEqual(writeFailure.expandedChanges, [true])
})

test('SessionTodoPanel does not notify or write for repeated plan and suppression values', t => {
  const harness = createHarness(t, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.props.plan = todoPlan()
  harness.props.plan = todoPlan()
  harness.props.suppressed = false
  harness.props.suppressed = false

  assert.deepEqual(harness.expandedChanges, [true])
  assert.equal(harness.storage.writes(), 0)

  harness.props.suppressed = true
  harness.props.suppressed = true
  assert.deepEqual(harness.expandedChanges, [true, false])
  harness.props.suppressed = false
  harness.props.suppressed = false
  assert.deepEqual(harness.expandedChanges, [true, false, true])
  assert.equal(harness.storage.writes(), 0)

  harness.toggleCollapsed()
  harness.toggleCollapsed()
  assert.equal(harness.collapsed.value, false)
  assert.equal(harness.storage.writes(), 2)
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), false)
  assert.deepEqual(harness.expandedChanges, [true, false, true, true])
})
