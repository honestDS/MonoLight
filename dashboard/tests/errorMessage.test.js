import assert from 'node:assert/strict'
import test from 'node:test'

import { MAX_ERROR_MESSAGE_LENGTH, truncateErrorMessage } from '../src/utils/errorMessage.js'

test('leaves short error messages unchanged', () => {
  assert.equal(truncateErrorMessage('short message'), 'short message')
})

test('leaves a message at the maximum length unchanged', () => {
  const message = 'a'.repeat(MAX_ERROR_MESSAGE_LENGTH)

  assert.equal(truncateErrorMessage(message), message)
})

test('truncates messages over the maximum length with an ASCII suffix', () => {
  for (const message of ['a'.repeat(501), 'a'.repeat(700)]) {
    const result = truncateErrorMessage(message)

    assert.equal(result.length, MAX_ERROR_MESSAGE_LENGTH)
    assert.equal(result.endsWith('...'), true)
    assert.equal(result.slice(0, -3), message.slice(0, MAX_ERROR_MESSAGE_LENGTH - 3))
  }
})

test('supports custom limits of three and two code units', () => {
  assert.equal(truncateErrorMessage('abcd', 3), 'abc')
  assert.equal(truncateErrorMessage('abcd', 2), 'ab')
})

test('converts nullish values to empty strings', () => {
  assert.equal(truncateErrorMessage(null), '')
  assert.equal(truncateErrorMessage(undefined), '')
})

test('converts non-string values to strings', () => {
  assert.equal(truncateErrorMessage(123), '123')
  assert.equal(truncateErrorMessage({ message: 'error' }), '[object Object]')
})

test('falls back to the default limit for non-finite limits', () => {
  const message = 'a'.repeat(MAX_ERROR_MESSAGE_LENGTH + 1)
  const expected = `${'a'.repeat(MAX_ERROR_MESSAGE_LENGTH - 3)}...`

  assert.equal(truncateErrorMessage(message, NaN), expected)
  assert.equal(truncateErrorMessage(message, Infinity), expected)
})
