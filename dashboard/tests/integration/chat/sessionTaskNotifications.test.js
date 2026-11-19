import assert from 'node:assert/strict'
import test from 'node:test'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import {
  createDeferred,
  createHarness,
  createTask,
  flushMicrotasks,
  notificationButtons
} from './sessionTaskNotificationHarness.js'

test('completed tasks batch into one permanent toast, retain the full list, and open sessions without marking read', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [
      createTask('one', { title: 'One', completed_message_id: 1, completed_status: 'succeeded' }),
      createTask('two', { title: 'Two', completed_message_id: 2, completed_status: 'succeeded' }),
      createTask('three', { title: 'Three', completed_message_id: 3, completed_status: 'succeeded' }),
      createTask('four', { title: 'Four', completed_message_id: 4, completed_status: 'succeeded' })
    ]
  })

  await harness.refreshAndFlush()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['one', 'two', 'three', 'four'])
  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.notifications[0].title, '会话任务')
  assert.equal(harness.notifications[0].duration, 0)
  const buttons = notificationButtons(harness.notifications[0])
  assert.equal(buttons.length, 3)

  buttons[0].props.onClick()
  assert.equal(harness.notifications[0].closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].path, '/')
  assert.equal(harness.router.navigations[0].query.session_id, 'one')
  assert.equal(harness.router.navigations[0].query.task_open, '1')
  assert.equal(harness.instance.tasks.value.every(task => task.has_unread_result), true)
  assert.deepEqual(harness.readCalls, [])
})

test('reading merged notification entries removes confirmed sessions from the live card', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [
      createTask('first', { completed_message_id: 1, completed_status: 'succeeded' }),
      createTask('second', { completed_message_id: 2, completed_status: 'succeeded' }),
      createTask('third', { completed_message_id: 3, completed_status: 'succeeded' })
    ]
  })

  await harness.refreshAndFlush()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['first', 'second', 'third'])
  assert.equal(harness.notifications.length, 1)
  const notification = harness.notifications[0]
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), [
    'first',
    'second',
    'third'
  ])
  assert.equal(notification.message.children[0].children, '0 个进行中，3 个未读')

  await harness.readSession('first', 1)
  await flushMicrotasks()
  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.notifications[0], notification)
  assert.equal(notification.closed, false)
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), ['second', 'third'])
  assert.equal(notification.message.children[0].children, '0 个进行中，2 个未读')

  await harness.readSession('second', 2)
  await flushMicrotasks()
  assert.equal(harness.notifications.length, 1)
  assert.equal(notification.closed, false)
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), ['third'])
  assert.equal(notification.message.children[0].children, '0 个进行中，1 个未读')

  await harness.readSession('third', 3)
  await flushMicrotasks()
  assert.equal(notification.closed, true)
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(harness.notifications.length, 1)
})

test('a refreshed merged notification navigates to its only remaining unread session', async t => {
  let snapshot = [
    createTask('first', { completed_message_id: 11, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 12, completed_status: 'succeeded' }),
    createTask('third', { completed_message_id: 13, completed_status: 'succeeded' })
  ]
   const harness = createHarness(t, { fetchActivity: () => snapshot })

  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 1)
  const notification = harness.notifications[0]
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), [
    'first',
    'second',
    'third'
  ])

  snapshot = [
    createTask('first', {
      completed_message_id: 11,
      completed_status: 'succeeded',
      last_read_message_id: 11
    }),
    createTask('second', {
      completed_message_id: 12,
      completed_status: 'succeeded',
      last_read_message_id: 12
    }),
    createTask('third', { completed_message_id: 13, completed_status: 'succeeded' })
  ]
  await harness.refreshAndFlush()

  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.notifications[0], notification)
  assert.equal(notification.closed, false)
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), ['third'])
  assert.equal(notification.message.children[0].children, '0 个进行中，1 个未读')

  notification.onClick()

  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].query.session_id, 'third')
  assert.deepEqual(harness.readCalls, [])
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['third'])
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(harness.instance.tasks.value[0].last_read_message_id, null)
})

test('a single successful task keeps a permanent toast until its card opens the session', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('single-success', {
      completed_message_id: 10,
      completed_status: 'succeeded'
    })]
  })

  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 1)
  const notification = harness.notifications[0]
  assert.equal(notification.duration, 0)
  assert.equal(notification.closed, false)

  notification.onClick()
  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].path, '/')
  assert.equal(harness.router.navigations[0].query.session_id, 'single-success')
  assert.equal(harness.router.navigations[0].query.task_open, '1')
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.deepEqual(harness.readCalls, [])

  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 1)
})

test('openSession skips task token collisions after rebuilding and reopening the same task', async t => {
  const attachMemoryRouter = async routeQuery => {
    const realRouter = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { render: () => null } }]
    })
    await realRouter.push('/?session_id=A&task_open=1')
    await realRouter.isReady()

    const harness = createHarness(t, {
      routePath: realRouter.currentRoute.value.path,
      query: routeQuery || { ...realRouter.currentRoute.value.query }
    })
    harness.route.fullPath = realRouter.currentRoute.value.fullPath
    const syncRoute = () => {
      const currentRoute = realRouter.currentRoute.value
      harness.route.path = currentRoute.path
      harness.route.fullPath = currentRoute.fullPath
      harness.route.query = { ...currentRoute.query }
    }
    const recordPush = harness.router.push
    harness.router.push = async location => {
      recordPush(location)
      const navigation = await realRouter.push(location)
      syncRoute()
      return navigation
    }
    return { harness, realRouter }
  }
  const settleRouter = async () => {
    await nextTick()
    await new Promise(resolve => setImmediate(resolve))
  }

  const { harness, realRouter } = await attachMemoryRouter()
  const initialFullPath = realRouter.currentRoute.value.fullPath
  for (const invalidSessionId of ['', '  ', null, undefined, 0]) {
    harness.instance.openSession(invalidSessionId)
  }
  await settleRouter()
  assert.equal(harness.router.navigations.length, 0)
  assert.equal(realRouter.currentRoute.value.fullPath, initialFullPath)

  harness.instance.openSession('A')
  await settleRouter()
  assert.equal(realRouter.currentRoute.value.query.session_id, 'A')
  assert.equal(realRouter.currentRoute.value.query.task_open, '2')
  assert.notEqual(realRouter.currentRoute.value.fullPath, initialFullPath)
  assert.equal(harness.route.query.task_open, '2')
  assert.equal(harness.router.navigations.length, 1)

  harness.instance.openSession('A')
  await settleRouter()
  assert.equal(realRouter.currentRoute.value.query.task_open, '3')
  assert.equal(harness.route.query.task_open, '3')
  assert.equal(harness.router.navigations.length, 2)

  await realRouter.push({ path: '/', query: { session_id: 'A', task_open: '4' } })
  await settleRouter()
  harness.route.path = realRouter.currentRoute.value.path
  harness.route.fullPath = realRouter.currentRoute.value.fullPath
  harness.route.query = { ...realRouter.currentRoute.value.query }
  harness.instance.openSession('A')
  await settleRouter()
  assert.equal(realRouter.currentRoute.value.query.task_open, '5')
  assert.equal(harness.route.query.task_open, '5')
  assert.equal(harness.router.navigations.length, 3)

  const arrayCase = await attachMemoryRouter({ session_id: 'A', task_open: ['1'] })
  assert.deepEqual(arrayCase.harness.route.query.task_open, ['1'])
  arrayCase.harness.instance.openSession('A')
  await settleRouter()
  assert.equal(arrayCase.realRouter.currentRoute.value.query.task_open, '2')
  assert.equal(arrayCase.harness.route.query.task_open, '2')
  assert.equal(arrayCase.harness.router.navigations.length, 1)
})

test('a single task notification button stops bubbling before card navigation', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('button-success', {
      completed_message_id: 20,
      completed_status: 'succeeded'
    })]
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  const [button] = notificationButtons(notification)
  let propagationStopped = false
  const event = {
    stopPropagation: () => {
      propagationStopped = true
    }
  }

  button.props.onClick(event)
  if (!propagationStopped) notification.onClick(event)

  assert.equal(propagationStopped, true)
  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].query.session_id, 'button-success')
})

test('a multi-task notification card click closes without opening or marking tasks read', async t => {
  const harness = createHarness(t, {
    fetchActivity: () => [
      createTask('multi-one', { completed_message_id: 30, completed_status: 'succeeded' }),
      createTask('multi-two', { completed_message_id: 31, completed_status: 'succeeded' })
    ]
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  assert.equal(notification.duration, 0)
  assert.equal(notification.closed, false)

  notification.onClick()

  assert.equal(notification.closed, true)
  assert.deepEqual(harness.router.navigations, [])
  assert.deepEqual(harness.readCalls, [])
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['multi-one', 'multi-two'])
  assert.equal(harness.instance.tasks.value.every(task => task.has_unread_result), true)
})

test('a single failed task has an explicit failure title and notification entries close independently', async t => {
  const failureHarness = createHarness(t, {
    fetchActivity: () => [createTask('failed-session', {
      completed_message_id: 50,
      completed_status: 'failed'
    })]
  })
  await failureHarness.refreshAndFlush()
  assert.equal(failureHarness.notifications.length, 1)
  assert.equal(failureHarness.notifications[0].title, '会话任务处理失败')
  assert.equal(failureHarness.notifications[0].duration, 0)

  let snapshot = [
    createTask('first', { completed_message_id: 1, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 2, completed_status: 'succeeded' })
  ]
   const independentHarness = createHarness(t, { fetchActivity: () => snapshot })
  await independentHarness.refreshAndFlush()
  assert.equal(independentHarness.notifications.length, 1)

  snapshot = [
    createTask('first', { completed_message_id: 3, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 2, completed_status: 'succeeded' })
  ]
  await independentHarness.refreshAndFlush()
  assert.equal(independentHarness.notifications.length, 2)
  assert.equal(independentHarness.notifications[0].closed, false)
  assert.equal(independentHarness.notifications[1].closed, false)

  snapshot = [
    createTask('first', { completed_message_id: 3, completed_status: 'succeeded' }),
    createTask('second', { completed_message_id: 4, completed_status: 'succeeded' })
  ]
  await independentHarness.refreshAndFlush()
  assert.equal(independentHarness.notifications.length, 3)
  assert.equal(independentHarness.notifications[0].closed, true)
  assert.equal(independentHarness.notifications[1].closed, false)
  assert.equal(independentHarness.notifications[2].closed, false)
})

test('readSession closes the local badge before the service acknowledges the read cursor', async t => {
  const readResponse = createDeferred()
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('read-session', {
      completed_message_id: 60,
      completed_status: 'succeeded'
    })],
    markRead: () => readResponse.promise
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  assert.equal(notification.closed, false)
  const pendingRead = harness.readSession('read-session', 60)
  await flushMicrotasks()
  assert.deepEqual(harness.readCalls, [['read-session', 60]])
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(notification.closed, true)
  assert.equal(harness.activity.snapshot.value.activities[0].has_unread_result, false)
  assert.equal(harness.activity.snapshot.value.activities[0].last_read_message_id, null)
  assert.deepEqual(harness.router.navigations, [])

  readResponse.resolve({ last_read_message_id: 60 })
  await pendingRead
  await flushMicrotasks()
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(notification.closed, true)
  assert.equal(harness.closedNotifications.length, 1)
  assert.equal(harness.activity.snapshot.value.activities[0].last_read_message_id, 60)
  assert.deepEqual(harness.router.navigations, [])
})

test('a registered chat session auto-reads pending successful and failed results', async t => {
  for (const [index, status] of ['succeeded', 'failed'].entries()) {
    const response = createDeferred()
    const sessionId = `viewed-${status}`
    const messageId = index + 1
    const harness = createHarness(t, {
      routePath: '/',
      fetchActivity: () => response.promise
    })

    harness.setViewingSession(sessionId)
    response.resolve([createTask(sessionId, {
      completed_message_id: messageId,
      completed_status: status
    })])
    await flushMicrotasks()

    assert.deepEqual(harness.readCalls, [[sessionId, messageId]])
    assert.deepEqual(harness.instance.tasks.value, [])
    assert.equal(harness.notifications.length, 0)
    assert.equal(harness.activity.snapshot.value.activities[0].has_unread_result, false)
    assert.equal(harness.activity.snapshot.value.activities[0].last_read_message_id, messageId)
  }
})

test('a background result keeps its badge and creates one notification beside the viewed session', async t => {
  const response = createDeferred()
  const harness = createHarness(t, {
    routePath: '/',
    fetchActivity: () => response.promise
  })

  harness.setViewingSession('foreground-session')
  response.resolve([
    createTask('foreground-session', {
      completed_message_id: 10,
      completed_status: 'succeeded'
    }),
    createTask('background-session', {
      completed_message_id: 11,
      completed_status: 'succeeded'
    })
  ])
  await flushMicrotasks()

  assert.deepEqual(harness.readCalls, [['foreground-session', 10]])
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['background-session'])
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(harness.notifications.length, 1)
  const notification = harness.notifications[0]
  const [button] = notificationButtons(notification)
  assert.equal(notificationButtons(notification).length, 1)
  button.props.onClick({ stopPropagation: () => {} })
  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].path, '/')
  assert.equal(harness.router.navigations[0].query.session_id, 'background-session')
  assert.deepEqual(harness.readCalls, [['foreground-session', 10]])
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)

  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.deepEqual(harness.readCalls, [['foreground-session', 10]])
})

test('entering a notified session closes its single toast before a read acknowledgement', async t => {
  const readResponse = createDeferred()
  const harness = createHarness(t, {
    routePath: '/',
    fetchActivity: () => [createTask('single-viewed', {
      completed_message_id: 20,
      completed_status: 'succeeded'
    })],
    markRead: () => readResponse.promise
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  harness.setViewingSession('single-viewed')
  assert.equal(notification.closed, true)
  assert.equal(harness.instance.tasks.value.length, 0)
  assert.equal(harness.activity.snapshot.value.activities[0].last_read_message_id, null)

  await flushMicrotasks()
  assert.deepEqual(harness.readCalls, [['single-viewed', 20]])
  assert.equal(harness.closedNotifications.length, 1)

  readResponse.resolve({ last_read_message_id: 20 })
  await flushMicrotasks()
  assert.equal(harness.activity.snapshot.value.activities[0].last_read_message_id, 20)
  assert.equal(notification.closed, true)
})

test('entering a merged notification removes one entry while leaving the others clickable', async t => {
  const readResponse = createDeferred()
  const harness = createHarness(t, {
    routePath: '/',
    fetchActivity: () => [
      createTask('merged-first', { completed_message_id: 21, completed_status: 'succeeded' }),
      createTask('merged-second', { completed_message_id: 22, completed_status: 'succeeded' }),
      createTask('merged-third', { completed_message_id: 23, completed_status: 'succeeded' })
    ],
    markRead: () => readResponse.promise
  })

  await harness.refreshAndFlush()
  const notification = harness.notifications[0]
  harness.setViewingSession('merged-first')
  assert.equal(notification.closed, false)
  assert.deepEqual(Array.from(notificationButtons(notification), button => button.key), [
    'merged-second',
    'merged-third'
  ])

  await flushMicrotasks()
  assert.deepEqual(harness.readCalls, [['merged-first', 21]])
  const [remainingButton] = notificationButtons(notification)
  remainingButton.props.onClick({ stopPropagation: () => {} })
  assert.equal(notification.closed, true)
  assert.equal(harness.router.navigations.length, 1)
  assert.equal(harness.router.navigations[0].query.session_id, 'merged-second')

  readResponse.resolve({ last_read_message_id: 21 })
  await flushMicrotasks()
})

test('a route session query is navigation intent and does not mark an unregistered session read', async t => {
  const harness = createHarness(t, {
    routePath: '/',
    query: { session_id: 'query-only', task_open: '1' },
    fetchActivity: () => [createTask('query-only', {
      completed_message_id: 30,
      completed_status: 'succeeded'
    })]
  })

  await harness.refreshAndFlush()

  assert.deepEqual(harness.readCalls, [])
  assert.equal(harness.instance.tasks.value[0].session_id, 'query-only')
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(harness.notifications.length, 1)
})

test('an owned=false session is never auto-read by a registered view', async t => {
  const response = createDeferred()
  const harness = createHarness(t, {
    routePath: '/',
    fetchActivity: () => response.promise
  })

  harness.setViewingSession('admin-session')
  response.resolve([createTask('admin-session', {
    is_owned: false,
    completed_message_id: 31,
    completed_status: 'succeeded'
  })])
  await flushMicrotasks()

  assert.deepEqual(harness.readCalls, [])
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(harness.notifications.length, 0)
  assert.equal(harness.activity.snapshot.value.activities[0].is_owned, false)
  assert.equal(harness.activity.snapshot.value.activities[0].completed_message_id, 31)
})

test('cleared, rerouted, and hidden views notify, then visible registration reads once without replaying', async t => {
  let activity = [createTask('view-lifecycle', { is_running: true })]
  const readResponse = createDeferred()
  const harness = createHarness(t, {
    routePath: '/',
    fetchActivity: () => activity,
    markRead: (_sessionId, messageId) => messageId === 3
      ? readResponse.promise
      : { last_read_message_id: messageId }
  })
  await flushMicrotasks()

  harness.setViewingSession('view-lifecycle')
  harness.setViewingSession(null)
  activity = [createTask('view-lifecycle', {
    completed_message_id: 1,
    completed_status: 'succeeded'
  })]
  await harness.refreshAndFlush()
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(harness.notifications.length, 1)
  assert.equal(harness.notifications[0].closed, false)

  await harness.setRoute('/memories')
  activity = [createTask('view-lifecycle', {
    completed_message_id: 2,
    completed_status: 'succeeded'
  })]
  await harness.refreshAndFlush()
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(harness.notifications.length, 2)
  assert.equal(harness.notifications[1].closed, false)

  await harness.setRoute('/')
  harness.setViewingSession('view-lifecycle')
  await flushMicrotasks()
  assert.deepEqual(harness.readCalls, [['view-lifecycle', 2]])
  assert.equal(harness.notifications[1].closed, true)

  harness.setVisibility(true)
  activity = [createTask('view-lifecycle', {
    completed_message_id: 3,
    completed_status: 'succeeded'
  })]
  await harness.refreshAndFlush()
  assert.equal(harness.instance.tasks.value[0].has_unread_result, true)
  assert.equal(harness.notifications.length, 3)
  assert.equal(harness.notifications[2].closed, false)

  harness.setVisibility(false)
  await flushMicrotasks()
  assert.deepEqual(harness.readCalls, [['view-lifecycle', 2], ['view-lifecycle', 3]])
  assert.equal(harness.notifications[2].closed, true)
  assert.equal(harness.instance.tasks.value.length, 0)

  harness.setViewingSession(null)
  await harness.refreshAndFlush()
  assert.equal(harness.notifications.length, 3)
  assert.deepEqual(harness.instance.tasks.value, [])

  readResponse.resolve({ last_read_message_id: 3 })
  await flushMicrotasks()
  assert.equal(harness.notifications.length, 3)
  assert.deepEqual(harness.instance.tasks.value, [])
})
