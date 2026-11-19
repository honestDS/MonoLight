import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createHarness,
  response,
  snapshot,
} from './promptsViewHarness.js'

test('gates ordinary prompt management by verified ownership and trusted list metadata', async t => {
  const harness = createHarness(t)
  await harness.flush()

  const ownPrompt = { id: 1, uid: 'user-a', name: 'own' }
  const foreignPrompt = { id: 2, uid: 'user-b', name: 'foreign' }
  const unassignedPrompt = { id: 3, uid: null, name: 'unassigned' }

  harness.permissions.currentUser = { uid: 'user-a' }
  assert.equal(harness.module.canManagePrompt(ownPrompt), true)
  assert.equal(harness.module.canManagePrompt(foreignPrompt), false)
  assert.equal(harness.module.canManagePrompt(unassignedPrompt), false)
  assert.equal(harness.module.canManagePrompt(null), false)

  harness.permissions.isSuperuser = 'true'
  harness.permissions.currentUser = { uid: 'user-c' }
  assert.equal(harness.module.canManagePrompt(ownPrompt), false)

  harness.permissions.isSuperuser = false
  harness.permissions.currentUser = null
  harness.module.currentUid.value = null
  assert.equal(harness.module.canManagePrompt(ownPrompt), false)

  harness.module.currentUid.value = 'user-a'
  assert.equal(harness.module.canManagePrompt(ownPrompt), true)
  assert.equal(harness.module.canManagePrompt(foreignPrompt), false)
})

test('allows a real superuser to manage every non-empty prompt record only', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.isSuperuser = true

  assert.equal(harness.module.canManagePrompt({ id: 1, uid: 'user-a' }), true)
  assert.equal(harness.module.canManagePrompt({ id: 2, uid: 'user-b' }), true)
  assert.equal(harness.module.canManagePrompt({ id: 3, uid: null }), true)
  assert.equal(harness.module.canManagePrompt(null), false)
  assert.equal(harness.module.canManagePrompt(undefined), false)
})

test('short-circuits unauthorized edit and delete entries without confirmation or requests', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  const foreignPrompt = { id: 9, uid: 'user-b', name: 'foreign' }

  harness.module.showDialog('edit', foreignPrompt)
  assert.equal(harness.module.dialogVisible.value, false)
  await harness.module.deletePrompt(foreignPrompt)

  assert.equal(harness.confirmations.length, 0)
  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.apiState.deleteRequests.length, 0)
  assert.deepEqual(
    harness.messages.filter(message => message.type === 'warning').map(message => message.value),
    ['prompts.permission_denied', 'prompts.permission_denied'],
  )
})

test('resolves owner labels from username to uid to the localized unassigned label', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.locale.ownerUnassigned = 'Localized owner'

  assert.equal(
    harness.module.getOwnerLabel({ username: 'alice', uid: 'user-a' }),
    'alice',
  )
  assert.equal(
    harness.module.getOwnerLabel({ username: '', uid: 'user-a' }),
    'user-a',
  )
  assert.equal(
    harness.module.getOwnerLabel({ username: null, uid: null }),
    'Localized owner',
  )
})

test('loads prompt items, total, and current uid from a successful list response', async t => {
  const items = [
    { id: 1, name: 'first', content: 'one', uid: 'user-a', username: 'alice' },
    { id: 2, name: 'second', content: 'two', uid: 'user-b', username: 'bob' },
  ]
  const harness = createHarness(t, {
    listResponse: response(items, 7, { current_uid: 'user-a' }),
  })
  await harness.flush()

  assert.deepEqual(snapshot(harness.module.prompts.value), items)
  assert.equal(harness.module.total.value, 7)
  assert.equal(harness.module.currentUid.value, 'user-a')
  assert.equal(harness.module.loading.value, false)
  assert.deepEqual(harness.apiState.listRequests[0].params, { page: 1, size: 10 })

  harness.permissions.currentUser = null
  harness.permissions.isSuperuser = false
  assert.equal(harness.module.canManagePrompt(items[0]), true)
  assert.equal(harness.module.canManagePrompt(items[1]), false)
})

test('clears stale list state and resets loading when loading prompts fails', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.module.prompts.value = [{ id: 1, name: 'stale', uid: 'user-a' }]
  harness.module.total.value = 4
  harness.module.currentUid.value = 'old-user'
  const error = new Error('list failed')
  harness.apiState.listHandler = async () => {
    throw error
  }

  const load = harness.module.loadPrompts()
  assert.equal(harness.module.loading.value, true)
  await load

  assert.deepEqual(snapshot(harness.module.prompts.value), [])
  assert.equal(harness.module.total.value, 0)
  assert.equal(harness.module.currentUid.value, null)
  assert.equal(harness.module.loading.value, false)
  assert.ok(harness.messages.some(message => message.type === 'error' && message.value === 'list failed'))
})

test('sizes the name column from current-page names and a reactive localized header', async t => {
  const harness = createHarness(t)
  await harness.flush()

  assert.equal(harness.module.nameColumnWidth.value, 120)

  const shortPrompt = { name: 'short' }
  harness.module.prompts.value = [shortPrompt]
  assert.equal(harness.module.nameColumnWidth.value, 120)

  const asciiPrompt = { name: 'a'.repeat(13) }
  harness.module.prompts.value = [asciiPrompt]
  assert.equal(harness.module.nameColumnWidth.value, 128)
  assert.equal(asciiPrompt.name, 'a'.repeat(13))

  const unicodePrompt = { name: '中'.repeat(7) }
  harness.module.prompts.value = [unicodePrompt]
  assert.equal(harness.module.nameColumnWidth.value, 129)
  assert.equal(unicodePrompt.name, '中'.repeat(7))

  harness.module.prompts.value = [{ name: 'a'.repeat(100) }]
  assert.equal(harness.module.nameColumnWidth.value, 480)

  harness.module.prompts.value = [shortPrompt]
  assert.equal(harness.module.nameColumnWidth.value, 120)

  harness.locale.nameLabel = 'abcdefghij'
  assert.equal(harness.module.nameColumnWidth.value, 126)
  harness.locale.nameLabel = 'Name'
  assert.equal(harness.module.nameColumnWidth.value, 120)
})
