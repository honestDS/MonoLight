import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { useUserPermissionsStore } from '../src/composables/useUserPermissions.js'
import {
  BACKEND_UNAVAILABLE_PATH,
  createSetupGuard,
  createSetupStatusState,
} from '../src/router/setupGuard.js'
import { createUserManagementGuard } from '../src/router/userManagementGuard.js'

const routerSource = readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createRequestController = () => {
  const requests = []
  const waiters = new Map()
  let nextIndex = 0

  return {
    requests,
    create() {
      const request = { deferred: createDeferred() }
      const index = requests.length
      requests.push(request)
      waiters.get(index)?.resolve(request)
      return request
    },
    next() {
      const index = nextIndex
      nextIndex += 1
      if (requests[index]) return Promise.resolve(requests[index])
      const waiter = createDeferred()
      waiters.set(index, waiter)
      return waiter.promise
    },
  }
}

const setupResponse = required => ({ data: { data: { required } } })
const userResponse = user => ({ data: { data: user } })

const createHarness = () => {
  const pinia = createPinia()
  const permissions = useUserPermissionsStore(pinia)
  const setupRequests = createRequestController()
  const authRequests = createRequestController()
  const loadedViews = []
  const setupStatusState = createSetupStatusState()
  let token = null

  const localStorage = {
    getItem(key) {
      return key === 'token' ? token : null
    },
    removeItem(key) {
      if (key === 'token') token = null
    },
  }

  const authApi = {
    me() {
      return authRequests.create().deferred.promise
    },
  }
  const setupApi = {
    status() {
      return setupRequests.create().deferred.promise
    },
  }

  const context = vm.createContext({
    authApi,
    BACKEND_UNAVAILABLE_PATH,
    createRouter,
    createSetupGuard,
    createUserManagementGuard,
    createWebHashHistory: createMemoryHistory,
    loadView(viewPath) {
      loadedViews.push(viewPath)
      return Promise.resolve({ name: viewPath })
    },
    localStorage,
    setupApi,
    setupStatusState,
    useUserPermissionsStore: () => useUserPermissionsStore(pinia),
  })

  const executableSource = routerSource
    .split('\n')
    .filter(line => !/^\s*import\b/.test(line))
    .join('\n')
    .replaceAll('() => import(', '() => loadView(')
    .replace('export default router', 'globalThis.router = router')

  vm.runInContext(executableSource, context, { filename: 'src/router/index.js' })

  return {
    authRequests,
    loadedViews,
    permissions,
    router: context.router,
    setupRequests,
    storage: {
      get token() {
        return token
      },
      set token(value) {
        token = value
      },
      removeToken() {
        token = null
      },
    },
    async resolveNextSetup(required) {
      const request = await setupRequests.next()
      request.deferred.resolve(setupResponse(required))
      return request
    },
    async rejectNextSetup(error = new Error('setup status failed')) {
      const request = await setupRequests.next()
      request.deferred.reject(error)
      return request
    },
    async resolveNextAuth(user) {
      const request = await authRequests.next()
      request.deferred.resolve(userResponse(user))
      return request
    },
    async rejectNextAuth(error) {
      const request = await authRequests.next()
      if (error?.response?.status === 401) token = null
      request.deferred.reject(error)
      return request
    },
  }
}

const unauthorizedError = () => Object.assign(new Error('unauthorized'), {
  response: { status: 401 },
})

test('blocks an ordinary initial users navigation, including query refresh state', async () => {
  const harness = createHarness()
  harness.storage.token = 'ordinary-token'

  const navigation = harness.router.push({ path: '/users', query: { refresh: '1' } })
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: false })
  await harness.resolveNextSetup(false)
  await navigation
  await harness.router.isReady()

  assert.equal(harness.router.currentRoute.value.path, '/')
  assert.equal(harness.permissions.isSuperuser, false)
  assert.equal(harness.authRequests.requests.length, 1)
  assert.equal(harness.loadedViews.includes('../views/UsersView.vue'), false)
})

test('allows a real boolean superuser through the actual users route metadata', async () => {
  const harness = createHarness()
  harness.storage.token = 'super-token'

  const navigation = harness.router.push('/users')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: true })
  await navigation

  assert.equal(harness.router.currentRoute.value.path, '/users')
  assert.equal(harness.permissions.isSuperuser, true)
  assert.equal(harness.loadedViews.includes('../views/UsersView.vue'), true)
})

test('hydrates an ordinary role on an entry page and reuses it on normal navigation', async () => {
  const harness = createHarness()
  harness.storage.token = 'ordinary-token'

  const firstNavigation = harness.router.push('/channels')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: false })
  await firstNavigation

  const secondNavigation = harness.router.push('/profiles')
  await harness.resolveNextSetup(false)
  await secondNavigation

  assert.equal(harness.router.currentRoute.value.path, '/profiles')
  assert.equal(harness.permissions.isSuperuser, false)
  assert.equal(harness.authRequests.requests.length, 1)
})

test('rechecks a cached superuser on users navigation and blocks a downgraded role', async () => {
  const harness = createHarness()
  harness.storage.token = 'role-changing-token'

  const firstNavigation = harness.router.push('/channels')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: true })
  await firstNavigation

  const usersNavigation = harness.router.push('/users')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: false })
  await harness.resolveNextSetup(false)
  await usersNavigation

  assert.equal(harness.router.currentRoute.value.path, '/')
  assert.equal(harness.permissions.isSuperuser, false)
  assert.equal(harness.authRequests.requests.length, 2)
  assert.equal(harness.loadedViews.includes('../views/UsersView.vue'), false)
})

test('rejects failed or malformed identity data for users without blocking other pages', async () => {
  const cases = [
    {
      name: 'request failure',
      settle: harness => harness.rejectNextAuth(new Error('identity unavailable')),
    },
    {
      name: 'string superuser flag',
      settle: harness => harness.resolveNextAuth({ is_superuser: 'true' }),
    },
  ]

  for (const identityCase of cases) {
    const harness = createHarness()
    harness.storage.token = `${identityCase.name}-token`

    const usersNavigation = harness.router.push('/users')
    await harness.resolveNextSetup(false)
    await identityCase.settle(harness)
    await harness.resolveNextSetup(false)
    await identityCase.settle(harness)
    await usersNavigation

    assert.equal(harness.router.currentRoute.value.path, '/')
    assert.equal(harness.permissions.isSuperuser, false)
    assert.equal(harness.loadedViews.includes('../views/UsersView.vue'), false)

    const channelsNavigation = harness.router.push('/channels')
    await harness.resolveNextSetup(false)
    await identityCase.settle(harness)
    await channelsNavigation

    assert.equal(harness.router.currentRoute.value.path, '/channels')
    assert.equal(harness.permissions.isSuperuser, false)
  }
})

test('sends missing or unauthorized sessions to login and clears the token', async () => {
  const missingToken = createHarness()
  const missingNavigation = missingToken.router.push('/users')
  await missingToken.resolveNextSetup(false)
  await missingToken.resolveNextSetup(false)
  await missingNavigation

  assert.equal(missingToken.router.currentRoute.value.path, '/login')
  assert.equal(missingToken.storage.token, null)
  assert.equal(missingToken.authRequests.requests.length, 0)
  assert.equal(missingToken.permissions.isSuperuser, false)

  const unauthorized = createHarness()
  unauthorized.storage.token = 'expired-token'
  const unauthorizedNavigation = unauthorized.router.push('/users')
  await unauthorized.resolveNextSetup(false)
  await unauthorized.rejectNextAuth(unauthorizedError())
  await unauthorized.resolveNextSetup(false)
  await unauthorizedNavigation

  assert.equal(unauthorized.router.currentRoute.value.path, '/login')
  assert.equal(unauthorized.storage.token, null)
  assert.equal(unauthorized.permissions.isSuperuser, false)
})

test('clears permission state on login, setup, and backend-unavailable routes without identity requests', async () => {
  const harness = createHarness()
  harness.storage.token = 'super-token'

  const entryNavigation = harness.router.push('/channels')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: true })
  await entryNavigation

  const loginNavigation = harness.router.push('/login')
  await harness.resolveNextSetup(false)
  await loginNavigation
  assert.equal(harness.permissions.isSuperuser, false)

  harness.storage.token = 'new-token'
  const setupNavigation = harness.router.push('/setup')
  await harness.resolveNextSetup(true)
  await setupNavigation
  assert.equal(harness.router.currentRoute.value.path, '/setup')
  assert.equal(harness.permissions.isSuperuser, false)

  harness.storage.token = 'new-token'
  const backendNavigation = harness.router.push(BACKEND_UNAVAILABLE_PATH)
  await harness.rejectNextSetup()
  await backendNavigation
  assert.equal(harness.router.currentRoute.value.path, BACKEND_UNAVAILABLE_PATH)
  assert.equal(harness.permissions.isSuperuser, false)
  assert.equal(harness.authRequests.requests.length, 1)
})

test('prioritizes incomplete or failed setup before users authentication and view loading', async () => {
  const incomplete = createHarness()
  incomplete.storage.token = 'token'
  const incompleteNavigation = incomplete.router.push('/users')
  await incomplete.resolveNextSetup(true)
  await incomplete.resolveNextSetup(true)
  await incompleteNavigation

  assert.equal(incomplete.router.currentRoute.value.path, '/setup')
  assert.equal(incomplete.authRequests.requests.length, 0)
  assert.equal(incomplete.loadedViews.includes('../views/UsersView.vue'), false)

  const failed = createHarness()
  failed.storage.token = 'token'
  const failedNavigation = failed.router.push('/users')
  await failed.rejectNextSetup()
  await failedNavigation

  assert.equal(failed.router.currentRoute.value.path, BACKEND_UNAVAILABLE_PATH)
  assert.equal(failed.authRequests.requests.length, 0)
  assert.equal(failed.loadedViews.includes('../views/UsersView.vue'), false)
})

test('does not retain superuser state across logout or a token change', async () => {
  const harness = createHarness()
  harness.storage.token = 'super-token'

  const superNavigation = harness.router.push('/channels')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: true })
  await superNavigation

  harness.permissions.clear()
  harness.storage.removeToken()
  const logoutNavigation = harness.router.push('/login')
  await harness.resolveNextSetup(false)
  await logoutNavigation

  harness.storage.token = 'ordinary-token'
  const ordinaryNavigation = harness.router.push('/channels')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: false })
  await ordinaryNavigation
  assert.equal(harness.permissions.isSuperuser, false)

  harness.storage.token = 'replacement-token'
  const replacementNavigation = harness.router.push('/profiles')
  await harness.resolveNextSetup(false)
  await harness.resolveNextAuth({ is_superuser: false })
  await replacementNavigation

  assert.equal(harness.router.currentRoute.value.path, '/profiles')
  assert.equal(harness.permissions.isSuperuser, false)
  assert.equal(harness.authRequests.requests.length, 3)
})

test('ignores a late superuser response after the direct guard is cleared', async () => {
  const pinia = createPinia()
  const permissions = useUserPermissionsStore(pinia)
  const response = createDeferred()
  const guard = createUserManagementGuard({
    getToken: () => 'token',
    currentUserRequest: () => response.promise,
    getPermissions: () => permissions,
  })

  const navigation = guard({ path: '/users', meta: { requiresSuperuser: true } })
  permissions.clear()
  response.resolve(userResponse({ is_superuser: true }))

  assert.equal(await navigation, '/')
  assert.equal(permissions.isSuperuser, false)
})
