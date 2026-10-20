import assert from 'node:assert/strict'
import test from 'node:test'
import {
  filterWritableProfileConfigs,
  filterProfilesByUid,
  getNewSessionProfileOverrideId,
  resolveSessionProfileDisplayId,
  resolveProfileOwnerUid
} from '../src/utils/profileOptions.js'

test('filters profiles by uid without selecting another user profile', () => {
  const profiles = [
    { id: 1, uid: 'user-a', name: 'A default', is_default: true },
    { id: 2, uid: 'user-b', name: 'B default', is_default: true },
    { id: 3, uid: 'user-a', name: 'A secondary', is_default: false }
  ]

  assert.deepEqual(filterProfilesByUid(profiles, 'user-a'), [profiles[0], profiles[2]])
  assert.deepEqual(filterProfilesByUid(profiles, 'user-b'), [profiles[1]])
})

test('returns an empty array for an empty uid or non-array profiles', () => {
  const profiles = [{ id: 1, uid: 'user-a', name: 'Profile' }]

  assert.deepEqual(filterProfilesByUid(profiles, ''), [])
  assert.deepEqual(filterProfilesByUid(profiles, null), [])
  assert.deepEqual(filterProfilesByUid(null, 'user-a'), [])
  assert.deepEqual(filterProfilesByUid({}, 'user-a'), [])
})

test('does not modify the source profile array', () => {
  const profiles = [
    { id: 1, uid: 'user-a', name: 'Profile A' },
    { id: 2, uid: 'user-b', name: 'Profile B' }
  ]
  const originalProfiles = [...profiles]

  const result = filterProfilesByUid(profiles, 'user-a')

  assert.deepEqual(profiles, originalProfiles)
  assert.notEqual(result, profiles)
})

test('returns a valid draft profile override only for new sessions', () => {
  assert.equal(getNewSessionProfileOverrideId(null, 1), 1)
  assert.equal(getNewSessionProfileOverrideId('', 42), 42)
  assert.equal(getNewSessionProfileOverrideId('session-id', 1), null)
})

test('rejects invalid new-session draft profile override ids', () => {
  for (const profileId of [0, -1, true, false, '1', 1.5, null, undefined, NaN, Infinity]) {
    assert.equal(getNewSessionProfileOverrideId(null, profileId), null)
  }
})

test('resolves a valid new-session draft profile id for display', () => {
  assert.equal(resolveSessionProfileDisplayId(null, 1), 1)
  assert.equal(resolveSessionProfileDisplayId(undefined, 42), 42)
})

test('prefers an explicit profile override over an external session profile', () => {
  assert.equal(
    resolveSessionProfileDisplayId({ source: 'telegram', profile_override_id: 1, profile_id: 2 }, null),
    1
  )
})

test('ignores an external session profile id without an explicit override', () => {
  assert.equal(resolveSessionProfileDisplayId({ source: 'telegram', profile_id: 2 }, null), null)
  assert.equal(resolveSessionProfileDisplayId({ source: 'weixin-openclaw', profile_id: 2 }, null), null)
  assert.equal(
    resolveSessionProfileDisplayId({ source: 'telegram', profile_override_id: null, profile_id: 2 }, null),
    null
  )
})

test('ignores a web session profile id without an explicit override', () => {
  assert.equal(resolveSessionProfileDisplayId({ source: 'http', profile_id: 2 }, null), null)
  assert.equal(resolveSessionProfileDisplayId({ source: 'ws', profile_id: 2 }, null), null)
})

test('returns null for invalid profile ids', () => {
  for (const profileId of [0, -1, true, false, '1', 1.5, null, undefined, NaN, Infinity]) {
    assert.equal(resolveSessionProfileDisplayId(null, profileId), null)
    assert.equal(
      resolveSessionProfileDisplayId(
        { source: 'telegram', profile_override_id: profileId, profile_id: profileId },
        null
      ),
      null
    )
  }
})

test('resolves the profile owner from the current session before the current user', () => {
  assert.equal(resolveProfileOwnerUid({ uid: 'session-owner' }, 'current-user'), 'session-owner')
  assert.equal(resolveProfileOwnerUid(null, 'current-user'), 'current-user')
})

test('returns null when no valid profile owner uid is available', () => {
  assert.equal(resolveProfileOwnerUid({ uid: '' }, null), null)
  assert.equal(resolveProfileOwnerUid({ uid: 1 }, false), null)
  assert.equal(resolveProfileOwnerUid(null, ''), null)
})

test('filters denied profile config paths while preserving allowed groups', () => {
  const configs = {
    security: { enabled: true, allowedOrigins: ['https://example.test'] },
    tool: { max_parallel_tools: 8, timeout: 30 }
  }

  assert.deepEqual(
    filterWritableProfileConfigs(configs, {
      security: false,
      'tool.max_parallel_tools': false
    }),
    { tool: { timeout: 30 } }
  )
  assert.deepEqual(
    filterWritableProfileConfigs(configs, { security: true }),
    configs
  )
})

test('treats only true as writable and skips missing paths', () => {
  const configs = {
    tool: { keep: 1, falseValue: 2, zero: 3, nullValue: 4 },
    security: { enabled: true }
  }

  assert.deepEqual(
    filterWritableProfileConfigs(configs, {
      'tool.keep': true,
      'tool.falseValue': false,
      'tool.zero': 0,
      'tool.nullValue': null,
      'missing.child': false
    }),
    { tool: { keep: 1 }, security: { enabled: true } }
  )
  assert.deepEqual(filterWritableProfileConfigs(configs, {}), configs)
})

test('does not mutate inputs and returns deeply independent, idempotent results', () => {
  const configs = { tool: { nested: { value: 1 }, denied: true } }
  const permissions = { 'tool.denied': false }
  const originalConfigs = structuredClone(configs)
  const originalPermissions = { ...permissions }

  const result = filterWritableProfileConfigs(configs, permissions)
  assert.deepEqual(filterWritableProfileConfigs(result, permissions), result)
  assert.notEqual(result, configs)
  assert.notEqual(result.tool, configs.tool)
  assert.notEqual(result.tool.nested, configs.tool.nested)

  result.tool.nested.value = 2
  assert.deepEqual(configs, originalConfigs)
  assert.deepEqual(permissions, originalPermissions)
})
