import test from 'node:test'
import assert from 'node:assert/strict'

import { getKnowledgeBaseProfileLabel } from '../src/utils/knowledgeBaseManagement.js'

test('renders user profile names in source order', () => {
  const profileNames = Object.freeze(['中文配置', 'English Profile'])
  const knowledgeBase = Object.freeze({
    knowledge_base_type: 'user',
    profile_ids: ['profile-cn', 'profile-en'],
    profile_names: profileNames
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), '中文配置, English Profile')
  assert.deepEqual(profileNames, ['中文配置', 'English Profile'])
  assert.deepEqual(knowledgeBase, {
    knowledge_base_type: 'user',
    profile_ids: ['profile-cn', 'profile-en'],
    profile_names: profileNames
  })
})

test('renders the profile name for an llm-managed knowledge base', () => {
  const knowledgeBase = Object.freeze({
    knowledge_base_type: 'llm_managed',
    managed_profile_id: 'managed-profile',
    profile_names: Object.freeze(['托管配置'])
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), '托管配置')
})

test('keeps repeated names for different profiles', () => {
  const knowledgeBase = Object.freeze({
    profile_ids: ['profile-a', 'profile-b'],
    profile_names: Object.freeze(['Shared Profile', 'Shared Profile'])
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), 'Shared Profile, Shared Profile')
})

test('returns a placeholder when no profile name list is available', () => {
  const cases = [
    { profile_names: [], profile_ids: ['profile-id'] },
    { profile_names: undefined, profile_ids: ['profile-id'] },
    { profile_names: null, managed_profile_id: 'managed-profile-id' },
    { profile_names: 'not-an-array', profile_ids: ['profile-id'] },
    { profile_ids: ['profile-id'], managed_profile_id: 'managed-profile-id' },
    undefined,
    null
  ]

  for (const knowledgeBase of cases) {
    const label = getKnowledgeBaseProfileLabel(knowledgeBase)
    assert.equal(label, '-')
    assert.doesNotMatch(label, /#/)
  }
})

test('ignores non-string and whitespace-only names', () => {
  const knowledgeBase = Object.freeze({
    profile_names: Object.freeze(['中文配置', 42, '', '   ', null, 'English Profile', false])
  })

  assert.equal(getKnowledgeBaseProfileLabel(knowledgeBase), '中文配置, English Profile')
})
