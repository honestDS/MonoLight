import assert from 'node:assert/strict'
import test from 'node:test'
import { defaultProfileConfigs } from '../../../src/constants/index.js'
import {
  createHarness,
  flush,
  normalize,
} from './profilePermissionsHarness.js'

test('ordinary users and superusers load safe model options without the channel management API', async t => {
  const modelOptionsItems = [
    {
      id: 11,
      name: 'Safe channel',
      is_active: true,
      model_ids: [
        { model_id: 'CHAT', usage: 'CHAT', is_enabled: true },
        { model_id: 'EMBEDDING', usage: 'EMBEDDING', is_enabled: true },
      ],
    },
  ]

  for (const scenario of [
    { name: 'ordinary', superuser: false, currentUid: 'user-a' },
    { name: 'superuser', superuser: true, currentUid: 'admin-user' },
  ]) {
    const harness = await createHarness(t, {
      ...scenario,
      modelOptionsItems,
    })

    for (const callback of harness.mountedCallbacks) callback()
    await flush()

    assert.deepEqual(normalize(harness.api.modelOptions), [{ page: 1, size: 1000 }], scenario.name)
    assert.equal(harness.api.channelList.length, 0, scenario.name)
    assert.deepEqual(normalize(harness.module.channels.value), modelOptionsItems, scenario.name)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], scenario.name)

    const chatModel = modelOptionsItems[0].model_ids.find(model => model.usage === 'CHAT')
    harness.module.auditModelKey.value = `${modelOptionsItems[0].id}::${chatModel.model_id}`
    assert.equal(harness.module.form.configs.security.audit_channel_id, modelOptionsItems[0].id, scenario.name)
    assert.equal(harness.module.form.configs.security.audit_model_id, chatModel.model_id, scenario.name)

    harness.module.handleRefresh()
    await flush()

    assert.deepEqual(normalize(harness.api.modelOptions), [
      { page: 1, size: 1000 },
      { page: 1, size: 1000 },
    ], scenario.name)
    assert.equal(harness.api.channelList.length, 0, scenario.name)
    assert.deepEqual(normalize(harness.module.channels.value), modelOptionsItems, scenario.name)
    assert.equal(harness.module.form.configs.security.audit_channel_id, modelOptionsItems[0].id, scenario.name)
    assert.equal(harness.module.form.configs.security.audit_model_id, chatModel.model_id, scenario.name)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], scenario.name)
  }
})

test('new and legacy profiles keep missing auxiliary channel rules empty and independent', async t => {
  const chatConfig = {
    ...defaultProfileConfigs().channel.chat_channel,
    chat_timeout: 111,
    rules: [{ channel_id: 41, model_id: 'chat-model', reasoning_effort: 'low', priority: 1, weight: 2 }],
  }
  const summaryConfig = {
    ...defaultProfileConfigs().channel.context_summary_channel,
    chat_timeout: 222,
    rules: [{ channel_id: 42, model_id: 'summary-model', reasoning_effort: 'medium', priority: 1, weight: 2 }],
  }
  const expectedAuxiliary = normalize(defaultProfileConfigs().channel.auxiliary_channel)
  const legacyRow = {
    id: 113,
    uid: 'user-a',
    name: 'legacy profile without auxiliary channel',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      channel: {
        chat_channel: normalize(chatConfig),
        context_summary_channel: normalize(summaryConfig),
      },
    },
  }
  const originalLegacyRow = normalize(legacyRow)

  for (const scenario of [
    { type: 'create', name: 'new profile' },
    { type: 'edit', name: 'legacy profile', row: legacyRow },
  ]) {
    const harness = await createHarness(t)
    if (scenario.type === 'create') harness.module.showDialog('create')
    else harness.module.showDialog('edit', scenario.row)
    await flush()

    if (scenario.type === 'create') {
      harness.module.form.configs.channel.chat_channel = normalize(chatConfig)
      harness.module.form.configs.channel.context_summary_channel = normalize(summaryConfig)
    }

    const auxiliary = harness.module.form.configs.channel.auxiliary_channel
    assert.deepEqual(normalize(auxiliary), expectedAuxiliary, scenario.type)
    assert.deepEqual(normalize(auxiliary.rules), [], scenario.type)
    assert.notEqual(auxiliary, harness.module.form.configs.channel.chat_channel, scenario.type)
    assert.notEqual(auxiliary, harness.module.form.configs.channel.context_summary_channel, scenario.type)

    harness.module.form.name = scenario.name
    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    assert.deepEqual(normalize(payload.configs.channel.auxiliary_channel), expectedAuxiliary, scenario.type)
    assert.deepEqual(normalize(payload.configs.channel.chat_channel), chatConfig, scenario.type)
    assert.deepEqual(normalize(payload.configs.channel.context_summary_channel), summaryConfig, scenario.type)
    if (scenario.type === 'edit') assert.deepEqual(normalize(legacyRow), originalLegacyRow)
  }
})

test('configured auxiliary channel rules survive cleanup, save, reopen, and clearing', async t => {
  const chatConfig = {
    ...defaultProfileConfigs().channel.chat_channel,
    chat_timeout: 111,
    rules: [{ channel_id: 41, model_id: 'chat-model', reasoning_effort: 'low', priority: 1, weight: 2 }],
  }
  const summaryConfig = {
    ...defaultProfileConfigs().channel.context_summary_channel,
    chat_timeout: 222,
    rules: [{ channel_id: 42, model_id: 'summary-model', reasoning_effort: 'medium', priority: 1, weight: 2 }],
  }
  const expectedRules = [
    { channel_id: 12, model_id: 'aux-first', priority: 10, weight: 0, reasoning_effort: 'none' },
    { channel_id: 11, model_id: 'aux-later', priority: 20, weight: 2, reasoning_effort: 'high' },
  ]
  const expectedAuxiliary = {
    ...defaultProfileConfigs().channel.auxiliary_channel,
    chat_timeout: 73,
    rules: expectedRules,
  }
  const row = {
    id: 114,
    uid: 'user-a',
    name: 'configured auxiliary channel profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      channel: {
        chat_channel: normalize(chatConfig),
        context_summary_channel: normalize(summaryConfig),
        auxiliary_channel: {
          chat_timeout: 73,
          rules: [
            { channel_id: 11, model_id: 'aux-later', reasoning_effort: ' high ', priority: 20, weight: 2 },
            { channel_id: 13, model_id: '', reasoning_effort: 'low', priority: 5, weight: 1 },
            { channel_id: 12, model_id: 'aux-first', reasoning_effort: ' none ', priority: 10, weight: 0 },
            { channel_id: null, model_id: 'orphan-model', reasoning_effort: 'medium', priority: 3, weight: 1 },
          ],
        },
      },
    },
  }
  const originalRow = normalize(row)
  const harness = await createHarness(t)

  harness.module.showDialog('edit', row)
  await flush()
  assert.equal(harness.module.form.configs.channel.auxiliary_channel.chat_timeout, 73)
  assert.equal(harness.module.form.configs.channel.auxiliary_channel.rerank_timeout, expectedAuxiliary.rerank_timeout)
  assert.equal(harness.module.form.configs.channel.auxiliary_channel.rules.length, 4)

  await harness.module.submitForm()
  await flush()

  const firstPayload = harness.api.updates[0].payload
  assert.deepEqual(normalize(firstPayload.configs.channel.auxiliary_channel), expectedAuxiliary)
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), expectedAuxiliary)
  assert.deepEqual(normalize(firstPayload.configs.channel.chat_channel), chatConfig)
  assert.deepEqual(normalize(firstPayload.configs.channel.context_summary_channel), summaryConfig)
  assert.deepEqual(normalize(row), originalRow)

  let savedRow = { ...row, configs: normalize(firstPayload.configs) }
  harness.module.showDialog('edit', savedRow)
  await flush()
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), expectedAuxiliary)

  await harness.module.submitForm()
  await flush()

  const secondPayload = harness.api.updates[1].payload
  assert.deepEqual(normalize(secondPayload.configs.channel), normalize(firstPayload.configs.channel))
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), expectedAuxiliary)
  assert.deepEqual(normalize(row), originalRow)

  const clearedAuxiliary = { ...expectedAuxiliary, rules: [] }
  harness.module.form.configs.channel.auxiliary_channel.rules = []
  await harness.module.submitForm()
  await flush()

  const clearedPayload = harness.api.updates[2].payload
  assert.deepEqual(normalize(clearedPayload.configs.channel.auxiliary_channel), clearedAuxiliary)
  savedRow = { ...row, configs: normalize(clearedPayload.configs) }
  harness.module.showDialog('edit', savedRow)
  await flush()
  assert.deepEqual(normalize(harness.module.form.configs.channel.auxiliary_channel), clearedAuxiliary)
  assert.deepEqual(normalize(row), originalRow)
})

test('subagent channel falls back to chat channel on legacy profiles and supports independent rules', async t => {
  const chatConfig = {
    ...defaultProfileConfigs().channel.chat_channel,
    chat_timeout: 111,
    rules: [{ channel_id: 41, model_id: 'chat-model', reasoning_effort: 'low', priority: 1, weight: 2 }],
  }
  const summaryConfig = {
    ...defaultProfileConfigs().channel.context_summary_channel,
    chat_timeout: 222,
    rules: [{ channel_id: 42, model_id: 'summary-model', reasoning_effort: 'medium', priority: 1, weight: 2 }],
  }
  const legacyRow = {
    id: 115,
    uid: 'user-a',
    name: 'legacy profile without subagent channel',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      channel: {
        chat_channel: normalize(chatConfig),
        context_summary_channel: normalize(summaryConfig),
      },
    },
  }
  const originalLegacyRow = normalize(legacyRow)
  const harness = await createHarness(t)

  harness.module.showDialog('edit', legacyRow)
  await flush()

  const subagentChannel = harness.module.form.configs.channel.subagent_channel
  assert.deepEqual(normalize(subagentChannel), normalize(chatConfig))
  assert.notEqual(subagentChannel, harness.module.form.configs.channel.chat_channel)

  subagentChannel.rules = [
    { channel_id: 52, model_id: 'sub-later', reasoning_effort: ' high ', priority: 20, weight: 2 },
    { channel_id: 53, model_id: '', reasoning_effort: 'low', priority: 5, weight: 1 },
    { channel_id: 51, model_id: 'sub-first', reasoning_effort: ' none ', priority: 10, weight: 0 },
    { channel_id: null, model_id: 'orphan-model', reasoning_effort: 'medium', priority: 3, weight: 1 },
  ]

  const expectedSubagentRules = [
    { channel_id: 51, model_id: 'sub-first', priority: 10, weight: 0, reasoning_effort: 'none' },
    { channel_id: 52, model_id: 'sub-later', priority: 20, weight: 2, reasoning_effort: 'high' },
  ]
  const expectedSubagent = {
    ...normalize(chatConfig),
    rules: expectedSubagentRules,
  }

  await harness.module.submitForm()
  await flush()

  const firstPayload = harness.api.updates[0].payload
  assert.deepEqual(normalize(firstPayload.configs.channel.subagent_channel), expectedSubagent)
  assert.deepEqual(normalize(firstPayload.configs.channel.chat_channel), chatConfig)
  assert.deepEqual(normalize(legacyRow), originalLegacyRow)

  const savedRow = { ...legacyRow, configs: normalize(firstPayload.configs) }
  harness.module.showDialog('edit', savedRow)
  await flush()
  assert.deepEqual(normalize(harness.module.form.configs.channel.subagent_channel), expectedSubagent)

  harness.module.form.configs.channel.subagent_channel.rules = []
  await harness.module.submitForm()
  await flush()

  const secondPayload = harness.api.updates[1].payload
  assert.deepEqual(normalize(secondPayload.configs.channel.subagent_channel.rules), [])
})
