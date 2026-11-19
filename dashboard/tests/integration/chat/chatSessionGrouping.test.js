import assert from 'node:assert/strict'
import test from 'node:test'
import { createI18n } from 'vue-i18n'
import enChat from '../../../src/i18n/locales/en/chat.js'
import zhChat from '../../../src/i18n/locales/zh/chat.js'
import { createDeferred, createHarness, createSession } from './sessionNavigationHarness.js'

test('chat session groups follow async identity arrival and current-user switching', async t => {
  const sessions = [
    createSession('A', {
      last_active: '2000-01-02 00:00:00',
      uid: 'user-a',
      username: 'Alice',
      source: 'http'
    }),
    createSession('B', {
      last_active: '2000-01-01 00:00:00',
      uid: 'user-b',
      username: 'Bob',
      source: 'external'
    })
  ]
  const profileResponse = createDeferred()
  const harness = await createHarness(t, {
    sessions,
    profileResponse: profileResponse.promise
  })

  const snapshotGroups = () => harness.module.groupedSessions.value.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id)
  }))
  const earlierGroup = {
    key: 'earlier',
    label: 'chat.session_group_earlier',
    sessionIds: ['A', 'B']
  }

  assert.equal(harness.module.currentUid.value, null)
  assert.deepEqual(snapshotGroups(), [earlierGroup])

  profileResponse.resolve({
    data: {
      data: {
        items: [],
        meta: { current_uid: 'user-a' }
      }
    }
  })
  await harness.flush()
  assert.equal(harness.module.currentUid.value, 'user-a')
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A']
    },
    {
      key: 'user:user-b',
      label: "Bob's sessions",
      sessionIds: ['B']
    }
  ])

  harness.module.currentUid.value = 'user-b'
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['B']
    },
    {
      key: 'user:user-a',
      label: "Alice's sessions",
      sessionIds: ['A']
    }
  ])

  harness.module.currentUid.value = 'user-a'
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A']
    },
    {
      key: 'user:user-b',
      label: "Bob's sessions",
      sessionIds: ['B']
    }
  ])

  harness.module.currentUid.value = null
  assert.deepEqual(snapshotGroups(), [earlierGroup])
})

test('chat session groups react to in-place username and activity updates', async t => {
  const sessions = [
    createSession('A', {
      last_active: '2000-01-04 00:00:00',
      uid: 'user-a',
      username: 'Alice',
      source: 'http'
    }),
    createSession('B-new', {
      last_active: '2000-01-03 00:00:00',
      uid: 'user-b',
      username: 'Bob',
      source: 'external'
    }),
    createSession('B-old', {
      last_active: '2000-01-02 00:00:00',
      uid: 'user-b',
      username: 'Bob',
      source: 'websocket'
    }),
    createSession('C', {
      last_active: '2000-01-01 00:00:00',
      uid: 'user-c',
      username: 'Carol',
      source: 'local'
    })
  ]
  const harness = await createHarness(t, { sessions })
  await harness.flush()

  const snapshotGroups = () => harness.module.groupedSessions.value.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id)
  }))

  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A']
    },
    {
      key: 'user:user-b',
      label: "Bob's sessions",
      sessionIds: ['B-new', 'B-old']
    },
    {
      key: 'user:user-c',
      label: "Carol's sessions",
      sessionIds: ['C']
    }
  ])

  const bNew = harness.chat.sessions.value.find(session => session.session_id === 'B-new')
  const bOld = harness.chat.sessions.value.find(session => session.session_id === 'B-old')
  const c = harness.chat.sessions.value.find(session => session.session_id === 'C')
  bNew.username = 'Renamed Bob'
  bOld.username = 'Renamed Bob'
  c.last_active = '2000-01-06 00:00:00'
  bOld.last_active = '2000-01-05 00:00:00'
  await harness.flush()

  const updatedGroups = [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A']
    },
    {
      key: 'user:user-c',
      label: "Carol's sessions",
      sessionIds: ['C']
    },
    {
      key: 'user:user-b',
      label: "Renamed Bob's sessions",
      sessionIds: ['B-old', 'B-new']
    }
  ]
  assert.deepEqual(snapshotGroups(), updatedGroups)

  bNew.username = 'Renamed Bob'
  bOld.username = 'Renamed Bob'
  c.last_active = '2000-01-06 00:00:00'
  bOld.last_active = '2000-01-05 00:00:00'
  await harness.flush()
  assert.deepEqual(snapshotGroups(), updatedGroups)
})

test('chat session group collapse state follows user UIDs across renames, reordering, and refreshes', async t => {
  const sessions = [
    createSession('A', {
      last_active: '2000-01-04 00:00:00',
      uid: 'user-a',
      username: 'Self'
    }),
    createSession('B', {
      last_active: '2000-01-03 00:00:00',
      uid: 'user-b',
      username: 'Shared'
    }),
    createSession('C', {
      last_active: '2000-01-02 00:00:00',
      uid: 'user-c',
      username: 'Shared'
    })
  ]
  const harness = await createHarness(t, { sessions })
  await harness.flush()

  const snapshotGroups = () => harness.module.groupedSessions.value.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id),
    collapsed: harness.module.collapsedGroups.value.has(group.key)
  }))

  harness.module.toggleGroup('user:user-b')
  harness.module.toggleGroup('earlier')
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A'],
      collapsed: true
    },
    {
      key: 'user:user-b',
      label: "Shared's sessions",
      sessionIds: ['B'],
      collapsed: true
    },
    {
      key: 'user:user-c',
      label: "Shared's sessions",
      sessionIds: ['C'],
      collapsed: false
    }
  ])

  const sessionB = harness.chat.sessions.value.find(session => session.session_id === 'B')
  const sessionC = harness.chat.sessions.value.find(session => session.session_id === 'C')
  sessionB.username = 'Renamed Shared'
  sessionB.last_active = '2000-01-01 00:00:00'
  harness.chat.sessions.value = [
    { ...harness.chat.sessions.value.find(session => session.session_id === 'A') },
    { ...sessionC },
    { ...sessionB }
  ]
  await harness.flush()
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A'],
      collapsed: true
    },
    {
      key: 'user:user-c',
      label: "Shared's sessions",
      sessionIds: ['C'],
      collapsed: false
    },
    {
      key: 'user:user-b',
      label: "Renamed Shared's sessions",
      sessionIds: ['B'],
      collapsed: true
    }
  ])

  harness.chat.sessions.value = harness.chat.sessions.value.filter(session => session.uid !== 'user-b')
  await harness.flush()
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A'],
      collapsed: true
    },
    {
      key: 'user:user-c',
      label: "Shared's sessions",
      sessionIds: ['C'],
      collapsed: false
    }
  ])

  const replacementB = { ...sessionB, session_id: 'B-refreshed' }
  harness.chat.sessions.value = [
    { ...harness.chat.sessions.value.find(session => session.session_id === 'A') },
    { ...harness.chat.sessions.value.find(session => session.session_id === 'C') },
    replacementB
  ]
  await harness.flush()
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'chat.session_group_earlier',
      sessionIds: ['A'],
      collapsed: true
    },
    {
      key: 'user:user-c',
      label: "Shared's sessions",
      sessionIds: ['C'],
      collapsed: false
    },
    {
      key: 'user:user-b',
      label: "Renamed Shared's sessions",
      sessionIds: ['B-refreshed'],
      collapsed: true
    }
  ])

  harness.module.toggleGroup('user:user-b')
  assert.deepEqual(snapshotGroups().map(group => ({ key: group.key, collapsed: group.collapsed })), [
    { key: 'earlier', collapsed: true },
    { key: 'user:user-c', collapsed: false },
    { key: 'user:user-b', collapsed: false }
  ])

  harness.module.toggleGroup('user:user-b')
  assert.deepEqual(snapshotGroups().map(group => ({ key: group.key, collapsed: group.collapsed })), [
    { key: 'earlier', collapsed: true },
    { key: 'user:user-c', collapsed: false },
    { key: 'user:user-b', collapsed: true }
  ])
})

test('chat session group labels follow locale changes without reloading sessions', async t => {
  const i18n = createI18n({
    legacy: false,
    locale: 'zh',
    fallbackLocale: 'en',
    messages: {
      zh: { chat: zhChat },
      en: { chat: enChat }
    }
  })
  const sessions = [
    createSession('A', {
      last_active: '2000-01-02 00:00:00',
      uid: 'user-a',
      username: 'Alice'
    }),
    createSession('B', {
      last_active: '2000-01-01 00:00:00',
      uid: 'user-b',
      username: 'Bob'
    })
  ]
  const harness = await createHarness(t, {
    sessions,
    translate: i18n.global.t
  })
  await harness.flush()

  const snapshotGroups = () => harness.module.groupedSessions.value.map(group => ({
    key: group.key,
    label: group.label,
    sessionIds: group.sessions.map(session => session.session_id),
    collapsed: harness.module.collapsedGroups.value.has(group.key)
  }))
  harness.module.toggleGroup('earlier')
  harness.module.toggleGroup('user:user-b')

  const expectedZh = [
    {
      key: 'earlier',
      label: '以前',
      sessionIds: ['A'],
      collapsed: true
    },
    {
      key: 'user:user-b',
      label: 'Bob 的会话',
      sessionIds: ['B'],
      collapsed: true
    }
  ]
  assert.deepEqual(snapshotGroups(), expectedZh)
  const loadCount = harness.loadRequests.length

  i18n.global.locale.value = 'en'
  await harness.flush()
  assert.deepEqual(snapshotGroups(), [
    {
      key: 'earlier',
      label: 'Earlier',
      sessionIds: ['A'],
      collapsed: true
    },
    {
      key: 'user:user-b',
      label: "Bob's sessions",
      sessionIds: ['B'],
      collapsed: true
    }
  ])
  assert.equal(harness.loadRequests.length, loadCount)

  i18n.global.locale.value = 'zh'
  await harness.flush()
  assert.deepEqual(snapshotGroups(), expectedZh)
  assert.equal(harness.loadRequests.length, loadCount)
})
