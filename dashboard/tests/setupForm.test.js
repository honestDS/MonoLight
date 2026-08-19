import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SETUP_PROTOCOLS,
  buildSetupRequest,
  cloneSetupProfileConfigs,
  readSetupTokenData,
  readSetupProfileGuideData,
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

function validSetupProfileConfigs() {
  return {
    channel: {
      base_url: 'https://api.example.test'
    },
    security: {
      enabled: true
    },
    tool: {
      enabled_tools: ['read_file'],
      allowed_operation_dirs: ['/workspace'],
      file_send_blocked_extensions: ['.env']
    },
    other: {
      locale: 'en-US'
    },
    memory: {
      enabled: true
    }
  }
}

function validSetupProfileGuideResponse(configs = validSetupProfileConfigs(), toolOptions = [
  { value: 'read_file', label: 'Read file' }
]) {
  return {
    data: {
      data: {
        items: [{ id: 7, prompt_id: 13, configs }],
        meta: { tool_options: toolOptions }
      }
    }
  }
}

function validSetupPromptListResponse(prompts = [
  { id: 13, name: 'default', content: '' }
]) {
  return {
    data: {
      data: {
        items: prompts
      }
    }
  }
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

test('setup API keys require a non-whitespace string', () => {
  assertValidationError(validateSetupApiKey(''), 'required')
  assertValidationError(validateSetupApiKey('   '), 'required')
  assertValidationError(validateSetupApiKey(null), 'required')
  assertValidationError(validateSetupApiKey(42), 'required')
  assert.equal(validateSetupApiKey('  api-key  '), null)
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
          profile_id: 7,
          channel_id: 11,
          expires_in: 3600,
          ignored: 'ignored'
        }
      }
    }),
    {
      access_token: 'nested access token',
      token_type: 'Bearer',
      profile_id: 7,
      channel_id: 11
    }
  )
})

test('readSetupTokenData rejects missing, non-string, empty, top-level token fields, and invalid IDs', () => {
  assert.equal(readSetupTokenData(), null)
  assert.equal(readSetupTokenData({ data: {} }), null)
  assert.equal(readSetupTokenData({ data: { data: { token_type: 'Bearer' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 'token' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 1, token_type: 'Bearer' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 'token', token_type: false } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: '', token_type: 'Bearer' } } }), null)
  assert.equal(readSetupTokenData({ data: { data: { access_token: 'token', token_type: '' } } }), null)
  assert.equal(readSetupTokenData({ access_token: 'top-level token', token_type: 'Bearer' }), null)

  const validTokenData = {
    access_token: 'token',
    token_type: 'Bearer',
    profile_id: 7,
    channel_id: 11
  }
  const missingProfileId = { ...validTokenData }
  const missingChannelId = { ...validTokenData }
  delete missingProfileId.profile_id
  delete missingChannelId.channel_id

  assert.equal(readSetupTokenData({ data: { data: missingProfileId } }), null)
  assert.equal(readSetupTokenData({ data: { data: missingChannelId } }), null)

  for (const profileId of [0, -1, 1.5, '7']) {
    assert.equal(
      readSetupTokenData({ data: { data: { ...validTokenData, profile_id: profileId } } }),
      null
    )
  }

  for (const channelId of [0, -1, 1.5, '11']) {
    assert.equal(
      readSetupTokenData({ data: { data: { ...validTokenData, channel_id: channelId } } }),
      null
    )
  }
})

test('cloneSetupProfileConfigs returns a deep copy of valid profile configs', () => {
  const configs = validSetupProfileConfigs()
  const clonedConfigs = cloneSetupProfileConfigs(configs)

  assert.deepEqual(clonedConfigs, configs)
  assert.notStrictEqual(clonedConfigs, configs)
  assert.notStrictEqual(clonedConfigs.tool, configs.tool)

  clonedConfigs.tool.enabled_tools.push('web_search')
  assert.deepEqual(configs.tool.enabled_tools, ['read_file'])
})

test('readSetupProfileGuideData returns cloned configs and tool options for a valid profile', () => {
  const configs = validSetupProfileConfigs()
  const expectedConfigs = validSetupProfileConfigs()
  const toolOptions = [{ value: 'read_file', label: 'Read file' }]
  const response = validSetupProfileGuideResponse(configs, toolOptions)
  const promptResponse = validSetupPromptListResponse()
  const promptItems = promptResponse.data.data.items
  const result = readSetupProfileGuideData(response, promptResponse, 7)

  assert.deepEqual(result, {
    configs: expectedConfigs,
    toolOptions: [{ value: 'read_file', label: 'Read file' }],
    prompt: { id: 13, name: 'default', content: '' }
  })
  assert.notStrictEqual(result.configs, configs)
  assert.notStrictEqual(result.toolOptions, toolOptions)
  assert.notStrictEqual(result.prompt, promptItems[0])

  result.configs.tool.enabled_tools.push('web_search')
  result.configs.channel.base_url = 'https://changed.example.test'
  result.toolOptions[0].label = 'Changed label'
  result.prompt.content = 'Changed content'

  assert.deepEqual(configs, expectedConfigs)
  assert.deepEqual(toolOptions, [{ value: 'read_file', label: 'Read file' }])
  assert.deepEqual(promptItems, [{ id: 13, name: 'default', content: '' }])
})

test('readSetupProfileGuideData returns null when the target profile is missing', () => {
  assert.equal(
    readSetupProfileGuideData(validSetupProfileGuideResponse(), validSetupPromptListResponse(), 99),
    null
  )
})

test('readSetupProfileGuideData returns null when profile prompt_id is missing or invalid', () => {
  const missingPromptIdResponse = validSetupProfileGuideResponse()
  delete missingPromptIdResponse.data.data.items[0].prompt_id

  assert.equal(
    readSetupProfileGuideData(missingPromptIdResponse, validSetupPromptListResponse(), 7),
    null
  )

  for (const promptId of [0, -1, 1.5, '13', null]) {
    const response = validSetupProfileGuideResponse()
    response.data.data.items[0].prompt_id = promptId

    assert.equal(
      readSetupProfileGuideData(response, validSetupPromptListResponse(), 7),
      null
    )
  }
})

test('readSetupProfileGuideData returns null when prompt items are not an array', () => {
  for (const items of [null, {}, 'prompts']) {
    assert.equal(
      readSetupProfileGuideData(
        validSetupProfileGuideResponse(),
        validSetupPromptListResponse(items),
        7
      ),
      null
    )
  }
})

test('readSetupProfileGuideData returns null when the associated prompt is missing', () => {
  assert.equal(
    readSetupProfileGuideData(
      validSetupProfileGuideResponse(),
      validSetupPromptListResponse([{ id: 99, name: 'other', content: '' }]),
      7
    ),
    null
  )
})

test('readSetupProfileGuideData returns null for malformed associated prompts', () => {
  const nonPlainPrompt = Object.assign(new Date(0), {
    id: 13,
    name: 'default',
    content: ''
  })

  for (const prompt of [
    nonPlainPrompt,
    { id: 13, name: '', content: '' },
    { id: 13, name: 1, content: '' },
    { id: 13, name: 'default', content: 1 }
  ]) {
    assert.equal(
      readSetupProfileGuideData(
        validSetupProfileGuideResponse(),
        validSetupPromptListResponse([prompt]),
        7
      ),
      null
    )
  }
})

test('readSetupProfileGuideData returns null when required config groups are missing', () => {
  for (const group of ['channel', 'security', 'tool', 'other', 'memory']) {
    const configs = validSetupProfileConfigs()
    delete configs[group]

    assert.equal(
      readSetupProfileGuideData(
        validSetupProfileGuideResponse(configs),
        validSetupPromptListResponse(),
        7
      ),
      null
    )
  }
})

test('readSetupProfileGuideData returns null when required tool arrays are missing', () => {
  for (const arrayName of [
    'enabled_tools',
    'allowed_operation_dirs',
    'file_send_blocked_extensions'
  ]) {
    const configs = validSetupProfileConfigs()
    delete configs.tool[arrayName]

    assert.equal(
      readSetupProfileGuideData(
        validSetupProfileGuideResponse(configs),
        validSetupPromptListResponse(),
        7
      ),
      null
    )
  }
})

test('readSetupProfileGuideData returns null for malformed tool options', () => {
  for (const toolOption of [
    null,
    {},
    { value: '', label: 'Read file' },
    { value: 'read_file', label: '' },
    { value: 1, label: 'Read file' },
    { value: 'read_file', label: 1 },
    ['read_file', 'Read file']
  ]) {
    assert.equal(
      readSetupProfileGuideData(
        validSetupProfileGuideResponse(undefined, [toolOption]),
        validSetupPromptListResponse(),
        7
      ),
      null
    )
  }
})
