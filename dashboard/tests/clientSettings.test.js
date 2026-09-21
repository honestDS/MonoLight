import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CLIENT_SETTINGS_STORAGE_KEY,
  getClientSetting,
  readClientSettings,
  setClientSetting
} from '../src/utils/clientSettings.js'

const createStorage = (initialValue = null) => {
  let value = initialValue
  return {
    getItem: key => key === CLIENT_SETTINGS_STORAGE_KEY ? value : null,
    setItem: (key, nextValue) => {
      if (key === CLIENT_SETTINGS_STORAGE_KEY) value = nextValue
    },
    value: () => value
  }
}

test('client settings read from one JSON storage record', () => {
  const storage = createStorage(JSON.stringify({ requestMetadataCollapsed: false, compactMode: true }))

  assert.deepEqual(readClientSettings(storage), { requestMetadataCollapsed: false, compactMode: true })
  assert.equal(getClientSetting('requestMetadataCollapsed', true, storage), false)
})

test('client setting falls back when storage is empty or malformed', () => {
  assert.equal(getClientSetting('requestMetadataCollapsed', true, createStorage()), true)
  assert.equal(getClientSetting('requestMetadataCollapsed', true, createStorage('{broken')), true)
  assert.deepEqual(readClientSettings(createStorage('[]')), {})
})

test('client setting writes preserve unrelated settings in the same JSON record', () => {
  const storage = createStorage(JSON.stringify({ compactMode: true }))

  assert.equal(setClientSetting('requestMetadataCollapsed', false, storage), true)
  assert.deepEqual(JSON.parse(storage.value()), {
    compactMode: true,
    requestMetadataCollapsed: false
  })
})

test('client settings tolerate unavailable storage', () => {
  const storage = {
    getItem: () => { throw new Error('blocked') },
    setItem: () => { throw new Error('blocked') }
  }

  assert.deepEqual(readClientSettings(storage), {})
  assert.equal(getClientSetting('requestMetadataCollapsed', true, storage), true)
  assert.equal(setClientSetting('requestMetadataCollapsed', false, storage), false)
})