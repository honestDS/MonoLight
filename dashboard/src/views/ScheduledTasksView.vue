<template>
  <div class="view-container">
    <BaseDataTable
      :data="tasks"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :create-text="$t('scheduledTasks.create_task')"
      :refresh-text="$t('scheduledTasks.refresh')"
      :total-text="$t('common.total_items', { total })"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadTasks"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="name" :label="$t('scheduledTasks.name')" min-width="140" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="username" :label="$t('scheduledTasks.owner_username')" min-width="140" show-overflow-tooltip>
        <template #default="scope">{{ scope.row.username || scope.row.uid || '-' }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="session_id" :label="$t('scheduledTasks.session_id')" min-width="180" show-overflow-tooltip />
      <el-table-column :resizable="false" prop="message" :label="$t('scheduledTasks.message')" min-width="240" show-overflow-tooltip />
      <el-table-column :resizable="false" :label="$t('scheduledTasks.schedule_summary')" min-width="250" show-overflow-tooltip>
        <template #default="scope">{{ describeSchedule(scope.row, t) || '-' }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="timezone" :label="$t('scheduledTasks.timezone')" width="180" show-overflow-tooltip>
        <template #default="scope">{{ scope.row.timezone || browserTimeZone || '-' }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="status" :label="$t('scheduledTasks.status')" width="110" align="center">
        <template #default="scope">
          <el-switch
            :model-value="scope.row.status === 'enabled'"
            :active-text="$t('scheduledTasks.enabled')"
            :inactive-text="$t('scheduledTasks.disabled')"
            inline-prompt
            @change="toggleStatus(scope.row)" />
        </template>
      </el-table-column>
      <el-table-column :resizable="false" prop="next_run_at" :label="$t('scheduledTasks.next_run_at')" width="180">
        <template #default="scope">{{ scope.row.run_once === true && scope.row.status === 'disabled' ? '-' : formatTime(scope.row.next_run_at, scope.row.timezone) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="last_run_at" :label="$t('scheduledTasks.last_run_at')" width="180">
        <template #default="scope">{{ formatTime(scope.row.last_run_at, scope.row.timezone) }}</template>
      </el-table-column>
      <el-table-column :resizable="false" prop="run_count" :label="$t('scheduledTasks.run_count')" width="110" align="center" />
      <el-table-column :resizable="false" :label="$t('scheduledTasks.actions')" width="180" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">{{ $t('scheduledTasks.edit') }}</el-button>
            <el-button type="danger" size="small" @click="deleteTask(scope.row)">{{ $t('scheduledTasks.delete') }}</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? $t('scheduledTasks.create_task') : $t('scheduledTasks.edit_task')" v-model="dialogVisible" width="760px" class="scheduled-task-dialog standard-dialog dialog-with-scroll-body" center align-center @close="closeDialog">
      <el-form :model="form" label-width="130px" size="default">
        <el-form-item :label="$t('scheduledTasks.name')">
          <el-input v-model="form.name" :placeholder="$t('scheduledTasks.input_name')" />
        </el-form-item>
        <el-form-item :label="$t('scheduledTasks.session_id')">
          <el-select v-model="form.session_id" :placeholder="$t('scheduledTasks.select_session')" filterable class="full-width-input" popper-class="session-select-dropdown" :loading="sessionsLoading">
            <el-option v-for="session in selectableSessions" :key="session.session_id" class="session-select-option" :label="formatSessionLabel(session)" :value="session.session_id">
              <div class="session-option">
                <div class="session-option-title">{{ session.title || $t('chat.default_title') }}</div>
                <div class="session-option-meta">{{ session.username || session.uid || '-' }} · {{ session.session_id }} · {{ formatTime(session.created_at) }}</div>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('scheduledTasks.schedule_type')">
          <el-select v-model="form.schedule_mode" class="full-width-input">
            <el-option :label="$t('scheduledTasks.interval')" value="interval" />
            <el-option :label="$t('scheduledTasks.one_time')" value="once" />
            <el-option :label="$t('scheduledTasks.daily')" value="daily" />
            <el-option :label="$t('scheduledTasks.weekly')" value="weekly" />
            <el-option :label="$t('scheduledTasks.monthly')" value="monthly" />
            <el-option :label="$t('scheduledTasks.yearly')" value="yearly" />
            <el-option :label="$t('scheduledTasks.cron')" value="cron" />
          </el-select>
        </el-form-item>
        <div class="scheduled-task-schedule-fields">
          <el-form-item v-if="form.schedule_mode === 'interval' || form.schedule_mode === 'once'" :label="$t(form.schedule_mode === 'once' ? 'scheduledTasks.delay_seconds' : 'scheduledTasks.interval_seconds')">
            <el-input-number v-model="form.interval_seconds" :min="60" :precision="0" controls-position="right" class="full-width-input" />
            <div v-if="form.schedule_mode === 'once'" class="help-text mt-4">{{ $t('scheduledTasks.one_time_hint') }}</div>
          </el-form-item>

          <template v-if="isCalendarSchedule">
            <el-form-item :label="$t('scheduledTasks.time_of_day')">
              <el-time-picker v-model="form.time_of_day" value-format="HH:mm" format="HH:mm" :editable="false" :clearable="false" :aria-label="$t('scheduledTasks.time_of_day')" class="full-width-input" />
            </el-form-item>

            <el-form-item v-if="form.schedule_mode === 'weekly'" :label="$t('scheduledTasks.weekdays')">
              <el-checkbox-group v-model="form.weekdays" :aria-label="$t('scheduledTasks.weekdays')" class="scheduled-task-weekdays">
                <el-checkbox v-for="day in weekdayOptions" :key="day" :label="day">{{ $t(`scheduledTasks.weekday_${day}`) }}</el-checkbox>
              </el-checkbox-group>
            </el-form-item>

            <el-form-item v-if="form.schedule_mode === 'monthly'" :label="$t('scheduledTasks.month_day')">
              <div class="scheduled-task-month-controls">
                <el-date-picker
                  v-model="form.month_date"
                  type="date"
                  value-format="YYYY-MM-DD"
                  format="DD"
                  popper-class="scheduled-task-date-picker scheduled-task-month-picker"
                  :default-value="new Date(2000, 0, 1)"
                  :editable="false"
                  :clearable="false"
                  :aria-label="$t('scheduledTasks.month_day')"
                  :disabled="form.month_last_day"
                />
                <el-switch v-model="form.month_last_day" :active-text="$t('scheduledTasks.month_last_day')" :aria-label="$t('scheduledTasks.month_last_day')" />
              </div>
              <div class="help-text mt-4">{{ $t('scheduledTasks.month_day_hint') }}</div>
            </el-form-item>

            <el-form-item v-if="form.schedule_mode === 'yearly'" :label="$t('scheduledTasks.year_date')">
              <el-date-picker
                v-model="form.year_date"
                type="date"
                value-format="YYYY-MM-DD"
                format="MM-DD"
                popper-class="scheduled-task-date-picker"
                :default-value="new Date(2000, 0, 1)"
                :editable="false"
                :clearable="false"
                :aria-label="$t('scheduledTasks.year_date')"
                class="full-width-input"
              />
              <div class="help-text mt-4">{{ $t('scheduledTasks.year_date_hint') }}</div>
            </el-form-item>
          </template>

          <el-form-item v-if="form.schedule_mode === 'cron'" :label="$t('scheduledTasks.cron_expression')">
            <el-input v-model="form.cron_expression" :placeholder="$t('scheduledTasks.cron_hint')" />
            <div class="help-text mt-4">{{ $t('scheduledTasks.cron_hint') }}</div>
          </el-form-item>
          <el-form-item v-if="isCalendarSchedule || form.schedule_mode === 'cron'" :label="$t('scheduledTasks.timezone')">
            <el-select v-model="form.timezone" class="full-width-input" filterable allow-create default-first-option :aria-label="$t('scheduledTasks.timezone')">
              <el-option v-for="timezone in timeZoneOptions" :key="timezone" :label="timezone" :value="timezone" />
            </el-select>
            <div class="help-text mt-4">{{ $t('scheduledTasks.timezone_hint') }}</div>
          </el-form-item>

          <div v-if="scheduleValidationError" class="scheduled-task-schedule-error" role="alert">{{ scheduleValidationError }}</div>
        </div>

        <el-form-item :label="$t('scheduledTasks.schedule_summary')">
          <section class="scheduled-task-preview" aria-live="polite" :aria-busy="previewLoading">
            <div class="scheduled-task-preview-header">
              <div class="scheduled-task-preview-summary">
                <span v-if="scheduleSummary">{{ scheduleSummary }}</span>
                <span v-else>{{ $t('scheduledTasks.preview_pending') }}</span>
              </div>
              <el-button type="default" :loading="previewLoading" :disabled="previewLoading" @click="togglePreview" :aria-expanded="previewExpanded" aria-controls="scheduled-task-preview-details">
                {{ $t('scheduledTasks.preview_action') }}
                <el-icon aria-hidden="true">
                  <ArrowUp v-if="previewExpanded" />
                  <ArrowDown v-else />
                </el-icon>
              </el-button>
            </div>
            <div v-if="previewError" class="scheduled-task-schedule-error" role="alert">{{ previewError }}</div>
            <div id="scheduled-task-preview-details" v-show="previewExpanded">
              <div v-if="previewLoading" class="help-text" role="status">{{ $t('scheduledTasks.preview_pending') }}</div>
              <template v-else-if="previewMatches">
                <div class="scheduled-task-preview-timezone">
                  <span>{{ $t('scheduledTasks.timezone') }}</span>
                  <strong>{{ previewDisplayTimeZone }}</strong>
                </div>
                <div v-if="form.schedule_mode !== 'once'" class="scheduled-task-preview-title">{{ $t('scheduledTasks.preview_title') }}</div>
                <ul class="scheduled-task-preview-times">
                  <li v-for="(runTime, index) in previewTimes" :key="`${previewKey}-${index}`">
                    {{ formatTime(runTime, previewTimezone || browserTimeZone) }}
                  </li>
                </ul>
              </template>
            </div>
          </section>
        </el-form-item>

        <el-form-item :label="$t('scheduledTasks.status')" v-if="dialogType === 'edit'">
          <el-select v-model="form.status" class="full-width-input">
            <el-option :label="$t('scheduledTasks.enabled')" value="enabled" />
            <el-option :label="$t('scheduledTasks.disabled')" value="disabled" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('scheduledTasks.message')">
          <el-input v-model="form.message" type="textarea" :rows="8" :placeholder="$t('scheduledTasks.input_message')" />
          <div class="help-text mt-4">{{ $t('scheduledTasks.instruction_hint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="closeDialog" size="default">{{ $t('scheduledTasks.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting" :disabled="previewLoading || !previewMatches">{{ $t('scheduledTasks.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowUp } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { chatApi, scheduledTaskApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import { useConfirmDialog } from '../composables/useConfirmDialog.js'
import { buildSchedulePayload, describeSchedule, getScheduleDefaults, readScheduleForm } from '../utils/scheduledTaskSchedule.js'

const { t, locale } = useI18n()
const { confirm } = useConfirmDialog()

const tasks = ref([])
const loading = ref(false)
const submitting = ref(false)
const sessionsLoading = ref(false)
const total = ref(0)
const sessions = ref([])
const currentPage = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)
const dialogType = ref('create')
const taskOwnerUid = ref(null)
const defaultSchedule = getScheduleDefaults()
const browserTimeZone = defaultSchedule.timezone || ''
const weekdayOptions = [1, 2, 3, 4, 5, 6, 0]
const fallbackTimeZones = [
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Asia/Kolkata',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'Australia/Sydney',
  'Pacific/Auckland'
]

const supportedTimeZones = (() => {
  try {
    if (typeof Intl.supportedValuesOf === 'function') {
      const values = Intl.supportedValuesOf('timeZone')
      if (Array.isArray(values) && values.length) return values
    }
  } catch {
    // Fall back to a small IANA set on older browsers.
  }
  return fallbackTimeZones
})()

const form = reactive({
  ...defaultSchedule,
  id: null,
  name: '',
  session_id: '',
  status: 'enabled',
  message: ''
})

const timeZoneOptions = computed(() => {
  const values = new Set([...supportedTimeZones, 'UTC', form.timezone])
  return [...values].filter(Boolean).sort((left, right) => left.localeCompare(right))
})

const isCalendarSchedule = computed(() => ['daily', 'weekly', 'monthly', 'yearly'].includes(form.schedule_mode))
const scheduleValidationError = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const previewTimes = ref([])
const previewTimezone = ref('')
const previewKey = ref('')
const previewExpanded = ref(false)
let previewRequestSequence = 0

const scheduleKey = computed(() => {
  try {
    return JSON.stringify(buildSchedulePayload(form))
  } catch {
    return ''
  }
})

const scheduleSummary = computed(() => {
  try {
    const payload = buildSchedulePayload(form)
    return describeSchedule(payload, t) || ''
  } catch {
    return ''
  }
})

const previewMatches = computed(() => Boolean(
  scheduleKey.value &&
  previewKey.value === scheduleKey.value &&
  previewTimes.value.length
))

const previewDisplayTimeZone = computed(() => previewTimezone.value || browserTimeZone || '-')

const formatTime = (timeStr, timezone = browserTimeZone) => {
  if (!timeStr) return '-'
  const date = new Date(timeStr)
  if (Number.isNaN(date.getTime())) return timeStr
  try {
    const options = { dateStyle: 'medium', timeStyle: 'short' }
    const displayTimeZone = timezone || browserTimeZone
    if (displayTimeZone) options.timeZone = displayTimeZone
    return new Intl.DateTimeFormat(locale.value, options).format(date)
  } catch {
    return timeStr
  }
}

const loadTasks = async () => {
  loading.value = true
  try {
    const res = await scheduledTaskApi.list({ page: currentPage.value, size: pageSize.value })
    tasks.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.load_failed'))
  } finally {
    loading.value = false
  }
}

const loadSessions = async () => {
  sessionsLoading.value = true
  try {
    const res = await chatApi.sessionsList()
    sessions.value = res.data.data || []
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.load_sessions_failed'))
  } finally {
    sessionsLoading.value = false
  }
}

const formatSessionLabel = (session) => {
  const title = session.title || t('chat.default_title')
  return `${title} · ${session.username || session.uid || '-'} · ${session.session_id} · ${formatTime(session.created_at)}`
}

const selectableSessions = computed(() => {
  if (dialogType.value === 'create') return sessions.value
  return sessions.value.filter(session => session.uid === taskOwnerUid.value)
})

const resetForm = () => {
  taskOwnerUid.value = null
  Object.assign(form, {
    ...getScheduleDefaults(),
    id: null,
    name: '',
    session_id: '',
    status: 'enabled',
    message: ''
  })
}

const showDialog = (type, row = null) => {
  invalidatePreview()
  dialogType.value = type
  resetForm()
  if (type === 'edit' && row) {
    taskOwnerUid.value = row.uid
    form.id = row.id
    form.name = row.name
    form.session_id = row.session_id
    Object.assign(form, readScheduleForm(row))
    form.status = row.status
    form.message = row.message || ''
  }
  dialogVisible.value = true
  nextTick(() => {
    if (dialogVisible.value && scheduleKey.value) previewSchedule()
  })
}

const getScheduleErrorMessage = (error, fallback = 'scheduledTasks.preview_failed') => {
  const rawMessage = error && typeof error.message === 'string' ? error.message : ''
  if (rawMessage.startsWith('scheduledTasks.')) return t(rawMessage)
  return rawMessage || t(fallback)
}

const invalidatePreview = () => {
  previewRequestSequence += 1
  previewLoading.value = false
  previewError.value = ''
  previewTimes.value = []
  previewTimezone.value = ''
  previewKey.value = ''
  previewExpanded.value = false
  scheduleValidationError.value = ''
}

const previewSchedule = async () => {
  if (!dialogVisible.value || previewLoading.value) return

  let schedulePayload
  try {
    schedulePayload = buildSchedulePayload(form)
  } catch (error) {
    invalidatePreview()
    scheduleValidationError.value = getScheduleErrorMessage(error)
    return
  }

  const key = JSON.stringify(schedulePayload)
  const requestSequence = ++previewRequestSequence
  previewLoading.value = true
  previewError.value = ''
  scheduleValidationError.value = ''
  previewTimes.value = []
  previewTimezone.value = ''
  previewKey.value = ''

  try {
    const res = await scheduledTaskApi.preview(schedulePayload)
    const nextRunTimes = res?.data?.data?.next_run_times
    if (!Array.isArray(nextRunTimes) || nextRunTimes.length === 0) {
      throw new Error(t('scheduledTasks.preview_failed'))
    }
    if (
      requestSequence !== previewRequestSequence ||
      key !== scheduleKey.value ||
      !dialogVisible.value
    ) return
    previewTimes.value = schedulePayload.run_once === true ? nextRunTimes.slice(0, 1) : nextRunTimes
    previewTimezone.value = schedulePayload.timezone || browserTimeZone
    previewKey.value = key
  } catch (error) {
    if (
      requestSequence !== previewRequestSequence ||
      key !== scheduleKey.value ||
      !dialogVisible.value
    ) return
    previewError.value = getScheduleErrorMessage(error)
  } finally {
    if (
      requestSequence === previewRequestSequence &&
      key === scheduleKey.value &&
      dialogVisible.value
    ) previewLoading.value = false
  }
}

const togglePreview = async () => {
  if (!dialogVisible.value || previewLoading.value) return
  if (previewMatches.value) {
    previewExpanded.value = !previewExpanded.value
    return
  }
  previewExpanded.value = true
  await previewSchedule()
}

const buildPayload = () => {
  if (!form.name || !form.session_id || !form.message) {
    ElMessage.warning(t('scheduledTasks.fill_required'))
    return null
  }

  let schedulePayload
  try {
    schedulePayload = buildSchedulePayload(form)
  } catch (error) {
    const message = getScheduleErrorMessage(error, 'scheduledTasks.invalid_schedule')
    scheduleValidationError.value = message
    ElMessage.warning(message)
    return null
  }

  scheduleValidationError.value = ''
  const payload = {
    name: form.name,
    session_id: form.session_id,
    message: form.message,
    ...schedulePayload
  }
  if (dialogType.value === 'edit') payload.status = form.status
  return payload
}

const submitForm = async () => {
  if (submitting.value || previewLoading.value) return
  const payload = buildPayload()
  if (!payload) return
  if (!scheduleKey.value || previewKey.value !== scheduleKey.value || !previewTimes.value.length) {
    const message = t('scheduledTasks.preview_required')
    scheduleValidationError.value = message
    ElMessage.warning(message)
    return
  }
  submitting.value = true
  try {
    if (dialogType.value === 'create') {
      await scheduledTaskApi.create(payload)
    } else {
      await scheduledTaskApi.update(form.id, payload)
    }
    ElMessage.success(t('scheduledTasks.save_success'))
    closeDialog()
    loadTasks()
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.submit_failed'))
  } finally {
    submitting.value = false
  }
}

const toggleStatus = async (row) => {
  const status = row.status === 'enabled' ? 'disabled' : 'enabled'
  try {
    const res = await scheduledTaskApi.update(row.id, { status })
    const updatedTask = res.data.data
    row.status = updatedTask.status
    row.next_run_at = updatedTask.next_run_at
  } catch (err) {
    ElMessage.error(err.message || t('scheduledTasks.status_update_failed'))
  }
}

const deleteTask = async (row) => {
  try {
    if (!await confirm({
      message: t('scheduledTasks.delete_confirm'),
      title: t('common.warning'),
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel')
    })) return
    await scheduledTaskApi.delete(row.id)
    ElMessage.success(t('scheduledTasks.delete_success'))
    loadTasks()
  } catch (err) {
    if (err !== 'cancel') {
      ElMessage.error(err.message || t('scheduledTasks.delete_failed'))
    }
  }
}

const closeDialog = () => {
  invalidatePreview()
  dialogVisible.value = false
}

const handleRefresh = () => {
  currentPage.value = 1
  loadTasks()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadTasks()
}

watch(scheduleKey, () => {
  invalidatePreview()
}, { flush: 'sync' })

onMounted(() => {
  loadSessions()
  loadTasks()
})
</script>

<style lang="scss">
@import "@/assets/css/ScheduledTasksView.scss";
</style>
