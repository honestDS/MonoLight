import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileTemplate, parse } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import {
  CLIENT_SETTINGS_STORAGE_KEY,
  getClientSetting,
  setClientSetting
} from '../src/utils/clientSettings.js'
import { normalizeTodoPlan, summarizeTodoPlan } from '../src/utils/todoPresentation.js'

const panelPath = new URL('../src/components/SessionTodoPanel.vue', import.meta.url)

const runtime = {
  ...Vue,
  resolveComponent: name => ({ name }),
  resolveDirective: () => undefined,
  withDirectives: (node, directives) => {
    node.dirs = directives
    return node
  }
}

const vnodesOf = value => {
  if (value == null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return []
  if (typeof value === 'function') return vnodesOf(value())
  if (Array.isArray(value)) return value.flatMap(vnodesOf)
  if (!Vue.isVNode(value)) return []

  const children = []
  if (Array.isArray(value.children)) {
    children.push(...value.children.flatMap(vnodesOf))
  } else if (value.children && typeof value.children === 'object') {
    for (const child of Object.values(value.children)) children.push(...vnodesOf(child))
  }
  return [value, ...children]
}

const hasClass = (value, expected) => (
  typeof value === 'string' && value.split(/\s+/).includes(expected)
)

const createStorage = (settings = {}) => {
  let value = JSON.stringify(settings)
  let writes = 0

  return {
    getItem: key => key === CLIENT_SETTINGS_STORAGE_KEY ? value : null,
    setItem: (key, nextValue) => {
      if (key === CLIENT_SETTINGS_STORAGE_KEY) {
        value = nextValue
        writes += 1
      }
    },
    read: () => JSON.parse(value),
    writes: () => writes
  }
}

let implementationPromise
const loadImplementation = () => {
  implementationPromise ||= (async () => {
    const source = await readFile(panelPath, 'utf8')
    const parsed = parse(source, { filename: 'SessionTodoPanel.vue' })
    assert.equal(parsed.errors.length, 0, String(parsed.errors))

    const compiled = compileTemplate({
      source: parsed.descriptor.template.content,
      filename: 'SessionTodoPanel.vue',
      id: 'session-todo-overlay-test',
      compilerOptions: { mode: 'function' }
    })
    assert.equal(compiled.errors.length, 0, String(compiled.errors))

    const scriptSetup = parsed.descriptor.scriptSetup?.content
    assert.ok(scriptSetup, 'SessionTodoPanel script setup should be available')
    const scriptBody = scriptSetup.replace(/^\s*import .*$/gm, '')
    const setup = new Function(
      'computed',
      'onBeforeUnmount',
      'ref',
      'watch',
      'getClientSetting',
      'setClientSetting',
      'normalizeTodoPlan',
      'summarizeTodoPlan',
      'defineProps',
      'defineEmits',
      `${scriptBody}
return { props, normalizedPlan, summary, collapsed, expanded, handleDrawerAfterLeave, toggleCollapsed }`
    )

    return {
      render: new Function('Vue', compiled.code)(runtime),
      setup
    }
  })()
  return implementationPromise
}

const createHarness = async (t, implementation, options = {}) => {
  const storage = createStorage(options.settings)
  const props = Vue.reactive({
    plan: options.plan ?? null,
    suppressed: options.suppressed ?? false
  })
  const expandedChanges = []
  const unmountCallbacks = []
  const emit = (name, value) => {
    if (name === 'expanded-change') expandedChanges.push(value)
  }
  const scope = Vue.effectScope()
  t.after(() => scope.stop())

  const setupState = scope.run(() => implementation.setup(
    Vue.computed,
    callback => unmountCallbacks.push(callback),
    Vue.ref,
    Vue.watch,
    (key, fallback) => getClientSetting(key, fallback, storage),
    (key, value) => setClientSetting(key, value, storage),
    normalizeTodoPlan,
    summarizeTodoPlan,
    () => props,
    () => emit
  ))

  const context = Vue.proxyRefs({
    ...setupState,
    $t: key => key,
    CaretRight: { name: 'CaretRight' },
    CircleCheckFilled: { name: 'CircleCheckFilled' },
    Clock: { name: 'Clock' },
    List: { name: 'List' }
  })
  const render = () => vnodesOf(implementation.render(context, []))
  const node = predicate => render().find(predicate)
  const root = () => node(value => value.type === 'section' && hasClass(value.props?.class, 'session-todo-anchor'))
  const trigger = () => node(value => value.type === 'button' && hasClass(value.props?.class, 'session-todo-trigger'))
  const drawer = () => node(value => hasClass(value.props?.class, 'session-todo-drawer'))
  const transition = () => node(value => typeof value.props?.onAfterLeave === 'function')
  const showValue = () => {
    const directive = drawer()?.dirs?.find(([value]) => value === Vue.vShow)
    return directive?.[1]
  }

  return {
    context,
    drawer,
    expandedChanges,
    props,
    root,
    showValue,
    storage,
    trigger,
    transition,
    unmount: () => unmountCallbacks.forEach(callback => callback())
  }
}

const todoPlan = (items = [{ content: 'inspect session', status: 'in_progress' }]) => ({
  revision: 1,
  todos: items
})

test('SessionTodoPanel restores its collapsed preference and exposes the VNode contract', async t => {
  const implementation = await loadImplementation()

  for (const [storedCollapsed, expectedExpanded] of [[true, false], [false, true]]) {
    const harness = await createHarness(t, implementation, {
      plan: todoPlan(),
      settings: { sessionTodoDrawerCollapsed: storedCollapsed }
    })
    const trigger = harness.trigger()
    const drawer = harness.drawer()

    assert.ok(trigger)
    assert.ok(drawer)
    assert.equal(trigger.props['aria-expanded'], expectedExpanded)
    assert.equal(trigger.props.disabled, false)
    assert.equal(trigger.props['aria-controls'], 'session-todo-list')
    assert.equal(harness.showValue(), expectedExpanded)
    assert.deepEqual(harness.expandedChanges, expectedExpanded ? [true] : [])
  }
})

test('SessionTodoPanel holds occupancy through collapse and ignores a stale leave after reopening', async t => {
  const implementation = await loadImplementation()
  const harness = await createHarness(t, implementation, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  assert.deepEqual(harness.expandedChanges, [true])
  const leaveAfterCollapse = harness.transition().props.onAfterLeave
  harness.trigger().props.onClick()
  assert.deepEqual(harness.expandedChanges, [true])
  assert.equal(harness.storage.writes(), 1)

  leaveAfterCollapse()
  assert.deepEqual(harness.expandedChanges, [true, false])

  const lateLeave = harness.transition().props.onAfterLeave
  harness.trigger().props.onClick()
  assert.deepEqual(harness.expandedChanges, [true, false, true])
  lateLeave()
  assert.deepEqual(harness.expandedChanges, [true, false, true])
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), false)
  assert.equal(harness.storage.writes(), 2)
})

test('SessionTodoPanel suppresses immediately without changing the stored expanded preference', async t => {
  const implementation = await loadImplementation()
  const harness = await createHarness(t, implementation, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.props.suppressed = true
  assert.deepEqual(harness.expandedChanges, [true, false])
  assert.equal(harness.trigger().props.disabled, true)
  assert.equal(harness.trigger().props['aria-expanded'], false)
  assert.equal(harness.showValue(), false)

  const writesBeforeToggle = harness.storage.writes()
  harness.trigger().props.onClick()
  assert.equal(harness.storage.writes(), writesBeforeToggle)
  assert.equal(getClientSetting('sessionTodoDrawerCollapsed', null, harness.storage), false)

  harness.props.suppressed = false
  assert.deepEqual(harness.expandedChanges, [true, false, true])
  assert.equal(harness.trigger().props.disabled, false)
  assert.equal(harness.trigger().props['aria-expanded'], true)
  assert.equal(harness.showValue(), true)
})

test('SessionTodoPanel releases immediately when no todo plan is mounted or tasks become empty', async t => {
  const implementation = await loadImplementation()
  const missingPlan = await createHarness(t, implementation, {
    settings: { sessionTodoDrawerCollapsed: false }
  })
  assert.deepEqual(missingPlan.expandedChanges, [false])
  assert.equal(missingPlan.root(), undefined)
  assert.equal(missingPlan.trigger(), undefined)

  const harness = await createHarness(t, implementation, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })
  assert.deepEqual(harness.expandedChanges, [true])
  harness.props.plan = todoPlan([])
  assert.deepEqual(harness.expandedChanges, [true, false])
  assert.equal(harness.root(), undefined)
  assert.equal(harness.trigger(), undefined)
})

test('SessionTodoPanel emits false before unmount cleanup completes', async t => {
  const implementation = await loadImplementation()
  const harness = await createHarness(t, implementation, {
    plan: todoPlan(),
    settings: { sessionTodoDrawerCollapsed: false }
  })

  harness.unmount()
  assert.deepEqual(harness.expandedChanges, [true, false])
})
