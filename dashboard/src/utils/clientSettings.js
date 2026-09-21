export const CLIENT_SETTINGS_STORAGE_KEY = 'monolight_client_settings'

const resolveStorage = (storage) => {
  if (storage !== undefined) return storage
  try {
    return globalThis.localStorage
  } catch {
    return null
  }
}

const isSettingsObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)

export const readClientSettings = (storage) => {
  const target = resolveStorage(storage)
  if (!target) return {}

  try {
    const raw = target.getItem(CLIENT_SETTINGS_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return isSettingsObject(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

export const getClientSetting = (key, fallbackValue, storage) => {
  const settings = readClientSettings(storage)
  return Object.prototype.hasOwnProperty.call(settings, key) ? settings[key] : fallbackValue
}

export const setClientSetting = (key, value, storage) => {
  const target = resolveStorage(storage)
  if (!target) return false

  try {
    const settings = readClientSettings(target)
    target.setItem(CLIENT_SETTINGS_STORAGE_KEY, JSON.stringify({ ...settings, [key]: value }))
    return true
  } catch {
    return false
  }
}