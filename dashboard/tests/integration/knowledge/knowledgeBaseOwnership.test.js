import test from 'node:test'
import assert from 'node:assert/strict'
import { renderToString } from 'vue/server-renderer'

import {
  createHarness,
  dataResponse,
  normalize,
  deferred,
  escapeHtml,
} from './knowledgeBaseManagementHarness.js'

test('keeps owner identity when displaying same-name knowledge bases and profiles', async t => {
  const knowledgeBases = [
    {
      id: 'kb-owner-a',
      uid: 'owner-a',
      username: 'Alice',
      name: 'Shared knowledge base',
      description: 'Owner A',
      embedding_channel_id: 1,
      embedding_model_id: 'embedding-a',
      knowledge_base_type: 'user',
      profile_ids: ['profile-owner-a'],
      profile_names: ['Shared profile'],
    },
    {
      id: 'kb-owner-b',
      uid: 'owner-b',
      username: 'Bob',
      name: 'Shared knowledge base',
      description: 'Owner B',
      embedding_channel_id: 2,
      embedding_model_id: 'embedding-b',
      knowledge_base_type: 'user',
      profile_ids: ['profile-owner-b'],
      profile_names: ['Shared profile'],
    },
  ]

  for (const scenario of [
    { superuser: false, currentUid: 'owner-a', currentUsername: 'Alice' },
    { superuser: true, currentUid: 'admin-user', currentUsername: 'Administrator' },
  ]) {
    const harness = await createHarness(t, { ...scenario, knowledgeBases })
    await harness.module.fetchData()

    assert.equal(harness.module.isSuperuser.value, scenario.superuser)
    assert.deepEqual(harness.module.tableData.value.map(row => ({
      id: row.id,
      uid: row.uid,
      username: row.username,
      name: row.name,
      profile_ids: row.profile_ids,
      profile_names: row.profile_names,
    })), [
      {
        id: 'kb-owner-a',
        uid: 'owner-a',
        username: 'Alice',
        name: 'Shared knowledge base',
        profile_ids: ['profile-owner-a'],
        profile_names: ['Shared profile'],
      },
      {
        id: 'kb-owner-b',
        uid: 'owner-b',
        username: 'Bob',
        name: 'Shared knowledge base',
        profile_ids: ['profile-owner-b'],
        profile_names: ['Shared profile'],
      },
    ])

    const [rowA, rowB] = harness.module.tableData.value
    harness.module.showEditDialog(rowA)
    assert.equal(harness.module.form.username, 'Alice')
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(rowA), 'Alice')
    assert.equal(harness.module.editingId.value, 'kb-owner-a')

    harness.module.showEditDialog(rowB)
    assert.equal(harness.module.form.username, 'Bob')
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(rowB), 'Bob')
    assert.equal(harness.module.editingId.value, 'kb-owner-b')

    await harness.module.showDialog()
    assert.equal(harness.module.form.username, scenario.currentUsername)
    assert.equal(harness.module.editingId.value, null)
    assert.equal(harness.module.isEditing.value, false)
  }
})

test('does not request owner users for ordinary users', async t => {
  const harness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    users: [{ uid: 'owner-b', username: 'Bob' }],
  })

  await harness.module.showDialog()

  assert.equal(harness.api.userLists.length, 0)
  assert.deepEqual(normalize(harness.module.ownerUsers.value), [])
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, null)
  assert.equal(harness.module.form.uid, null)
})

test('loads every owner-user page without replacing the current selection', async t => {
  const users = Array.from({ length: 1101 }, (_, index) => ({
    uid: `user-${index}`,
    username: `User ${index}`,
    is_active: true,
  }))
  users[500] = { uid: 'admin-user', username: 'Administrator', is_active: true }
  users[1000] = { uid: 'inactive-user', username: 'Inactive User', is_active: false }
  users[1100] = { uid: 'last-page-user', username: 'Last Page User', is_active: false }

  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users,
  })

  await harness.module.showDialog()

  assert.equal(harness.api.userLists.length, 12)
  assert.deepEqual(harness.api.userLists.map(request => request.params), Array.from({ length: 12 }, (_, index) => ({
    page: index + 1,
    size: 100,
  })))
  assert.deepEqual(normalize(harness.module.ownerUsers.value), users)
  assert.equal(harness.module.form.uid, 'admin-user')
  assert.equal(harness.module.getKnowledgeBaseOwnerLabel(users[1000]), 'Inactive User')
  assert.equal(harness.module.getKnowledgeBaseOwnerLabel(users[1100]), 'Last Page User')
  assert.ok(!harness.module.getKnowledgeBaseOwnerLabel(users[1000]).includes(users[1000].uid))
  assert.ok(!harness.module.getKnowledgeBaseOwnerLabel(users[1100]).includes(users[1100].uid))
})

test('reopening the owner dialog resets selection and options to the current user', async t => {
  const currentUser = { uid: 'admin-user', username: 'Administrator' }
  const users = [currentUser, { uid: 'owner-b', username: 'Bob' }]
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: currentUser.uid,
    currentUsername: currentUser.username,
    users,
  })

  await harness.module.showDialog()
  harness.module.form.uid = 'owner-b'
  harness.module.form.username = 'Bob'

  const reopening = harness.module.showDialog()
  assert.equal(harness.module.form.uid, currentUser.uid)
  assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
  await reopening

  assert.equal(harness.module.form.uid, currentUser.uid)
  assert.deepEqual(normalize(harness.module.ownerUsers.value), users)
  assert.equal(harness.module.ownerUsersError.value, null)
})

test('clears the owner selection when a successful list excludes the current user or is empty', async t => {
  for (const users of [
    [{ uid: 'owner-b', username: 'Bob' }],
    [],
  ]) {
    const harness = await createHarness(t, {
      superuser: true,
      currentUid: 'admin-user',
      currentUsername: 'Administrator',
      users,
    })

    await harness.module.showDialog()

    assert.deepEqual(normalize(harness.module.ownerUsers.value), users)
    assert.equal(harness.module.form.uid, null)
    assert.equal(harness.module.ownerUsersLoading.value, false)
    assert.equal(harness.module.ownerUsersError.value, null)
  }
})

test('keeps only the current owner visible while the owner list is loading', async t => {
  const request = deferred()
  const currentUser = { uid: 'admin-user', username: 'Administrator' }
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: currentUser.uid,
    currentUsername: currentUser.username,
    userList: () => request.promise,
  })

  const pending = harness.module.showDialog()

  assert.deepEqual(harness.api.userLists[0].params, { page: 1, size: 100 })
  assert.deepEqual(normalize(harness.module.ownerUsers.value.map(user => user.username)), [currentUser.username])
  assert.equal(harness.module.form.uid, currentUser.uid)
  assert.equal(harness.module.ownerUsersLoading.value, true)

  request.resolve(dataResponse({ items: [currentUser], total: 1 }))
  await pending
  assert.equal(harness.module.ownerUsersLoading.value, false)
})

test('keeps the old options on a failed page and restores them after a successful retry', async t => {
  for (const locale of ['zh', 'en']) {
    const currentUser = { uid: 'admin-user', username: 'Administrator' }
    const otherUser = { uid: 'owner-b', username: 'Bob' }
    const firstPage = deferred()
    const failedPage = deferred()
    let callCount = 0
    const harness = await createHarness(t, {
      locale,
      superuser: true,
      currentUid: currentUser.uid,
      currentUsername: currentUser.username,
      userList: params => {
        callCount += 1
        if (callCount === 1) return firstPage.promise
        if (callCount === 2) return failedPage.promise
        return Promise.resolve(dataResponse({
          items: params.page === 1 ? [currentUser] : [otherUser],
          total: 2,
        }))
      },
    })

    const pending = harness.module.showDialog()
    firstPage.resolve(dataResponse({ items: [currentUser], total: 2 }))
    await Promise.resolve()
    assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
    assert.equal(harness.module.ownerUsersLoading.value, true)

    failedPage.reject(new Error('network error'))
    await pending

    assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
    assert.equal(harness.module.form.uid, currentUser.uid)
    assert.equal(harness.module.ownerUsersLoading.value, false)
    assert.equal(
      harness.module.ownerUsersError.value,
      locale === 'zh' ? '获取用户列表失败：network error' : 'Failed to load users: network error',
    )

    await harness.module.fetchOwnerUsers()
    assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser, otherUser])
    assert.equal(harness.module.form.uid, currentUser.uid)
    assert.equal(harness.module.ownerUsersError.value, null)
    assert.equal(harness.module.ownerUsersLoading.value, false)
  }
})

test('submits owner uid only for superuser creates and never for ordinary users or edits', async t => {
  const owner = { uid: 'owner-b', username: 'Bob' }
  const installFormRef = harness => {
    const state = { validateCalls: 0 }
    harness.module.formRef.value = {
      resetFields() {},
      validate: async callback => {
        state.validateCalls += 1
        if (callback) await callback(true)
        return true
      },
    }
    return state
  }

  const superuserHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
    currentUsername: 'Administrator',
    users: [
      { uid: 'admin-user', username: 'Administrator' },
      owner,
    ],
  })
  const superuserForm = installFormRef(superuserHarness)
  await superuserHarness.module.showDialog()
  superuserHarness.module.form.uid = owner.uid
  superuserHarness.module.form.name = 'Owned knowledge base'
  superuserHarness.module.form.description = 'Created for Bob'
  superuserHarness.module.form.embedding_model_key = '2::embed-v1'

  assert.equal(superuserHarness.module.isEditing.value, false)
  assert.equal(superuserHarness.module.rules.value.uid[0].required, true)
  await superuserHarness.module.submitForm()

  assert.equal(superuserForm.validateCalls, 1)
  assert.deepEqual(normalize(superuserHarness.api.creates[0].payload), {
    name: 'Owned knowledge base',
    description: 'Created for Bob',
    embedding_channel_id: 2,
    embedding_model_id: 'embed-v1',
    uid: owner.uid,
  })
  assert.equal(Object.prototype.hasOwnProperty.call(superuserHarness.api.creates[0].payload, 'username'), false)

  const ordinaryHarness = await createHarness(t, {
    currentUid: 'user-a',
    currentUsername: 'Alice',
    users: [owner],
  })
  const ordinaryForm = installFormRef(ordinaryHarness)
  await ordinaryHarness.module.showDialog()
  ordinaryHarness.module.form.uid = 'other-user'
  ordinaryHarness.module.form.username = 'forged'
  ordinaryHarness.module.form.name = 'Ordinary knowledge base'
  ordinaryHarness.module.form.description = 'Created by Alice'
  ordinaryHarness.module.form.embedding_model_key = '2::embed-v1'

  assert.equal(ordinaryHarness.api.userLists.length, 0)
  assert.equal(ordinaryHarness.module.rules.value.uid[0].required, false)
  await ordinaryHarness.module.submitForm()

  assert.equal(ordinaryForm.validateCalls, 1)
  assert.deepEqual(normalize(ordinaryHarness.api.creates[0].payload), {
    name: 'Ordinary knowledge base',
    description: 'Created by Alice',
    embedding_channel_id: 2,
    embedding_model_id: 'embed-v1',
  })

  const row = {
    id: 'kb-owner-b',
    uid: owner.uid,
    username: owner.username,
    name: 'Existing knowledge base',
    description: 'Existing description',
    embedding_channel_id: 2,
    embedding_model_id: 'embed-v1',
  }
  for (const scenario of [
    { superuser: false, currentUid: 'user-a', currentUsername: 'Alice' },
    { superuser: true, currentUid: 'admin-user', currentUsername: 'Administrator' },
  ]) {
    const harness = await createHarness(t, scenario)
    const form = installFormRef(harness)
    harness.module.showEditDialog(row)
    harness.module.form.name = 'Updated knowledge base'
    harness.module.form.description = 'Updated description'

    assert.equal(harness.api.userLists.length, 0)
    assert.equal(harness.module.form.uid, owner.uid)
    assert.equal(harness.module.form.username, owner.username)
    assert.equal(harness.module.rules.value.uid[0].required, false)
    await harness.module.submitForm()

    assert.equal(form.validateCalls, 1)
    assert.deepEqual(normalize(harness.api.updates[0].payload), {
      name: 'Updated knowledge base',
      description: 'Updated description',
    })
  }
})

test('blocks owner create while users load or fail, then submits the selected retry owner', async t => {
  for (const locale of ['zh', 'en']) {
    const currentUser = { uid: 'admin-user', username: 'Administrator' }
    const owner = { uid: 'owner-b', username: 'Bob' }
    const firstRequest = deferred()
    const retryRequest = deferred()
    let userListCalls = 0
    const harness = await createHarness(t, {
      locale,
      superuser: true,
      currentUid: currentUser.uid,
      currentUsername: currentUser.username,
      userList: () => {
        userListCalls += 1
        return userListCalls === 1 ? firstRequest.promise : retryRequest.promise
      },
    })
    const form = { validateCalls: 0 }
    harness.module.formRef.value = {
      resetFields() {},
      validate: async callback => {
        form.validateCalls += 1
        if (callback) await callback(true)
        return true
      },
    }

    const pending = harness.module.showDialog()
    assert.equal(harness.module.ownerUsersLoading.value, true)
    harness.module.form.name = 'Retry-owned knowledge base'
    harness.module.form.description = 'Created after retry'
    harness.module.form.embedding_model_key = '2::embed-v1'

    await harness.module.submitForm()
    assert.equal(form.validateCalls, 0)
    assert.equal(harness.api.creates.length, 0)

    firstRequest.reject(new Error('network error'))
    await pending
    assert.equal(
      harness.module.ownerUsersError.value,
      locale === 'zh' ? '获取用户列表失败：network error' : 'Failed to load users: network error',
    )

    await harness.module.submitForm()
    assert.equal(form.validateCalls, 0)
    assert.equal(harness.api.creates.length, 0)

    const retry = harness.module.fetchOwnerUsers()
    assert.equal(harness.module.ownerUsersLoading.value, true)
    retryRequest.resolve(dataResponse({ items: [currentUser, owner], total: 2 }))
    await retry
    assert.equal(harness.module.ownerUsersError.value, null)

    harness.module.form.uid = owner.uid
    await harness.module.submitForm()

    assert.equal(form.validateCalls, 1)
    assert.deepEqual(normalize(harness.api.creates.map(request => request.payload)), [{
      name: 'Retry-owned knowledge base',
      description: 'Created after retry',
      embedding_channel_id: 2,
      embedding_model_id: 'embed-v1',
      uid: owner.uid,
    }])
    assert.notEqual(harness.api.creates[0].payload.uid, currentUser.uid)
  }
})

test('ignores an old response after retry, reopen, edit, unmount, role change, or login change', async t => {
  for (const action of ['retry', 'reopen', 'edit', 'unmount', 'role', 'uid']) {
    const currentUser = { uid: 'admin-user', username: 'Administrator' }
    const staleRequest = deferred()
    const freshRequest = deferred()
    let callCount = 0
    const harness = await createHarness(t, {
      superuser: true,
      currentUid: currentUser.uid,
      currentUsername: currentUser.username,
      userList: () => {
        callCount += 1
        return ['retry', 'reopen'].includes(action) && callCount === 2
          ? freshRequest.promise
          : staleRequest.promise
      },
    })
    const pending = harness.module.showDialog()

    if (action === 'retry') {
      const retry = harness.module.fetchOwnerUsers()
      freshRequest.resolve(dataResponse({ items: [currentUser], total: 1 }))
      await retry
    } else if (action === 'reopen') {
      const reopening = harness.module.showDialog()
      freshRequest.resolve(dataResponse({ items: [currentUser], total: 1 }))
      await reopening
    } else if (action === 'edit') {
      harness.module.showEditDialog({
        id: 'kb-edit',
        name: 'Edited knowledge base',
        description: '',
        embedding_channel_id: 1,
        embedding_model_id: 'embedding-model',
        uid: 'edited-owner',
        username: 'Edited owner',
      })
    } else if (action === 'unmount') {
      for (const callback of harness.unmountedCallbacks) callback()
    } else if (action === 'role') {
      harness.permissionsStore.currentUser = {
        uid: currentUser.uid,
        username: currentUser.username,
        is_superuser: false,
      }
    } else {
      harness.permissionsStore.currentUser = {
        uid: 'new-user',
        username: 'New user',
        is_superuser: true,
      }
    }

    staleRequest.resolve(dataResponse({ items: [{ uid: 'stale-owner', username: 'Stale owner' }], total: 1 }))
    await pending

    if (['unmount', 'role', 'uid'].includes(action)) {
      assert.deepEqual(normalize(harness.module.ownerUsers.value), [])
      assert.equal(harness.module.form.uid, null)
      assert.equal(harness.module.form.username, '')
      assert.equal(harness.module.dialogVisible.value, false)
    } else {
      assert.deepEqual(normalize(harness.module.ownerUsers.value), [currentUser])
      assert.equal(harness.module.form.uid, action === 'edit' ? 'edited-owner' : currentUser.uid)
    }
    if (action === 'edit') assert.equal(harness.module.isEditing.value, true)
  }
})

test('uses the localized owner identity for each knowledge-base delete confirmation', async t => {
  const knowledgeBases = [
    {
      id: 'kb-owner-a',
      uid: 'opaque-account-a',
      username: 'Alice',
      name: 'Shared knowledge base',
      profile_names: ['Shared profile'],
    },
    {
      id: 'kb-owner-b',
      uid: 'opaque-account-b',
      username: 'Bob',
      name: 'Shared knowledge base',
      profile_names: ['Shared profile'],
    },
  ]

  for (const locale of ['zh', 'en']) {
    const harness = await createHarness(t, { locale, knowledgeBases })
    await harness.module.fetchData()
    const [first, second] = harness.module.tableData.value

    assert.deepEqual(first.profile_names, second.profile_names)
    await harness.module.handleDelete(first)
    await harness.module.handleDelete(second)

    assert.equal(harness.confirms.length, 2)
    assert.equal(harness.confirms[0][1], locale === 'zh' ? '提示' : 'Prompt')
    const renderedMessages = await Promise.all(
      harness.confirms.map(([message]) => renderToString(message)),
    )
    assert.notEqual(renderedMessages[0], renderedMessages[1])

    for (const [index, [confirmation, row, other]] of [
      [0, [harness.confirms[0], first, second]],
      [1, [harness.confirms[1], second, first]],
    ]) {
      const [, , options] = confirmation
      const html = renderedMessages[index]
      assert.ok(html.includes(escapeHtml(row.username)))
      assert.ok(html.includes(escapeHtml(row.id)))
      assert.ok(html.includes(escapeHtml(row.name)))
      assert.ok(!html.includes(escapeHtml(other.username)))
      assert.ok(!html.includes(escapeHtml(row.uid)))
      assert.ok(!html.includes(escapeHtml(other.uid)))
      assert.equal(options.dangerouslyUseHTMLString, false)
    }

    assert.deepEqual(harness.api.deletes.map(request => request.id), [first.id, second.id])
  }
})

test('does not delete a knowledge base when confirmation is cancelled or closed', async t => {
  const row = {
    id: 'kb-cancelled',
    uid: 'owner-cancelled',
    name: 'Cancelled knowledge base',
  }

  for (const locale of ['zh', 'en']) {
    for (const action of ['cancel', 'close']) {
      const harness = await createHarness(t, {
        locale,
        confirm: async () => {
          if (action === 'close') throw 'close'
          return false
        },
      })

      await harness.module.handleDelete(row)
      assert.equal(harness.confirms.length, 1, `${locale}:${action}`)
      assert.equal(harness.api.deletes.length, 0, `${locale}:${action}`)
    }
  }
})

test('waits for confirmation and preserves literal names and unknown owner placeholders', async t => {
  for (const locale of ['zh', 'en']) {
    let resolveConfirmation
    const confirmation = new Promise(resolve => {
      resolveConfirmation = resolve
    })
    const harness = await createHarness(t, { locale, confirm: () => confirmation })
    const unknownOwnerLabel = locale === 'zh' ? '未知用户' : 'Unknown user'
    for (const username of [undefined, null, '']) {
      assert.equal(harness.module.getKnowledgeBaseOwnerLabel({ username }), unknownOwnerLabel)
    }
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(undefined), unknownOwnerLabel)
    assert.equal(harness.module.getKnowledgeBaseOwnerLabel(null), unknownOwnerLabel)
    const specialRow = {
      id: 'kb-special',
      uid: 'owner-special',
      name: 'KB <alpha> & "quotes"',
    }

    const pendingDelete = harness.module.handleDelete(specialRow)
    assert.equal(harness.api.deletes.length, 0)
    assert.equal(harness.confirms.length, 1)
    const [specialMessage, , specialOptions] = harness.confirms[0]
    const specialHtml = await renderToString(specialMessage)
    assert.ok(specialHtml.includes(escapeHtml(specialRow.name)))
    assert.doesNotMatch(specialHtml, /<(?:alpha|admin|script)\b/i)
    assert.equal(specialOptions.dangerouslyUseHTMLString, false)

    resolveConfirmation(true)
    await pendingDelete

    const missingUsernameRow = {
      id: 'missingKbId',
      uid: 'opaque-user-id',
      name: 'Missing UID knowledge base',
    }
    await harness.module.handleDelete(missingUsernameRow)
    const [missingMessage, , missingOptions] = harness.confirms[1]
    const missingHtml = await renderToString(missingMessage)
    assert.ok(missingHtml.includes(escapeHtml(unknownOwnerLabel)))
    assert.ok(!missingHtml.includes(escapeHtml(missingUsernameRow.uid)))
    assert.doesNotMatch(missingHtml, /undefined/)
    assert.equal(missingOptions.dangerouslyUseHTMLString, false)
    assert.deepEqual(harness.api.deletes.map(request => request.id), [specialRow.id, missingUsernameRow.id])
  }
})

test('accepts the page-change event page number and updates the requested page normally', async t => {
  const harness = await createHarness(t, {
    knowledgeBases: [{
      id: 'kb-page-two',
      uid: 'user-a',
      username: 'Alice',
      name: 'Page two KB',
      knowledge_base_type: 'user',
      embedding_channel_id: 1,
      embedding_model_id: 'embedding-model',
    }],
  })
  harness.module.currentPage.value = 2

  await harness.module.fetchData(2)

  assert.equal(harness.api.lists.at(-1).params.page, 2)
  assert.deepEqual(harness.module.tableData.value.map(row => row.id), ['kb-page-two'])
  assert.equal(harness.messages.some(message => message.type === 'error'), false)
})
