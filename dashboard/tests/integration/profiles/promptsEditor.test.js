import assert from 'node:assert/strict'
import test from 'node:test'
import {
  conflictError,
  createDeferred,
  createHarness,
  response,
  snapshot,
} from './promptsViewHarness.js'

test('creates an ordinary prompt for its own identity without sending client ownership', async t => {
  for (const isSuperuser of [false, 'true']) {
    const harness = createHarness(t)
    await harness.flush()
    harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
    harness.permissions.isSuperuser = isSuperuser

    harness.module.showDialog('create')
    assert.equal(harness.module.form.uid, 'user-a')
    assert.equal(harness.module.form.username, 'Alice')
    assert.equal(harness.apiState.userListRequests.length, 0)

    harness.module.form.uid = 'foreign-user'
    harness.module.form.name = `ordinary-${String(isSuperuser)}`
    harness.module.form.content = 'content'
    await harness.module.submitForm()

    assert.equal(harness.apiState.userListRequests.length, 0)
    assert.deepEqual(snapshot(harness.apiState.createRequests.map(request => request.payload)), [
      {
        name: `ordinary-${String(isSuperuser)}`,
        content: 'content',
      },
    ])
  }
})

test('loads all owners before a superuser can submit a selected owner', async t => {
  const firstPage = createDeferred()
  const admin = { uid: 'admin', username: 'Admin' }
  const userB = { uid: 'user-b', username: 'Bob' }
  const harness = createHarness(t)
  await harness.flush()
  harness.apiState.userListHandler = params => {
    if (params.page === 1) return firstPage.promise
    return Promise.resolve(response([userB], 2))
  }
  harness.permissions.currentUser = admin
  harness.permissions.isSuperuser = true

  harness.module.showDialog('create')
  assert.equal(harness.module.form.uid, 'admin')
  assert.equal(harness.module.ownerUsersLoading.value, true)
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
  ])

  harness.module.form.name = 'admin prompt'
  harness.module.form.content = 'prompt content'
  await harness.module.submitForm()
  assert.equal(harness.apiState.createRequests.length, 0)

  firstPage.resolve(response([admin], 2))
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.deepEqual(snapshot(harness.module.ownerUsers.value), [admin, userB])
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
    { page: 2, size: 1000 },
  ])

  harness.module.form.uid = 'user-b'
  await harness.module.submitForm()
  assert.deepEqual(snapshot(harness.apiState.createRequests.map(request => request.payload)), [
    {
      name: 'admin prompt',
      content: 'prompt content',
      uid: 'user-b',
    },
  ])
})

test('blocks creation after owner loading fails until a retry succeeds', async t => {
  const admin = { uid: 'admin', username: 'Admin' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = admin
  harness.permissions.isSuperuser = true
  harness.apiState.userListHandler = async () => {
    throw new Error('owner list failed')
  }

  harness.module.showDialog('create')
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, 'prompts.load_users_failed')
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'prompts.load_users_failed'
  )))

  harness.module.form.name = 'retry prompt'
  harness.module.form.content = 'retry content'
  await harness.module.submitForm()
  assert.equal(harness.apiState.createRequests.length, 0)

  harness.apiState.userListHandler = async () => response([admin], 1)
  await harness.module.fetchOwnerUsers()
  assert.equal(harness.module.ownerUsersError.value, '')
  assert.deepEqual(snapshot(harness.module.ownerUsers.value), [admin])

  await harness.module.submitForm()
  assert.deepEqual(snapshot(harness.apiState.createRequests.map(request => request.payload)), [
    {
      name: 'retry prompt',
      content: 'retry content',
      uid: 'admin',
    },
  ])
})

test('blocks missing owner and required fields without creating a prompt', async t => {
  const missingOwner = createHarness(t)
  await missingOwner.flush()
  missingOwner.permissions.currentUser = { uid: null, username: 'Admin' }
  missingOwner.permissions.isSuperuser = true
  missingOwner.module.showDialog('create')
  await missingOwner.flush()
  missingOwner.module.form.name = 'missing owner'
  missingOwner.module.form.content = 'content'
  missingOwner.module.form.uid = null
  await missingOwner.module.submitForm()
  assert.equal(missingOwner.apiState.createRequests.length, 0)
  assert.ok(missingOwner.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_owner'
  )))

  const missingFields = createHarness(t)
  await missingFields.flush()
  missingFields.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  missingFields.permissions.isSuperuser = false
  missingFields.module.showDialog('create')
  missingFields.module.form.name = ''
  missingFields.module.form.content = 'content'
  await missingFields.module.submitForm()
  assert.equal(missingFields.apiState.createRequests.length, 0)
  assert.ok(missingFields.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.fill_required'
  )))
})

test('submits edits without changing ownership and rechecks the current identity', async t => {
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  const prompt = {
    id: 17,
    name: 'old name',
    content: 'old content',
    uid: 'user-a',
    username: 'Alice',
  }

  harness.module.showDialog('edit', prompt)
  harness.module.form.name = 'new name'
  harness.module.form.content = 'new content'
  await harness.module.submitForm()

  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: 'new name',
    content: 'new content',
  })
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.form.username, 'Alice')

  harness.permissions.currentUser = { uid: 'user-b', username: 'Bob' }
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.form.username, 'Alice')
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.permission_denied'
  )))
})

test('prevents duplicate creation during a slow request and refreshes after success', async t => {
  const createRequest = createDeferred()
  const harness = createHarness(t, { createHandler: () => createRequest.promise })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  harness.module.showDialog('create')
  harness.module.form.name = 'slow prompt'
  harness.module.form.content = 'slow content'

  const firstSubmit = harness.module.submitForm()
  assert.equal(harness.module.submitting.value, true)
  await harness.module.submitForm()
  assert.equal(harness.apiState.createRequests.length, 1)

  createRequest.resolve(response())
  await firstSubmit
  await harness.flush()
  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.module.dialogVisible.value, false)
  assert.equal(harness.apiState.createRequests.length, 1)
  assert.equal(harness.apiState.listRequests.length, 2)
})

test('retains the form after a failed creation and supports retry', async t => {
  const failedRequest = createDeferred()
  let attempt = 0
  const harness = createHarness(t, {
    createHandler: () => {
      attempt += 1
      if (attempt === 1) return failedRequest.promise
      return Promise.resolve(response())
    },
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  harness.module.showDialog('create')
  harness.module.form.name = 'retryable prompt'
  harness.module.form.content = 'retryable content'

  const failedSubmit = harness.module.submitForm()
  assert.equal(harness.module.submitting.value, true)
  failedRequest.reject(new Error('save failed'))
  await failedSubmit
  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.name, 'retryable prompt')
  assert.equal(harness.module.form.content, 'retryable content')
  assert.ok(harness.messages.some(message => message.type === 'error' && message.value === 'save failed'))

  await harness.module.submitForm()
  await harness.flush()
  assert.equal(harness.apiState.createRequests.length, 2)
  assert.equal(harness.module.dialogVisible.value, false)
  assert.equal(harness.module.submitting.value, false)
})

test('updates a superuser prompt after paginated owner loading and refreshes the saved owner', async t => {
  const ownerPage = createDeferred()
  const prompt = {
    id: 901,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const updatedPrompt = {
    id: prompt.id,
    name: 'updated name',
    content: 'updated content',
    uid: 'user-b',
    username: 'Bob',
  }
  const originalPrompt = snapshot(prompt)
  let listCall = 0
  const harness = createHarness(t, {
    listHandler: async () => {
      listCall += 1
      return listCall === 1 ? response([prompt], 1, { current_uid: 'admin' }) : response([updatedPrompt], 1, { current_uid: 'admin' })
    },
    userListHandler: params => {
      if (params.page === 1) return ownerPage.promise
      return Promise.resolve(response([{ uid: 'user-b', username: 'Bob' }], 2))
    },
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  assert.equal(harness.module.form.uid, 'user-a')
  assert.equal(harness.module.ownerUsersLoading.value, true)
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
  ])

  harness.module.form.name = updatedPrompt.name
  harness.module.form.content = updatedPrompt.content
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 0)

  ownerPage.resolve(response([{ uid: 'user-a', username: 'Alice' }], 2))
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.form.uid, 'user-a')
  assert.deepEqual(snapshot(harness.apiState.userListRequests.map(request => request.params)), [
    { page: 1, size: 1000 },
    { page: 2, size: 1000 },
  ])

  harness.module.form.uid = updatedPrompt.uid
  await harness.module.submitForm()
  await harness.flush()

  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: updatedPrompt.name,
    content: updatedPrompt.content,
    uid: updatedPrompt.uid,
  })
  assert.equal(harness.module.dialogVisible.value, false)
  assert.deepEqual(snapshot(harness.module.prompts.value), [updatedPrompt])
  assert.equal(harness.module.prompts.value[0].username, 'Bob')
  assert.equal(harness.module.getOwnerLabel(harness.module.prompts.value[0]), 'Bob')
  assert.deepEqual(snapshot(harness.apiState.listRequests.map(request => request.params)), [
    { page: 1, size: 10 },
    { page: 1, size: 10 },
  ])

  harness.module.showDialog('edit', harness.module.prompts.value[0])
  assert.equal(harness.module.form.id, prompt.id)
  assert.equal(harness.module.form.uid, updatedPrompt.uid)
  assert.deepEqual(snapshot(prompt), originalPrompt)
})

test('blocks a superuser edit after owner loading fails, then retries with a new owner', async t => {
  const prompt = {
    id: 1001,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const newOwner = { uid: 'user-b', username: 'Bob' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true
  harness.apiState.userListHandler = async () => {
    throw new Error('owner list failed')
  }

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.uid, prompt.uid)
  assert.equal(harness.module.form.username, prompt.username)
  assert.equal(harness.module.ownerUsersError.value, 'prompts.load_users_failed')
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'prompts.load_users_failed'
  )))

  harness.module.form.name = 'updated name'
  harness.module.form.content = 'updated content'
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.uid, prompt.uid)
  assert.equal(harness.module.form.username, prompt.username)

  harness.apiState.userListHandler = async () => response([prompt, newOwner], 2)
  await harness.module.fetchOwnerUsers()
  assert.equal(harness.module.ownerUsersError.value, '')
  assert.deepEqual(snapshot(harness.module.ownerUsers.value), [prompt, newOwner])

  harness.module.form.uid = newOwner.uid
  await harness.module.submitForm()
  await harness.flush()
  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: 'updated name',
    content: 'updated content',
    uid: newOwner.uid,
  })
  assert.equal(harness.module.dialogVisible.value, false)
})

test('blocks a superuser edit when the owner is cleared', async t => {
  const prompt = {
    id: 1101,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const harness = createHarness(t, {
    listResponse: response([prompt], 1, { current_uid: 'admin' }),
    userListHandler: async () => response([{ uid: 'user-a', username: 'Alice' }], 1),
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  harness.module.form.uid = null
  harness.module.form.name = 'updated name'
  harness.module.form.content = 'updated content'
  await harness.module.submitForm()

  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_owner'
  )))
})

test('requires a superuser to assign an owner before updating an unassigned prompt', async t => {
  const prompt = {
    id: 1151,
    name: 'original name',
    content: 'original content',
    uid: null,
    username: null,
  }
  const owner = { uid: 'user-b', username: 'Bob' }
  const updatedPrompt = {
    ...prompt,
    ...owner,
    name: 'updated name',
    content: 'updated content',
  }
  let listCall = 0
  const harness = createHarness(t, {
    listHandler: async () => {
      listCall += 1
      return response([listCall === 1 ? prompt : updatedPrompt], 1, { current_uid: 'admin' })
    },
    userListHandler: async () => response([owner], 1),
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  assert.equal(harness.module.form.uid, null)
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, '')

  harness.module.form.name = updatedPrompt.name
  harness.module.form.content = updatedPrompt.content
  await harness.module.submitForm()

  assert.equal(harness.apiState.updateRequests.length, 0)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_owner'
  )))

  harness.module.form.uid = owner.uid
  await harness.module.submitForm()
  await harness.flush()

  assert.equal(harness.apiState.updateRequests.length, 1)
  assert.equal(harness.apiState.updateRequests[0].id, prompt.id)
  assert.deepEqual(snapshot(harness.apiState.updateRequests[0].payload), {
    name: updatedPrompt.name,
    content: updatedPrompt.content,
    uid: updatedPrompt.uid,
  })
  assert.equal(harness.module.dialogVisible.value, false)
  assert.deepEqual(snapshot(harness.module.prompts.value), [updatedPrompt])
  assert.deepEqual(snapshot(harness.apiState.listRequests.map(request => request.params)), [
    { page: 1, size: 10 },
    { page: 1, size: 10 },
  ])
})

test('retains a superuser edit after a prompt-in-use conflict and retries with the same payload', async t => {
  const prompt = {
    id: 1201,
    name: 'original name',
    content: 'original content',
    uid: 'user-a',
    username: 'Alice',
  }
  const newOwner = { uid: 'user-b', username: 'Bob' }
  const firstUpdate = createDeferred()
  let updateAttempt = 0
  let listAttempt = 0
  const harness = createHarness(t, {
    listHandler: async () => {
      listAttempt += 1
      return listAttempt === 1
        ? response([prompt], 1, { current_uid: 'admin' })
        : response([{ ...prompt, name: 'updated name', content: 'updated content', ...newOwner }], 1, { current_uid: 'admin' })
    },
    userListHandler: async () => response([{ uid: prompt.uid, username: prompt.username }, newOwner], 2),
    updateHandler: () => {
      updateAttempt += 1
      return updateAttempt === 1 ? firstUpdate.promise : Promise.resolve(response())
    },
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true

  harness.module.showDialog('edit', prompt)
  await harness.flush()
  assert.equal(harness.module.ownerUsersLoading.value, false)
  assert.equal(harness.module.ownerUsersError.value, '')

  harness.module.form.name = 'updated name'
  harness.module.form.content = 'updated content'
  harness.module.form.uid = newOwner.uid
  const firstSubmit = harness.module.submitForm()
  assert.equal(harness.module.submitting.value, true)
  await harness.module.submitForm()
  assert.equal(harness.apiState.updateRequests.length, 1)

  firstUpdate.reject(conflictError())
  await firstSubmit
  await harness.flush()

  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.module.dialogVisible.value, true)
  assert.equal(harness.module.form.name, 'updated name')
  assert.equal(harness.module.form.content, 'updated content')
  assert.equal(harness.module.form.uid, newOwner.uid)
  assert.equal(harness.apiState.listRequests.length, 1)
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'prompt is in use'
  )))

  await harness.module.submitForm()
  await harness.flush()

  assert.equal(harness.apiState.updateRequests.length, 2)
  assert.deepEqual(snapshot(harness.apiState.updateRequests.map(request => request.payload)), [
    {
      name: 'updated name',
      content: 'updated content',
      uid: newOwner.uid,
    },
    {
      name: 'updated name',
      content: 'updated content',
      uid: newOwner.uid,
    },
  ])
  assert.equal(harness.module.dialogVisible.value, false)
  assert.equal(harness.module.submitting.value, false)
  assert.equal(harness.apiState.listRequests.length, 2)
})
