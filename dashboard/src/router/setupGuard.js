export const SETUP_PATH = '/setup'
export const LOGIN_PATH = '/login'
export const HOME_PATH = '/'

export function createSetupStatusState() {
  let snapshot = Object.freeze({ phase: 'idle', required: null, error: null })
  const listeners = new Set()

  const publish = next => {
    const error = next.error
    snapshot = Object.freeze({
      ...next,
      error: error && typeof error === 'object' ? Object.freeze({ ...error }) : error,
    })
    listeners.forEach(listener => listener(snapshot))
  }

  return {
    getSnapshot() {
      return snapshot
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    setChecking() {
      publish({ phase: 'checking', required: null, error: null })
    },
    setReady(required) {
      if (typeof required !== 'boolean') {
        throw new TypeError('Setup required state must be a boolean')
      }
      publish({ phase: 'ready', required, error: null })
    },
    setError(error) {
      publish({ phase: 'error', required: null, error })
    },
  }
}

export const setupStatusState = createSetupStatusState()

export function readSetupRequired(response) {
  const required = response?.data?.data?.required
  if (typeof required !== 'boolean') {
    throw new TypeError('Setup status response must contain a boolean required value')
  }
  return required
}

export function normalizeSetupError(error) {
  return {
    code: error?.response?.data?.code ?? null,
    message: error?.response?.data?.message ?? error?.message ?? '',
  }
}

export async function refreshSetupStatus({ statusRequest, state = setupStatusState }) {
  state.setChecking()
  try {
    const required = readSetupRequired(await statusRequest())
    state.setReady(required)
    return required
  } catch (error) {
    state.setError(normalizeSetupError(error))
    throw error
  }
}

export function resolveSetupNavigation({ required, toPath, hasToken }) {
  if (required) {
    return toPath === SETUP_PATH ? undefined : SETUP_PATH
  }
  if (toPath === SETUP_PATH) {
    return hasToken ? HOME_PATH : LOGIN_PATH
  }
  if (!hasToken && toPath !== LOGIN_PATH) {
    return LOGIN_PATH
  }
  return undefined
}

export function createSetupGuard({ statusRequest, getToken, state = setupStatusState }) {
  return async to => {
    const toPath = typeof to === 'string' ? to : to?.path
    const snapshot = state.getSnapshot()

    if (toPath === SETUP_PATH && snapshot.phase === 'error') {
      return undefined
    }

    try {
      const required = await refreshSetupStatus({ statusRequest, state })
      return resolveSetupNavigation({
        required,
        toPath,
        hasToken: Boolean(getToken()),
      })
    } catch {
      return toPath === SETUP_PATH ? undefined : SETUP_PATH
    }
  }
}
