import assert from 'node:assert/strict'
import test from 'node:test'
import {
  buildKnowledgeBaseBindingPayload,
  filterKnowledgeBaseIdsForOwner,
  filterProfilesByUid,
  formatProfileOptionLabel,
  getNewSessionProfileOverrideId,
  resolveDefaultProfileLabel,
  resolveSessionProfileDisplayId,
  resolveSessionProfilePlaceholder,
  resolveProfileOwnerUid
} from '../src/utils/profileOptions.js'

test('filters knowledge base bindings by owner and available knowledge bases', () => {
  const ids = ['kb-a', 'kb-b', 'kb-missing', 'kb-a']
  const knowledgeBases = [
    { id: 'kb-a', uid: 'user-a' },
    { id: 'kb-b', uid: 'user-b' },
    { id: 'kb-c', uid: 'user-a' }
  ]

  assert.deepEqual(filterKnowledgeBaseIdsForOwner(ids, knowledgeBases, 'user-a'), ['kb-a', 'kb-a'])
  assert.deepEqual(filterKnowledgeBaseIdsForOwner([], [], 'user-a'), [])
  assert.deepEqual(filterKnowledgeBaseIdsForOwner(ids, [], 'user-a'), [])
})

test('only includes normalized knowledge base bindings when ready', () => {
  assert.equal(buildKnowledgeBaseBindingPayload(['kb-a'], false), undefined)
  assert.equal(buildKnowledgeBaseBindingPayload(['kb-a'], null), undefined)
  assert.deepEqual(buildKnowledgeBaseBindingPayload([], true), [])
  assert.deepEqual(
    buildKnowledgeBaseBindingPayload(['kb-a', null, 'kb-a', undefined, '', 'kb-b'], true),
    ['kb-a', 'kb-b']
  )
})

test('does not turn existing bindings into an empty payload before readiness', () => {
  const existingIds = ['kb-a']

  assert.equal(buildKnowledgeBaseBindingPayload(existingIds, false), undefined)
  assert.deepEqual(existingIds, ['kb-a'])
})

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

test('formats only default profiles with the suffix', () => {
  assert.equal(
    formatProfileOptionLabel({ name: 'Default profile', is_default: true }, ' (default)'),
    'Default profile (default)'
  )
  assert.equal(
    formatProfileOptionLabel({ name: 'Regular profile', is_default: false }, ' (default)'),
    'Regular profile'
  )
})

test('formats profiles without a name as an empty string', () => {
  assert.equal(formatProfileOptionLabel({}, ' (default)'), '')
  assert.equal(formatProfileOptionLabel({ is_default: true }, ' (default)'), '')
  assert.equal(formatProfileOptionLabel(null, ' (default)'), '')
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

test('uses the external session profile id without an explicit override', () => {
  assert.equal(resolveSessionProfileDisplayId({ source: 'telegram', profile_id: 2 }, null), 2)
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

test('resolves the default profile label with its suffix', () => {
  const profiles = [
    { id: 1, name: 'Secondary profile', is_default: false },
    { id: 2, name: 'Default profile', is_default: true }
  ]

  assert.equal(resolveDefaultProfileLabel(profiles, ' (default)', 'Inherited'), 'Default profile (default)')
})

test('returns the fallback when no valid default profile is available', () => {
  const fallback = 'Inherited'

  assert.equal(resolveDefaultProfileLabel([], ' (default)', fallback), fallback)
  assert.equal(resolveDefaultProfileLabel(null, ' (default)', fallback), fallback)
  assert.equal(resolveDefaultProfileLabel({}, ' (default)', fallback), fallback)
  assert.equal(
    resolveDefaultProfileLabel([{ id: 1, name: 'Secondary profile', is_default: false }], ' (default)', fallback),
    fallback
  )
  assert.equal(
    resolveDefaultProfileLabel([{ id: 1, is_default: true }], ' (default)', fallback),
    fallback
  )

  for (const profileId of [0, -1, true, false, '1', 1.5, null, undefined, NaN, Infinity]) {
    assert.equal(
      resolveDefaultProfileLabel([{ id: profileId, name: 'Default profile', is_default: true }], ' (default)', fallback),
      fallback
    )
  }
})

test('returns the generic placeholder for external sessions even with a default profile', () => {
  const profiles = [
    { id: 1, name: 'Default profile', is_default: true },
    { id: 2, name: 'External profile', is_default: false }
  ]

  assert.equal(
    resolveSessionProfilePlaceholder(profiles, true, ' (default)', 'Inherited'),
    'Inherited'
  )
})

test('resolves the current default profile for web and new sessions', () => {
  const profiles = [
    { id: 1, name: 'Default profile', is_default: true },
    { id: 2, name: 'External profile', is_default: false }
  ]

  assert.equal(
    resolveSessionProfilePlaceholder(profiles, false, ' (default)', 'Inherited'),
    'Default profile (default)'
  )
  assert.equal(
    resolveSessionProfilePlaceholder(profiles, false, ' (default)', 'Inherited'),
    'Default profile (default)'
  )
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
