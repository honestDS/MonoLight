import assert from 'node:assert/strict'
import test from 'node:test'
import { defaultProfileConfigs } from '../../../src/constants/index.js'
import {
  createHarness,
  flush,
  IMAGE_COMPRESSION_DEFAULT_KB,
  normalize,
  pageResponse,
} from './profilePermissionsHarness.js'

test('unknown users cannot set any profile as default', async t => {
  const harness = await createHarness(t)
  harness.module.showOwnerColumn.value = true
  harness.module.currentUid.value = 'user-a'

  for (const row of [
    { uid: 'user-a' },
    { uid: 'user-b' },
    { uid: null },
    {},
    undefined,
    null,
  ]) {
    assert.equal(harness.module.canSetDefaultProfile(row), false)
  }
})

test('ordinary users can set only their own profile as default', async t => {
  const harness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
  })

  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-a' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-b' }), false)
  assert.equal(harness.module.canSetDefaultProfile({ uid: null }), false)
  assert.equal(harness.module.canSetDefaultProfile({}), false)
  assert.equal(harness.module.canSetDefaultProfile(), false)

  harness.module.showOwnerColumn.value = true
  harness.module.currentUid.value = 'user-b'
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-a' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-b' }), false)
})

test('superusers can set profiles with any owner as default', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
  })

  assert.equal(harness.module.canSetDefaultProfile({ uid: 'admin-user' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: 'user-a' }), true)
  assert.equal(harness.module.canSetDefaultProfile({ uid: null }), true)
  assert.equal(harness.module.canSetDefaultProfile({}), true)
  assert.equal(harness.module.canSetDefaultProfile(), false)
})

test('clearing permissions removes default-profile permission', async t => {
  const ordinaryHarness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
  })
  const ownRow = { uid: 'user-a' }
  assert.equal(ordinaryHarness.module.canSetDefaultProfile(ownRow), true)
  ordinaryHarness.permissionsStore.clear()
  assert.equal(ordinaryHarness.module.canSetDefaultProfile(ownRow), false)

  const superuserHarness = await createHarness(t, {
    superuser: true,
    currentUid: 'admin-user',
  })
  const otherRow = { uid: 'user-b' }
  assert.equal(superuserHarness.module.canSetDefaultProfile(otherRow), true)
  superuserHarness.permissionsStore.clear()
  assert.equal(superuserHarness.module.canSetDefaultProfile(otherRow), false)
})

test('superuser create form saves system-directory protection and audit depth without leaking between dialogs', async t => {
  const harness = await createHarness(t, { superuser: true })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.security.protect_system_directories, true)
  assert.equal(harness.module.form.configs.security.audit_depth, 3)

  harness.module.form.name = 'custom security settings'
  harness.module.form.configs.security.protect_system_directories = false
  harness.module.form.configs.security.audit_depth = 1
  await harness.module.submitForm()
  await flush()

  const customPayload = harness.api.creates[0]
  assert.ok(customPayload)
  assert.equal(customPayload.name, 'custom security settings')
  assert.equal(typeof customPayload.configs.security.protect_system_directories, 'boolean')
  assert.equal(customPayload.configs.security.protect_system_directories, false)
  assert.equal(customPayload.configs.security.audit_depth, 1)

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.security.protect_system_directories, true)
  assert.equal(harness.module.form.configs.security.audit_depth, 3)
  assert.equal(customPayload.configs.security.protect_system_directories, false)
  assert.equal(customPayload.configs.security.audit_depth, 1)

  harness.module.form.name = 'default security settings'
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.creates.length, 2)
  assert.equal(harness.api.creates[1].configs.security.protect_system_directories, true)
  assert.equal(harness.api.creates[1].configs.security.audit_depth, 3)
})

test('superuser editing legacy security configs supplies defaults and converts timeout without mutating the row', async t => {
  const harness = await createHarness(t, { superuser: true })
  const row = {
    id: 81,
    uid: 'user-a',
    name: 'legacy security profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      security: {
        audit_threshold: 0,
        audit_confirmation_timeout_minutes: 10,
      },
    },
  }
  const originalRow = normalize(row)

  for (let iteration = 0; iteration < 2; iteration += 1) {
    harness.module.showDialog('edit', row)
    await flush()

    assert.equal(harness.module.form.configs.security.protect_system_directories, true)
    assert.equal(harness.module.form.configs.security.audit_depth, 3)
    assert.equal(harness.module.form.configs.security.audit_threshold, 0)
    assert.equal(harness.module.form.configs.security.audit_confirmation_timeout_seconds, 600)

    await harness.module.submitForm()
    await flush()

    const update = harness.api.updates[iteration]
    assert.equal(update.id, row.id)
    assert.equal(typeof update.payload.configs.security.protect_system_directories, 'boolean')
    assert.equal(update.payload.configs.security.protect_system_directories, true)
    assert.equal(update.payload.configs.security.audit_depth, 3)
    assert.equal(update.payload.configs.security.audit_threshold, 0)
    assert.equal(update.payload.configs.security.audit_confirmation_timeout_seconds, 600)
    assert.equal(Object.prototype.hasOwnProperty.call(update.payload.configs.security, 'audit_confirmation_timeout_minutes'), false)
    assert.deepEqual(normalize(row), originalRow)
  }
})

test('editing and resaving configured audit and tool settings preserves boundary values', async t => {
  const modelOptionsItems = [{
    id: 71,
    name: 'Audit channel',
    is_active: true,
    model_ids: [{ model_id: 'audit-model', usage: 'CHAT', is_enabled: true }],
  }]
  const enabledTools = ['file_tool', 'read_multimodal_file']
  const allowedOperationDirs = ['D:/workspace/allowed']
  const scenarios = [
    { id: 91, protectSystemDirectories: false, auditDepth: 1, auditThreshold: 0 },
    { id: 92, protectSystemDirectories: true, auditDepth: 100, auditThreshold: 7 },
  ]

  for (const scenario of scenarios) {
    const harness = await createHarness(t, {
      superuser: false,
      currentUid: 'user-a',
      modelOptionsItems,
    })
    for (const callback of harness.mountedCallbacks) callback()
    await flush()

    const configs = defaultProfileConfigs({
      tool: { image_compression_threshold_kb: IMAGE_COMPRESSION_DEFAULT_KB },
    })
    configs.security = {
      ...configs.security,
      audit_channel_id: modelOptionsItems[0].id,
      audit_model_id: 'audit-model',
      audit_threshold: scenario.auditThreshold,
      audit_confirmation_timeout_seconds: 420,
      audit_report_language: 'en',
      audit_depth: scenario.auditDepth,
      protect_system_directories: scenario.protectSystemDirectories,
    }
    configs.tool = {
      ...configs.tool,
      enabled_tools: [...enabledTools],
      allowed_operation_dirs: [...allowedOperationDirs],
    }
    let editableRow = {
      id: scenario.id,
      uid: 'user-a',
      name: `configured security profile ${scenario.id}`,
      prompt_id: null,
      knowledge_base_ids: [],
      configs,
    }
    const originalRow = normalize(editableRow)

    for (let iteration = 0; iteration < 2; iteration += 1) {
      harness.module.showDialog('edit', editableRow)
      await flush()

      assert.equal(harness.module.auditModelKey.value, '71::audit-model')
      assert.equal(typeof harness.module.form.configs.security.protect_system_directories, 'boolean')
      assert.equal(harness.module.form.configs.security.protect_system_directories, scenario.protectSystemDirectories)
      assert.equal(harness.module.form.configs.security.audit_depth, scenario.auditDepth)
      assert.deepEqual(normalize(harness.module.form.configs.tool.enabled_tools), enabledTools)
      assert.deepEqual(normalize(harness.module.form.configs.tool.allowed_operation_dirs), allowedOperationDirs)

      const expectedSecurity = normalize(editableRow.configs.security)
      const expectedTool = normalize(editableRow.configs.tool)
      const formSecurity = normalize(harness.module.form.configs.security)
      assert.deepEqual(formSecurity, expectedSecurity)
      await harness.module.submitForm()
      await flush()

      const update = harness.api.updates[iteration]
      assert.equal(update.id, scenario.id)
      assert.equal(Object.prototype.hasOwnProperty.call(update.payload.configs, 'security'), false)
      assert.deepEqual(normalize(harness.module.form.configs.security), formSecurity)
      assert.deepEqual(normalize(update.payload.configs.tool), expectedTool)
      assert.deepEqual(normalize(update.payload.configs.tool.enabled_tools), enabledTools)
      assert.deepEqual(normalize(update.payload.configs.tool.allowed_operation_dirs), allowedOperationDirs)
      assert.deepEqual(normalize(editableRow), originalRow)

      editableRow = {
        ...editableRow,
        configs: {
          ...normalize(update.payload.configs),
          security: expectedSecurity,
        },
      }
    }
  }
})

test('ordinary create dialog defaults image compression and keeps submitted values isolated', async t => {
  const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_enabled, true)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, IMAGE_COMPRESSION_DEFAULT_KB)

  harness.module.form.name = 'custom image compression profile'
  harness.module.form.configs.tool.image_compression_enabled = false
  harness.module.form.configs.tool.image_compression_threshold_kb = 1234
  await harness.module.submitForm()
  await flush()

  const firstPayload = harness.api.creates[0]
  assert.equal(firstPayload.configs.tool.image_compression_enabled, false)
  assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, 1234)

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_enabled, true)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, IMAGE_COMPRESSION_DEFAULT_KB)
  assert.equal(firstPayload.configs.tool.image_compression_enabled, false)
  assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, 1234)

  harness.module.form.name = 'default image compression profile'
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.creates.length, 2)
  assert.equal(harness.api.creates[1].configs.tool.image_compression_enabled, true)
  assert.equal(harness.api.creates[1].configs.tool.image_compression_threshold_kb, IMAGE_COMPRESSION_DEFAULT_KB)
  assert.equal(firstPayload.configs.tool.image_compression_enabled, false)
  assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, 1234)
})

test('ordinary edit supplies image compression defaults for legacy tool configs', async t => {
  const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })
  const row = {
    id: 121,
    uid: 'user-a',
    name: 'legacy image compression profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: { tool: { tool_timeout: 45 } },
  }
  const originalRow = normalize(row)

  harness.module.showDialog('edit', row)
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_enabled, true)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, IMAGE_COMPRESSION_DEFAULT_KB)

  await harness.module.submitForm()
  await flush()

  const update = harness.api.updates[0]
  assert.equal(update.id, row.id)
  assert.equal(update.payload.configs.tool.image_compression_enabled, true)
  assert.equal(update.payload.configs.tool.image_compression_threshold_kb, IMAGE_COMPRESSION_DEFAULT_KB)
  assert.deepEqual(normalize(row), originalRow)
})

test('profile default refresh preserves open values and updates later image compression defaults', async t => {
  let listRequestCount = 0
  const harness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
    profileListRequest: () => {
      listRequestCount += 1
      const threshold = listRequestCount === 1 ? IMAGE_COMPRESSION_DEFAULT_KB : 1024
      return Promise.resolve(pageResponse([], 0, {
        show_owner: false,
        current_uid: 'user-a',
        tool_options: [],
        config_defaults: { tool: { image_compression_threshold_kb: threshold } },
        config_permissions: { security: false },
      }))
    },
  })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, IMAGE_COMPRESSION_DEFAULT_KB)
  harness.module.form.name = 'profile with an open custom threshold'
  harness.module.form.configs.tool.image_compression_threshold_kb = 1234

  harness.module.handleRefresh()
  await flush()

  assert.equal(listRequestCount, 2)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 1234)
  await harness.module.submitForm()
  await flush()

  const firstPayload = harness.api.creates[0]
  const firstPayloadSnapshot = normalize(firstPayload)
  assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, 1234)

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 1024)
  assert.deepEqual(normalize(firstPayload), firstPayloadSnapshot)
  harness.module.form.name = 'new profile with refreshed defaults'
  await harness.module.submitForm()
  await flush()

  const refreshedCreatePayload = harness.api.creates[1]
  const refreshedCreatePayloadSnapshot = normalize(refreshedCreatePayload)
  assert.equal(refreshedCreatePayload.configs.tool.image_compression_threshold_kb, 1024)

  const legacyRow = {
    id: 124,
    uid: 'user-a',
    name: 'legacy profile without an image compression threshold',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: { tool: { tool_timeout: 45 } },
  }
  const originalLegacyRow = normalize(legacyRow)
  harness.module.showDialog('edit', legacyRow)
  await flush()
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 1024)
  harness.module.form.name = legacyRow.name
  await harness.module.submitForm()
  await flush()

  const update = harness.api.updates[0]
  assert.equal(update.payload.configs.tool.image_compression_threshold_kb, 1024)
  assert.deepEqual(normalize(legacyRow), originalLegacyRow)
  assert.deepEqual(normalize(firstPayload), firstPayloadSnapshot)
  assert.deepEqual(normalize(refreshedCreatePayload), refreshedCreatePayloadSnapshot)
})

test('missing profile config defaults do not invent image compression thresholds', async t => {
  const scenarios = [
    { type: 'create', name: 'new profile without defaults', hasThreshold: false },
    {
      type: 'edit',
      name: 'legacy profile without a threshold',
      hasThreshold: false,
      row: {
        id: 125,
        uid: 'user-a',
        name: 'legacy profile without a threshold',
        prompt_id: null,
        knowledge_base_ids: [],
        configs: { tool: { tool_timeout: 45 } },
      },
    },
    {
      type: 'edit',
      name: 'explicit threshold without defaults',
      hasThreshold: true,
      threshold: 1536,
      row: {
        id: 126,
        uid: 'user-a',
        name: 'explicit threshold without defaults',
        prompt_id: null,
        knowledge_base_ids: [],
        configs: { tool: { image_compression_threshold_kb: 1536 } },
      },
    },
  ]

  for (const scenario of scenarios) {
    const harness = await createHarness(t, {
      superuser: false,
      currentUid: 'user-a',
      profileListRequest: () => Promise.resolve(pageResponse([], 0, {
        show_owner: false,
        current_uid: 'user-a',
        tool_options: [],
        config_permissions: { security: false },
      })),
    })
    const originalRow = scenario.row ? normalize(scenario.row) : null
    if (scenario.type === 'create') harness.module.showDialog('create')
    else harness.module.showDialog('edit', scenario.row)
    await flush()
    harness.module.form.name = scenario.name

    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.name)
    assert.equal(
      Object.prototype.hasOwnProperty.call(payload.configs.tool, 'image_compression_threshold_kb'),
      scenario.hasThreshold,
      scenario.name,
    )
    if (scenario.hasThreshold) {
      assert.equal(payload.configs.tool.image_compression_threshold_kb, scenario.threshold, scenario.name)
    }
    if (scenario.row) assert.deepEqual(normalize(scenario.row), originalRow, scenario.name)
  }
})

test('ordinary edits preserve image compression values through save, reopen, and toggle changes', async t => {
  const scenarios = [
    { id: 122, enabled: false, threshold: 1 },
    { id: 123, enabled: true, threshold: 2048 },
  ]

  for (const scenario of scenarios) {
    const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })
    const row = {
      id: scenario.id,
      uid: 'user-a',
      name: `image compression profile ${scenario.id}`,
      prompt_id: null,
      knowledge_base_ids: [],
      configs: {
        tool: {
          image_compression_enabled: scenario.enabled,
          image_compression_threshold_kb: scenario.threshold,
        },
      },
    }
    const originalRow = normalize(row)

    harness.module.showDialog('edit', row)
    await flush()
    assert.equal(harness.module.form.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, scenario.threshold)
    await harness.module.submitForm()
    await flush()

    const firstPayload = harness.api.updates[0].payload
    assert.equal(firstPayload.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(firstPayload.configs.tool.image_compression_threshold_kb, scenario.threshold)

    const savedRow = { ...row, configs: normalize(firstPayload.configs) }
    harness.module.showDialog('edit', savedRow)
    await flush()
    assert.equal(harness.module.form.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, scenario.threshold)
    await harness.module.submitForm()
    await flush()

    const secondPayload = harness.api.updates[1].payload
    assert.equal(secondPayload.configs.tool.image_compression_enabled, scenario.enabled)
    assert.equal(secondPayload.configs.tool.image_compression_threshold_kb, scenario.threshold)

    harness.module.form.configs.tool.image_compression_enabled = !scenario.enabled
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, scenario.threshold)
    await harness.module.submitForm()
    await flush()

    const toggledPayload = harness.api.updates[2].payload
    assert.equal(toggledPayload.configs.tool.image_compression_enabled, !scenario.enabled)
    assert.equal(toggledPayload.configs.tool.image_compression_threshold_kb, scenario.threshold)
    assert.deepEqual(normalize(row), originalRow)
  }
})

test('unknown and ordinary saves omit directly edited security without mutating the form', async t => {
  for (const identity of [
    { name: 'unknown', options: {} },
    { name: 'ordinary', options: { superuser: false, currentUid: 'user-a' } },
  ]) {
    for (const type of ['create', 'edit']) {
      const harness = await createHarness(t, identity.options)
      const configs = defaultProfileConfigs()
      configs.security.audit_depth = 2
      configs.tool.enabled_tools = ['file_tool']
      configs.other.retained_value = 'keep'
      const row = {
        id: 111,
        uid: 'user-a',
        name: `${identity.name} ${type}`,
        prompt_id: null,
        knowledge_base_ids: [],
        configs,
      }
      const originalRow = normalize(row)

      if (type === 'edit') harness.module.showDialog('edit', row)
      else harness.module.showDialog('create')
      await flush()
      harness.module.form.name = row.name
      harness.module.form.configs.security.protect_system_directories = false
      harness.module.form.configs.security.audit_depth = 11
      harness.module.form.configs.tool.enabled_tools = ['file_tool', 'read_multimodal_file']
      harness.module.form.configs.other.retained_value = 'keep'
      const formSecurity = normalize(harness.module.form.configs.security)

      await harness.module.submitForm()
      await flush()

      const payload = type === 'create'
        ? harness.api.creates[0]
        : harness.api.updates[0]?.payload
      assert.ok(payload, `${identity.name} ${type}`)
      assert.equal(Object.prototype.hasOwnProperty.call(payload.configs, 'security'), false)
      assert.deepEqual(normalize(payload.configs.tool.enabled_tools), ['file_tool', 'read_multimodal_file'])
      assert.equal(payload.configs.other.retained_value, 'keep')
      assert.deepEqual(normalize(harness.module.form.configs.security), formSecurity)
      if (type === 'edit') assert.deepEqual(normalize(row), originalRow)
    }
  }
})

test('profile config permissions default to false when unavailable', async t => {
  const unknownHarness = await createHarness(t)
  assert.deepEqual(normalize(unknownHarness.module.configPermissions.value), { security: false })

  const missingMetadataHarness = await createHarness(t, {
    superuser: true,
    profileListRequest: () => Promise.resolve(pageResponse([], 0, {
      show_owner: false,
      current_uid: 'admin-user',
      tool_options: [],
    })),
  })
  assert.deepEqual(normalize(missingMetadataHarness.module.configPermissions.value), { security: false })
})

test('superuser can save security configs granted by profile metadata', async t => {
  const harness = await createHarness(t, { superuser: true })
  assert.equal(harness.module.configPermissions.value.security, true)
  harness.module.showDialog('create')
  await flush()
  harness.module.form.name = 'permitted security profile'
  harness.module.form.configs.security.audit_depth = 9
  const formSecurity = normalize(harness.module.form.configs.security)

  await harness.module.submitForm()
  await flush()

  assert.deepEqual(normalize(harness.api.creates[0].configs.security), formSecurity)
})

test('point config permissions omit only the denied tool field', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    configPermissions: { 'tool.max_parallel_tools': false },
  })
  harness.module.showDialog('create')
  await flush()
  harness.module.form.name = 'point permission profile'
  harness.module.form.configs.tool.max_parallel_tools = 17
  harness.module.form.configs.tool.enabled_tools = ['file_tool']
  harness.module.form.configs.tool.allowed_operation_dirs = ['D:/workspace/allowed']
  harness.module.form.configs.tool.file_send_blocked_extensions = ['.zip']

  await harness.module.submitForm()
  await flush()

  const { tool } = harness.api.creates[0].configs
  assert.equal(Object.prototype.hasOwnProperty.call(tool, 'max_parallel_tools'), false)
  assert.deepEqual(normalize(tool.enabled_tools), ['file_tool'])
  assert.deepEqual(normalize(tool.allowed_operation_dirs), ['D:/workspace/allowed'])
  assert.deepEqual(normalize(tool.file_send_blocked_extensions), ['.zip'])
})

test('repeated profile submissions do not progressively mutate denied configs', async t => {
  const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })
  const row = {
    id: 112,
    uid: 'user-a',
    name: 'repeated denied security profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: defaultProfileConfigs(),
  }
  row.configs.security.audit_depth = 4
  row.configs.tool.enabled_tools = ['file_tool']
  const originalRow = normalize(row)
  harness.module.showDialog('edit', row)
  await flush()
  harness.module.form.configs.security.audit_depth = 12
  const formConfigs = normalize(harness.module.form.configs)

  await harness.module.submitForm()
  await flush()
  await harness.module.submitForm()
  await flush()

  assert.equal(harness.api.updates.length, 2)
  assert.deepEqual(normalize(harness.api.updates[0].payload), normalize(harness.api.updates[1].payload))
  assert.equal(Object.prototype.hasOwnProperty.call(harness.api.updates[0].payload.configs, 'security'), false)
  assert.deepEqual(normalize(harness.module.form.configs), formConfigs)
  assert.deepEqual(normalize(row), originalRow)
})

test('clearing permissions ignores late profile list permissions and rows', async t => {
  let resolveLateList
  let listRequestCount = 0
  const harness = await createHarness(t, {
    superuser: true,
    profileListRequest: () => {
      listRequestCount += 1
      if (listRequestCount === 1) {
        return Promise.resolve(pageResponse([], 0, {
          show_owner: false,
          current_uid: 'admin-user',
          tool_options: [],
          config_permissions: { security: true },
        }))
      }
      return new Promise(resolve => {
        resolveLateList = resolve
      })
    },
  })
  assert.equal(harness.module.configPermissions.value.security, true)

  const loading = harness.module.loadProfiles()
  assert.equal(harness.api.profileList.length, 2)
  harness.permissionsStore.clear()
  await flush()
  assert.deepEqual(normalize(harness.module.configPermissions.value), { security: false })

  resolveLateList(pageResponse([{ id: 999, name: 'stale admin profile' }], 1, {
    show_owner: true,
    current_uid: 'admin-user',
    tool_options: [],
    config_permissions: { security: true },
  }))
  await loading
  await flush()

  assert.deepEqual(normalize(harness.module.configPermissions.value), { security: false })
  assert.deepEqual(normalize(harness.module.profiles.value), [])
  assert.equal(harness.module.total.value, 0)
})

test('ordinary profile editor preserves context summary concurrency defaults and isolation', async t => {
  const harness = await createHarness(t, { superuser: false, currentUid: 'user-a' })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.other.context_summary_max_concurrency, 4)

  harness.module.form.name = 'custom context summary concurrency profile'
  harness.module.form.configs.other.context_summary_max_concurrency = 1
  await harness.module.submitForm()
  await flush()

  const firstPayload = harness.api.creates[0]
  const firstPayloadSnapshot = normalize(firstPayload)
  assert.equal(firstPayload.configs.other.context_summary_max_concurrency, 1)

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.configs.other.context_summary_max_concurrency, 4)
  assert.deepEqual(normalize(firstPayload), firstPayloadSnapshot)

  const legacyRow = {
    id: 127,
    uid: 'user-a',
    name: 'legacy context summary profile',
    prompt_id: null,
    knowledge_base_ids: [],
    configs: {
      other: { context_summary_threshold_percent: 80 },
      tool: { image_compression_threshold_kb: 1234 },
    },
  }
  const originalLegacyRow = normalize(legacyRow)
  harness.module.showDialog('edit', legacyRow)
  await flush()
  assert.equal(harness.module.form.configs.other.context_summary_max_concurrency, 4)
  assert.equal(harness.module.form.configs.other.context_summary_threshold_percent, 80)
  assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 1234)
  harness.module.form.configs.other.context_summary_max_concurrency = 20
  assert.deepEqual(normalize(legacyRow), originalLegacyRow)

  for (const scenario of [
    { id: 128, concurrency: 1 },
    { id: 129, concurrency: 20 },
  ]) {
    const row = {
      id: scenario.id,
      uid: 'user-a',
      name: `configured context summary profile ${scenario.concurrency}`,
      prompt_id: null,
      knowledge_base_ids: [],
      configs: {
        other: {
          context_summary_threshold_percent: 70,
          context_summary_max_concurrency: scenario.concurrency,
        },
        tool: {
          image_compression_enabled: false,
          image_compression_threshold_kb: 1536,
        },
      },
    }
    const originalRow = normalize(row)

    harness.module.showDialog('edit', row)
    await flush()
    assert.equal(harness.module.form.configs.other.context_summary_max_concurrency, scenario.concurrency)
    assert.equal(harness.module.form.configs.other.context_summary_threshold_percent, 70)
    assert.equal(harness.module.form.configs.tool.image_compression_enabled, false)
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 1536)

    await harness.module.submitForm()
    await flush()

    const update = harness.api.updates[scenario.concurrency === 1 ? 0 : 1]
    assert.equal(update.id, scenario.id)
    assert.equal(update.payload.configs.other.context_summary_max_concurrency, scenario.concurrency)
    assert.equal(update.payload.configs.other.context_summary_threshold_percent, 70)
    assert.equal(update.payload.configs.tool.image_compression_enabled, false)
    assert.equal(update.payload.configs.tool.image_compression_threshold_kb, 1536)
    assert.deepEqual(normalize(row), originalRow)

    const savedRow = { ...row, configs: normalize(update.payload.configs) }
    harness.module.showDialog('edit', savedRow)
    await flush()
    assert.equal(harness.module.form.configs.other.context_summary_max_concurrency, scenario.concurrency)
    assert.equal(harness.module.form.configs.other.context_summary_threshold_percent, 70)
    assert.equal(harness.module.form.configs.tool.image_compression_enabled, false)
    assert.equal(harness.module.form.configs.tool.image_compression_threshold_kb, 1536)
  }
})
