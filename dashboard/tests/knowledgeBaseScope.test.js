import assert from 'node:assert/strict'
import test from 'node:test'
import { createKnowledgeBaseScope } from '../src/utils/knowledgeBaseScope.js'

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

test('confirms successfully, treats cancel and close as false, and propagates other errors', async () => {
  const confirmed = createKnowledgeBaseScope({ confirm: async () => true })
  assert.equal(await confirmed.confirm('confirm'), true)

  for (const reason of ['cancel', 'close']) {
    const cancelled = createKnowledgeBaseScope({
      confirm: async () => { throw reason },
    })
    assert.equal(await cancelled.confirm('cancel'), false)
  }

  const error = new Error('confirmation failed')
  const failed = createKnowledgeBaseScope({
    confirm: async () => { throw error },
  })
  await assert.rejects(failed.confirm('fail'), received => received === error)
})

test('does not show a confirmation when the scope is inactive', async () => {
  let confirmCalls = 0
  const scope = createKnowledgeBaseScope({
    isActive: () => false,
    confirm: async () => {
      confirmCalls += 1
      return true
    },
  })

  assert.equal(await scope.confirm('inactive'), false)
  assert.equal(confirmCalls, 0)
})

test('capture guards follow activity and stay invalid after A-to-B-to-A changes', () => {
  let active = true
  const scope = createKnowledgeBaseScope({
    isActive: () => active,
    confirm: async () => true,
  })
  const capture = scope.capture()

  assert.equal(capture(), true)
  active = false
  assert.equal(capture(), false)
  active = true
  assert.equal(capture(), true)

  active = false
  scope.invalidate()
  active = true
  assert.equal(capture(), false)
  assert.equal(scope.capture()(), true)
})

test('invalidation settles pending confirmations and closes them only once', async () => {
  const confirmation = deferred()
  let closeCalls = 0
  const scope = createKnowledgeBaseScope({
    confirm: () => confirmation.promise,
    closeConfirmations: () => { closeCalls += 1 },
  })
  const pending = scope.confirm('pending')

  scope.invalidate()
  assert.equal(await pending, false)
  assert.equal(closeCalls, 1)

  scope.invalidate()
  assert.equal(closeCalls, 1)
})

test('late confirmation outcomes do not reject or affect a reused scope', async () => {
  const unhandledRejections = []
  const onUnhandledRejection = reason => unhandledRejections.push(reason)
  process.on('unhandledRejection', onUnhandledRejection)

  try {
    for (const outcome of ['success', 'failure']) {
      const confirmation = deferred()
      let confirmCalls = 0
      const scope = createKnowledgeBaseScope({
        confirm: () => {
          confirmCalls += 1
          return confirmCalls === 1 ? confirmation.promise : Promise.resolve(true)
        },
        closeConfirmations() {},
      })
      const oldPending = scope.confirm('old confirmation')
      scope.invalidate()
      assert.equal(await oldPending, false)
      assert.equal(await scope.confirm('new confirmation'), true)

      if (outcome === 'success') confirmation.resolve(true)
      else confirmation.reject(new Error('late confirmation failure'))
      await new Promise(resolve => setImmediate(resolve))

      assert.equal(await scope.confirm('scope remains active'), true)
      assert.equal(confirmCalls, 3)
    }

    assert.deepEqual(unhandledRejections, [])
  } finally {
    process.removeListener('unhandledRejection', onUnhandledRejection)
  }
})
