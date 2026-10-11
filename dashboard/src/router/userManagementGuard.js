import {
  BACKEND_UNAVAILABLE_PATH,
  HOME_PATH,
  LOGIN_PATH,
  SETUP_PATH,
} from './setupGuard.js'

export function createUserManagementGuard({ getToken, currentUserRequest, getPermissions }) {
  return async to => {
    const permissions = getPermissions()
    const requiresSuperuser = to?.meta?.requiresSuperuser === true
    const toPath = to?.path

    if (
      toPath === LOGIN_PATH ||
      toPath === SETUP_PATH ||
      toPath === BACKEND_UNAVAILABLE_PATH
    ) {
      permissions.clear()
      return undefined
    }

    const token = getToken()
    if (!token) {
      permissions.clear()
      return LOGIN_PATH
    }

    if (requiresSuperuser || permissions.verifiedToken !== token) {
      const refreshed = await permissions.refresh({
        request: currentUserRequest,
        getToken,
      })

      if (!refreshed) {
        return getToken() ? (requiresSuperuser ? HOME_PATH : undefined) : LOGIN_PATH
      }
    }

    if (requiresSuperuser && !permissions.isSuperuser) {
      return HOME_PATH
    }

    return undefined
  }
}
