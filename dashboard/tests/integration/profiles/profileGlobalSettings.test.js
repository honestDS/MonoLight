import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createHarness,
  dataResponse,
  flush,
  normalize,
} from './profilePermissionsHarness.js'

test('unknown and ordinary users never request or mutate global settings', async t => {
  for (const scenario of [
    { name: 'unknown', options: {} },
    { name: 'ordinary', options: { superuser: false, currentUid: 'user-a' } },
  ]) {
    const harness = await createHarness(t, scenario.options)
    const settingsBefore = normalize(harness.module.systemSettings)
    const dialogBefore = harness.module.settingsDialogVisible.value
    const submittingBefore = harness.module.settingsSubmitting.value

    assert.equal(harness.module.systemSettings.log_redaction_enabled, false, scenario.name)
    for (const callback of harness.mountedCallbacks) callback()
    await flush()
    harness.module.handleRefresh()
    await flush()
    assert.equal(await harness.module.loadSystemSettings(), false, scenario.name)
    await harness.module.showSystemSettingsDialog()
    await harness.module.saveSystemSettings()
    await flush()

    assert.equal(harness.api.systemSettings.length, 0, scenario.name)
    assert.equal(harness.api.systemSettingsUpdates.length, 0, scenario.name)
    assert.deepEqual(normalize(harness.module.systemSettings), settingsBefore, scenario.name)
    assert.equal(harness.module.settingsDialogVisible.value, dialogBefore, scenario.name)
    assert.equal(harness.module.settingsSubmitting.value, submittingBefore, scenario.name)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], scenario.name)
  }
})

test('superuser can load, open, and save global settings', async t => {
  const loadedSettings = {
    log_locale: 'en',
    log_retention_days: 30,
    log_max_entries: 50000,
    temp_dir_max_size_mb: 4096,
    llm_request_max_size_mb: 128,
    audit_retention_days: 180,
    audit_report_email: 'ops@example.com',
    agent_work_max_concurrency: 12,
    log_redaction_enabled: true,
  }
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => Promise.resolve(dataResponse(loadedSettings)),
    systemSettingsUpdate: payload => dataResponse({
      ...payload,
      audit_report_email: 'saved@example.com',
    }),
  })

  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(harness.module.systemSettings.log_retention_days, 7)
  assert.equal(harness.module.systemSettings.log_max_entries, 100000)
  assert.equal(await harness.module.loadSystemSettings(), true)
  assert.equal(harness.api.systemSettings.length, 1)
  assert.deepEqual(normalize(harness.module.systemSettings), loadedSettings)
  assert.equal(harness.module.systemSettings.log_retention_days, 30)
  assert.equal(harness.module.systemSettings.log_max_entries, 50000)
  assert.equal(harness.module.systemSettings.agent_work_max_concurrency, 12)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')

  await harness.module.showSystemSettingsDialog()
  assert.equal(harness.api.systemSettings.length, 2)
  assert.equal(harness.module.settingsDialogVisible.value, true)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, true)
  assert.equal(harness.module.systemSettings.log_retention_days, 30)
  assert.equal(harness.module.systemSettings.log_max_entries, 50000)

  harness.module.systemSettings.audit_retention_days = 365
  harness.module.systemSettings.log_retention_days = 45
  harness.module.systemSettings.log_max_entries = 250000
  harness.module.systemSettings.llm_request_max_size_mb = 256
  harness.module.systemSettings.agent_work_max_concurrency = 6
  harness.module.systemSettings.log_redaction_enabled = false
  const expectedPayload = normalize(harness.module.systemSettings)
  assert.equal(expectedPayload.log_retention_days, 45)
  assert.equal(expectedPayload.log_max_entries, 250000)
  assert.equal(typeof expectedPayload.log_redaction_enabled, 'boolean')
  assert.equal(expectedPayload.log_redaction_enabled, false)
  await harness.module.saveSystemSettings()

  assert.deepEqual(harness.api.systemSettingsUpdates, [expectedPayload])
  assert.equal(harness.api.systemSettingsUpdates[0].log_retention_days, 45)
  assert.equal(harness.api.systemSettingsUpdates[0].log_max_entries, 250000)
  assert.equal(harness.api.systemSettingsUpdates[0].llm_request_max_size_mb, 256)
  assert.equal(harness.api.systemSettingsUpdates[0].agent_work_max_concurrency, 6)
  assert.equal('session_reply_max_concurrency' in harness.api.systemSettingsUpdates[0], false)
  assert.equal(typeof harness.api.systemSettingsUpdates[0].log_redaction_enabled, 'boolean')
  assert.equal(harness.api.systemSettingsUpdates[0].log_redaction_enabled, false)
  assert.deepEqual(normalize(harness.module.systemSettings), {
    ...expectedPayload,
    audit_report_email: 'saved@example.com',
  })
  assert.equal(harness.module.systemSettings.log_retention_days, 45)
  assert.equal(harness.module.systemSettings.log_max_entries, 250000)
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 256)
  assert.equal(harness.module.systemSettings.agent_work_max_concurrency, 6)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')
  assert.equal(harness.module.settingsDialogVisible.value, false)
  assert.equal(harness.module.settingsSubmitting.value, false)

  await harness.module.showSystemSettingsDialog()
  assert.equal(harness.api.systemSettings.length, 3)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, true)
  assert.equal(harness.module.systemSettings.log_retention_days, 30)
  assert.equal(harness.module.systemSettings.log_max_entries, 50000)
  harness.module.systemSettings.log_redaction_enabled = false
  await harness.module.saveSystemSettings()

  assert.equal(harness.api.systemSettingsUpdates.length, 2)
  assert.equal(harness.api.systemSettingsUpdates[1].log_retention_days, 30)
  assert.equal(harness.api.systemSettingsUpdates[1].log_max_entries, 50000)
  assert.equal(typeof harness.api.systemSettingsUpdates[1].log_redaction_enabled, 'boolean')
  assert.equal(harness.api.systemSettingsUpdates[1].log_redaction_enabled, false)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')
})

test('legacy global settings responses preserve the default request size across reloads and save', async t => {
  const legacySettings = {
    log_locale: 'en',
    temp_dir_max_size_mb: 4096,
    audit_retention_days: 180,
    audit_report_email: 'ops@example.com',
    agent_work_max_concurrency: 12,
  }
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => Promise.resolve(dataResponse(legacySettings)),
  })

  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
  assert.equal(await harness.module.loadSystemSettings(), true)
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)

  assert.equal(await harness.module.loadSystemSettings(), true)
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)

  await harness.module.showSystemSettingsDialog()
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)

  await harness.module.saveSystemSettings()

  assert.equal(harness.api.systemSettingsUpdates[0].llm_request_max_size_mb, 32)
  assert.notEqual(harness.api.systemSettingsUpdates[0].llm_request_max_size_mb, undefined)
  assert.equal(typeof harness.api.systemSettingsUpdates[0].log_redaction_enabled, 'boolean')
  assert.equal(harness.api.systemSettingsUpdates[0].log_redaction_enabled, false)
  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 32)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, false)
})

test('failed global settings load does not open the dialog', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => Promise.reject(new Error('settings load failed')),
  })
  const settingsBefore = normalize(harness.module.systemSettings)

  await harness.module.showSystemSettingsDialog()

  assert.equal(harness.api.systemSettings.length, 1)
  assert.equal(harness.module.settingsDialogVisible.value, false)
  assert.deepEqual(normalize(harness.module.systemSettings), settingsBefore)
  assert.equal(harness.module.settingsSubmitting.value, false)
  assert.deepEqual(harness.messages.filter(message => message.type === 'error').map(message => message.args[0]), [
    'settings load failed',
  ])
})

test('failed global settings save preserves the request size and dialog state', async t => {
  const harness = await createHarness(t, {
    superuser: true,
    systemSettingsUpdate: () => Promise.reject(new Error('settings save failed')),
  })

  await harness.module.showSystemSettingsDialog()
  harness.module.systemSettings.llm_request_max_size_mb = 256
  harness.module.systemSettings.log_retention_days = 14
  harness.module.systemSettings.log_max_entries = 200000
  harness.module.systemSettings.log_redaction_enabled = true
  await harness.module.saveSystemSettings()

  assert.equal(harness.module.systemSettings.llm_request_max_size_mb, 256)
  assert.equal(harness.module.systemSettings.log_retention_days, 14)
  assert.equal(harness.module.systemSettings.log_max_entries, 200000)
  assert.equal(harness.module.systemSettings.log_redaction_enabled, true)
  assert.equal(typeof harness.module.systemSettings.log_redaction_enabled, 'boolean')
  assert.equal(harness.module.settingsDialogVisible.value, true)
  assert.equal(harness.module.settingsSubmitting.value, false)
  assert.deepEqual(harness.messages.filter(message => message.type === 'error').map(message => message.args[0]), [
    'settings save failed',
  ])
})

test('clearing permissions ignores late global settings responses', async t => {
  let resolveSettingsRequest
  const settingsRequest = new Promise(resolve => {
    resolveSettingsRequest = resolve
  })
  const loadHarness = await createHarness(t, {
    superuser: true,
    systemSettingsRequest: () => settingsRequest,
  })
  const settingsBeforeLoad = normalize(loadHarness.module.systemSettings)
  const opening = loadHarness.module.showSystemSettingsDialog()
  assert.equal(loadHarness.api.systemSettings.length, 1)

  loadHarness.permissionsStore.clear()
  resolveSettingsRequest(dataResponse({
    log_locale: 'late',
    temp_dir_max_size_mb: 8192,
    audit_retention_days: 365,
    audit_report_email: 'late@example.com',
    agent_work_max_concurrency: 32,
  }))
  await opening
  await flush()

  assert.deepEqual(normalize(loadHarness.module.systemSettings), settingsBeforeLoad)
  assert.equal(loadHarness.module.settingsDialogVisible.value, false)
  assert.equal(loadHarness.module.settingsSubmitting.value, false)
  assert.deepEqual(loadHarness.messages, [])

  let resolveSettingsUpdate
  const settingsUpdate = new Promise(resolve => {
    resolveSettingsUpdate = resolve
  })
  const saveHarness = await createHarness(t, {
    superuser: true,
    systemSettingsUpdate: () => settingsUpdate,
  })
  await saveHarness.module.showSystemSettingsDialog()
  saveHarness.module.systemSettings.log_locale = 'pending'
  const settingsBeforeSave = normalize(saveHarness.module.systemSettings)
  const saving = saveHarness.module.saveSystemSettings()
  assert.equal(saveHarness.module.settingsSubmitting.value, true)

  saveHarness.permissionsStore.clear()
  resolveSettingsUpdate(dataResponse({
    ...settingsBeforeSave,
    log_locale: 'late-save',
  }))
  await saving
  await flush()

  assert.deepEqual(saveHarness.api.systemSettingsUpdates, [settingsBeforeSave])
  assert.deepEqual(normalize(saveHarness.module.systemSettings), settingsBeforeSave)
  assert.equal(saveHarness.module.settingsDialogVisible.value, true)
  assert.equal(saveHarness.module.settingsSubmitting.value, false)
  assert.deepEqual(saveHarness.messages, [])
})
