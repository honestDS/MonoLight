import assert from 'node:assert/strict'
import test from 'node:test'

import { isValidHttpProxy, normalizeHttpProxy } from '../src/utils/channelHttpProxy.js'

test('normalizes string proxies by trimming surrounding whitespace', () => {
  assert.equal(
    normalizeHttpProxy('  http://proxy.example.test:8080/  '),
    'http://proxy.example.test:8080/'
  )
  assert.equal(normalizeHttpProxy(null), '')
  assert.equal(normalizeHttpProxy(undefined), '')
  assert.equal(normalizeHttpProxy(123), '')
})

test('treats empty and whitespace-only proxy values as direct connections', () => {
  for (const value of ['', '   ', '\t\n', null, undefined]) {
    assert.equal(isValidHttpProxy(value), true, `expected ${String(value)} to be valid`)
  }
})

test('accepts HTTP proxies with no authentication', () => {
  assert.equal(isValidHttpProxy('http://proxy.example.test:8080'), true)
  assert.equal(isValidHttpProxy('http://proxy.example.test:1/'), true)
  assert.equal(isValidHttpProxy('http://proxy.example.test:65535'), true)
})

test('accepts HTTP proxies with URL-encoded authentication', () => {
  assert.equal(
    isValidHttpProxy('http://user%40example:p%3Ass%21word@proxy.example.test:3128'),
    true
  )
})

test('rejects unsupported protocols and missing ports', () => {
  for (const value of [
    'https://proxy.example.test:8080',
    'http://proxy.example.test'
  ]) {
    assert.equal(isValidHttpProxy(value), false, `expected ${value} to be invalid`)
  }
})

test('rejects incomplete authentication', () => {
  for (const value of [
    'http://user@proxy.example.test:8080',
    'http://:password@proxy.example.test:8080',
    'http://user:@proxy.example.test:8080'
  ]) {
    assert.equal(isValidHttpProxy(value), false, `expected ${value} to be invalid`)
  }
})

test('rejects ports outside the valid range', () => {
  for (const value of [
    'http://proxy.example.test:0',
    'http://proxy.example.test:65536'
  ]) {
    assert.equal(isValidHttpProxy(value), false, `expected ${value} to be invalid`)
  }
})

test('rejects whitespace, paths, queries, and fragments', () => {
  for (const value of [
    'http://proxy example.test:8080',
    'http://proxy.example.test :8080',
    'http://proxy.example.test:8080/path',
    'http://proxy.example.test:8080?target=api',
    'http://proxy.example.test:8080#fragment'
  ]) {
    assert.equal(isValidHttpProxy(value), false, `expected ${value} to be invalid`)
  }
})
