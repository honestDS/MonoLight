import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { computed, reactive, ref } from 'vue'

const viewSource = readFileSync(new URL('../src/views/ScheduledTasksView.vue', import.meta.url), 'utf8')
const scriptMatch = viewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!scriptMatch) throw new Error('ScheduledTasksView.vue script setup was not found')

const stripSingleLineImports = sourceText => sourceText
  .split(/\r?\n/)
  .filter(line => !/^\s*import\b/.test(line))
  .join('\n')

const snapshot = value => JSON.parse(JSON.stringify(value))

const fillForm = (form, overrides = {}) => Object.assign(form, {
  name: 'Scheduled review',
  session_id: 'session-1',
  message: 'Review the latest changes',
  interval_seconds: 300,
  run_once: false,
  status: 'enabled',
  ...overrides,
})

const createHarness = (options = {}) => {
  const messages = []
  const mountedCallbacks = []
  const apiState = {
    listRequests: [],
    createRequests: [],
    updateRequests: [],
  }
  const ElMessage = Object.fromEntries(['warning', 'error', 'success'].map(type => [
    type,
    value => messages.push({ type, value }),
  ]))
  const scheduledTaskApi = {
    list: async params => {
      apiState.listRequests.push({ ...params })
      return { data: { data: { items: [], total: 0 } } }
    },
    create: async payload => {
      const request = { payload }
      apiState.createRequests.push(request)
      return options.createHandler ? options.createHandler(request) : { data: { data: {} } }
    },
    update: async (id, payload) => {
      const request = { id, payload }
      apiState.updateRequests.push(request)
      return options.updateHandler
        ? options.updateHandler(request)
        : { data: { data: { status: payload.status, next_run_at: null } } }
    },
    delete: async () => ({ data: { data: {} } }),
  }
  const context = vm.createContext({
    ref,
    reactive,
    computed,
    useI18n: () => ({ t: key => key }),
    ElMessage,
    ElMessageBox: { confirm: async () => 'confirm' },
    chatApi: { sessionsList: async () => ({ data: { data: [] } }) },
    scheduledTaskApi,
    onMounted: callback => mountedCallbacks.push(callback),
  })

  vm.runInContext(`${stripSingleLineImports(scriptMatch[1])}
globalThis.__page = {
  form,
  showDialog,
  buildPayload,
  submitForm,
  toggleStatus,
  dialogVisible,
  submitting
}
`, context, { filename: 'ScheduledTasksView.vue?script-setup' })

  return { apiState, messages, mountedCallbacks, page: context.__page }
}

test('defaults and reset use repeating mode; edit loads one-time and legacy rows as repeating', () => {
  const { page } = createHarness()

  assert.equal(page.form.run_once, false)
  page.form.run_once = true
  page.showDialog('create')
  assert.equal(page.form.run_once, false)
  assert.equal(page.form.interval_seconds, 300)

  page.showDialog('edit', {
    id: 1,
    uid: 'owner-1',
    name: 'One-time task',
    session_id: 'session-1',
    interval_seconds: 60,
    run_once: true,
    status: 'disabled',
    message: 'Run once',
  })
  assert.equal(page.form.run_once, true)

  page.showDialog('create')
  assert.equal(page.form.run_once, false)

  page.showDialog('edit', {
    id: 2,
    uid: 'owner-2',
    name: 'Legacy task',
    session_id: 'session-2',
    interval_seconds: 120,
    status: 'enabled',
    message: 'Legacy message',
  })
  assert.equal(page.form.run_once, false)
})

test('buildPayload preserves run_once and interval seconds and includes edit status', () => {
  const { page } = createHarness()

  page.showDialog('create')
  fillForm(page.form, { run_once: true, interval_seconds: 125 })
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    interval_seconds: 125,
    run_once: true,
  })

  page.showDialog('edit', {
    id: 3,
    uid: 'owner-3',
    name: 'Edited task',
    session_id: 'session-3',
    interval_seconds: 600,
    run_once: false,
    status: 'disabled',
    message: 'Edited message',
  })
  page.form.interval_seconds = 1200
  assert.deepEqual(snapshot(page.buildPayload()), {
    name: 'Edited task',
    session_id: 'session-3',
    message: 'Edited message',
    interval_seconds: 1200,
    run_once: false,
    status: 'disabled',
  })
})

test('interval validation accepts 60 and rejects non-integer or sub-minimum values without requests', async () => {
  const { apiState, messages, page } = createHarness()
  page.showDialog('create')
  fillForm(page.form, { interval_seconds: 60 })
  assert.equal(page.buildPayload().interval_seconds, 60)

  for (const interval of [null, undefined, NaN, Infinity, 0, 59, 60.5, '60']) {
    page.form.interval_seconds = interval
    await page.submitForm()
    assert.equal(apiState.createRequests.length, 0)
    assert.equal(apiState.updateRequests.length, 0)
    assert.deepEqual(messages.at(-1), {
      type: 'warning',
      value: 'scheduledTasks.invalid_interval',
    })
  }
})

test('one-time create and edit call their respective APIs with run_once true', async () => {
  const createHarnessResult = createHarness()
  createHarnessResult.page.showDialog('create')
  fillForm(createHarnessResult.page.form, { run_once: true, interval_seconds: 3600 })
  await createHarnessResult.page.submitForm()

  assert.equal(createHarnessResult.apiState.createRequests.length, 1)
  assert.equal(createHarnessResult.apiState.updateRequests.length, 0)
  assert.deepEqual(snapshot(createHarnessResult.apiState.createRequests[0].payload), {
    name: 'Scheduled review',
    session_id: 'session-1',
    message: 'Review the latest changes',
    interval_seconds: 3600,
    run_once: true,
  })
  assert.equal(createHarnessResult.page.dialogVisible.value, false)

  const editHarnessResult = createHarness()
  editHarnessResult.page.showDialog('edit', {
    id: 4,
    uid: 'owner-4',
    name: 'One-time edit',
    session_id: 'session-4',
    interval_seconds: 90,
    run_once: true,
    status: 'disabled',
    message: 'Run once after editing',
  })
  await editHarnessResult.page.submitForm()

  assert.equal(editHarnessResult.apiState.createRequests.length, 0)
  assert.equal(editHarnessResult.apiState.updateRequests.length, 1)
  assert.equal(editHarnessResult.apiState.updateRequests[0].id, 4)
  assert.deepEqual(snapshot(editHarnessResult.apiState.updateRequests[0].payload), {
    name: 'One-time edit',
    session_id: 'session-4',
    message: 'Run once after editing',
    interval_seconds: 90,
    run_once: true,
    status: 'disabled',
  })
  assert.equal(editHarnessResult.page.dialogVisible.value, false)
})

test('failed create and edit retain the form and dialog and release submitting state', async () => {
  for (const type of ['create', 'edit']) {
    const { apiState, messages, page } = createHarness({
      createHandler: async () => { throw new Error('create failed') },
      updateHandler: async () => { throw new Error('edit failed') },
    })
    if (type === 'create') {
      page.showDialog('create')
      fillForm(page.form, { run_once: true, interval_seconds: 600 })
    } else {
      page.showDialog('edit', {
        id: 5,
        uid: 'owner-5',
        name: 'Edit failure',
        session_id: 'session-5',
        interval_seconds: 600,
        run_once: true,
        status: 'enabled',
        message: 'Keep this form',
      })
    }
    const formBeforeSubmit = snapshot(page.form)

    await page.submitForm()

    assert.deepEqual(snapshot(page.form), formBeforeSubmit)
    assert.equal(page.dialogVisible.value, true)
    assert.equal(page.submitting.value, false)
    assert.equal(messages.at(-1).type, 'error')
    assert.equal(apiState.createRequests.length, type === 'create' ? 1 : 0)
    assert.equal(apiState.updateRequests.length, type === 'edit' ? 1 : 0)
  }
})

test('toggleStatus only applies server status and next_run_at, preserving username; failures leave the row unchanged', async () => {
  for (const { id, initialStatus, expectedStatus, serverNextRunAt } of [
    {
      id: 6,
      initialStatus: 'enabled',
      expectedStatus: 'disabled',
      serverNextRunAt: '2026-10-19T16:00:00.000Z',
    },
    {
      id: 8,
      initialStatus: 'disabled',
      expectedStatus: 'enabled',
      serverNextRunAt: '2026-10-19T17:00:00.000Z',
    },
  ]) {
    const successHarness = createHarness({
      updateHandler: async () => ({
        data: { data: { status: expectedStatus, next_run_at: serverNextRunAt, username: null } },
      }),
    })
    const row = {
      id,
      name: 'Owned task',
      status: initialStatus,
      run_once: true,
      next_run_at: '2026-10-19T15:00:00.000Z',
      username: 'list-owner',
    }
    await successHarness.page.toggleStatus(row)

    assert.deepEqual(successHarness.apiState.updateRequests.map(({ id: requestId, payload }) => ({
      id: requestId,
      payload: snapshot(payload),
    })), [{ id, payload: { status: expectedStatus } }])
    assert.deepEqual(snapshot(row), {
      id,
      name: 'Owned task',
      status: expectedStatus,
      run_once: true,
      next_run_at: serverNextRunAt,
      username: 'list-owner',
    })
  }

  const failureHarness = createHarness({
    updateHandler: async () => { throw new Error('status update failed') },
  })
  const failedRow = {
    id: 7,
    status: 'enabled',
    next_run_at: 'unchanged-next-run',
    username: 'unchanged-owner',
  }
  const failedRowBefore = snapshot(failedRow)
  await failureHarness.page.toggleStatus(failedRow)

  assert.deepEqual(snapshot(failedRow), failedRowBefore)
  assert.equal(failureHarness.messages.at(-1).type, 'error')
})
