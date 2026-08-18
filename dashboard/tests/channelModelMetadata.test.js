import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyOpenRouterModelMetadata,
  getOpenRouterModelMatches,
  toPositiveInteger
} from '../src/utils/channelModelMetadata.js'

test('prefers exact id and canonical_slug matches over provider-agnostic matches', () => {
  const exactId = { id: 'OpenAI/GPT-4o', name: 'exact id' }
  const exactCanonicalSlug = {
    id: 'provider/canonical-match',
    canonical_slug: 'OpenAI/GPT-4o',
    name: 'exact canonical slug'
  }
  const providerAgnosticMatch = { id: 'other/GPT-4o', name: 'fallback match' }

  assert.deepEqual(
    getOpenRouterModelMatches(
      [providerAgnosticMatch, exactId, exactCanonicalSlug],
      '  openai/gpt-4o '
    ),
    [exactId, exactCanonicalSlug]
  )
})

test('ignores provider prefixes and preserves ambiguous matches', () => {
  const firstProvider = { id: 'provider-a/Model-X' }
  const secondProvider = { canonical_slug: 'provider-b/model-x', id: 'provider-b/model-x' }

  assert.deepEqual(
    getOpenRouterModelMatches([firstProvider, secondProvider], 'requested/model-x'),
    [firstProvider, secondProvider]
  )
  assert.deepEqual(
    getOpenRouterModelMatches([firstProvider, secondProvider], 'MODEL-X'),
    [firstProvider, secondProvider]
  )
})

test('matches identifiers case-insensitively', () => {
  const model = { id: 'Provider/Model-Name', canonical_slug: 'provider/model-name' }

  assert.deepEqual(getOpenRouterModelMatches([model], 'PROVIDER/MODEL-NAME'), [model])
  assert.deepEqual(getOpenRouterModelMatches([model], '  provider/model-name  '), [model])
})

test('ignores invalid inputs and removes duplicate model ids', () => {
  const first = { id: 'provider/model', name: 'first' }
  const duplicate = { id: 'provider/model', name: 'duplicate' }
  const matches = getOpenRouterModelMatches([null, 1, {}, first, duplicate], 'provider/model')

  assert.equal(matches.length, 1)
  assert.strictEqual(matches[0], first)
  assert.deepEqual(getOpenRouterModelMatches(null, 'provider/model'), [])
  assert.deepEqual(getOpenRouterModelMatches([], 'provider/model'), [])
  assert.deepEqual(getOpenRouterModelMatches([first], ''), [])
  assert.deepEqual(getOpenRouterModelMatches([first], '   '), [])
  assert.deepEqual(getOpenRouterModelMatches([first], null), [])
  assert.deepEqual(getOpenRouterModelMatches([first], 42), [])
})

test('converts finite positive numbers to floored integers', () => {
  assert.equal(toPositiveInteger(1), 1)
  assert.equal(toPositiveInteger(1.99), 1)
  assert.equal(toPositiveInteger(0.5), 0)
  assert.equal(toPositiveInteger(Number.MAX_VALUE), Math.floor(Number.MAX_VALUE))
})

test('returns null for zero, negative, non-finite, and non-number values', () => {
  for (const value of [0, -1, -0.5, NaN, Infinity, -Infinity, null, undefined, '1', true]) {
    assert.equal(toPositiveInteger(value), null)
  }
})

test('applies context and multimodal metadata and fills an empty description', () => {
  const entry = { description: '  ', context_window_k: 1 }
  const model = {
    context_length: 128000,
    top_provider: { context_length: 131072 },
    architecture: { input_modalities: ['Text', ' IMAGE ', 'audio', 'video'] },
    description: '  A multimodal model.  '
  }

  const result = applyOpenRouterModelMetadata(entry, model)

  assert.deepEqual(result.fields, [
    'context_window_k',
    'image_understanding',
    'audio_understanding',
    'video_understanding',
    'description'
  ])
  assert.strictEqual(result.model, model)
  assert.equal(entry.context_window_k, 131)
  assert.equal(entry.image_understanding, true)
  assert.equal(entry.audio_understanding, true)
  assert.equal(entry.video_understanding, true)
  assert.equal(entry.description, 'A multimodal model.')
})

test('preserves an existing description while applying metadata mappings', () => {
  const entry = { description: 'Existing description' }
  const model = {
    context_length: 4096,
    architecture: { input_modalities: ['text'] },
    description: 'Description from OpenRouter'
  }

  const result = applyOpenRouterModelMetadata(entry, model)

  assert.deepEqual(result.fields, [
    'context_window_k',
    'image_understanding',
    'audio_understanding',
    'video_understanding'
  ])
  assert.equal(entry.context_window_k, 4)
  assert.equal(entry.image_understanding, false)
  assert.equal(entry.audio_understanding, false)
  assert.equal(entry.video_understanding, false)
  assert.equal(entry.description, 'Existing description')
})

test('does not modify invalid metadata inputs', () => {
  const entry = { description: 'unchanged' }
  const originalEntry = { ...entry }
  const model = { description: 'ignored' }

  assert.deepEqual(applyOpenRouterModelMetadata(null, model), { fields: [], model: null })
  assert.deepEqual(applyOpenRouterModelMetadata(entry, null), { fields: [], model: entry })
  assert.deepEqual(applyOpenRouterModelMetadata(entry, []), { fields: [], model: entry })
  assert.deepEqual(entry, originalEntry)
})
