import assert from 'node:assert/strict'
import test from 'node:test'
import { renderToString } from 'vue/server-renderer'
import { createHarness, snapshot } from './scheduledTasksHarness.js'

test('deleteTask waits for confirmation, deletes the correct id, and refreshes on success', async () => {
  const { apiState, messages, confirmationCalls, page, settleConfirmation } = createHarness()
  const deletion = page.deleteTask({ id: 12, name: 'Task to delete' })

  assert.deepEqual(apiState.deleteRequests, [])
  assert.deepEqual(apiState.listRequests, [])
  assert.equal(confirmationCalls.length, 1)

  const confirmation = confirmationCalls[0]
  const html = await renderToString(confirmation.message)
  assert.match(html, /<p class="confirm-dialog-content__message">scheduledTasks\.delete_confirm<\/p>/)
  assert.equal(confirmation.title, 'common.warning')
  assert.equal(confirmation.options.confirmButtonText, 'common.confirm')
  assert.equal(confirmation.options.cancelButtonText, 'common.cancel')
  assert.equal(confirmation.options.customClass, 'app-confirm-dialog')

  settleConfirmation()
  await deletion

  assert.deepEqual(apiState.deleteRequests, [{ id: 12 }])
  assert.deepEqual(apiState.listRequests, [{ page: 1, size: 20 }])
  assert.deepEqual(messages, [{ type: 'success', value: 'scheduledTasks.delete_success' }])
})

test('cancelled or closed delete confirmations have no side effects', async () => {
  for (const action of ['cancel', 'close']) {
    const { apiState, messages, page, settleConfirmation } = createHarness()
    const deletion = page.deleteTask({ id: 13, name: `Task ${action}` })

    assert.deepEqual(apiState.deleteRequests, [])
    settleConfirmation(action)
    await deletion

    assert.deepEqual(apiState.deleteRequests, [])
    assert.deepEqual(apiState.listRequests, [])
    assert.deepEqual(messages, [])
  }
})

test('confirmation and delete API failures show errors without refreshing', async () => {
  const confirmationFailure = new Error('confirmation failed')
  const confirmationHarness = createHarness()
  const failedConfirmation = confirmationHarness.page.deleteTask({ id: 14 })
  confirmationHarness.settleConfirmation(confirmationFailure)
  await failedConfirmation

  assert.deepEqual(confirmationHarness.apiState.deleteRequests, [])
  assert.deepEqual(confirmationHarness.apiState.listRequests, [])
  assert.deepEqual(confirmationHarness.messages, [
    { type: 'error', value: confirmationFailure.message },
  ])

  const deleteFailure = new Error('delete failed')
  const deleteHarness = createHarness({
    deleteHandler: async () => { throw deleteFailure },
  })
  const failedDelete = deleteHarness.page.deleteTask({ id: 15 })
  deleteHarness.settleConfirmation()
  await failedDelete

  assert.deepEqual(deleteHarness.apiState.deleteRequests, [{ id: 15 }])
  assert.deepEqual(deleteHarness.apiState.listRequests, [])
  assert.deepEqual(deleteHarness.messages, [
    { type: 'error', value: deleteFailure.message },
  ])
})

test('toggleStatus only applies server status and next_run_at, preserving username; failures leave the row unchanged', async () => {
  for (const { id, initialStatus, expectedStatus, serverNextRunAt } of [
    {
      id: 6,
      initialStatus: 'enabled',
      expectedStatus: 'disabled',
      serverNextRunAt: '2026-10-19T16:00:00.000Z',
    },
    {
      id: 8,
      initialStatus: 'disabled',
      expectedStatus: 'enabled',
      serverNextRunAt: '2026-10-19T17:00:00.000Z',
    },
  ]) {
    const successHarness = createHarness({
      updateHandler: async () => ({
        data: { data: { status: expectedStatus, next_run_at: serverNextRunAt, username: null } },
      }),
    })
    const row = {
      id,
      name: 'Owned task',
      status: initialStatus,
      run_once: true,
      next_run_at: '2026-10-19T15:00:00.000Z',
      username: 'list-owner',
    }
    await successHarness.page.toggleStatus(row)

    assert.deepEqual(successHarness.apiState.updateRequests.map(({ id: requestId, payload }) => ({
      id: requestId,
      payload: snapshot(payload),
    })), [{ id, payload: { status: expectedStatus } }])
    assert.deepEqual(snapshot(row), {
      id,
      name: 'Owned task',
      status: expectedStatus,
      run_once: true,
      next_run_at: serverNextRunAt,
      username: 'list-owner',
    })
  }

  const failureHarness = createHarness({
    updateHandler: async () => { throw new Error('status update failed') },
  })
  const failedRow = {
    id: 7,
    status: 'enabled',
    next_run_at: 'unchanged-next-run',
    username: 'unchanged-owner',
  }
  const failedRowBefore = snapshot(failedRow)
  await failureHarness.page.toggleStatus(failedRow)

  assert.deepEqual(snapshot(failedRow), failedRowBefore)
  assert.equal(failureHarness.messages.at(-1).type, 'error')
})
