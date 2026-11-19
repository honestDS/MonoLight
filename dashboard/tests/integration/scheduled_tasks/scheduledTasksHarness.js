import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { computed, nextTick, reactive, ref, watch } from 'vue'
import {
  buildSchedulePayload,
  describeSchedule,
  getScheduleDefaults,
  readScheduleForm,
} from '../../../src/utils/scheduledTaskSchedule.js'
import { loadUseConfirmDialog } from '../../confirmDialogHarness.js'

const viewSource = readFileSync(new URL('../../../src/views/ScheduledTasksView.vue', import.meta.url), 'utf8')
const scriptMatch = viewSource.match(/<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/)
if (!scriptMatch) throw new Error('ScheduledTasksView.vue script setup was not found')

const stripSingleLineImports = sourceText => sourceText
  .split(/\r?\n/)
  .filter(line => !/^\s*import\b/.test(line))
  .join('\n')

const snapshot = value => JSON.parse(JSON.stringify(value))

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const previewResponse = (nextRunTimes = [
  '2026-10-20T09:00:00.000Z',
  '2026-10-21T09:00:00.000Z',
  '2026-10-22T09:00:00.000Z',
]) => ({
  data: {
    data: { next_run_times: nextRunTimes },
  },
})

const flushVue = async () => {
  for (let index = 0; index < 4; index += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

const flushPreview = async page => {
  await nextTick()
  await page.previewSchedule()
  await flushVue()
}

const fillForm = (form, overrides = {}) => Object.assign(form, {
  name: 'Scheduled review',
  session_id: 'session-1',
  message: 'Review the latest changes',
  interval_seconds: 300,
  schedule_mode: 'interval',
  status: 'enabled',
  ...overrides,
})

const createHarness = (options = {}) => {
  const messages = []
  const mountedCallbacks = []
  const locale = ref(options.locale ?? 'en')
  const apiState = {
    listRequests: [],
    createRequests: [],
    updateRequests: [],
    deleteRequests: [],
    previewRequests: [],
  }
  const ElMessage = Object.fromEntries(['warning', 'error', 'success'].map(type => [
    type,
    value => messages.push({ type, value }),
  ]))
  const confirmationCalls = []
  const pendingConfirmations = []
  const ElMessageBox = {
    confirm: (message, title, options) => new Promise((resolve, reject) => {
      confirmationCalls.push({ message, title, options })
      pendingConfirmations.push({ resolve, reject })
    }),
  }
  const actualUseConfirmDialog = loadUseConfirmDialog({
    t: key => key,
    ElMessageBox,
  })
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
    preview: async payload => {
      const request = { payload, deferred: createDeferred() }
      apiState.previewRequests.push(request)
      return options.previewHandler
        ? options.previewHandler(request)
        : (options.previewResponse || previewResponse())
    },
    delete: async id => {
      const request = { id }
      apiState.deleteRequests.push(request)
      return options.deleteHandler
        ? options.deleteHandler(request)
        : { data: { data: {} } }
    },
  }
  const context = vm.createContext({
    ref,
    reactive,
    computed,
    nextTick,
    watch,
    buildSchedulePayload,
    describeSchedule,
    getScheduleDefaults,
    readScheduleForm,
    useI18n: () => ({ t: key => key, locale }),
    ElMessage,
    useConfirmDialog: actualUseConfirmDialog,
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
    deleteTask,
    previewSchedule,
    togglePreview,
    previewTimes,
    previewLoading,
    previewMatches,
    previewError,
    scheduleValidationError,
    previewExpanded,
    scheduleKey,
    closeDialog,
    formatTime,
    dialogVisible,
    submitting
  }
`, context, { filename: 'ScheduledTasksView.vue?script-setup' })

  return {
    apiState,
    messages,
    mountedCallbacks,
    confirmationCalls,
    locale,
    page: context.__page,
    settleConfirmation: (action = 'confirm') => {
      const pending = pendingConfirmations.shift()
      assert.ok(pending, 'a confirmation must be pending')
      if (action === 'confirm') pending.resolve()
      else pending.reject(action)
    },
  }
}

export {
  createHarness,
  fillForm,
  flushPreview,
  flushVue,
  previewResponse,
  snapshot,
}
