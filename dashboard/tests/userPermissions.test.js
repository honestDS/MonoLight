import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia } from 'pinia'
import { useUserPermissionsStore } from '../src/composables/useUserPermissions.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })

  return { promise, resolve, reject }
}

test('starts with an unknown permission state', () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)

  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)
})

for (const isSuperuser of [true, false]) {
  test(`reads boolean is_superuser=${isSuperuser} and binds the token`, async () => {
    const pinia = createPinia()
    const store = useUserPermissionsStore(pinia)
    const token = `token-${isSuperuser}`
    const user = { id: token, is_superuser: isSuperuser }

    const result = await store.refresh({
      request: async () => ({ data: { data: user } }),
      getToken: () => token,
    })

    assert.equal(result, true)
    assert.deepEqual(store.currentUser, user)
    assert.equal(store.verifiedToken, token)
    assert.equal(store.isSuperuser, isSuperuser)
  })
}

test('does not call the backend without a token', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)
  let requestCalled = false

  const result = await store.refresh({
    request: async () => {
      requestCalled = true
      return { data: { data: { is_superuser: true } } }
    },
    getToken: () => null,
  })

  assert.equal(result, false)
  assert.equal(requestCalled, false)
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)
})

const invalidResponses = [
  ['string true', { data: { data: { is_superuser: 'true' } } }],
  ['number one', { data: { data: { is_superuser: 1 } } }],
  ['null', { data: { data: { is_superuser: null } } }],
  ['missing field', { data: { data: {} } }],
  ['missing data', { data: {} }],
  ['null response', null],
  ['undefined response', undefined],
  ['malformed user', { data: { data: 'user' } }],
]

for (const [label, response] of invalidResponses) {
  test(`does not authorize ${label} responses`, async () => {
    const pinia = createPinia()
    const store = useUserPermissionsStore(pinia)

    const result = await store.refresh({
      request: async () => response,
      getToken: () => 'token',
    })

    assert.equal(result, false)
    assert.equal(store.currentUser, null)
    assert.equal(store.verifiedToken, null)
    assert.equal(store.isSuperuser, false)
  })
}

test('clears existing superuser permission when the request fails', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)
  const token = 'token'

  await store.refresh({
    request: async () => ({ data: { data: { is_superuser: true } } }),
    getToken: () => token,
  })
  assert.equal(store.isSuperuser, true)

  const result = await store.refresh({
    request: async () => {
      throw new Error('request failed')
    },
    getToken: () => token,
  })

  assert.equal(result, false)
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)
})

test('clear empties permission state immediately and is idempotent', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)

  await store.refresh({
    request: async () => ({ data: { data: { is_superuser: true } } }),
    getToken: () => 'token',
  })

  store.clear()
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)

  assert.doesNotThrow(() => store.clear())
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)
})

test('does not restore a pending superuser response after clear', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)
  const request = deferred()

  const pendingRefresh = store.refresh({
    request: () => request.promise,
    getToken: () => 'token',
  })

  store.clear()
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)

  request.resolve({ data: { data: { is_superuser: true } } })

  assert.equal(await pendingRefresh, false)
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)
})

test('does not grant a pending response to a switched token', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)
  const request = deferred()
  let currentToken = 'old-token'

  const pendingRefresh = store.refresh({
    request: () => request.promise,
    getToken: () => currentToken,
  })

  currentToken = 'new-token'
  request.resolve({ data: { data: { is_superuser: true } } })

  assert.equal(await pendingRefresh, false)
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)
})

test('does not let an old superuser response overwrite a newer ordinary refresh', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)
  const oldRequest = deferred()
  const newRequest = deferred()
  let currentToken = 'old-token'

  const oldRefresh = store.refresh({
    request: () => oldRequest.promise,
    getToken: () => currentToken,
  })

  currentToken = 'new-token'
  const newUser = { id: 'new-user', is_superuser: false }
  const newRefresh = store.refresh({
    request: () => newRequest.promise,
    getToken: () => currentToken,
  })

  newRequest.resolve({ data: { data: newUser } })
  assert.equal(await newRefresh, true)
  assert.deepEqual(store.currentUser, newUser)
  assert.equal(store.verifiedToken, currentToken)
  assert.equal(store.isSuperuser, false)

  oldRequest.resolve({ data: { data: { id: 'old-user', is_superuser: true } } })
  assert.equal(await oldRefresh, false)
  assert.deepEqual(store.currentUser, newUser)
  assert.equal(store.verifiedToken, currentToken)
  assert.equal(store.isSuperuser, false)
})

test('does not let an old failed refresh clear a newer superuser success', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)
  const oldRequest = deferred()
  const newRequest = deferred()
  let currentToken = 'old-token'

  const oldRefresh = store.refresh({
    request: () => oldRequest.promise,
    getToken: () => currentToken,
  })

  currentToken = 'new-token'
  const newUser = { id: 'new-user', is_superuser: true }
  const newRefresh = store.refresh({
    request: () => newRequest.promise,
    getToken: () => currentToken,
  })

  newRequest.resolve({ data: { data: newUser } })
  assert.equal(await newRefresh, true)
  assert.deepEqual(store.currentUser, newUser)
  assert.equal(store.verifiedToken, currentToken)
  assert.equal(store.isSuperuser, true)

  oldRequest.reject(new Error('old request failed'))
  assert.equal(await oldRefresh, false)
  assert.deepEqual(store.currentUser, newUser)
  assert.equal(store.verifiedToken, currentToken)
  assert.equal(store.isSuperuser, true)
})

test('keeps permission state isolated between Pinia instances', async () => {
  const piniaA = createPinia()
  const piniaB = createPinia()
  const storeA = useUserPermissionsStore(piniaA)
  const storeB = useUserPermissionsStore(piniaB)

  assert.equal(storeB.currentUser, null)
  assert.equal(storeB.verifiedToken, null)
  assert.equal(storeB.isSuperuser, false)

  const userA = { id: 'user-a', is_superuser: true }
  await storeA.refresh({
    request: async () => ({ data: { data: userA } }),
    getToken: () => 'token-a',
  })

  assert.deepEqual(storeA.currentUser, userA)
  assert.equal(storeA.verifiedToken, 'token-a')
  assert.equal(storeA.isSuperuser, true)
  assert.equal(storeB.currentUser, null)
  assert.equal(storeB.verifiedToken, null)
  assert.equal(storeB.isSuperuser, false)

  const userB = { id: 'user-b', is_superuser: false }
  await storeB.refresh({
    request: async () => ({ data: { data: userB } }),
    getToken: () => 'token-b',
  })

  assert.deepEqual(storeB.currentUser, userB)
  assert.equal(storeB.verifiedToken, 'token-b')
  assert.equal(storeB.isSuperuser, false)
  assert.deepEqual(storeA.currentUser, userA)
  assert.equal(storeA.verifiedToken, 'token-a')
  assert.equal(storeA.isSuperuser, true)
})

for (const expectedSuperuser of [false, true]) {
  test(`keeps slow permission responses non-superuser until ${expectedSuperuser} is confirmed`, async () => {
    const pinia = createPinia()
    const store = useUserPermissionsStore(pinia)
    const request = deferred()
    const token = `slow-token-${expectedSuperuser}`
    const user = { id: token, is_superuser: expectedSuperuser }

    const pendingRefresh = store.refresh({
      request: () => request.promise,
      getToken: () => token,
    })

    assert.equal(store.currentUser, null)
    assert.equal(store.verifiedToken, null)
    assert.equal(store.isSuperuser, false)

    await Promise.resolve()
    assert.equal(store.currentUser, null)
    assert.equal(store.verifiedToken, null)
    assert.equal(store.isSuperuser, false)

    request.resolve({ data: { data: user } })
    assert.equal(await pendingRefresh, true)
    assert.deepEqual(store.currentUser, user)
    assert.equal(store.verifiedToken, token)
    assert.equal(store.isSuperuser, expectedSuperuser)
  })
}

test('clears existing superuser state during a slow same-token revalidation', async () => {
  const pinia = createPinia()
  const store = useUserPermissionsStore(pinia)
  const token = 'token'

  await store.refresh({
    request: async () => ({ data: { data: { id: 'superuser', is_superuser: true } } }),
    getToken: () => token,
  })
  assert.equal(store.isSuperuser, true)

  const request = deferred()
  const pendingRefresh = store.refresh({
    request: () => request.promise,
    getToken: () => token,
  })

  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)

  await Promise.resolve()
  assert.equal(store.currentUser, null)
  assert.equal(store.verifiedToken, null)
  assert.equal(store.isSuperuser, false)

  const ordinaryUser = { id: 'ordinary-user', is_superuser: false }
  request.resolve({ data: { data: ordinaryUser } })
  assert.equal(await pendingRefresh, true)
  assert.deepEqual(store.currentUser, ordinaryUser)
  assert.equal(store.verifiedToken, token)
  assert.equal(store.isSuperuser, false)
})
