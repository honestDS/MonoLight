import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { createSessionSubagentBoard } from '../../../src/composables/chat/sessionSubagentBoard.js'
import { createSessionTodoOverlay } from '../../../src/composables/chat/sessionTodoOverlay.js'

const panelSource = readFileSync(
  new URL('../../../src/components/SessionTodoPanel.vue', import.meta.url),
  'utf8'
)
const { descriptor } = parse(panelSource)
const compiledPanel = compileScript(descriptor, {
  id: 'session-task-panel-rendering-test',
  inlineTemplate: true,
  templateOptions: {
    compilerOptions: { hoistStatic: false }
  }
})

const icon = { setup: () => () => Vue.h('i') }
const locale = Vue.ref('en')
const t = key => key
const api = {
  sessionSubagents: () => {
    throw new Error('unexpected request while document hidden')
  },
  sessionSubagentDetail: () => {
    throw new Error('unexpected request while document hidden')
  }
}

const injectedImports = {
  vue: { ...Vue, Transition: Vue.BaseTransition },
  'vue-i18n': { useI18n: () => ({ t, locale }) },
  '@element-plus/icons-vue': {
    ArrowLeft: icon,
    CaretRight: icon,
    CircleCheckFilled: icon,
    CircleCloseFilled: icon,
    Clock: icon,
    Loading: icon,
    WarningFilled: icon
  },
  '../api/index.js': { chatApi: api },
  '../composables/chat/sessionSubagentBoard.js': { createSessionSubagentBoard },
  '../composables/chat/sessionTodoOverlay.js': { createSessionTodoOverlay },
  '../composables/chat/useSessionTasks.js': { SESSION_ACTIVITY_KEY: 'sessionActivity' }
}

const panelCode = compiledPanel.content
  .replace(
    /import\s*\{([\s\S]*?)\}\s*from\s*(['"])([^'"]+)\2\s*;?/g,
    (_match, specifiers, _quote, moduleName) => {
      assert.ok(injectedImports[moduleName], `missing injected import: ${moduleName}`)
      const bindings = specifiers
        .split(',')
        .map(specifier => specifier.trim())
        .filter(Boolean)
        .map(specifier => specifier.replace(/\s+as\s+/, ': '))
        .join(', ')
      return `const { ${bindings} } = __imports[${JSON.stringify(moduleName)}];\n`
    }
  )
  .replace(/export\s+default\s+/, 'const SessionTodoPanel = ')

const createPanelComponent = (
  createBoard = createSessionSubagentBoard,
  documentHidden = true
) => vm.runInNewContext(
  `${panelCode}\nSessionTodoPanel`,
  {
    Vue,
    __imports: {
      ...injectedImports,
      '../composables/chat/sessionSubagentBoard.js': {
        createSessionSubagentBoard: createBoard
      }
    },
    document: {
      hidden: documentHidden,
      addEventListener() {},
      removeEventListener() {}
    }
  },
  { filename: 'SessionTodoPanel.vue' }
)

const SessionTodoPanel = createPanelComponent()

const createHost = () => {
  const root = {
    kind: 'root',
    children: [],
    parent: null
  }

  const isElement = node => node?.kind === 'element'
  const matches = (node, selector) => {
    if (!isElement(node)) return false
    if (selector.startsWith('#')) return node.attrs.id === selector.slice(1)
    if (selector.startsWith('.')) {
      return String(node.attrs.class || '').split(/\s+/).includes(selector.slice(1))
    }
    return node.type === selector
  }
  const collect = (node, selector, matchesFound) => {
    for (const child of node.children || []) {
      if (matches(child, selector)) matchesFound.push(child)
      collect(child, selector, matchesFound)
    }
  }
  const querySelectorAll = function (selector) {
    const matchesFound = []
    collect(this, selector, matchesFound)
    return matchesFound
  }
  const querySelector = function (selector) {
    return querySelectorAll.call(this, selector)[0] || null
  }
  const rootQuerySelector = selector => querySelector.call(root, selector)

  root.querySelector = rootQuerySelector
  root.querySelectorAll = querySelectorAll

  const createElement = type => {
    const element = {
      kind: 'element',
      type,
      children: [],
      parent: null,
      style: { display: '' },
      attrs: Object.create(null),
      listeners: Object.create(null)
    }
    element.querySelector = selector => querySelector.call(element, selector)
    element.querySelectorAll = querySelectorAll
    element.focus = () => {}
    return element
  }
  const createText = text => ({ kind: 'text', text: String(text), parent: null })
  const createComment = text => ({ kind: 'comment', text: String(text), parent: null })
  const insert = (child, parent, anchor = null) => {
    if (child === anchor) return
    if (child.parent) {
      const oldIndex = child.parent.children.indexOf(child)
      child.parent.children.splice(oldIndex, 1)
    }
    const index = anchor === null ? parent.children.length : parent.children.indexOf(anchor)
    parent.children.splice(index, 0, child)
    child.parent = parent
  }
  const remove = node => {
    const parent = node.parent
    if (!parent) throw new Error('cannot remove a node without a parent')
    const index = parent.children.indexOf(node)
    parent.children.splice(index, 1)
    node.parent = null
  }
  const setElementText = (element, text) => {
    for (const child of element.children) child.parent = null
    element.children = []
    element.textContent = String(text)
  }
  const patchProp = (element, key, _previous, next) => {
    if (key === 'textContent') {
      setElementText(element, next ?? '')
      return
    }
    if (key === 'style') {
      if (next && typeof next === 'object') Object.assign(element.style, next)
      return
    }
    if (key.startsWith('on') && key.length > 2 && key[2] === key[2].toUpperCase()) {
      const eventName = key.slice(2).toLowerCase()
      if (next == null) delete element.listeners[eventName]
      else element.listeners[eventName] = next
      return
    }
    if (next == null) delete element.attrs[key]
    else element.attrs[key] = String(next)
  }

  return {
    root,
    renderer: Vue.createRenderer({
      createElement,
      createText,
      createComment,
      setText: (node, text) => {
        node.text = String(text)
      },
      setElementText,
      parentNode: node => node.parent,
      nextSibling: node => {
        const parent = node.parent
        if (!parent) return null
        const index = parent.children.indexOf(node)
        if (index === -1) throw new Error('parent does not contain node')
        return parent.children[index + 1] || null
      },
      insert,
      remove,
      patchProp,
      setScopeId: () => {},
      querySelector: rootQuerySelector
    })
  }
}

const passthrough = (tag = 'div') => ({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) {
    return () => Vue.h(tag, attrs, slots.default ? slots.default() : null)
  }
})

const mountScenario = async (initialSessionId, panelComponent = SessionTodoPanel) => {
  const { root, renderer } = createHost()
  const state = Vue.reactive({ sessionId: initialSessionId })
  const activitySnapshot = Vue.shallowRef(initialSessionId
    ? {
        activities: [{
          session_id: initialSessionId,
          subagent_board_revision: 0
        }]
      }
    : null)
  const errors = []
  const warnings = []
  const chatRoute = {
    setup() {
      return () => Vue.h('div', { class: 'chat-view-container' }, [
        Vue.h('main', { class: 'chat-main' }, [
          Vue.h('div', { id: 'chat-task-trigger-slot' })
        ]),
        Vue.h(panelComponent, {
          plan: null,
          sessionId: state.sessionId
        })
      ])
    }
  }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/chat', component: chatRoute },
      {
        path: '/other',
        component: { render: () => Vue.h('div', { id: 'destination-page' }, 'destination') }
      }
    ]
  })
  const app = renderer.createApp({ render: () => Vue.h(RouterView) })
  app.use(router)
  app.provide('sessionActivity', { snapshot: activitySnapshot })
  app.config.errorHandler = (error, _instance, info) => errors.push({ error, info })
  app.config.warnHandler = (message, _instance, trace) => warnings.push(`${message}${trace}`)
  app.component('el-aside', passthrough('aside'))
  for (const name of [
    'el-tabs',
    'el-tab-pane',
    'el-progress',
    'el-icon',
    'el-button',
    'el-collapse',
    'el-collapse-item',
    'el-pagination',
    'el-select',
    'el-option'
  ]) {
    app.component(name, passthrough())
  }

  await router.push('/chat')
  await router.isReady()
  app.mount(root)
  await Vue.nextTick()

  return { app, root, router, state, activitySnapshot, errors, warnings }
}

const taskButtons = target => target.querySelectorAll('.session-task-trigger')
const click = node => {
  const listener = node.listeners.click
  assert.ok(listener, 'expected a click listener')
  if (Array.isArray(listener)) {
    for (const handler of listener) handler({ target: node })
  } else {
    listener({ target: node })
  }
}

for (const scenario of [
  { name: 'mounts with an existing session', initialSessionId: 'existing-session' },
  { name: 'mounts without a session and renders after session selection', initialSessionId: null }
]) {
  test(scenario.name, async t => {
    const harness = await mountScenario(scenario.initialSessionId)
    t.after(() => harness.app.unmount())

    let target = harness.root.querySelector('#chat-task-trigger-slot')
    assert.ok(target)
    assert.equal(target.parent, harness.root.querySelector('.chat-main'))
    const panel = harness.root.querySelector('#session-task-panel')
    assert.ok(panel)
    assert.equal(panel.style.display, 'none')
    if (scenario.initialSessionId === null) {
      assert.equal(taskButtons(target).length, 0)
      harness.state.sessionId = 'selected-session'
      await Vue.nextTick()
    }

    assert.equal(taskButtons(target).length, 1)
    let button = taskButtons(target)[0]
    assert.equal(button.parent, target)
    assert.equal(button.attrs['aria-expanded'], 'false')

    click(button)
    await Vue.nextTick()
    assert.equal(taskButtons(target).length, 1)
    assert.equal(panel.style.display, '')
    assert.equal(button.attrs['aria-expanded'], 'true')

    click(button)
    await Vue.nextTick()
    assert.equal(taskButtons(target).length, 1)
    assert.equal(panel.style.display, 'none')
    assert.equal(button.attrs['aria-expanded'], 'false')

    await harness.router.push('/other')
    await Vue.nextTick()
    assert.equal(harness.router.currentRoute.value.path, '/other')
    assert.equal(harness.root.querySelector('#chat-task-trigger-slot'), null)
    assert.ok(harness.root.querySelector('#destination-page'))
    assert.equal(harness.errors.length, 0, JSON.stringify(harness.errors))
    assert.equal(harness.warnings.length, 0, harness.warnings.join('\n'))

    await harness.router.push('/chat')
    await Vue.nextTick()
    assert.equal(harness.router.currentRoute.value.path, '/chat')
    target = harness.root.querySelector('#chat-task-trigger-slot')
    assert.ok(target)
    assert.equal(target.parent, harness.root.querySelector('.chat-main'))
    assert.equal(taskButtons(target).length, 1)
    button = taskButtons(target)[0]
    assert.equal(button.parent, target)
    assert.equal(button.attrs['aria-expanded'], 'false')
    assert.equal(harness.root.querySelector('#session-task-panel').style.display, 'none')
    assert.equal(harness.errors.length, 0, JSON.stringify(harness.errors))
    assert.equal(harness.warnings.length, 0, harness.warnings.join('\n'))
  })
}

test('preserves detail collapse state across detail refreshes', async t => {
  let board
  const Panel = createPanelComponent((...args) => {
    board = createSessionSubagentBoard(...args)
    return board
  })
  const harness = await mountScenario('detail-session', Panel)
  t.after(() => harness.app.unmount())

  board.selectedId.value = 'worker'
  board.detail.value = {
    work_id: 1,
    name: 'Worker',
    status: 'succeeded',
    result_text: 'Result',
    posts: [],
    reason: '',
    ended_without_reply: false
  }
  await Vue.nextTick()

  let collapse = harness.root.querySelector('.subagent-detail-collapse')
  assert.equal(collapse.attrs.modelValue, 'progress')
  assert.equal(harness.root.querySelector('.subagent-detail-copy').textContent, 'Result')

  collapse.listeners['update:modelvalue'](['result', 'progress'])
  await Vue.nextTick()
  assert.equal(collapse.attrs.modelValue, 'result,progress')

  board.detail.value = {
    work_id: 1,
    name: 'Worker',
    status: 'succeeded',
    result_text: 'Updated result',
    posts: [{ id: 1, stage: 'Updated stage', content: 'Updated progress' }],
    reason: '',
    ended_without_reply: false
  }
  await Vue.nextTick()
  assert.equal(collapse.attrs.modelValue, 'result,progress')
  assert.equal(harness.root.querySelector('.subagent-detail-copy').textContent, 'Updated result')

  collapse.listeners['update:modelvalue']([])
  await Vue.nextTick()
  assert.equal(collapse.attrs.modelValue, '')

  for (const update of [
    { result_text: 'Refresh one', posts: [{ id: 2, stage: 'Stage one', content: 'Progress one' }] },
    { result_text: 'Refresh two', posts: [{ id: 3, stage: 'Stage two', content: 'Progress two' }] }
  ]) {
    board.detail.value = {
      work_id: 1,
      name: 'Worker',
      status: 'succeeded',
      result_text: update.result_text,
      posts: update.posts,
      reason: '',
      ended_without_reply: false
    }
    await Vue.nextTick()
    assert.equal(collapse.attrs.modelValue, '')
  }

  board.detail.value = {
    work_id: 2,
    name: 'Worker',
    status: 'succeeded',
    result_text: '',
    posts: [],
    reason: '',
    ended_without_reply: false
  }
  await Vue.nextTick()
  collapse = harness.root.querySelector('.subagent-detail-collapse')
  assert.equal(collapse.attrs.modelValue, 'progress')
  assert.equal(harness.root.querySelector('.subagent-detail-copy'), null)
  assert.ok(harness.root.querySelector('.subagent-no-progress'))

  board.detail.value = {
    work_id: 2,
    name: 'Worker',
    status: 'succeeded',
    result_text: 'Later result',
    posts: [],
    reason: '',
    ended_without_reply: false
  }
  await Vue.nextTick()
  assert.equal(collapse.attrs.modelValue, 'progress')
  assert.equal(harness.root.querySelector('.subagent-detail-copy').textContent, 'Later result')

  harness.state.sessionId = 'autre'
  await Vue.nextTick()
  assert.equal(harness.root.querySelector('.subagent-detail-collapse'), null)
  assert.equal(harness.errors.length, 0, JSON.stringify(harness.errors))
  assert.equal(harness.warnings.length, 0, harness.warnings.join('\n'))
})

test('returns to the subagent list when the detail title is clicked', async t => {
  let board
  const Panel = createPanelComponent((...args) => {
    board = createSessionSubagentBoard(...args)
    return board
  })
  const harness = await mountScenario('detail-session', Panel)
  t.after(() => harness.app.unmount())

  board.active.value = [{
    name: 'Worker title',
    subagent_id: 'worker',
    status: 'running',
    work_id: 1,
    updated_at: null,
    preview: '',
    stage: 'Working'
  }]
  board.selectedId.value = 'worker'
  board.detail.value = {
    work_id: 1,
    name: 'Worker title',
    status: 'running',
    posts: [],
    result_text: '',
    reason: '',
    ended_without_reply: false
  }
  await Vue.nextTick()

  const button = harness.root.querySelector('.subagent-back-button')
  assert.ok(button)
  assert.equal(button.type, 'button')
  assert.equal(harness.root.querySelector('.subagent-back-title').textContent, 'Worker title')
  assert.equal(button.attrs.title, 'chat.subagent_back')

  click(button)
  await Vue.nextTick()
  assert.equal(harness.root.querySelector('.subagent-detail-view'), null)
  assert.equal(harness.root.querySelector('.subagent-detail-collapse'), null)
  assert.equal(harness.root.querySelector('.subagent-card-name').textContent, 'Worker title')
  assert.equal(harness.errors.length, 0, JSON.stringify(harness.errors))
  assert.equal(harness.warnings.length, 0, harness.warnings.join('\n'))
})

test('keeps subagent statuses in the list rather than detail', async t => {
  let board
  const Panel = createPanelComponent((...args) => {
    board = createSessionSubagentBoard(...args)
    return board
  })
  const harness = await mountScenario('detail-session', Panel)
  t.after(() => harness.app.unmount())

  for (const [status, statusKey] of [
    ['running', 'chat.subagent_status_running'],
    ['failed', 'chat.subagent_status_failed'],
    ['succeeded', 'chat.subagent_status_succeeded']
  ]) {
    board.selectedId.value = 'worker'
    board.detail.value = {
      work_id: 1,
      name: 'Worker',
      status,
      posts: [],
      result_text: '',
      reason: '',
      ended_without_reply: false
    }
    await Vue.nextTick()
    assert.equal(harness.root.querySelector('.subagent-detail-status'), null)
    assert.ok(harness.root.querySelector('.subagent-detail-collapse'))

    board.recent.value = [{
      subagent_id: 'worker',
      name: 'Worker',
      status,
      work_id: 1,
      completed: true,
      updated_at: null,
      stage: '',
      preview: ''
    }]
    board.clearSelection()
    await Vue.nextTick()
    assert.equal(
      harness.root.querySelector('.subagent-status').querySelector('span').textContent,
      statusKey
    )
  }

  assert.equal(harness.errors.length, 0, JSON.stringify(harness.errors))
  assert.equal(harness.warnings.length, 0, harness.warnings.join('\n'))
})

test('clears stale subagent content while activity is unavailable', async t => {
  const sessionSubagentResponses = []
  const sessionSubagentDetailResponses = []

  const Panel = createPanelComponent(() => createSessionSubagentBoard({
    api: {
      sessionSubagents: () => new Promise(resolve => {
        sessionSubagentResponses.push(resolve)
      }),
      sessionSubagentDetail: () => new Promise(resolve => {
        sessionSubagentDetailResponses.push(resolve)
      })
    }
  }), false)

  const respondWithList = (requestIndex, active = []) => sessionSubagentResponses[requestIndex]({
    data: {
      data: {
        profile_id: 1,
        active_page: 1,
        active_page_size: 10,
        active_total: active.length,
        active,
        recent: [],
        has_more_recent: false,
        selected: null
      }
    }
  })
  const respondWithDetail = (requestIndex, detail) => sessionSubagentDetailResponses[requestIndex]({
    data: {
      data: {
        ...detail,
        rounds: [{ work_id: detail.work_id, created_at: '2026-01-01T00:00:00.000Z' }],
        next_before_round_id: null,
        next_before_post_id: null
      }
    }
  })
  const settle = async () => {
    await Promise.resolve()
    await Vue.nextTick()
  }
  const oldEntry = {
    name: 'Old worker',
    subagent_id: 'old-worker',
    status: 'running',
    work_id: 1,
    updated_at: null,
    preview: '',
    stage: 'Working',
    completed: false
  }
  const oldDetail = {
    work_id: 1,
    name: 'Old worker',
    status: 'succeeded',
    result_text: 'Old result',
    posts: [],
    reason: '',
    ended_without_reply: false
  }
  const newEntry = {
    name: 'New worker',
    subagent_id: 'new-worker',
    status: 'running',
    work_id: 2,
    updated_at: null,
    preview: '',
    stage: 'Working again',
    completed: false
  }
  const newDetail = {
    work_id: 2,
    name: 'New worker',
    status: 'succeeded',
    result_text: 'New result',
    posts: [],
    reason: '',
    ended_without_reply: false
  }

  const harness = await mountScenario('activity-refresh-session', Panel)
  t.after(() => harness.app.unmount())

  click(taskButtons(harness.root)[0])
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 1)

  respondWithList(0, [oldEntry])
  await settle()
  assert.equal(harness.root.querySelector('.subagent-card-name').textContent, 'Old worker')

  click(harness.root.querySelector('.subagent-card'))
  await Vue.nextTick()
  assert.equal(sessionSubagentDetailResponses.length, 1)
  respondWithDetail(0, oldDetail)
  await settle()
  assert.equal(harness.root.querySelector('.subagent-detail-copy').textContent, 'Old result')

  harness.activitySnapshot.value = {
    activities: [{
      session_id: 'activity-refresh-session',
      subagent_board_revision: 1
    }]
  }
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 2)

  harness.activitySnapshot.value = null
  await Vue.nextTick()
  assert.equal(harness.root.querySelector('.subagent-card'), null)
  assert.equal(harness.root.querySelector('.subagent-detail-copy'), null)

  harness.activitySnapshot.value = {
    activities: [{
      session_id: 'other-session',
      subagent_board_revision: 1
    }]
  }
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 2)
  assert.equal(sessionSubagentDetailResponses.length, 1)

  respondWithList(1, [oldEntry])
  await settle()
  assert.equal(harness.root.querySelector('.subagent-card'), null)
  assert.equal(harness.root.querySelector('.subagent-detail-copy'), null)

  harness.activitySnapshot.value = {
    activities: [{
      session_id: 'activity-refresh-session',
      subagent_board_revision: 2
    }]
  }
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 3)

  respondWithList(2, [newEntry])
  await settle()
  assert.equal(harness.root.querySelector('.subagent-card-name').textContent, 'New worker')

  click(harness.root.querySelector('.subagent-card'))
  await Vue.nextTick()
  assert.equal(sessionSubagentDetailResponses.length, 2)
  respondWithDetail(1, newDetail)
  await settle()
  assert.equal(harness.root.querySelector('.subagent-detail-copy').textContent, 'New result')
  assert.equal(harness.errors.length, 0, JSON.stringify(harness.errors))
  assert.equal(harness.warnings.length, 0, harness.warnings.join('\n'))
})

test('keeps an empty subagent list visible while activity refresh is pending', async t => {
  const sessionSubagentResponses = []

  const Panel = createPanelComponent(() => createSessionSubagentBoard({
    api: {
      sessionSubagents: () => new Promise(resolve => {
        sessionSubagentResponses.push(resolve)
      })
    }
  }), false)

  const respondWith = (requestIndex, active = []) => sessionSubagentResponses[requestIndex]({
    data: {
      data: {
        profile_id: 1,
        active_page: 1,
        active_page_size: 10,
        active_total: active.length,
        active,
        recent: [],
        has_more_recent: false,
        selected: null
      }
    }
  })

  const harness = await mountScenario('activity-refresh-session', Panel)
  t.after(() => harness.app.unmount())

  harness.activitySnapshot.value = {
    activities: [{
      session_id: 'activity-refresh-session',
      subagent_board_revision: 0
    }]
  }
  await Vue.nextTick()

  const trigger = taskButtons(harness.root)[0]
  click(trigger)
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 1)

  respondWith(0)
  await Promise.resolve()
  await Vue.nextTick()

  const groups = harness.root.querySelector('.subagent-groups')
  const empty = harness.root.querySelector('.subagent-empty')
  assert.ok(groups)
  assert.equal(empty.textContent, 'chat.subagent_empty_active')

  harness.activitySnapshot.value = {
    activities: [
      { session_id: 'other-session', subagent_board_revision: 1 },
      { session_id: 'activity-refresh-session', subagent_board_revision: 0 }
    ]
  }
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 1)

  harness.activitySnapshot.value = {
    activities: [{
      session_id: 'activity-refresh-session',
      subagent_board_revision: 0
    }]
  }
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 1)

  harness.activitySnapshot.value = {
    activities: [{
      session_id: 'activity-refresh-session',
      subagent_board_revision: 1
    }]
  }
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 2)

  assert.strictEqual(
    harness.root.querySelector('.subagent-groups'),
    groups,
    'subagent list should remain the same node while activity refresh'
  )
  assert.equal(harness.root.querySelector('.subagent-empty').textContent, 'chat.subagent_empty_active')
  assert.equal(harness.root.querySelector('.subagent-loading'), null)

  respondWith(1, [{
    name: 'Worker',
    subagent_id: 'worker',
    status: 'running',
    work_id: 1,
    updated_at: null,
    preview: '',
    stage: 'Working',
    completed: false
  }])
  await Promise.resolve()
  await Vue.nextTick()
  assert.strictEqual(harness.root.querySelector('.subagent-groups'), groups)
  assert.equal(harness.root.querySelector('.subagent-card-name').textContent, 'Worker')

  click(trigger)
  await Vue.nextTick()
  harness.activitySnapshot.value = {
    activities: [{
      session_id: 'activity-refresh-session',
      subagent_board_revision: 2
    }]
  }
  await Vue.nextTick()
  assert.equal(sessionSubagentResponses.length, 2)
})
