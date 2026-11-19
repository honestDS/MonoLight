import assert from 'node:assert/strict'
import test from 'node:test'
import { renderToString } from 'vue/server-renderer'
import {
  conflictError,
  createDeferred,
  createHarness,
  response,
  snapshot,
} from './promptsViewHarness.js'

test('reassigns an ordinary owner prompt only to a same-owner paginated replacement', async t => {
  const target = { id: 101, name: 'target', content: 'target content', uid: 'user-a' }
  const samePageReplacement = { id: 102, name: 'same-page', uid: 'user-a' }
  const foreignPrompt = { id: 103, name: 'foreign', uid: 'user-b' }
  const unassignedPrompt = { id: 104, name: 'unassigned', uid: null }
  const secondPageReplacement = { id: 105, name: 'second-page', uid: 'user-a' }
  const secondPageForeign = { id: 106, name: 'second-page-foreign', uid: 'user-b' }
  let deleteAttempt = 0
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a', username: 'Alice' }
  harness.permissions.isSuperuser = false
  harness.apiState.listHandler = params => {
    if (params.size !== 1000) return Promise.resolve(response())
    if (params.page === 1) {
      return Promise.resolve(response([
        target,
        samePageReplacement,
        foreignPrompt,
        unassignedPrompt,
      ], 6))
    }
    return Promise.resolve(response([secondPageReplacement, secondPageForeign], 6))
  }
  harness.apiState.deleteHandler = () => {
    deleteAttempt += 1
    return deleteAttempt === 1
      ? Promise.reject(conflictError())
      : Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.equal(harness.module.pendingDeletePrompt.value.id, target.id)
  assert.equal(harness.module.reassignDialogVisible.value, true)
  assert.deepEqual(
    snapshot(harness.module.sameOwnerPromptOptions.value).map(prompt => prompt.id),
    [samePageReplacement.id, secondPageReplacement.id],
  )
  assert.equal(harness.module.replacementPromptId.value, samePageReplacement.id)
  assert.deepEqual(snapshot(harness.apiState.listRequests.map(request => request.params)), [
    { page: 1, size: 10 },
    { page: 1, size: 1000 },
    { page: 2, size: 1000 },
  ])

  harness.module.replacementPromptId.value = secondPageReplacement.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.equal(harness.apiState.deleteRequests[1].id, target.id)
  assert.deepEqual(snapshot(harness.apiState.deleteRequests[1].params), {
    replacement_prompt_id: secondPageReplacement.id,
    confirm_reassign: true,
  })
  await harness.flush()
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.deepEqual(snapshot(harness.apiState.listRequests.at(-1).params), { page: 1, size: 10 })
})

test('restricts superuser replacement choices to the deleted prompt owner', async t => {
  const target = { id: 201, name: 'user prompt', uid: 'user-a' }
  const ownerReplacement = { id: 202, name: 'owner replacement', uid: 'user-a' }
  const adminPrompt = { id: 203, name: 'admin prompt', uid: 'admin' }
  const foreignPrompt = { id: 204, name: 'foreign prompt', uid: 'user-b' }
  const unassignedPrompt = { id: 205, name: 'unassigned prompt', uid: null }
  let deleteAttempt = 0
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'admin', username: 'Admin' }
  harness.permissions.isSuperuser = true
  harness.apiState.listHandler = params => {
    if (params.size !== 1000) return Promise.resolve(response())
    return Promise.resolve(response([
      target,
      ownerReplacement,
      adminPrompt,
      foreignPrompt,
      unassignedPrompt,
    ], 5))
  }
  harness.apiState.deleteHandler = () => {
    deleteAttempt += 1
    return deleteAttempt === 1
      ? Promise.reject(conflictError())
      : Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  assert.deepEqual(
    snapshot(harness.module.sameOwnerPromptOptions.value).map(prompt => prompt.id),
    [ownerReplacement.id],
  )

  harness.module.replacementPromptId.value = adminPrompt.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_replacement_prompt'
  )))

  harness.module.replacementPromptId.value = ownerReplacement.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.deepEqual(snapshot(harness.apiState.deleteRequests[1].params), {
    replacement_prompt_id: ownerReplacement.id,
    confirm_reassign: true,
  })
})

test('handles replacement candidate loading failures without opening the reassign dialog', async t => {
  const target = { id: 301, name: 'target', uid: 'user-a' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.deleteHandler = () => Promise.reject(conflictError())
  harness.apiState.listHandler = params => {
    if (params.size === 1000) return Promise.reject(new Error('candidate list failed'))
    return Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.deepEqual(snapshot(harness.module.replacementPromptOptions.value), [])
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'candidate list failed'
  )))
})

test('warns and stops when a conflicting prompt has no same-owner replacement', async t => {
  const target = { id: 401, name: 'target', uid: 'user-a' }
  const foreignPrompt = { id: 402, name: 'foreign', uid: 'user-b' }
  const unassignedPrompt = { id: 403, name: 'unassigned', uid: null }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.deleteHandler = () => Promise.reject(conflictError())
  harness.apiState.listHandler = params => params.size === 1000
    ? Promise.resolve(response([target, foreignPrompt, unassignedPrompt], 3))
    : Promise.resolve(response())

  await harness.module.deletePrompt(target)
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.deepEqual(snapshot(harness.module.sameOwnerPromptOptions.value), [])
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.equal(harness.module.reassignDialogVisible.value, false)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.no_replacement_prompt'
  )))
})

test('rejects foreign replacements and identity changes without a new delete request', async t => {
  const target = { id: 501, name: 'target', uid: 'user-a' }
  const sameOwnerReplacement = { id: 502, name: 'same owner', uid: 'user-a' }
  const foreignPrompt = { id: 503, name: 'foreign', uid: 'user-b' }
  const unassignedPrompt = { id: 504, name: 'unassigned', uid: null }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.deleteHandler = () => Promise.reject(conflictError())
  harness.apiState.listHandler = params => params.size === 1000
    ? Promise.resolve(response([target, sameOwnerReplacement, foreignPrompt, unassignedPrompt], 4))
    : Promise.resolve(response())

  await harness.module.deletePrompt(target)
  harness.module.replacementPromptId.value = foreignPrompt.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.select_replacement_prompt'
  )))

  harness.module.replacementPromptId.value = sameOwnerReplacement.id
  harness.permissions.currentUser = { uid: 'user-b' }
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.pendingDeletePrompt.value.id, target.id)
  assert.equal(harness.module.reassignDialogVisible.value, true)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'warning' && message.value === 'prompts.permission_denied'
  )))
})

test('waits for shared confirmation, escapes the prompt name, and refreshes after deletion', async t => {
  const confirmation = createDeferred()
  const target = { id: 601, name: '<img src=x onerror=alert(1)>', uid: 'user-a' }
  const harness = createHarness(t, {
    confirmHandler: () => confirmation.promise,
    translate: (key, params) => key === 'prompts.delete_confirm'
      ? `Delete ${params.name}`
      : key,
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }

  const deletePromise = harness.module.deletePrompt(target)
  assert.equal(harness.module.deleteSubmitting.value, true)
  assert.equal(harness.apiState.deleteRequests.length, 0)
  assert.equal(harness.apiState.listRequests.length, 1)
  assert.equal(harness.confirmations.length, 1)

  const html = await renderToString(harness.confirmations[0][0])
  assert.match(html, /Delete &lt;img src=x onerror=alert\(1\)&gt;/)
  assert.doesNotMatch(html, /<img\b/i)

  confirmation.resolve('confirm')
  await deletePromise
  await harness.flush()

  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.apiState.deleteRequests[0].id, target.id)
  assert.equal(harness.apiState.listRequests.length, 2)
  assert.equal(harness.module.deleteSubmitting.value, false)
})

test('does not delete, refresh, or report errors after confirmation cancellation or close', async t => {
  for (const action of ['cancel', 'close']) {
    const target = { id: 602, name: `target-${action}`, uid: 'user-a' }
    const harness = createHarness(t)
    await harness.flush()
    harness.permissions.currentUser = { uid: 'user-a' }
    harness.apiState.confirmHandler = async () => {
      throw action
    }

    await harness.module.deletePrompt(target)
    await harness.flush()
    assert.equal(harness.confirmations.length, 1, action)
    assert.equal(harness.apiState.deleteRequests.length, 0, action)
    assert.equal(harness.apiState.listRequests.length, 1, action)
    assert.equal(harness.messages.some(message => message.type === 'error'), false, action)
    assert.equal(harness.module.deleteSubmitting.value, false, action)
    assert.equal(harness.module.pendingDeletePrompt.value, null, action)
    assert.equal(harness.module.reassignDialogVisible.value, false, action)
  }
})

test('reports non-cancel confirmation failures without deleting', async t => {
  const target = { id: 603, name: 'target', uid: 'user-a' }
  const error = new Error('confirmation failed')
  const harness = createHarness(t, {
    confirmHandler: async () => {
      throw error
    },
  })
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }

  await harness.module.deletePrompt(target)
  await harness.flush()

  assert.equal(harness.confirmations.length, 1)
  assert.equal(harness.apiState.deleteRequests.length, 0)
  assert.equal(harness.apiState.listRequests.length, 1)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === error.message
  )))
})

test('ignores repeated delete operations during slow confirmation and deletion', async t => {
  const firstConfirmation = createDeferred()
  const slowDelete = createDeferred()
  const firstTarget = { id: 701, name: 'first target', uid: 'user-a' }
  const secondTarget = { id: 702, name: 'second target', uid: 'user-a' }
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.confirmHandler = () => firstConfirmation.promise

  const firstDelete = harness.module.deletePrompt(firstTarget)
  assert.equal(harness.module.deleteSubmitting.value, true)
  await harness.module.deletePrompt(firstTarget)
  assert.equal(harness.confirmations.length, 1)
  assert.equal(harness.apiState.deleteRequests.length, 0)

  firstConfirmation.resolve('confirm')
  await firstDelete
  assert.equal(harness.apiState.deleteRequests.length, 1)
  assert.equal(harness.module.deleteSubmitting.value, false)

  harness.apiState.confirmHandler = async () => 'confirm'
  harness.apiState.deleteHandler = () => slowDelete.promise
  const secondDelete = harness.module.deletePrompt(secondTarget)
  await harness.flush()
  assert.equal(harness.module.deleteSubmitting.value, true)
  await harness.module.deletePrompt(secondTarget)
  assert.equal(harness.confirmations.length, 2)
  assert.equal(harness.apiState.deleteRequests.length, 2)

  slowDelete.resolve(response())
  await secondDelete
  await harness.flush()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.equal(harness.module.deleteSubmitting.value, false)
})

test('keeps the pending replacement state after a failed confirmed delete for retry', async t => {
  const target = { id: 801, name: 'target', uid: 'user-a' }
  const replacement = { id: 802, name: 'replacement', uid: 'user-a' }
  let deleteAttempt = 0
  const harness = createHarness(t)
  await harness.flush()
  harness.permissions.currentUser = { uid: 'user-a' }
  harness.apiState.listHandler = params => params.size === 1000
    ? Promise.resolve(response([target, replacement], 2))
    : Promise.resolve(response())
  harness.apiState.deleteHandler = () => {
    deleteAttempt += 1
    if (deleteAttempt === 1) return Promise.reject(conflictError())
    if (deleteAttempt === 2) return Promise.reject(new Error('confirmed delete failed'))
    return Promise.resolve(response())
  }

  await harness.module.deletePrompt(target)
  harness.module.replacementPromptId.value = replacement.id
  await harness.module.confirmReassignAndDelete()
  assert.equal(harness.apiState.deleteRequests.length, 2)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.equal(harness.module.pendingDeletePrompt.value.id, target.id)
  assert.equal(harness.module.replacementPromptId.value, replacement.id)
  assert.equal(harness.module.reassignDialogVisible.value, true)
  assert.ok(harness.messages.some(message => (
    message.type === 'error' && message.value === 'confirmed delete failed'
  )))

  await harness.module.confirmReassignAndDelete()
  await harness.flush()
  assert.equal(harness.apiState.deleteRequests.length, 3)
  assert.equal(harness.module.deleteSubmitting.value, false)
  assert.equal(harness.module.pendingDeletePrompt.value, null)
  assert.equal(harness.module.replacementPromptId.value, null)
  assert.equal(harness.module.reassignDialogVisible.value, false)
})
