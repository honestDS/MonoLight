import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SETUP_PROTOCOLS,
  buildSetupRequest,
  readSetupTokenData,
  unicodeLength,
  utf8ByteLength,
  validateSetupApiKey,
  validateSetupBaseUrl,
  validateSetupHttpProxy,
  validateSetupModelId,
  validateSetupName,
  validateSetupPassword,
  validateSetupPasswordConfirmation,
  validateSetupProtocol,
  validateSetupUsername
} from '../src/utils/setupForm.js'

const CHINESE_CHARACTER = String.fromCodePoint(0x4e2d)
const GRINNING_FACE = String.fromCodePoint(0x1f600)

function assertValidationError(result, key, params = {}) {
  assert.deepEqual(result, { key, params })
}

test('unicodeLength counts ASCII, Chinese, and emoji by Unicode code point', () => {
  assert.equal(unicodeLength('abc'), 3)
  assert.equal(unicodeLength(GRINNING_FACE), 1)
  assert.equal(unicodeLength(CHINESE_CHARACTER.repeat(2)), 2)
  assert.equal(unicodeLength(`A${GRINNING_FACE}${CHINESE_CHARACTER}`), 3)
})

test('setup username requires a value', () => {
  assertValidationError(validateSetupUsername(''), 'required')
})

test('setup username accepts the 3 and 50 character boundaries', () => {
  assert.equal(validateSetupUsername('abc'), null)
  assert.equal(validateSetupUsername('a'.repeat(50)), null)
})

test('setup username rejects short, long, and invalid values', () => {
  assertValidationError(validateSetupUsername('ab'), 'username_length')
  assertValidationError(validateSetupUsername('a'.repeat(51)), 'username_length')
  assertValidationError(validateSetupUsername('ab!'), 'username_format')
})

test('setup password requires a value and accepts ASCII byte boundaries', () => {
  assertValidationError(validateSetupPassword(''), 'required')
  assert.equal(validateSetupPassword('a'.repeat(8)), null)
  assert.equal(validateSetupPassword('a'.repeat(72)), null)
  assertValidationError(validateSetupPassword('a'.repeat(73)), 'password_length')
})

test('setup password enforces the UTF-8 byte limit for Chinese characters', () => {
  const atByteLimit = CHINESE_CHARACTER.repeat(24)
  const overByteLimit = CHINESE_CHARACTER.repeat(25)

  assert.equal(utf8ByteLength(atByteLimit), 72)
  assert.equal(validateSetupPassword(atByteLimit), null)
  assert.equal(utf8ByteLength(overByteLimit), 75)
  assertValidationError(validateSetupPassword(overByteLimit), 'password_bytes')
})

test('setup password enforces the UTF-8 byte limit for emoji', () => {
  const atByteLimit = GRINNING_FACE.repeat(18)
  const overByteLimit = GRINNING_FACE.repeat(19)

  assert.equal(unicodeLength(atByteLimit), 18)
  assert.equal(utf8ByteLength(atByteLimit), 72)
  assert.equal(validateSetupPassword(atByteLimit), null)
  assert.equal(utf8ByteLength(overByteLimit), 76)
  assertValidationError(validateSetupPassword(overByteLimit), 'password_bytes')
})

test('setup password confirmation requires an equal nonempty value', () => {
  assert.equal(validateSetupPasswordConfirmation('correct-password', 'correct-password'), null)
  assertValidationError(validateSetupPasswordConfirmation('', 'correct-password'), 'required')
  assertValidationError(validateSetupPasswordConfirmation('other-password', 'correct-password'), 'password_mismatch')
})

test('setup names trim whitespace and enforce Unicode character boundaries', () => {
  assertValidationError(validateSetupName(' \t\n '), 'required')
  assert.equal(validateSetupName(`  ${CHINESE_CHARACTER.repeat(100)}  `), null)
  assertValidationError(validateSetupName(CHINESE_CHARACTER.repeat(101)), 'max_length', { max: 100 })
})

test('setup base URLs trim whitespace and only allow HTTP(S) within the limit', () => {
  const maxLengthUrl = `https://${'a'.repeat(2040)}`
  const overLengthUrl = `https://${'a'.repeat(2041)}`

  assertValidationError(validateSetupBaseUrl(' \t '), 'required')
  assert.equal(validateSetupBaseUrl('  http://localhost:8000  '), null)
  assert.equal(validateSetupBaseUrl('  https://api.example.test/v1  '), null)
  assert.equal(validateSetupBaseUrl(maxLengthUrl), null)
  assertValidationError(validateSetupBaseUrl('ftp://api.example.test'), 'url_format')
  assertValidationError(validateSetupBaseUrl(overLengthUrl), 'max_length', { max: 2048 })
})

test('setup HTTP proxies accept empty values and valid HTTP proxy URLs', () => {
  assert.equal(validateSetupHttpProxy(''), null)
  assert.equal(validateSetupHttpProxy('http://proxy.example.test:8080'), null)
  assertValidationError(validateSetupHttpProxy('https://proxy.example.test:8080'), 'proxy_format')
  assertValidationError(validateSetupHttpProxy('http://proxy.example.test'), 'proxy_format')
})

test('setup API keys accept any nonempty string, including whitespace', () => {
  assert.equal(validateSetupApiKey('   '), null)
  assertValidationError(validateSetupApiKey(''), 'required')
  assertValidationError(validateSetupApiKey(null), 'required')
  assertValidationError(validateSetupApiKey(42), 'required')
})

test('setup model IDs trim whitespace and enforce Unicode character boundaries', () => {
  assertValidationError(validateSetupModelId('  \n '), 'required')
  assert.equal(validateSetupModelId(`  ${CHINESE_CHARACTER.repeat(255)}  `), null)
  assertValidationError(validateSetupModelId(CHINESE_CHARACTER.repeat(256)), 'max_length', { max: 255 })
})

test('setup protocol accepts only its frozen whitelist', () => {
  assert.deepEqual(SETUP_PROTOCOLS, ['OPENAI', 'OPENAI_RESPONSES'])
  assert.equal(Object.isFrozen(SETUP_PROTOCOLS), true)
  assert.equal(validateSetupProtocol('OPENAI'), null)
  assert.equal(validateSetupProtocol('OPENAI_RESPONSES'), null)
  assertValidationError(validateSetupProtocol('openai'), 'required')
})

test('buildSetupRequest preserves extended channel fields and normalizes its HTTP proxy', () => {
  const password = '  password is preserved  '
  const apiKey = '  api key is preserved  '

  assert.deepEqual(
    buildSetupRequest({
      admin: {
        username: '  administrator  ',
        password,
        password_confirm: 'ignored-confirmation',
        ignored: 'ignored'
      },
      channel: {
        name: '  Primary channel  ',
        base_url: '  https://api.example.test/v1  ',
        api_key: apiKey,
        model_id: '  model-id  ',
        protocol: 'OPENAI',
        http_proxy: '  http://proxy.example.test:8080  ',
        image_understanding: true,
        audio_understanding: false,
        video_understanding: true,
        context_window_k: 128,
        temperature: 0.7,
        top_p: 0.95,
        max_tokens: 4096,
        description: 'Vision model',
        advanced_settings: {
          reasoning_effort: 'high',
          request_timeout_ms: 30000
        },
        ignored: 'ignored'
      },
      profile: {
        name: '  Administrator  ',
        ignored: 'ignored'
      },
      ignored: 'ignored'
    }),
    {
      admin: {
        username: 'administrator',
        password
      },
      channel: {
        name: 'Primary channel',
        base_url: 'https://api.example.test/v1',
        api_key: apiKey,
        model_id: 'model-id',
        protocol: 'OPENAI',
        http_proxy: 'http://proxy.example.test:8080',
        image_understanding: true,
        audio_understanding: false,
        video_understanding: true,
        context_window_k: 128,
        temperature: 0.7,
        top_p: 0.95,
        max_tokens: 4096,
        description: 'Vision model',
        advanced_settings: {
          reasoning_effort: 'high',
          request_timeout_ms: 30000
        }
      },
      profile: {
        name: 'Administrator'
      }
    }
  )
})

test('buildSetupRequest safely normalizes missing nested objects', () => {
  const expected = {
    admin: {
      username: '',
      password: ''
    },
    channel: {
      name: '',
      base_url: '',
      api_key: '',
      model_id: '',
      protocol: '',
      http_proxy: null,
      image_understanding: false,
      audio_understanding: false,
      video_understanding: false,
      context_window_k: undefined,
      temperature: undefined,
      top_p: undefined,
      max_tokens: undefined,
      description: '',
      advanced_settings: {}
    },
    profile: {
      name: ''
    }
  }

  assert.deepEqual(buildSetupRequest(), expected)
  assert.deepEqual(buildSetupRequest({ admin: null, channel: null, profile: null }), expected)
})

test('readSetupTokenData returns only the nested nonempty token fields', () => {
  assert.deepEqual(
    readSetupTokenData({
      access_token: 'top-level token',
      token_type: 'Top-level type',
      data: {
        data: {
          access_token: 'nested access token',
          token_type: 'Bearer',
          expires_in: 3600,
          ignored: 'ignored'
        }
      }
    }),
    {
      access_token: 'nested access token',
      token_type: 'Bearer'
    }
  )
})

test('readSetupTokenData rejects missing, non-string, empty, and top-level token fields', () => {
  assert.equal(readSetupTokenData(), null)
  assert.equal(readSetupTokenData({ data: {} }), null)
  assert.equal(readSetupTokenData({ data: { data: { token_type: 'Bearer' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 'token' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 1, token_type: 'Bearer' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 'token', token_type: false } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: '', token_type: 'Bearer' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 'token', token_type: '' } } }), null)
  assert.equal(readSetupTokenData({ access_token: 'top-level token', token_type: 'Bearer' }), null)
})
