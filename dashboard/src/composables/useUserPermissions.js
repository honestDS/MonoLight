import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

export const useUserPermissionsStore = defineStore('userPermissions', () => {
  const currentUser = ref(null)
  const verifiedToken = ref(null)
  const showAdminNavigation = ref(false)
  let navigationToken = null
  let requestVersion = 0

  const isSuperuser = computed(() => currentUser.value?.is_superuser === true)

  function clear() {
    requestVersion += 1
    currentUser.value = null
    verifiedToken.value = null
    showAdminNavigation.value = false
    navigationToken = null
  }

  async function refresh({ request, getToken }) {
    const token = getToken()
    requestVersion += 1
    currentUser.value = null
    verifiedToken.value = null
    if (!token || token !== navigationToken) {
      showAdminNavigation.value = false
      navigationToken = null
    }
    const version = requestVersion

    if (!token) {
      return false
    }

    try {
      const response = await request()
      const user = response?.data?.data

      if (version !== requestVersion) {
        return false
      }

      if (getToken() !== token) {
        clear()
        return false
      }

      if (typeof user?.is_superuser !== 'boolean') {
        clear()
        return false
      }

      currentUser.value = user
      verifiedToken.value = token
      showAdminNavigation.value = user.is_superuser === true
      navigationToken = token
      return true
    } catch {
      if (version === requestVersion) {
        clear()
      }
      return false
    }
  }

  return {
    currentUser,
    verifiedToken,
    isSuperuser,
    showAdminNavigation,
    clear,
    refresh,
  }
})
